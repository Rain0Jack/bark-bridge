#!/usr/bin/env python3
"""bark-bridge v2: ntfy -> Bark bridge with multi-device, multi-user WebUI.

Subscribes to ntfy topics (JSON stream) and forwards messages to one or more
iOS Bark devices. Ships an embedded HTTP server providing a single-page admin
WebUI plus a JSON REST API. Configuration lives in SQLite and is editable at
runtime; changes take effect within ~2 seconds without a restart.

Zero third-party dependencies: Python 3.11+ standard library only.

WebUI / API:  http://<HOST>:<PORT>/
Data:         <DATA_DIR>/bark-bridge.db  (SQLite, WAL)

See docs/API.md for the HTTP contract and README.md for deployment.
"""

from __future__ import annotations

import base64
import collections
import gzip
import hashlib
import hmac
import io
import json
import os
import re
import secrets
import shutil
import signal
import socket
import sqlite3
import sys
import tarfile
import tempfile
import threading
import time
import traceback
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

__version__ = "2.0.0"

MAX_PAYLOAD = 64 * 1024
PBKDF2_ROUNDS = 210_000
CSP = (
    "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; "
    "img-src data:; connect-src 'self'; base-uri 'none'; form-action 'none'"
)
LEVELS = ("", "active", "timeSensitive", "passive", "critical")


# --------------------------------------------------------------------------- #
# logging / helpers
# --------------------------------------------------------------------------- #

def log(message: str) -> None:
    print(time.strftime("%Y-%m-%d %H:%M:%S"), message, file=sys.stderr, flush=True)


def b64u(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def b64u_dec(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def now_ts() -> int:
    return int(time.time())


def _short(exc: BaseException, limit: int = 200) -> str:
    text = f"{type(exc).__name__}: {exc}" if str(exc) else type(exc).__name__
    return text[:limit]


def _response_socket(response):
    """Best-effort access to the underlying socket of a urllib response."""
    try:
        return response.fp.raw._sock
    except AttributeError:
        return None


def _http_get(url: str, *, timeout: float = 25.0, max_bytes: int = 16 * 1024 * 1024,
              headers: dict[str, str] | None = None) -> bytes:
    """GET a URL with a size cap; raise RuntimeError on oversized responses."""
    request = urllib.request.Request(url, headers={
        "User-Agent": f"bark-bridge/{__version__}", **(headers or {})})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        data = response.read(max_bytes + 1)
    if len(data) > max_bytes:
        raise RuntimeError(f"响应过大（>{max_bytes} 字节）")
    return data


def _normalized(data: bytes) -> bytes:
    """Normalise CRLF so a CRLF working copy still matches the LF GitHub archive."""
    return data.replace(b"\r\n", b"\n")


def _version_key(version: str) -> tuple:
    parts = re.findall(r"\d+", version or "")
    return tuple(int(part) for part in parts[:3]) or (0,)


def _atomic_write(path: str, data: bytes) -> None:
    """Replace a file atomically (temp file in the same directory + os.replace)."""
    directory = os.path.dirname(os.path.abspath(path)) or "."
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=".bb-update-")
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            shutil.copymode(path, tmp)
        except OSError:
            pass
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def env_str(name: str, default: str = "") -> str:
    return (os.environ.get(name) or default).strip()


def env_int(name: str, default: int) -> int:
    try:
        return int(env_str(name, str(default)))
    except ValueError:
        return default


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, PBKDF2_ROUNDS)
    return "pbkdf2_sha256${}${}${}".format(PBKDF2_ROUNDS, b64u(salt), b64u(digest))


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, rounds, salt, digest = stored.split("$")
        if algo != "pbkdf2_sha256":
            return False
        calc = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), b64u_dec(salt), int(rounds))
        return hmac.compare_digest(calc, b64u_dec(digest))
    except (ValueError, TypeError):
        return False


def pwd_version(password_hash: str) -> str:
    return hashlib.sha256(password_hash.encode("utf-8")).hexdigest()[:12]


def map_level(priority: int) -> str:
    return "timeSensitive" if priority >= 4 else "active"


_IMAGE_SUFFIXES = (".png", ".jpg", ".jpeg", ".webp", ".gif", ".heic", ".bmp", ".avif")
_JSON_TITLE_KEYS = ("title", "subject")
_JSON_BODY_KEYS = ("message", "body", "text", "content")


def _looks_like_image(url: str) -> bool:
    path = urllib.parse.urlsplit(url).path.lower()
    return path.endswith(_IMAGE_SUFFIXES)


def _unwrap_json_notification(body: str) -> dict | None:
    """Unpack a JSON document used as the ntfy message body.

    Tools such as ani-rss post JSON to the ntfy topic root; ntfy then shows the
    whole document as the message text. When that document looks like a
    notification (has title/message-like keys) unpack it so the iOS banner
    shows the real title and text instead of raw JSON.
    """
    text = (body or "").strip()
    if not text.startswith("{"):
        return None
    try:
        data = json.loads(text)
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None
    has_title = any(isinstance(data.get(key), str) and data[key].strip()
                    for key in _JSON_TITLE_KEYS)
    has_body = any(isinstance(data.get(key), str) and data[key].strip()
                   for key in _JSON_BODY_KEYS)
    if not (has_title or has_body):
        return None

    def first_text(keys) -> str:
        for key in keys:
            value = data.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        return ""

    result: dict = {"title": first_text(_JSON_TITLE_KEYS), "body": first_text(_JSON_BODY_KEYS)}
    raw_priority = data.get("priority")
    if raw_priority is not None:
        try:
            result["priority"] = int(raw_priority)
        except (TypeError, ValueError):
            pass
    for key in ("click", "url"):
        value = data.get(key)
        if isinstance(value, str) and value.startswith(("http://", "https://")):
            result["click"] = value
            break
    for key in ("attach", "image", "icon"):
        value = data.get(key)
        if isinstance(value, str) and value.startswith(("http://", "https://")):
            result["attach"] = value
            break
    return result


def bark_push(server: str, key: str, title: str, body: str, level: str,
              group: str, icon: str, url: str | None, image: str | None = None) -> None:
    """POST /push to one Bark server; raise RuntimeError on failure."""
    payload = {
        "device_key": key,
        "title": title or "ntfy",
        "body": body or "",
        "level": level or "active",
        "group": group or "ntfy",
    }
    if url:
        payload["url"] = url
    if icon:
        payload["icon"] = icon
    if image:
        payload["image"] = image
    request = urllib.request.Request(
        (server or "https://api.day.app").rstrip("/") + "/push",
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json; charset=utf-8"},
    )
    with urllib.request.urlopen(request, timeout=10) as response:
        raw = response.read(65536)
    if raw:
        try:
            parsed = json.loads(raw)
        except ValueError:
            parsed = None
        if isinstance(parsed, dict):
            code = parsed.get("code")
            if code is not None and str(code) not in ("200", "0"):
                raise RuntimeError(str(parsed.get("message") or code)[:200])


class ApiError(Exception):
    def __init__(self, status: int, code: str, message: str):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


class _StopStream(Exception):
    pass


# --------------------------------------------------------------------------- #
# configuration
# --------------------------------------------------------------------------- #

class Config:
    def __init__(self) -> None:
        self.port = env_int("PORT", 8080)
        self.host = env_str("HOST", "0.0.0.0") or "0.0.0.0"
        here = os.path.dirname(os.path.abspath(__file__))
        # Default to <script dir>/data so a directory mount (./:/app) keeps the
        # SQLite database next to the code without extra configuration.
        self.data_dir = os.path.abspath(env_str("DATA_DIR") or os.path.join(here, "data"))
        self.webui_file = env_str("WEBUI_FILE") or os.path.join(here, "webui.html")
        self.app_file = os.path.abspath(env_str("APP_FILE") or os.path.abspath(__file__))
        # ntfy messages whose body is a JSON document get unpacked (ani-rss and
        # similar tools post JSON to the topic root); image attachments are
        # forwarded to Bark as the notification image.
        self.json_body_parse = env_str("JSON_BODY_PARSE", "1").lower() not in (
            "0", "false", "no", "off")
        self.send_image = env_str("SEND_IMAGE", "1").lower() not in (
            "0", "false", "no", "off")
        self.admin_user = env_str("ADMIN_USER", "admin") or "admin"
        self.admin_password = env_str("ADMIN_PASSWORD")
        self.default_bark_server = (env_str("BARK_SERVER", "https://api.day.app")
                                    or "https://api.day.app").rstrip("/")
        self.default_ntfy_url = (env_str("NTFY_BASE_URL", "http://127.0.0.1:40265")
                                 or "http://127.0.0.1:40265").rstrip("/")
        self.default_since = env_str("NTFY_SINCE", "1m") or "1m"
        self.session_ttl = max(3600, env_int("SESSION_TTL", 7 * 86400))
        self.max_body = max(64, env_int("MAX_BODY", 2000))
        self.reconnect_delay = max(1, env_int("RECONNECT_DELAY", 5))
        self.push_workers = max(1, min(32, env_int("PUSH_WORKERS", 4)))
        self.activity_size = max(20, min(2000, env_int("ACTIVITY_SIZE", 200)))
        self.access_log = env_str("ACCESS_LOG", "0") in ("1", "true", "yes", "on")
        # ---- online update (WebUI pulls from GitHub) ----------------------
        self.update_enabled = env_str("UPDATE_ENABLED", "1").lower() not in ("0", "false", "no", "off")
        self.update_repo = env_str("UPDATE_REPO", "Rain0Jack/bark-bridge") or "Rain0Jack/bark-bridge"
        self.update_branch = env_str("UPDATE_BRANCH", "main") or "main"
        self.update_api_base = (env_str("UPDATE_API_BASE", "https://api.github.com")
                                or "https://api.github.com").rstrip("/")
        self.update_codeload_base = (env_str("UPDATE_CODELOAD_BASE", "https://codeload.github.com")
                                     or "https://codeload.github.com").rstrip("/")
        # Background check talks to GitHub on startup and every 6 hours; tests and
        # air-gapped deployments can turn it off while keeping manual checks.
        self.update_autocheck = env_str("UPDATE_AUTOCHECK", "1").lower() not in (
            "0", "false", "no", "off")
        self.legacy = {key: env_str(key) for key in (
            "NTFY_TOPIC", "NTFY_TOKEN", "NTFY_SINCE", "BARK_KEY",
            "BARK_GROUP", "BARK_LEVEL", "BARK_ICON",
        )}
        self.db_file = os.path.join(self.data_dir, "bark-bridge.db")


# --------------------------------------------------------------------------- #
# SQLite store
# --------------------------------------------------------------------------- #

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY,
    username TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    role TEXT NOT NULL DEFAULT 'user',
    is_active INTEGER NOT NULL DEFAULT 1,
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS devices (
    id INTEGER PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    bark_key TEXT NOT NULL,
    server TEXT NOT NULL,
    grp TEXT NOT NULL DEFAULT 'ntfy',
    level TEXT NOT NULL DEFAULT '',
    icon TEXT NOT NULL DEFAULT '',
    is_active INTEGER NOT NULL DEFAULT 1,
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS subscriptions (
    id INTEGER PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    ntfy_url TEXT NOT NULL,
    topic TEXT NOT NULL,
    token TEXT NOT NULL DEFAULT '',
    since TEXT NOT NULL DEFAULT '1m',
    priority_min INTEGER NOT NULL DEFAULT 0,
    keyword TEXT NOT NULL DEFAULT '',
    exclude_keyword TEXT NOT NULL DEFAULT '',
    title_prefix TEXT NOT NULL DEFAULT '',
    is_active INTEGER NOT NULL DEFAULT 1,
    cursor_ts INTEGER NOT NULL DEFAULT 0,
    forwarded INTEGER NOT NULL DEFAULT 0,
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS subscription_devices (
    subscription_id INTEGER NOT NULL REFERENCES subscriptions(id) ON DELETE CASCADE,
    device_id INTEGER NOT NULL REFERENCES devices(id) ON DELETE CASCADE,
    PRIMARY KEY (subscription_id, device_id)
);
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_devices_user ON devices(user_id);
CREATE INDEX IF NOT EXISTS idx_subscriptions_user ON subscriptions(user_id);
"""


class Store:
    def __init__(self, path: str) -> None:
        self.path = path
        self._local = threading.local()
        self._wlock = threading.RLock()
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)

    def conn(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(self.path, timeout=10, check_same_thread=True)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute("PRAGMA busy_timeout=5000")
            self._local.conn = conn
        return conn

    def close(self) -> None:
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            try:
                conn.close()
            except sqlite3.Error:
                pass
            self._local.conn = None

    @contextmanager
    def write(self):
        with self._wlock:
            conn = self.conn()
            try:
                yield conn
            except BaseException:
                conn.rollback()
                raise
            else:
                conn.commit()

    def q(self, sql: str, args: tuple = ()) -> list[sqlite3.Row]:
        return self.conn().execute(sql, args).fetchall()

    def q1(self, sql: str, args: tuple = ()) -> sqlite3.Row | None:
        return self.conn().execute(sql, args).fetchone()

    def init(self) -> None:
        with self.write() as conn:
            conn.executescript(SCHEMA)
            conn.execute("PRAGMA user_version=1")
        if not self.settings_get("secret"):
            self.settings_set("secret", secrets.token_hex(32))

    # ---- settings -------------------------------------------------------- #
    def settings_get(self, key: str, default: str | None = None) -> str | None:
        row = self.q1("SELECT value FROM settings WHERE key=?", (key,))
        return row["value"] if row else default

    def settings_set(self, key: str, value: str) -> None:
        with self.write() as conn:
            conn.execute(
                "INSERT INTO settings(key, value) VALUES(?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, value),
            )

    def defaults(self, cfg: Config) -> dict:
        return {
            "ntfy_url": self.settings_get("default_ntfy_url", cfg.default_ntfy_url),
            "bark_server": self.settings_get("default_bark_server", cfg.default_bark_server),
            "since": self.settings_get("default_since", cfg.default_since),
        }

    # ---- users ----------------------------------------------------------- #
    def count_users(self) -> int:
        return int(self.q1("SELECT COUNT(*) AS n FROM users")["n"])

    def list_users(self) -> list[sqlite3.Row]:
        return self.q(
            "SELECT u.*, (SELECT COUNT(*) FROM devices d WHERE d.user_id=u.id) AS device_count, "
            "(SELECT COUNT(*) FROM subscriptions s WHERE s.user_id=u.id) AS subscription_count "
            "FROM users u ORDER BY u.id"
        )

    def get_user(self, user_id: int) -> sqlite3.Row | None:
        return self.q1("SELECT * FROM users WHERE id=?", (int(user_id),))

    def get_user_by_name(self, username: str) -> sqlite3.Row | None:
        return self.q1("SELECT * FROM users WHERE username=?", (username,))

    def create_user(self, username: str, password_hash: str, role: str = "user",
                    is_active: bool = True) -> int:
        ts = now_ts()
        with self.write() as conn:
            cur = conn.execute(
                "INSERT INTO users(username, password_hash, role, is_active, created_at, updated_at) "
                "VALUES(?,?,?,?,?,?)",
                (username, password_hash, role, 1 if is_active else 0, ts, ts),
            )
            return int(cur.lastrowid)

    def update_user(self, user_id: int, fields: dict) -> None:
        allowed = [key for key in ("username", "password_hash", "role", "is_active") if key in fields]
        if not allowed:
            return
        sets = ", ".join(f"{key}=?" for key in allowed)
        values = [fields[key] for key in allowed]
        values.extend([now_ts(), int(user_id)])
        with self.write() as conn:
            conn.execute(f"UPDATE users SET {sets}, updated_at=? WHERE id=?", values)

    def delete_user(self, user_id: int) -> None:
        with self.write() as conn:
            conn.execute("DELETE FROM users WHERE id=?", (int(user_id),))

    # ---- devices --------------------------------------------------------- #
    _DEVICE_SELECT = (
        "SELECT d.*, u.username AS owner FROM devices d "
        "LEFT JOIN users u ON u.id=d.user_id"
    )

    def list_devices(self, user_id: int | None = None) -> list[sqlite3.Row]:
        if user_id is None:
            return self.q(self._DEVICE_SELECT + " ORDER BY d.id")
        return self.q(self._DEVICE_SELECT + " WHERE d.user_id=? ORDER BY d.id", (int(user_id),))

    def get_device(self, device_id: int) -> sqlite3.Row | None:
        return self.q1(self._DEVICE_SELECT + " WHERE d.id=?", (int(device_id),))

    def create_device(self, user_id: int, data: dict) -> int:
        ts = now_ts()
        with self.write() as conn:
            cur = conn.execute(
                "INSERT INTO devices(user_id, name, bark_key, server, grp, level, icon, is_active, "
                "created_at, updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (int(user_id), data["name"], data["bark_key"], data["server"], data["group"],
                 data["level"], data["icon"], 1 if data.get("is_active", True) else 0, ts, ts),
            )
            return int(cur.lastrowid)

    def update_device(self, device_id: int, fields: dict) -> None:
        mapping = [("name", "name"), ("bark_key", "bark_key"), ("server", "server"),
                   ("group", "grp"), ("level", "level"), ("icon", "icon"),
                   ("is_active", "is_active")]
        columns = [column for key, column in mapping if key in fields]
        if not columns:
            return
        sets = ", ".join(f"{column}=?" for column in columns)
        values = [fields[key] for key, _ in mapping if key in fields]
        values.extend([now_ts(), int(device_id)])
        with self.write() as conn:
            conn.execute(f"UPDATE devices SET {sets}, updated_at=? WHERE id=?", values)

    def delete_device(self, device_id: int) -> None:
        with self.write() as conn:
            conn.execute("DELETE FROM devices WHERE id=?", (int(device_id),))

    # ---- subscriptions --------------------------------------------------- #
    _SUB_SELECT = (
        "SELECT s.*, u.username AS owner FROM subscriptions s "
        "LEFT JOIN users u ON u.id=s.user_id"
    )

    def list_subscriptions(self, user_id: int | None = None) -> list[sqlite3.Row]:
        if user_id is None:
            return self.q(self._SUB_SELECT + " ORDER BY s.id")
        return self.q(self._SUB_SELECT + " WHERE s.user_id=? ORDER BY s.id", (int(user_id),))

    def get_subscription(self, sub_id: int) -> sqlite3.Row | None:
        return self.q1(self._SUB_SELECT + " WHERE s.id=?", (int(sub_id),))

    def active_subscriptions(self) -> list[sqlite3.Row]:
        return self.q("SELECT * FROM subscriptions WHERE is_active=1 ORDER BY id")

    def create_subscription(self, user_id: int, data: dict) -> int:
        ts = now_ts()
        with self.write() as conn:
            cur = conn.execute(
                "INSERT INTO subscriptions(user_id, name, ntfy_url, topic, token, since, "
                "priority_min, keyword, exclude_keyword, title_prefix, is_active, created_at, updated_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (int(user_id), data["name"], data["ntfy_url"], data["topic"], data["token"],
                 data["since"], data["priority_min"], data["keyword"], data["exclude_keyword"],
                 data["title_prefix"], 1 if data.get("is_active", True) else 0, ts, ts),
            )
            return int(cur.lastrowid)

    def update_subscription(self, sub_id: int, fields: dict) -> None:
        allowed = [key for key in ("name", "ntfy_url", "topic", "token", "since", "priority_min",
                                   "keyword", "exclude_keyword", "title_prefix", "is_active")
                   if key in fields]
        if not allowed:
            return
        sets = ", ".join(f"{key}=?" for key in allowed)
        values = [fields[key] for key in allowed]
        values.extend([now_ts(), int(sub_id)])
        with self.write() as conn:
            conn.execute(f"UPDATE subscriptions SET {sets}, updated_at=? WHERE id=?", values)

    def delete_subscription(self, sub_id: int) -> None:
        with self.write() as conn:
            conn.execute("DELETE FROM subscriptions WHERE id=?", (int(sub_id),))

    def subscription_device_map(self) -> dict[int, list[int]]:
        result: dict[int, list[int]] = {}
        for row in self.q("SELECT subscription_id, device_id FROM subscription_devices"):
            result.setdefault(row["subscription_id"], []).append(row["device_id"])
        return result

    def subscription_device_ids(self, sub_id: int) -> list[int]:
        return [row["device_id"] for row in self.q(
            "SELECT device_id FROM subscription_devices WHERE subscription_id=?", (int(sub_id),))]

    def set_subscription_devices(self, sub_id: int, device_ids: list[int]) -> None:
        with self.write() as conn:
            conn.execute("DELETE FROM subscription_devices WHERE subscription_id=?", (int(sub_id),))
            conn.executemany(
                "INSERT OR IGNORE INTO subscription_devices(subscription_id, device_id) VALUES(?,?)",
                [(int(sub_id), int(device_id)) for device_id in device_ids],
            )

    def subscription_targets(self, sub_id: int) -> list[sqlite3.Row]:
        return self.q(
            self._DEVICE_SELECT + " JOIN subscription_devices sd ON sd.device_id=d.id "
            "WHERE sd.subscription_id=? AND d.is_active=1 ORDER BY d.id",
            (int(sub_id),),
        )

    def update_cursor(self, sub_id: int, cursor: int, forwarded_incr: int) -> None:
        with self.write() as conn:
            conn.execute(
                "UPDATE subscriptions SET cursor_ts=?, forwarded=forwarded+? WHERE id=?",
                (int(cursor), int(forwarded_incr), int(sub_id)),
            )


# --------------------------------------------------------------------------- #
# activity ring buffer
# --------------------------------------------------------------------------- #

class Activity:
    def __init__(self, size: int) -> None:
        self._items: collections.deque = collections.deque(maxlen=size)
        self._lock = threading.Lock()
        self._next = 1

    def add(self, **fields) -> dict:
        with self._lock:
            item = {"id": self._next, "ts": round(time.time(), 3)}
            item.update(fields)
            self._next += 1
            self._items.append(item)
            return item

    def since(self, after: int, limit: int) -> tuple[list[dict], int]:
        with self._lock:
            items = [item for item in self._items if item["id"] > after]
            page = items[:limit]
            last_id = page[-1]["id"] if page else after
            return page, last_id


# --------------------------------------------------------------------------- #
# forwarding engine
# --------------------------------------------------------------------------- #

class SubscriptionWorker(threading.Thread):
    """One thread streaming one ntfy topic and fanning messages out to devices."""

    def __init__(self, engine: "Engine", sub_id: int, fingerprint: tuple) -> None:
        super().__init__(name=f"bb-sub-{sub_id}", daemon=True)
        self.engine = engine
        self.sub_id = sub_id
        self.fingerprint = fingerprint
        self.state = "connecting"
        self.last_error = ""
        self.connected_at = 0.0
        self._stop = threading.Event()
        self._resp = None
        self._sock = None
        self._cursor = 0
        self._last_flush = 0.0
        self._fwd_pending = 0
        self._sub_name = f"#{sub_id}"
        self._started_at = time.time()
        self._backoff = engine.cfg.reconnect_delay
        self._seen: set[str] = set()
        self._seen_order: collections.deque = collections.deque()
        self._resume_until: int | None = None
        self._initial = True

    # -- lifecycle --------------------------------------------------------- #
    def stop(self) -> None:
        self._stop.set()
        # A blocked readline() on the stream socket only returns when the socket
        # is shut down or times out; shutdown() wakes it immediately.
        sock = self._sock
        if sock is not None:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
        response = self._resp
        if response is not None:
            try:
                response.close()
            except Exception:  # noqa: BLE001 - closing a dead socket is best effort
                pass

    def _set_state(self, state: str, error: str = "") -> None:
        changed = state != self.state
        self.state = state
        self.last_error = error
        if changed and state in ("reconnecting", "error"):
            self.engine.activity.add(
                kind="stream", subscription_id=self.sub_id, subscription=self._sub_name,
                device_id=None, device="", ok=False,
                title="订阅流重连" if state == "reconnecting" else "订阅流异常", error=error,
            )

    # -- main loop --------------------------------------------------------- #
    def run(self) -> None:
        while not self._stop.is_set():
            try:
                row = self.engine.store.get_subscription(self.sub_id)
                if row is None or not row["is_active"]:
                    self._set_state("stopped")
                    return
                self._sub_name = row["name"]
                self._stream(row)
                self._set_state("reconnecting")
            except _StopStream:
                self._flush(force=True)
                self._set_state("stopped")
                return
            except Exception as exc:  # noqa: BLE001 - keep the worker alive
                if self._stop.is_set():
                    # Shutdown shuts the socket down on purpose; the resulting
                    # BrokenPipe/OSError is a normal stop, not a stream error.
                    self._flush(force=True)
                    self._set_state("stopped")
                    return
                message = _short(exc)
                self._set_state("error" if self._backoff >= 60 else "reconnecting", message)
                log(f"subscription #{self.sub_id} stream error: {message}")
            self._flush(force=True)
            if self._stop.is_set():
                break
            self._stop.wait(self._backoff)
            self._backoff = min(self._backoff * 2, 60)
        self._set_state("stopped")

    def _stream(self, row: sqlite3.Row) -> None:
        cursor = self._cursor or int(row["cursor_ts"] or 0)
        if self._resume_until is None:
            # First connection of this worker: skip anything we already handled
            # before the last shutdown (the persisted cursor), and ignore an old
            # backlog when there is no persisted cursor at all.
            self._resume_until = int(row["cursor_ts"] or 0)
            self._initial = not cursor
        since = str(cursor) if cursor else (row["since"] or "1m")
        base = (row["ntfy_url"] or "").rstrip("/")
        url = "{}/{}/json?since={}".format(
            base, urllib.parse.quote(row["topic"] or "", safe=""), urllib.parse.quote(since, safe=""))
        request = urllib.request.Request(url)
        if row["token"]:
            request.add_header("Authorization", f"Bearer {row['token']}")
        self._set_state("connecting")
        response = urllib.request.urlopen(request, timeout=120)
        self._resp = response
        self._sock = _response_socket(response)
        self._set_state("streaming")
        self._backoff = self.engine.cfg.reconnect_delay
        self.connected_at = time.time()
        try:
            for raw in response:
                if self._stop.is_set():
                    raise _StopStream
                line = raw.strip()
                if not line:
                    continue
                try:
                    message = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(message, dict) or message.get("event") != "message":
                    continue
                self._handle(row, message)
        finally:
            self._resp = None
            self._sock = None
            try:
                response.close()
            except Exception:  # noqa: BLE001
                pass

    # -- message handling -------------------------------------------------- #
    def _handle(self, row: sqlite3.Row, message: dict) -> None:
        try:
            timestamp = int(message.get("time") or 0)
        except (TypeError, ValueError):
            timestamp = 0
        # ntfy only has second-resolution timestamps, so several messages can
        # share one second: de-duplicate by message id as well as by cursor.
        message_id = message.get("id")
        if isinstance(message_id, str) and message_id:
            if message_id in self._seen:
                return
            self._seen.add(message_id)
            self._seen_order.append(message_id)
            if len(self._seen_order) > 500:
                self._seen.discard(self._seen_order.popleft())
        if timestamp:
            if self._resume_until and timestamp <= self._resume_until:
                # Replay of messages forwarded before the last restart.
                self._cursor = max(self._cursor, timestamp)
                self._flush()
                return
            if timestamp < self._cursor:
                return
            if self._initial and timestamp < self._started_at - 5:
                # First-ever connection with an "since" window: ignore old backlog.
                self._cursor = max(self._cursor, timestamp)
                self._flush()
                return
            if self._resume_until and timestamp > self._resume_until:
                self._resume_until = 0
            self._initial = False
            self._cursor = max(self._cursor, timestamp)
        try:
            priority = int(message.get("priority") or 3)
        except (TypeError, ValueError):
            priority = 3
        title = message.get("title") or row["topic"] or "ntfy"
        body = message.get("message") or ""
        click = message.get("click")
        click_url = click if isinstance(click, str) and click.startswith(("http://", "https://")) else None
        attach_url = ""
        attachment = message.get("attachment")
        if isinstance(attachment, dict):
            attach_url = str(attachment.get("url") or "").strip()
        raw_attach = message.get("attach")
        if not attach_url and isinstance(raw_attach, str):
            attach_url = raw_attach.strip()
        if self.engine.cfg.json_body_parse:
            unwrapped = _unwrap_json_notification(body)
            if unwrapped is not None:
                title = unwrapped.get("title") or title
                body = unwrapped.get("body") or body
                if unwrapped.get("priority") is not None:
                    priority = unwrapped["priority"]
                if unwrapped.get("click"):
                    click_url = unwrapped["click"]
                if unwrapped.get("attach"):
                    attach_url = unwrapped["attach"]
        if not self._passes(row, title, body, priority):
            self._flush()
            return
        image_url = None
        if attach_url and self.engine.cfg.send_image:
            if _looks_like_image(attach_url):
                image_url = attach_url
            else:
                body = f"{body}\n附件：{attach_url}".strip()
        prefix = row["title_prefix"] or ""
        title = (prefix + title)[:100]
        targets = self.engine.store.subscription_targets(self.sub_id)
        if targets:
            self.engine.push_to_devices(targets, title, body, priority, click_url,
                                        "push", self.sub_id, row["name"], image=image_url)
            self.engine.count_forwarded()
            self._fwd_pending += 1
        self._flush()

    def _passes(self, row: sqlite3.Row, title: str, body: str, priority: int) -> bool:
        minimum = int(row["priority_min"] or 0)
        if minimum and priority < minimum:
            return False
        haystack = f"{title}\n{body}".lower()
        keywords = (row["keyword"] or "").strip().lower()
        if keywords and not all(word in haystack for word in keywords.split()):
            return False
        excluded = (row["exclude_keyword"] or "").strip().lower()
        if excluded and any(word in haystack for word in excluded.split()):
            return False
        return True

    def _flush(self, force: bool = False) -> None:
        now = time.time()
        if not force and now - self._last_flush < 5 and not self._fwd_pending:
            return
        self._last_flush = now
        try:
            self.engine.store.update_cursor(self.sub_id, self._cursor, self._fwd_pending)
            self._fwd_pending = 0
        except Exception as exc:  # noqa: BLE001 - cursor persistence is best effort
            log(f"subscription #{self.sub_id} cursor update failed: {_short(exc)}")


class Engine:
    def __init__(self, store: Store, cfg: Config) -> None:
        self.store = store
        self.cfg = cfg
        self.activity = Activity(cfg.activity_size)
        self.started_at = time.time()
        self._pool = ThreadPoolExecutor(max_workers=cfg.push_workers, thread_name_prefix="bb-push")
        self._workers: dict[int, tuple[tuple, SubscriptionWorker]] = {}
        self._lock = threading.RLock()
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._device_stats: dict[int, dict] = {}
        self._stats_lock = threading.Lock()
        self._forwarded_total = 0
        self._supervisor = threading.Thread(target=self._supervise, name="bb-supervisor", daemon=True)

    # -- lifecycle --------------------------------------------------------- #
    def start(self) -> None:
        self._supervisor.start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        try:
            self._supervisor.join(timeout=5)
        except RuntimeError:
            pass
        stale: list[SubscriptionWorker] = []
        with self._lock:
            for _, worker in self._workers.values():
                stale.append(worker)
            self._workers.clear()
        for worker in stale:
            worker.stop()
        self._pool.shutdown(wait=False, cancel_futures=True)

    def notify_change(self) -> None:
        self._wake.set()

    def count_forwarded(self) -> None:
        with self._stats_lock:
            self._forwarded_total += 1

    # -- supervisor -------------------------------------------------------- #
    @staticmethod
    def _fingerprint(row: sqlite3.Row) -> tuple:
        return (row["ntfy_url"], row["topic"], row["token"], row["since"],
                row["priority_min"], row["keyword"], row["exclude_keyword"], row["title_prefix"])

    def _supervise(self) -> None:
        while not self._stop.is_set():
            try:
                self._reconcile()
            except Exception as exc:  # noqa: BLE001 - the supervisor must never die
                log(f"supervisor error: {_short(exc)}")
                traceback.print_exc()
            self._wake.wait(1.5)
            self._wake.clear()

    def _reconcile(self) -> None:
        desired = {int(row["id"]): self._fingerprint(row) for row in self.store.active_subscriptions()}
        stale: list[SubscriptionWorker] = []
        with self._lock:
            for sub_id, fingerprint in desired.items():
                current = self._workers.get(sub_id)
                if current is not None and (current[0] != fingerprint or not current[1].is_alive()):
                    stale.append(current[1])
                    del self._workers[sub_id]
                    current = None
                if current is None:
                    worker = SubscriptionWorker(self, sub_id, fingerprint)
                    self._workers[sub_id] = (fingerprint, worker)
                    worker.start()
                    log(f"subscription #{sub_id} worker started")
            for sub_id in list(self._workers):
                if sub_id not in desired:
                    stale.append(self._workers[sub_id][1])
                    del self._workers[sub_id]
                    log(f"subscription #{sub_id} worker stopped")
        # Stop workers outside the engine lock: interrupting a blocked stream
        # read must never stall the supervisor or an HTTP request thread.
        for worker in stale:
            worker.stop()

    # -- stats ------------------------------------------------------------- #
    def states(self) -> dict[int, dict]:
        with self._lock:
            return {
                sub_id: {"state": worker.state, "last_error": worker.last_error,
                         "connected_at": worker.connected_at}
                for sub_id, (_, worker) in self._workers.items()
            }

    def device_stats(self, device_id: int) -> dict:
        with self._stats_lock:
            stats = self._device_stats.get(device_id)
            return dict(stats) if stats else {"pushed_count": 0, "last_push_at": 0, "last_error": ""}

    def forwarded_total(self) -> int:
        with self._stats_lock:
            return self._forwarded_total

    def _record_device(self, device_id: int, ok: bool, error: str = "") -> None:
        with self._stats_lock:
            stats = self._device_stats.setdefault(
                device_id, {"pushed_count": 0, "last_push_at": 0, "last_error": ""})
            stats["last_push_at"] = now_ts()
            if ok:
                stats["pushed_count"] += 1
                stats["last_error"] = ""
            else:
                stats["last_error"] = error

    # -- pushing ----------------------------------------------------------- #
    def push_to_devices(self, devices, title: str, body: str, priority: int, url: str | None,
                        kind: str, sub_id: int | None, sub_name: str,
                        image: str | None = None) -> None:
        for device in devices:
            future = self._pool.submit(self._push_one, device, title, body, priority, url,
                                       kind, sub_id, sub_name, image)
            future.add_done_callback(self._log_push_error)

    @staticmethod
    def _log_push_error(future) -> None:
        try:
            exc = future.exception()
        except BaseException:  # noqa: BLE001 - cancelled during shutdown
            return
        if exc is not None:  # noqa: BLE001 - never let a push task fail silently
            log(f"push task crashed: {_short(exc)}")

    def push_now(self, devices, title: str, body: str, priority: int, url: str | None,
                 kind: str, sub_id: int | None, sub_name: str,
                 image: str | None = None) -> tuple[int, int]:
        sent = failed = 0
        for device in devices:
            if self._push_one(device, title, body, priority, url, kind, sub_id, sub_name, image):
                sent += 1
            else:
                failed += 1
        return sent, failed

    def _push_one(self, device, title: str, body: str, priority: int, url: str | None,
                  kind: str, sub_id: int | None, sub_name: str,
                  image: str | None = None) -> bool:
        level = (device["level"] or "").strip() or map_level(priority)
        safe_title = (title or "")[:100]
        safe_body = (body or "")[:self.cfg.max_body]
        try:
            bark_push(device["server"], device["bark_key"], safe_title, safe_body,
                      level, device["grp"], device["icon"], url, image)
        except Exception as exc:  # noqa: BLE001 - one device must not break the others
            error = _short(exc)
            self._record_device(device["id"], False, error)
            self.activity.add(kind=kind, subscription_id=sub_id, subscription=sub_name,
                              device_id=device["id"], device=device["name"], ok=False,
                              title=safe_title, error=error)
            log(f"push failed device #{device['id']} ({device['name']}): {error}")
            return False
        self._record_device(device["id"], True)
        self.activity.add(kind=kind, subscription_id=sub_id, subscription=sub_name,
                          device_id=device["id"], device=device["name"], ok=True,
                          title=safe_title, error="")
        log(f"forwarded {safe_title!r} -> {device['name']}")
        return True


# --------------------------------------------------------------------------- #
# online update (GitHub archive -> replace program files -> restart)
# --------------------------------------------------------------------------- #

class Updater:
    """Check a GitHub repository for a newer bark-bridge and apply it in place."""

    CACHE_KEY = "update_last_check"
    APPLIED_KEY = "update_last_applied"
    CACHE_TTL = 600
    REPO_RE = re.compile(r"^[A-Za-z0-9_.-]{1,100}/[A-Za-z0-9_.-]{1,100}$")
    BRANCH_RE = re.compile(r"^[A-Za-z0-9_.\-/]{1,100}$")
    REQUIRED_FILES = ("bark-bridge.py", "webui.html")

    def __init__(self, store: Store, cfg: Config, activity: Activity) -> None:
        self.store = store
        self.cfg = cfg
        self.activity = activity
        self._fetch_lock = threading.Lock()

    # -- local state ------------------------------------------------------- #
    def _local_bytes(self) -> bytes:
        try:
            with open(self.cfg.app_file, "rb") as handle:
                return handle.read()
        except OSError:
            return b""

    def _local_hash(self) -> str:
        return hashlib.sha256(_normalized(self._local_bytes())).hexdigest()

    def _applied(self) -> dict | None:
        raw = self.store.settings_get(self.APPLIED_KEY)
        if not raw:
            return None
        try:
            applied = json.loads(raw)
        except ValueError:
            return None
        return applied if isinstance(applied, dict) else None

    def _writability(self) -> tuple[bool, str]:
        if not self.cfg.update_enabled:
            return False, "在线更新已关闭（UPDATE_ENABLED=0）"
        try:
            for path in (self.cfg.app_file, self.cfg.webui_file):
                directory = os.path.dirname(os.path.abspath(path)) or "."
                if not os.access(directory, os.W_OK):
                    return False, f"程序目录不可写：{directory}（Docker 部署请以可写方式挂载代码目录）"
                fd, probe = tempfile.mkstemp(dir=directory, prefix=".bb-write-test-")
                os.close(fd)
                os.unlink(probe)
        except OSError as exc:
            return False, f"程序目录不可写：{_short(exc)}（只读挂载或权限不足）"
        return True, ""

    # -- cache ------------------------------------------------------------- #
    def _cache(self) -> dict:
        raw = self.store.settings_get(self.CACHE_KEY)
        if not raw:
            return {}
        try:
            cache = json.loads(raw)
        except ValueError:
            return {}
        return cache if isinstance(cache, dict) else {}

    def _save_cache(self, cache: dict) -> None:
        try:
            self.store.settings_set(self.CACHE_KEY, json.dumps(cache, ensure_ascii=False))
        except Exception as exc:  # noqa: BLE001 - cache writes are best effort
            log(f"update cache write failed: {_short(exc)}")

    def _base_payload(self) -> dict:
        writable, reason = self._writability()
        return {
            "current_version": __version__,
            "current_hash": self._local_hash()[:8],
            "repo": self.cfg.update_repo,
            "branch": self.cfg.update_branch,
            "enabled": self.cfg.update_enabled,
            "checked_at": 0,
            "cached": False,
            "remote": None,
            "warning": "",
            "error": "",
            "apply": {"supported": writable, "reason": reason},
            "last_applied": self._applied(),
        }

    @staticmethod
    def _fresh(cache: dict) -> bool:
        return (isinstance(cache.get("remote"), dict)
                and now_ts() - int(cache.get("ts") or 0) < Updater.CACHE_TTL)

    def _from_cache(self, payload: dict, cache: dict, cached: bool = True) -> dict:
        payload.update(
            cached=cached,
            checked_at=int(cache.get("ts") or 0),
            remote=cache.get("remote"),
            warning=cache.get("warning") or "",
            error=cache.get("error") or "",
        )
        return payload

    # -- public API -------------------------------------------------------- #
    def status(self) -> dict:
        return self.refresh(force=False)

    def refresh(self, force: bool = False) -> dict:
        payload = self._base_payload()
        if not self.cfg.update_enabled:
            return payload
        cache = self._cache()
        if not force and self._fresh(cache):
            return self._from_cache(payload, cache)
        if not self._fetch_lock.acquire(timeout=1.0):
            if isinstance(cache.get("remote"), dict):
                return self._from_cache(payload, cache)
            payload["error"] = "正在检查更新，请稍后重试"
            return payload
        try:
            fetched = self._fetch()
        except Exception as exc:  # noqa: BLE001 - surface the reason to the WebUI
            error = _short(exc)
            log(f"update check failed: {error}")
            cache = dict(cache)
            cache.update(ts=now_ts(), error=error)
            self._save_cache(cache)
            payload["checked_at"] = cache["ts"]
            payload["error"] = error
            if isinstance(cache.get("remote"), dict):
                payload["remote"] = cache["remote"]
                payload["warning"] = cache.get("warning") or ""
            return payload
        finally:
            self._fetch_lock.release()

        remote_hash = hashlib.sha256(_normalized(fetched["script"])).hexdigest()
        local_full = self._local_hash()
        remote_version = fetched["version"]
        missing = fetched.get("missing") or []
        behind = _version_key(remote_version) < _version_key(__version__) or bool(missing)
        available = remote_hash != local_full and not behind
        warning = ""
        if missing:
            warning = ("远端仓库缺少 " + " / ".join(missing) +
                       "（可能还是旧版 v1）：请先把新版本 push 到 GitHub 再更新")
        elif behind:
            warning = (f"远端版本 v{remote_version} 低于本地 v{__version__}，"
                       "可能是本地改动还没有推送到 GitHub")
        commit = fetched.get("commit") or {}
        remote = {
            "available": available,
            "behind": behind,
            "version": remote_version,
            "hash": remote_hash[:8],
            "commit": (commit.get("sha") or "")[:7],
            "subject": (commit.get("subject") or "")[:160],
            "committed_at": commit.get("committed_at") or "",
            "missing": missing,
        }
        cache = {"ts": now_ts(), "remote": remote, "error": "", "warning": warning}
        self._save_cache(cache)
        payload.update(cached=False, checked_at=cache["ts"], remote=remote, warning=warning)
        return payload

    def apply(self) -> dict:
        cfg = self.cfg
        if not cfg.update_enabled:
            raise ApiError(400, "update_disabled", "在线更新已关闭（UPDATE_ENABLED=0）")
        status = self.refresh(force=True)
        if status.get("error"):
            raise ApiError(502, "update_failed", f"检查更新失败：{status['error']}")
        remote = status.get("remote") or {}
        if remote.get("missing"):
            raise ApiError(400, "invalid_request", status.get("warning") or "远端仓库缺少必要文件")
        if remote.get("behind"):
            raise ApiError(400, "invalid_request",
                           status.get("warning") or "远端版本比本地旧，拒绝降级")
        if not remote.get("available"):
            raise ApiError(400, "invalid_request", "已是最新版本")
        writable, reason = self._writability()
        if not writable:
            raise ApiError(502, "update_unsupported", reason)
        backup_dir = ""
        try:
            fetched = self._fetch()
            script, webui = fetched["script"], fetched["webui"]
            if len(script) > 4_000_000 or len(webui) > 4_000_000:
                raise RuntimeError("远端文件过大")
            compile(script.decode("utf-8"), "bark-bridge.py", "exec")
            if b"<script" not in webui or b"/api/" not in webui:
                raise RuntimeError("远端 webui.html 内容异常（缺少脚本或 /api/ 引用）")
            backup_dir = self._backup(fetched)
            _atomic_write(cfg.app_file, script)
            _atomic_write(cfg.webui_file, webui)
        except ApiError:
            raise
        except Exception as exc:  # noqa: BLE001 - report the concrete failure
            raise ApiError(502, "update_failed", f"更新失败：{_short(exc)}")
        applied = {
            "from_version": __version__,
            "to_version": fetched["version"],
            "at": now_ts(),
            "backup_dir": backup_dir,
            "commit": (fetched.get("commit") or {}).get("sha") or "",
        }
        self.store.settings_set(self.CACHE_KEY, "{}")
        self.store.settings_set(self.APPLIED_KEY, json.dumps(applied, ensure_ascii=False))
        self.activity.add(kind="update", subscription_id=None, subscription="", device_id=None,
                          device="", ok=True, title=f"更新到 v{fetched['version']}", error="")
        log(f"update applied: v{__version__} -> v{fetched['version']} (backup {backup_dir})")
        return {"restarting": True, "from_version": __version__,
                "to_version": fetched["version"], "backup_dir": backup_dir}

    # -- remote ------------------------------------------------------------ #
    def _fetch(self) -> dict:
        repo = self.cfg.update_repo
        branch = self.cfg.update_branch
        if not self.REPO_RE.match(repo):
            raise RuntimeError(f"UPDATE_REPO 格式不正确：{repo!r}（应为 owner/name）")
        if not self.BRANCH_RE.match(branch):
            raise RuntimeError(f"UPDATE_BRANCH 格式不正确：{branch!r}")
        quoted_branch = urllib.parse.quote(branch, safe="/")
        raw = None
        last_error: Exception | None = None
        # Branches first, then tags, so UPDATE_BRANCH can pin a release tag.
        for ref in ("heads", "tags"):
            url = "{}/{}/tar.gz/refs/{}/{}".format(
                self.cfg.update_codeload_base, repo, ref, quoted_branch)
            try:
                raw = _http_get(url, timeout=30, max_bytes=20 * 1024 * 1024)
                break
            except Exception as exc:  # noqa: BLE001 - try the next ref kind
                last_error = exc
        if raw is None:
            raise RuntimeError(f"下载源码包失败（分支或 tag 不存在）：{_short(last_error or RuntimeError('unknown'))}")
        try:
            archive = tarfile.open(fileobj=io.BytesIO(raw), mode="r:gz")
        except tarfile.TarError as exc:
            raise RuntimeError(f"源码包解压失败：{_short(exc)}")
        with archive:
            best: dict[str, tuple[int, tarfile.TarInfo]] = {}
            for member in archive.getmembers():
                if not member.isfile():
                    continue
                name = member.name.rsplit("/", 1)[-1]
                if name not in self.REQUIRED_FILES:
                    continue
                depth = member.name.count("/")
                if name not in best or depth < best[name][0]:
                    best[name] = (depth, member)
            missing = [name for name in self.REQUIRED_FILES if name not in best]
            if "bark-bridge.py" in missing:
                raise RuntimeError("源码包里缺少 bark-bridge.py（仓库结构不对）")
            script = archive.extractfile(best["bark-bridge.py"][1]).read()
            webui = b""
            if "webui.html" not in missing:
                webui = archive.extractfile(best["webui.html"][1]).read()
        if not script:
            raise RuntimeError("源码包里的 bark-bridge.py 为空")
        text = script.decode("utf-8", "replace")
        match = re.search(r'^__version__\s*=\s*["\']([^"\']+)["\']', text, re.M)
        # Repositories still on the v1 script have no __version__ marker and no
        # webui.html; treat them as older so the WebUI shows "remote is behind"
        # (and refuses to "update", which would otherwise downgrade to v1).
        version = match.group(1) if match else "0.0.0"
        return {"script": script, "webui": webui, "version": version,
                "version_missing": match is None, "missing": missing,
                "commit": self._fetch_commit()}

    def _fetch_commit(self) -> dict:
        url = "{}/repos/{}/commits/{}".format(
            self.cfg.update_api_base, self.cfg.update_repo,
            urllib.parse.quote(self.cfg.update_branch, safe="/"))
        try:
            raw = _http_get(url, timeout=10, max_bytes=1024 * 1024,
                            headers={"Accept": "application/vnd.github+json"})
            data = json.loads(raw)
            commit = data.get("commit") or {}
            author = commit.get("author") or {}
            message = (commit.get("message") or "").strip().splitlines()
            return {"sha": data.get("sha") or "",
                    "subject": (message[0] if message else "")[:160],
                    "committed_at": author.get("date") or ""}
        except Exception as exc:  # noqa: BLE001 - commit info is optional
            log(f"update: commit info unavailable ({_short(exc)})")
            return {}

    # -- applying ---------------------------------------------------------- #
    def _backup(self, fetched: dict) -> str:
        stamp = time.strftime("%Y%m%d-%H%M%S")
        directory = os.path.join(self.cfg.data_dir, f"update-backup-{stamp}")
        os.makedirs(directory, exist_ok=True)
        for source, name in ((self.cfg.app_file, "bark-bridge.py"),
                             (self.cfg.webui_file, "webui.html")):
            target = os.path.join(directory, name)
            try:
                shutil.copy2(source, target)
            except OSError:
                with open(source, "rb") as src, open(target, "wb") as dst:
                    dst.write(src.read())
        commit = fetched.get("commit") or {}
        meta = {"from_version": __version__, "to_version": fetched.get("version") or "",
                "commit": commit.get("sha") or "", "applied_at": now_ts()}
        with open(os.path.join(directory, "meta.json"), "w", encoding="utf-8") as handle:
            json.dump(meta, handle, ensure_ascii=False, indent=2)
        return directory


# --------------------------------------------------------------------------- #
# validation helpers
# --------------------------------------------------------------------------- #

USERNAME_RE = re.compile(r"^[A-Za-z0-9_.-]{2,32}$")
TOPIC_RE = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")
SINCE_RE = re.compile(r"^(?:all|now|\d+(?:[smhd])?)$")
URL_RE = re.compile(r"^https?://\S{1,190}$")
BARK_KEY_RE = re.compile(r"^\S{4,128}$")


class RateLimiter:
    def __init__(self, limit: int = 8, window: int = 300) -> None:
        self.limit = limit
        self.window = window
        self._hits: dict[str, list[float]] = {}
        self._lock = threading.Lock()

    def blocked(self, key: str) -> int:
        now = time.time()
        with self._lock:
            hits = [stamp for stamp in self._hits.get(key, ()) if now - stamp < self.window]
            self._hits[key] = hits
            if len(self._hits) > 5000:
                self._hits = {name: stamps for name, stamps in self._hits.items() if stamps}
            if len(hits) >= self.limit:
                return max(1, int(self.window - (now - hits[0])))
            return 0

    def fail(self, key: str) -> None:
        with self._lock:
            self._hits.setdefault(key, []).append(time.time())

    def reset(self, key: str) -> None:
        with self._lock:
            self._hits.pop(key, None)


def user_public(row) -> dict:
    return {"id": row["id"], "username": row["username"], "role": row["role"],
            "is_active": bool(row["is_active"]), "created_at": row["created_at"]}


def device_public(row: sqlite3.Row, engine: Engine) -> dict:
    stats = engine.device_stats(row["id"])
    return {
        "id": row["id"], "owner_id": row["user_id"], "owner": row["owner"] or "",
        "name": row["name"], "bark_key": row["bark_key"], "server": row["server"],
        "group": row["grp"], "level": row["level"], "icon": row["icon"],
        "is_active": bool(row["is_active"]), "created_at": row["created_at"],
        "updated_at": row["updated_at"], "pushed_count": stats["pushed_count"],
        "last_push_at": stats["last_push_at"], "last_error": stats["last_error"],
    }


def subscription_public(row: sqlite3.Row, device_ids: list[int], states: dict[int, dict]) -> dict:
    state = states.get(row["id"])
    return {
        "id": row["id"], "owner_id": row["user_id"], "owner": row["owner"] or "",
        "name": row["name"], "ntfy_url": row["ntfy_url"], "topic": row["topic"],
        "has_token": bool(row["token"]), "since": row["since"],
        "priority_min": row["priority_min"], "keyword": row["keyword"],
        "exclude_keyword": row["exclude_keyword"], "title_prefix": row["title_prefix"],
        "is_active": bool(row["is_active"]), "device_ids": device_ids,
        "created_at": row["created_at"], "updated_at": row["updated_at"],
        "stats": {
            "state": state["state"] if state else ("connecting" if row["is_active"] else "stopped"),
            "last_error": state["last_error"] if state else "",
            "forwarded": row["forwarded"],
            "last_message_at": row["cursor_ts"],
        },
    }


class Route:
    __slots__ = ("method", "pattern", "auth", "handler", "csrf_exempt")

    def __init__(self, method: str, pattern: str, auth: str, handler: str,
                 csrf_exempt: bool = False) -> None:
        self.method = method
        self.pattern = re.compile(pattern)
        self.auth = auth
        self.handler = handler
        self.csrf_exempt = csrf_exempt


ROUTES = [
    Route("GET", r"^/api/health$", "public", "health"),
    Route("POST", r"^/api/setup$", "public", "setup", True),
    Route("POST", r"^/api/login$", "public", "login", True),
    Route("POST", r"^/api/logout$", "public", "logout", True),
    Route("GET", r"^/api/me$", "user", "me"),
    Route("POST", r"^/api/password$", "user", "password"),
    Route("GET", r"^/api/settings$", "user", "settings_get"),
    Route("PUT", r"^/api/settings$", "admin", "settings_put"),
    Route("GET", r"^/api/devices$", "user", "devices_list"),
    Route("POST", r"^/api/devices$", "user", "devices_create"),
    Route("PUT", r"^/api/devices/(?P<id>\d+)$", "user", "devices_update"),
    Route("DELETE", r"^/api/devices/(?P<id>\d+)$", "user", "devices_delete"),
    Route("POST", r"^/api/devices/(?P<id>\d+)/test$", "user", "devices_test"),
    Route("GET", r"^/api/subscriptions$", "user", "subs_list"),
    Route("POST", r"^/api/subscriptions$", "user", "subs_create"),
    Route("PUT", r"^/api/subscriptions/(?P<id>\d+)$", "user", "subs_update"),
    Route("DELETE", r"^/api/subscriptions/(?P<id>\d+)$", "user", "subs_delete"),
    Route("POST", r"^/api/subscriptions/(?P<id>\d+)/test$", "user", "subs_test"),
    Route("GET", r"^/api/users$", "admin", "users_list"),
    Route("POST", r"^/api/users$", "admin", "users_create"),
    Route("PUT", r"^/api/users/(?P<id>\d+)$", "admin", "users_update"),
    Route("DELETE", r"^/api/users/(?P<id>\d+)$", "admin", "users_delete"),
    Route("GET", r"^/api/status$", "user", "status"),
    Route("GET", r"^/api/activity$", "user", "activity"),
    Route("GET", r"^/api/update$", "admin", "update_status"),
    Route("POST", r"^/api/update/check$", "admin", "update_check"),
    Route("POST", r"^/api/update/apply$", "admin", "update_apply"),
]


# --------------------------------------------------------------------------- #
# application context
# --------------------------------------------------------------------------- #

class App:
    def __init__(self, store: Store, cfg: Config, engine: Engine) -> None:
        self.store = store
        self.cfg = cfg
        self.engine = engine
        self.started_at = time.time()
        self.login_limiter = RateLimiter()
        self.secret = (store.settings_get("secret") or "").encode("utf-8")
        self.updater = Updater(store, cfg, engine.activity)
        self.restart_requested = False
        self.request_restart = None
        self._background_stop = threading.Event()

    # -- background update check ------------------------------------------- #
    def start_background(self) -> None:
        if self.cfg.update_enabled and self.cfg.update_autocheck:
            threading.Thread(target=self._update_warmup, name="bb-update", daemon=True).start()

    def _update_warmup(self) -> None:
        self._background_stop.wait(6)
        while not self._background_stop.is_set():
            try:
                status = self.updater.refresh(force=False)
                remote = status.get("remote") or {}
                if status.get("error"):
                    log(f"update check failed: {status['error']}")
                elif remote.get("available"):
                    log(f"update available: v{remote.get('version')} "
                        f"(local v{__version__}, commit {remote.get('commit') or '?'})")
            except Exception as exc:  # noqa: BLE001 - background check must not crash
                log(f"update check error: {_short(exc)}")
            self._background_stop.wait(6 * 3600)

    def schedule_restart(self) -> None:
        """Ask main() to restart the process shortly after the HTTP response."""
        if self.request_restart is None:
            return
        self.restart_requested = True
        threading.Thread(target=self.request_restart, name="bb-restart", daemon=True).start()

    def issue_session(self, user) -> str:
        payload = {"u": int(user["id"]), "e": now_ts() + self.cfg.session_ttl,
                   "v": pwd_version(user["password_hash"])}
        raw = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        signature = hmac.new(self.secret, raw, hashlib.sha256).digest()
        return b64u(raw) + "." + b64u(signature)

    def decode_session(self, token: str | None) -> dict | None:
        if not token or "." not in token:
            return None
        try:
            payload_b64, signature_b64 = token.split(".", 1)
            raw = b64u_dec(payload_b64)
            signature = b64u_dec(signature_b64)
        except (ValueError, TypeError):
            return None
        expected = hmac.new(self.secret, raw, hashlib.sha256).digest()
        if not hmac.compare_digest(signature, expected):
            return None
        try:
            payload = json.loads(raw)
        except ValueError:
            return None
        if not isinstance(payload, dict) or int(payload.get("e") or 0) < now_ts():
            return None
        return payload


# --------------------------------------------------------------------------- #
# HTTP request handler
# --------------------------------------------------------------------------- #

class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = f"bark-bridge/{__version__}"
    sys_version = ""
    timeout = 60

    app: App = None  # type: ignore[assignment]  # set in main()

    # -- plumbing ---------------------------------------------------------- #
    def log_message(self, fmt: str, *args) -> None:  # noqa: A003
        if self.app is not None and self.app.cfg.access_log:
            log(f"{self.address_string()} {fmt % args}")

    def do_GET(self) -> None:  # noqa: N802
        self._handle("GET")

    def do_POST(self) -> None:  # noqa: N802
        self._handle("POST")

    def do_PUT(self) -> None:  # noqa: N802
        self._handle("PUT")

    def do_PATCH(self) -> None:  # noqa: N802
        self._handle("PATCH")

    def do_DELETE(self) -> None:  # noqa: N802
        self._handle("DELETE")

    def do_HEAD(self) -> None:  # noqa: N802
        self._handle("HEAD")

    def do_OPTIONS(self) -> None:  # noqa: N802
        self._handle("OPTIONS")

    def _handle(self, method: str) -> None:
        self._cookies_out: list[str] = []
        self._body_read = False
        try:
            self._handle_inner(method)
        except ApiError as exc:
            # An unread request body would be parsed as the next request on a
            # keep-alive connection, so errors before the body is consumed must
            # close the connection.
            if not self._body_read and self.command in ("POST", "PUT", "PATCH", "DELETE"):
                self.close_connection = True
            self._send_json_error(exc.status, exc.code, exc.message)
        except (BrokenPipeError, ConnectionResetError):
            self.close_connection = True
        except Exception:  # noqa: BLE001 - never leak a traceback to the client
            traceback.print_exc()
            self.close_connection = True
            try:
                self._send_json_error(500, "internal", "服务器内部错误")
            except Exception:  # noqa: BLE001
                self.close_connection = True

    def _handle_inner(self, method: str) -> None:
        split = urllib.parse.urlsplit(self.path)
        path = split.path.rstrip("/") or "/"
        query = urllib.parse.parse_qs(split.query)

        if method in ("GET", "HEAD"):
            if path in ("/", "/index.html"):
                return self._serve_webui()
            if path == "/favicon.ico":
                return self._send(204, b"", "text/plain")

        route = None
        path_matched = False
        for candidate in ROUTES:
            if candidate.pattern.match(path):
                path_matched = True
                if candidate.method == method:
                    route = candidate
                    break
        if route is None:
            if path_matched:
                raise ApiError(405, "method_not_allowed", "不允许的请求方法")
            raise ApiError(404, "not_found", "接口不存在")

        user = None
        if route.auth != "public":
            user = self._auth_user()
            if route.auth == "admin" and user["role"] != "admin":
                raise ApiError(403, "forbidden", "需要管理员权限")
        if method in ("POST", "PUT", "PATCH", "DELETE") and not route.csrf_exempt:
            self._check_csrf()

        body = self._read_json_body() if method in ("POST", "PUT", "PATCH", "DELETE") else {}
        match = route.pattern.match(path)
        result = getattr(self, f"api_{route.handler}")(match, body, query, user)
        status, data = result if isinstance(result, tuple) else (200, result)
        if method == "HEAD":
            return self._send(status, b"", "application/json; charset=utf-8")
        self._send_json(status, data)

    # -- cookies / auth ---------------------------------------------------- #
    def _cookies(self) -> dict[str, str]:
        raw = self.headers.get("Cookie")
        if not raw:
            return {}
        try:
            jar = SimpleCookie()
            jar.load(raw)
        except Exception:  # noqa: BLE001 - ignore malformed Cookie headers
            return {}
        return {name: morsel.value for name, morsel in jar.items()}

    def _add_cookie(self, name: str, value: str, max_age: int, http_only: bool = True) -> None:
        parts = [f"{name}={value}", "Path=/", f"Max-Age={int(max_age)}", "SameSite=Lax"]
        if http_only:
            parts.append("HttpOnly")
        self._cookies_out.append("; ".join(parts))

    def _auth_user(self) -> dict:
        payload = self.app.decode_session(self._cookies().get("bb_session"))
        if not payload:
            raise ApiError(401, "unauthorized", "请先登录")
        row = self.app.store.get_user(int(payload.get("u") or 0))
        if row is None or not row["is_active"]:
            raise ApiError(401, "unauthorized", "账号不可用")
        if payload.get("v") != pwd_version(row["password_hash"]):
            raise ApiError(401, "unauthorized", "会话已失效，请重新登录")
        return dict(row)

    def _check_csrf(self) -> None:
        cookie = self._cookies().get("bb_csrf") or ""
        header = self.headers.get("X-CSRF-Token") or ""
        if not cookie or not header or not hmac.compare_digest(cookie, header):
            raise ApiError(403, "csrf_failed", "CSRF 校验失败，请刷新页面后重试")

    def _login_session(self, user) -> dict:
        csrf = secrets.token_urlsafe(24)
        ttl = self.app.cfg.session_ttl
        self._add_cookie("bb_session", self.app.issue_session(user), ttl, True)
        self._add_cookie("bb_csrf", csrf, ttl, False)
        return {"user": user_public(user), "csrf": csrf}

    # -- body / validation ------------------------------------------------- #
    def _read_json_body(self) -> dict:
        raw_length = self.headers.get("Content-Length")
        try:
            length = int(raw_length) if raw_length else 0
        except ValueError:
            self.close_connection = True
            raise ApiError(400, "invalid_request", "Content-Length 无效")
        if length == 0:
            self._body_read = True
            return {}
        if length > MAX_PAYLOAD:
            self.close_connection = True
            raise ApiError(413, "payload_too_large", "请求体过大")
        content_type = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        if content_type and content_type != "application/json":
            self.close_connection = True
            raise ApiError(400, "invalid_request", "Content-Type 必须是 application/json")
        raw = self.rfile.read(length)
        self._body_read = True
        if not raw:
            return {}
        try:
            parsed = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            raise ApiError(400, "invalid_request", "请求体不是合法 JSON")
        if not isinstance(parsed, dict):
            raise ApiError(400, "invalid_request", "请求体必须是 JSON 对象")
        return parsed

    @staticmethod
    def _vstr(body: dict, key: str, label: str, *, required: bool = False,
              default: str | None = None, min_len: int = 0, max_len: int = 200,
              pattern: re.Pattern | None = None, allow_empty: bool = False) -> str | None:
        present = key in body and body[key] is not None
        if not present:
            if required:
                raise ApiError(400, "invalid_request", f"{label}不能为空")
            return default
        value = body[key]
        if not isinstance(value, str):
            raise ApiError(400, "invalid_request", f"{label}必须是字符串")
        value = value.strip()
        if not value:
            if required or not allow_empty:
                raise ApiError(400, "invalid_request", f"{label}不能为空")
            return ""
        if len(value) < min_len or len(value) > max_len:
            raise ApiError(400, "invalid_request", f"{label}长度必须在 {min_len}~{max_len} 之间")
        if pattern is not None and not pattern.match(value):
            raise ApiError(400, "invalid_request", f"{label}格式不正确")
        return value

    @staticmethod
    def _vint(body: dict, key: str, label: str, *, required: bool = False,
              default: int | None = None, lo: int = 0, hi: int = 2 ** 31 - 1) -> int | None:
        present = key in body and body[key] is not None
        if not present:
            if required:
                raise ApiError(400, "invalid_request", f"{label}不能为空")
            return default
        value = body[key]
        if isinstance(value, bool):
            raise ApiError(400, "invalid_request", f"{label}必须是整数")
        try:
            number = int(value)
        except (TypeError, ValueError):
            raise ApiError(400, "invalid_request", f"{label}必须是整数")
        if not lo <= number <= hi:
            raise ApiError(400, "invalid_request", f"{label}必须在 {lo}~{hi} 之间")
        return number

    @staticmethod
    def _vbool(body: dict, key: str, label: str, *, default: bool | None = None) -> bool | None:
        if key not in body or body[key] is None:
            return default
        value = body[key]
        if isinstance(value, bool):
            return value
        if isinstance(value, int) and value in (0, 1):
            return bool(value)
        if isinstance(value, str) and value.strip().lower() in ("true", "false", "1", "0"):
            return value.strip().lower() in ("true", "1")
        raise ApiError(400, "invalid_request", f"{label}必须是布尔值")

    def _vurl(self, body: dict, key: str, label: str, *, required: bool = False,
              default: str | None = None) -> str | None:
        value = self._vstr(body, key, label, required=required, default=default,
                           max_len=200, pattern=URL_RE, allow_empty=False)
        return value.rstrip("/") if value else value

    def _vsince(self, body: dict, key: str, label: str, *, default: str | None = None,
                required: bool = False) -> str | None:
        return self._vstr(body, key, label, required=required, default=default,
                          min_len=1, max_len=32, pattern=SINCE_RE)

    @staticmethod
    def _vlevel(body: dict, key: str, label: str, *, default: str | None = None) -> str | None:
        value = Handler._vstr(body, key, label, default=default, max_len=16, allow_empty=True)
        if value is not None and value not in LEVELS:
            raise ApiError(400, "invalid_request", f"{label}只能是空/{'/'.join(x for x in LEVELS if x)}")
        return value

    def _vdevice_ids(self, body: dict, owner_id: int, user: dict) -> list[int] | None:
        if "device_ids" not in body or body["device_ids"] is None:
            return None
        value = body["device_ids"]
        if not isinstance(value, list):
            raise ApiError(400, "invalid_request", "device_ids 必须是数组")
        if len(value) > 100:
            raise ApiError(400, "invalid_request", "device_ids 最多 100 项")
        cleaned: list[int] = []
        for item in value:
            if isinstance(item, bool):
                raise ApiError(400, "invalid_request", "device_ids 必须是整数数组")
            try:
                device_id = int(item)
            except (TypeError, ValueError):
                raise ApiError(400, "invalid_request", "device_ids 必须是整数数组")
            if device_id not in cleaned:
                cleaned.append(device_id)
        for device_id in cleaned:
            device = self.app.store.get_device(device_id)
            if device is None:
                raise ApiError(400, "invalid_request", f"设备 {device_id} 不存在")
            if device["user_id"] != owner_id and user["role"] != "admin":
                raise ApiError(403, "forbidden", "不能关联其他用户的设备")
        return cleaned

    def _target_owner(self, body: dict, user: dict) -> int:
        if "owner_id" not in body or body["owner_id"] is None:
            return int(user["id"])
        owner_id = self._vint(body, "owner_id", "所属用户", lo=1)
        if owner_id != int(user["id"]) and user["role"] != "admin":
            raise ApiError(403, "forbidden", "无权为其他用户创建记录")
        if self.app.store.get_user(owner_id) is None:
            raise ApiError(400, "invalid_request", "所属用户不存在")
        return int(owner_id)

    @staticmethod
    def _owned(row, user: dict, label: str) -> None:
        if user["role"] != "admin" and int(row["user_id"]) != int(user["id"]):
            raise ApiError(403, "forbidden", f"无权操作该{label}")

    def _owner_filter(self, user: dict) -> int | None:
        return None if user["role"] == "admin" else int(user["id"])

    # -- responses --------------------------------------------------------- #
    def _send(self, status: int, body: bytes, content_type: str,
              extra: dict[str, str] | None = None, cache_control: str = "no-store") -> None:
        try:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Content-Security-Policy", CSP)
            self.send_header("Cache-Control", cache_control)
            if self.close_connection:
                # Explicitly signal the close so clients reconnect instead of
                # reusing a connection whose request body we may not have read.
                self.send_header("Connection", "close")
            for name, value in (extra or {}).items():
                self.send_header(name, value)
            for cookie in self._cookies_out:
                self.send_header("Set-Cookie", cookie)
            self.end_headers()
            if body and self.command != "HEAD":
                self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            self.close_connection = True

    @staticmethod
    def _compressible(body: bytes, accept_encoding: str) -> tuple[bytes, dict[str, str]]:
        if len(body) >= 512 and "gzip" in accept_encoding.lower():
            return gzip.compress(body, compresslevel=6, mtime=0), {
                "Content-Encoding": "gzip", "Vary": "Accept-Encoding"}
        return body, {}

    def _send_json(self, status: int, data: dict) -> None:
        payload = json.dumps({"ok": True, "data": data}, ensure_ascii=False).encode("utf-8")
        body, extra = self._compressible(payload, self.headers.get("Accept-Encoding") or "")
        self._send(status, body, "application/json; charset=utf-8", extra)

    def _send_json_error(self, status: int, code: str, message: str) -> None:
        payload = json.dumps({"ok": False, "error": message, "code": code},
                             ensure_ascii=False).encode("utf-8")
        body, extra = self._compressible(payload, self.headers.get("Accept-Encoding") or "")
        self._send(status, body, "application/json; charset=utf-8", extra)

    _webui_cache: dict = {"mtime": -1.0, "size": -1, "data": b""}

    def _serve_webui(self) -> None:
        cfg = self.app.cfg
        try:
            stat = os.stat(cfg.webui_file)
            cache = Handler._webui_cache
            if stat.st_mtime != cache["mtime"] or stat.st_size != cache["size"]:
                with open(cfg.webui_file, "rb") as handle:
                    cache["data"] = handle.read()
                cache["mtime"] = stat.st_mtime
                cache["size"] = stat.st_size
            data = cache["data"]
        except OSError as exc:
            log(f"webui.html unreadable: {_short(exc)}")
            return self._send_json_error(500, "internal", "webui.html 缺失，无法加载管理界面")
        body, extra = self._compressible(data, self.headers.get("Accept-Encoding") or "")
        self._send(200, body, "text/html; charset=utf-8", extra, cache_control="no-cache")

    # ------ API: system / auth ------------------------------------------- #
    def api_health(self, match, body, query, user):
        return {"version": __version__, "uptime": int(time.time() - self.app.started_at),
                "now": now_ts(), "needs_setup": self.app.store.count_users() == 0}

    def api_setup(self, match, body, query, user):
        store = self.app.store
        if store.count_users() > 0:
            raise ApiError(409, "conflict", "已完成初始化，请直接登录")
        username = self._vstr(body, "username", "用户名", default=self.app.cfg.admin_user,
                              min_len=2, max_len=32, pattern=USERNAME_RE)
        password = self._vstr(body, "password", "密码", required=True, min_len=8, max_len=128)
        user_id = store.create_user(username, hash_password(password), "admin")
        _seed_pending_legacy(store, self.app.cfg, user_id)
        log(f"admin account created: {username}")
        return 201, self._login_session(store.get_user(user_id))

    def api_login(self, match, body, query, user):
        store = self.app.store
        if store.count_users() == 0:
            raise ApiError(409, "needs_setup", "尚未初始化，请先创建管理员")
        username = self._vstr(body, "username", "用户名", required=True, min_len=1, max_len=64)
        password = self._vstr(body, "password", "密码", required=True, min_len=1, max_len=128)
        limit_key = f"{self.client_address[0]}|{username.lower()}"
        retry_after = self.app.login_limiter.blocked(limit_key)
        if retry_after:
            raise ApiError(429, "rate_limited", f"尝试过于频繁，请 {retry_after} 秒后再试")
        row = store.get_user_by_name(username)
        if row is None or not verify_password(password, row["password_hash"]):
            self.app.login_limiter.fail(limit_key)
            raise ApiError(401, "invalid_credentials", "用户名或密码错误")
        if not row["is_active"]:
            self.app.login_limiter.fail(limit_key)
            raise ApiError(401, "invalid_credentials", "账号已停用")
        self.app.login_limiter.reset(limit_key)
        log(f"login: {username} from {self.client_address[0]}")
        return self._login_session(row)

    def api_logout(self, match, body, query, user):
        self._add_cookie("bb_session", "", 0, True)
        self._add_cookie("bb_csrf", "", 0, False)
        return {}

    def api_me(self, match, body, query, user):
        csrf = self._cookies().get("bb_csrf")
        if not csrf:
            csrf = secrets.token_urlsafe(24)
            self._add_cookie("bb_csrf", csrf, self.app.cfg.session_ttl, False)
        return {"user": user_public(user), "csrf": csrf, "version": __version__,
                "is_admin": user["role"] == "admin"}

    def api_password(self, match, body, query, user):
        old_password = self._vstr(body, "old_password", "当前密码", required=True,
                                  min_len=1, max_len=128)
        new_password = self._vstr(body, "new_password", "新密码", required=True,
                                  min_len=8, max_len=128)
        if not verify_password(old_password, user["password_hash"]):
            raise ApiError(400, "invalid_request", "当前密码不正确")
        self.app.store.update_user(user["id"], {"password_hash": hash_password(new_password)})
        row = self.app.store.get_user(user["id"])
        self._add_cookie("bb_session", self.app.issue_session(row), self.app.cfg.session_ttl, True)
        return {}

    # ------ API: settings ------------------------------------------------- #
    def api_settings_get(self, match, body, query, user):
        return {"version": __version__, "defaults": self.app.store.defaults(self.app.cfg),
                "limits": {"max_body": self.app.cfg.max_body},
                "features": {"multi_device": True, "users": True, "activity": True}}

    def api_settings_put(self, match, body, query, user):
        current = self.app.store.defaults(self.app.cfg)
        if "default_ntfy_url" in body:
            current["ntfy_url"] = self._vurl(body, "default_ntfy_url", "默认 ntfy 地址")
            self.app.store.settings_set("default_ntfy_url", current["ntfy_url"])
        if "default_bark_server" in body:
            current["bark_server"] = self._vurl(body, "default_bark_server", "默认 Bark 服务器")
            self.app.store.settings_set("default_bark_server", current["bark_server"])
        if "default_since" in body:
            current["since"] = self._vsince(body, "default_since", "默认回溯窗口")
            self.app.store.settings_set("default_since", current["since"])
        return {"defaults": current}

    # ------ API: devices -------------------------------------------------- #
    def _device_fields(self, body: dict, existing: bool = False) -> dict:
        defaults = self.app.store.defaults(self.app.cfg)
        fields: dict = {}
        name = self._vstr(body, "name", "设备名称", required=not existing, min_len=1, max_len=64)
        if name is not None:
            fields["name"] = name
        bark_key = self._vstr(body, "bark_key", "Bark Key", required=not existing,
                              min_len=4, max_len=128, pattern=BARK_KEY_RE)
        if bark_key is not None:
            fields["bark_key"] = bark_key
        if "server" in body or not existing:
            server = self._vurl(body, "server", "Bark 服务器", default=defaults["bark_server"])
            fields["server"] = server or defaults["bark_server"]
        if "group" in body or not existing:
            group = self._vstr(body, "group", "通知分组", default="ntfy", max_len=64, allow_empty=True)
            fields["group"] = group if group is not None else "ntfy"
        if "level" in body or not existing:
            level = self._vlevel(body, "level", "通知级别", default="")
            fields["level"] = level if level is not None else ""
        if "icon" in body or not existing:
            icon = self._vstr(body, "icon", "图标 URL", default="", max_len=200,
                              pattern=URL_RE, allow_empty=True)
            fields["icon"] = icon if icon is not None else ""
        if "is_active" in body or not existing:
            fields["is_active"] = self._vbool(body, "is_active", "启用状态", default=True)
        return fields

    def api_devices_list(self, match, body, query, user):
        rows = self.app.store.list_devices(self._owner_filter(user))
        return {"devices": [device_public(row, self.app.engine) for row in rows]}

    def api_devices_create(self, match, body, query, user):
        owner_id = self._target_owner(body, user)
        device_id = self.app.store.create_device(owner_id, self._device_fields(body))
        self.app.engine.notify_change()
        row = self.app.store.get_device(device_id)
        return 201, {"device": device_public(row, self.app.engine)}

    def api_devices_update(self, match, body, query, user):
        device_id = int(match.group("id"))
        row = self.app.store.get_device(device_id)
        if row is None:
            raise ApiError(404, "not_found", "设备不存在")
        self._owned(row, user, "设备")
        fields = self._device_fields(body, existing=True)
        if fields:
            self.app.store.update_device(device_id, fields)
            self.app.engine.notify_change()
        return {"device": device_public(self.app.store.get_device(device_id), self.app.engine)}

    def api_devices_delete(self, match, body, query, user):
        device_id = int(match.group("id"))
        row = self.app.store.get_device(device_id)
        if row is None:
            raise ApiError(404, "not_found", "设备不存在")
        self._owned(row, user, "设备")
        self.app.store.delete_device(device_id)
        self.app.engine.notify_change()
        return {}

    def api_devices_test(self, match, body, query, user):
        device_id = int(match.group("id"))
        row = self.app.store.get_device(device_id)
        if row is None:
            raise ApiError(404, "not_found", "设备不存在")
        self._owned(row, user, "设备")
        title = self._vstr(body, "title", "标题", default="Bark Bridge 测试", max_len=100)
        note = self._vstr(body, "body", "正文", default=f"来自 {row['name']} 的测试通知", max_len=500)
        sent, failed = self.app.engine.push_now([row], title, note, 3, None, "test", None, "手动测试")
        if not sent:
            raise ApiError(502, "push_failed", "推送失败，请检查 Bark Key 与服务器地址")
        return {"sent": True, "detail": f"已推送到 {row['name']}", "failed": failed}

    # ------ API: subscriptions -------------------------------------------- #
    def _subscription_public(self, row, mapping: dict[int, list[int]], states: dict) -> dict:
        return subscription_public(row, mapping.get(row["id"], []), states)

    def _sub_fields(self, body: dict, existing: bool = False) -> dict:
        defaults = self.app.store.defaults(self.app.cfg)
        fields: dict = {}
        name = self._vstr(body, "name", "订阅名称", required=not existing, min_len=1, max_len=64)
        if name is not None:
            fields["name"] = name
        if "ntfy_url" in body or not existing:
            url = self._vurl(body, "ntfy_url", "ntfy 地址", default=defaults["ntfy_url"])
            fields["ntfy_url"] = url or defaults["ntfy_url"]
        topic = self._vstr(body, "topic", "topic", required=not existing,
                           min_len=1, max_len=64, pattern=TOPIC_RE)
        if topic is not None:
            fields["topic"] = topic
        if "token" in body and body["token"] is not None:
            token = body["token"]
            if not isinstance(token, str):
                raise ApiError(400, "invalid_request", "ntfy Token 必须是字符串")
            token = token.strip()
            if len(token) > 256:
                raise ApiError(400, "invalid_request", "ntfy Token 过长")
            fields["token"] = token
        elif not existing:
            fields["token"] = ""
        if "since" in body or not existing:
            fields["since"] = self._vsince(body, "since", "回溯窗口",
                                           default=defaults["since"]) or defaults["since"]
        if "priority_min" in body or not existing:
            fields["priority_min"] = self._vint(body, "priority_min", "最低优先级",
                                                default=0, lo=0, hi=5)
        if "keyword" in body or not existing:
            fields["keyword"] = self._vstr(body, "keyword", "关键词", default="",
                                           max_len=256, allow_empty=True)
        if "exclude_keyword" in body or not existing:
            fields["exclude_keyword"] = self._vstr(body, "exclude_keyword", "排除词", default="",
                                                   max_len=256, allow_empty=True)
        if "title_prefix" in body or not existing:
            fields["title_prefix"] = self._vstr(body, "title_prefix", "标题前缀", default="",
                                                max_len=32, allow_empty=True)
        if "is_active" in body or not existing:
            fields["is_active"] = self._vbool(body, "is_active", "启用状态", default=True)
        return fields

    def api_subs_list(self, match, body, query, user):
        rows = self.app.store.list_subscriptions(self._owner_filter(user))
        mapping = self.app.store.subscription_device_map()
        states = self.app.engine.states()
        return {"subscriptions": [self._subscription_public(row, mapping, states) for row in rows]}

    def api_subs_create(self, match, body, query, user):
        owner_id = self._target_owner(body, user)
        fields = self._sub_fields(body)
        device_ids = self._vdevice_ids(body, owner_id, user) or []
        sub_id = self.app.store.create_subscription(owner_id, fields)
        if device_ids:
            self.app.store.set_subscription_devices(sub_id, device_ids)
        self.app.engine.notify_change()
        row = self.app.store.get_subscription(sub_id)
        return 201, {"subscription": self._subscription_public(
            row, {sub_id: device_ids}, self.app.engine.states())}

    def api_subs_update(self, match, body, query, user):
        sub_id = int(match.group("id"))
        row = self.app.store.get_subscription(sub_id)
        if row is None:
            raise ApiError(404, "not_found", "订阅不存在")
        self._owned(row, user, "订阅")
        fields = self._sub_fields(body, existing=True)
        if fields:
            self.app.store.update_subscription(sub_id, fields)
        device_ids = self._vdevice_ids(body, int(row["user_id"]), user)
        if device_ids is not None:
            self.app.store.set_subscription_devices(sub_id, device_ids)
        self.app.engine.notify_change()
        row = self.app.store.get_subscription(sub_id)
        mapping = {sub_id: self.app.store.subscription_device_ids(sub_id)}
        return {"subscription": self._subscription_public(
            row, mapping, self.app.engine.states())}

    def api_subs_delete(self, match, body, query, user):
        sub_id = int(match.group("id"))
        row = self.app.store.get_subscription(sub_id)
        if row is None:
            raise ApiError(404, "not_found", "订阅不存在")
        self._owned(row, user, "订阅")
        self.app.store.delete_subscription(sub_id)
        self.app.engine.notify_change()
        return {}

    def api_subs_test(self, match, body, query, user):
        sub_id = int(match.group("id"))
        row = self.app.store.get_subscription(sub_id)
        if row is None:
            raise ApiError(404, "not_found", "订阅不存在")
        self._owned(row, user, "订阅")
        targets = self.app.store.subscription_targets(sub_id)
        if not targets:
            raise ApiError(400, "invalid_request", "该订阅还没有关联任何可用设备")
        title = self._vstr(body, "title", "标题", default=f"订阅测试：{row['name']}", max_len=100)
        note = self._vstr(body, "body", "正文",
                          default=f"topic {row['topic']} 的测试消息", max_len=500)
        sent, failed = self.app.engine.push_now(targets, title, note, 4, None,
                                                "test", sub_id, row["name"])
        return {"sent": sent, "failed": failed}

    # ------ API: users (admin) -------------------------------------------- #
    def api_users_list(self, match, body, query, user):
        users = []
        for row in self.app.store.list_users():
            item = user_public(row)
            item["device_count"] = row["device_count"]
            item["subscription_count"] = row["subscription_count"]
            users.append(item)
        return {"users": users}

    def api_users_create(self, match, body, query, user):
        username = self._vstr(body, "username", "用户名", required=True, min_len=2,
                              max_len=32, pattern=USERNAME_RE)
        password = self._vstr(body, "password", "密码", required=True, min_len=8, max_len=128)
        role = self._vstr(body, "role", "角色", default="user")
        if role not in ("admin", "user"):
            raise ApiError(400, "invalid_request", "角色只能是 admin 或 user")
        is_active = self._vbool(body, "is_active", "启用状态", default=True)
        if self.app.store.get_user_by_name(username) is not None:
            raise ApiError(409, "conflict", "用户名已存在")
        user_id = self.app.store.create_user(username, hash_password(password), role, is_active)
        return 201, {"user": user_public(self.app.store.get_user(user_id))}

    def _admin_remains(self, target_row, new_role: str, new_active: bool) -> None:
        if not (target_row["role"] == "admin" and target_row["is_active"]):
            return
        if new_role == "admin" and new_active:
            return
        others = [row for row in self.app.store.list_users()
                  if row["id"] != target_row["id"] and row["role"] == "admin" and row["is_active"]]
        if not others:
            raise ApiError(400, "last_admin", "必须保留至少一个启用的管理员")

    def api_users_update(self, match, body, query, user):
        target_id = int(match.group("id"))
        row = self.app.store.get_user(target_id)
        if row is None:
            raise ApiError(404, "not_found", "用户不存在")
        fields: dict = {}
        if "username" in body:
            username = self._vstr(body, "username", "用户名", required=True, min_len=2,
                                  max_len=32, pattern=USERNAME_RE)
            existing = self.app.store.get_user_by_name(username)
            if existing is not None and existing["id"] != target_id:
                raise ApiError(409, "conflict", "用户名已存在")
            fields["username"] = username
        if "password" in body:
            fields["password_hash"] = hash_password(
                self._vstr(body, "password", "密码", required=True, min_len=8, max_len=128))
        role = self._vstr(body, "role", "角色", default=None)
        if role is not None and role not in ("admin", "user"):
            raise ApiError(400, "invalid_request", "角色只能是 admin 或 user")
        is_active = self._vbool(body, "is_active", "启用状态")
        new_role = role if role is not None else row["role"]
        new_active = is_active if is_active is not None else bool(row["is_active"])
        if target_id == int(user["id"]) and not new_active:
            raise ApiError(400, "invalid_request", "不能停用自己的账号")
        self._admin_remains(row, new_role, new_active)
        if role is not None:
            fields["role"] = role
        if is_active is not None:
            fields["is_active"] = 1 if is_active else 0
        if fields:
            self.app.store.update_user(target_id, fields)
        return {"user": user_public(self.app.store.get_user(target_id))}

    def api_users_delete(self, match, body, query, user):
        target_id = int(match.group("id"))
        row = self.app.store.get_user(target_id)
        if row is None:
            raise ApiError(404, "not_found", "用户不存在")
        if target_id == int(user["id"]):
            raise ApiError(400, "invalid_request", "不能删除自己的账号")
        if row["role"] == "admin" and row["is_active"]:
            others = [other for other in self.app.store.list_users()
                      if other["id"] != target_id and other["role"] == "admin" and other["is_active"]]
            if not others:
                raise ApiError(400, "last_admin", "必须保留至少一个启用的管理员")
        self.app.store.delete_user(target_id)
        self.app.engine.notify_change()
        return {}

    # ------ API: status / activity ---------------------------------------- #
    def api_status(self, match, body, query, user):
        store = self.app.store
        rows = store.list_subscriptions(self._owner_filter(user))
        states = self.app.engine.states()
        subscriptions = []
        for row in rows:
            state = states.get(row["id"])
            subscriptions.append({
                "id": row["id"], "name": row["name"], "topic": row["topic"],
                "is_active": bool(row["is_active"]),
                "state": state["state"] if state else ("connecting" if row["is_active"] else "stopped"),
                "last_error": state["last_error"] if state else "",
                "forwarded": row["forwarded"], "last_message_at": row["cursor_ts"],
            })
        devices = store.list_devices(self._owner_filter(user))
        return {
            "version": __version__,
            "uptime": int(time.time() - self.app.started_at),
            "subscriptions": subscriptions,
            "totals": {
                "subscriptions_active": sum(1 for row in rows if row["is_active"]),
                "devices_active": sum(1 for row in devices if row["is_active"]),
                "forwarded_since_start": self.app.engine.forwarded_total(),
            },
        }

    def api_activity(self, match, body, query, user):
        try:
            after = int((query.get("since") or ["0"])[0])
        except ValueError:
            after = 0
        try:
            limit = max(1, min(200, int((query.get("limit") or ["100"])[0])))
        except ValueError:
            limit = 100
        items, last_id = self.app.engine.activity.since(after, limit)
        if user["role"] != "admin":
            allowed_subs = {row["id"] for row in self.app.store.list_subscriptions(int(user["id"]))}
            allowed_devices = {row["id"] for row in self.app.store.list_devices(int(user["id"]))}
            items = [item for item in items
                     if item.get("subscription_id") in allowed_subs
                     or item.get("device_id") in allowed_devices]
        return {"items": items, "last_id": last_id}

    # ------ API: online update (admin) ------------------------------------ #
    def api_update_status(self, match, body, query, user):
        return self.app.updater.status()

    def api_update_check(self, match, body, query, user):
        return self.app.updater.refresh(force=True)

    def api_update_apply(self, match, body, query, user):
        result = self.app.updater.apply()
        self.app.schedule_restart()
        return 202, result


# --------------------------------------------------------------------------- #
# bootstrap helpers
# --------------------------------------------------------------------------- #

def _seed_pending_legacy(store: Store, cfg: Config, user_id: int) -> None:
    """Migrate legacy single-device env config once, after an admin exists."""
    if not store.settings_get("legacy_pending"):
        return
    legacy = cfg.legacy
    if not legacy.get("BARK_KEY") or not legacy.get("NTFY_TOPIC"):
        store.settings_set("legacy_pending", "0")
        return
    try:
        device_id = store.create_device(user_id, {
            "name": "默认设备", "bark_key": legacy["BARK_KEY"],
            "server": cfg.default_bark_server,
            "group": legacy.get("BARK_GROUP") or "ntfy",
            "level": legacy.get("BARK_LEVEL") or "",
            "icon": legacy.get("BARK_ICON") or "",
            "is_active": True,
        })
        sub_id = store.create_subscription(user_id, {
            "name": legacy["NTFY_TOPIC"], "ntfy_url": cfg.default_ntfy_url,
            "topic": legacy["NTFY_TOPIC"], "token": legacy.get("NTFY_TOKEN") or "",
            "since": legacy.get("NTFY_SINCE") or cfg.default_since,
            "priority_min": 0, "keyword": "", "exclude_keyword": "",
            "title_prefix": "", "is_active": True,
        })
        store.set_subscription_devices(sub_id, [device_id])
        store.settings_set("legacy_pending", "0")
        store.settings_set("legacy_seeded", "1")
        log(f"migrated legacy env config into device #{device_id} and subscription #{sub_id}")
    except Exception as exc:  # noqa: BLE001 - never block startup on migration
        log(f"legacy env migration failed: {_short(exc)}")


def bootstrap(store: Store, cfg: Config) -> None:
    if store.count_users() > 0:
        return
    legacy = cfg.legacy
    has_legacy = bool(legacy.get("BARK_KEY") and legacy.get("NTFY_TOPIC"))
    if has_legacy:
        store.settings_set("legacy_pending", "1")
    if cfg.admin_password:
        user_id = store.create_user(cfg.admin_user, hash_password(cfg.admin_password), "admin")
        log(f"bootstrap admin '{cfg.admin_user}' created from ADMIN_PASSWORD")
        _seed_pending_legacy(store, cfg, user_id)
    elif has_legacy:
        log("no users yet: open the WebUI to create the admin; legacy env config will be migrated")
    else:
        log("no users yet: open the WebUI to create the first admin account")


# --------------------------------------------------------------------------- #
# main
# --------------------------------------------------------------------------- #

def main() -> int:
    cfg = Config()
    store = Store(cfg.db_file)
    store.init()
    bootstrap(store, cfg)
    engine = Engine(store, cfg)
    app = App(store, cfg, engine)
    Handler.app = app

    try:
        server = ThreadingHTTPServer((cfg.host, cfg.port), Handler)
    except OSError as exc:
        log(f"cannot listen on {cfg.host}:{cfg.port}: {_short(exc)}")
        return 1
    server.daemon_threads = True
    actual_host, actual_port = server.server_address[0], server.server_address[1]

    def shutdown(_signum, _frame):
        threading.Thread(target=server.shutdown, daemon=True).start()

    for signame in ("SIGINT", "SIGTERM"):
        candidate = getattr(signal, signame, None)
        if candidate is not None:
            try:
                signal.signal(candidate, shutdown)
            except (ValueError, OSError):
                pass

    def request_restart():
        # Give the HTTP handler time to flush the 202 response, then leave
        # serve_forever(); main() performs a clean shutdown and re-execs.
        time.sleep(0.5)
        try:
            server.shutdown()
        except Exception:  # noqa: BLE001 - shutdown is best effort here
            pass

    app.request_restart = request_restart
    engine.start()
    app.start_background()
    log(f"bark-bridge {__version__} listening on http://{actual_host}:{actual_port}/")
    log(f"data directory: {cfg.data_dir}")
    if store.count_users() == 0:
        log("first run: open the WebUI to create the administrator account")
    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        pass
    finally:
        restarting = app.restart_requested
        app._background_stop.set()
        log("restarting to apply update" if restarting else "shutting down")
        engine.stop()
        server.server_close()
        store.close()
    if restarting:
        try:
            os.execv(sys.executable, [sys.executable, cfg.app_file] + sys.argv[1:])
        except OSError as exc:  # noqa: BLE001 - fall back to a plain exit
            log(f"restart failed: {_short(exc)}")
            return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
