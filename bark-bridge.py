#!/usr/bin/env python3
"""ntfy -> Bark bridge.

Subscribe to an ntfy topic (JSON stream) and forward new messages to Bark,
so iOS devices get reliable APNs notifications even though the official
ntfy iOS app cannot keep a background connection.

Zero third-party dependencies: Python 3.11+ standard library only.

Environment variables (see .env.example):
    NTFY_BASE_URL   ntfy server base URL        (default http://127.0.0.1:40265)
    NTFY_TOPIC      topic to watch              (default alerts)
    NTFY_TOKEN      ntfy access token           (required if auth enabled)
    NTFY_SINCE      initial since value         (default 1m, e.g. all/10m/30s)
    BARK_KEY        Bark device key             (required)
    BARK_SERVER     Bark server base URL        (default https://api.day.app)
    BARK_GROUP      Bark notification group     (default ntfy)
    BARK_LEVEL      force Bark level, empty = map from priority
    BARK_ICON       optional Bark icon URL
    MAX_BODY        body truncate length        (default 2000)
    RECONNECT_DELAY reconnect delay in seconds  (default 5)
"""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.request

NTFY_BASE_URL = os.environ.get("NTFY_BASE_URL", "http://127.0.0.1:40265").rstrip("/")
NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "alerts")
NTFY_TOKEN = os.environ.get("NTFY_TOKEN", "")
NTFY_SINCE = os.environ.get("NTFY_SINCE", "1m")

BARK_KEY = os.environ.get("BARK_KEY", "")
BARK_SERVER = os.environ.get("BARK_SERVER", "https://api.day.app").rstrip("/")
BARK_GROUP = os.environ.get("BARK_GROUP", "ntfy")
BARK_LEVEL = os.environ.get("BARK_LEVEL", "")
BARK_ICON = os.environ.get("BARK_ICON", "")

MAX_BODY = int(os.environ.get("MAX_BODY", "2000"))
RECONNECT_DELAY = int(os.environ.get("RECONNECT_DELAY", "5"))

_last_ts = 0
_started_at = time.time()


def log(msg: str) -> None:
    print(time.strftime("%Y-%m-%d %H:%M:%S"), msg, file=sys.stderr, flush=True)


def map_level(priority) -> str:
    """Map ntfy priority to Bark level."""
    if BARK_LEVEL:
        return BARK_LEVEL
    try:
        value = int(priority)
    except (TypeError, ValueError):
        value = 3
    return "timeSensitive" if value >= 4 else "active"


def bark_push(title: str, body: str, level: str, url: str | None = None) -> None:
    payload = {
        "device_key": BARK_KEY,
        "title": (title or "ntfy")[:100],
        "body": (body or "")[:MAX_BODY],
        "level": level,
        "group": BARK_GROUP,
    }
    if url:
        payload["url"] = url
    if BARK_ICON:
        payload["icon"] = BARK_ICON

    request = urllib.request.Request(
        BARK_SERVER + "/push",
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json; charset=utf-8"},
    )
    with urllib.request.urlopen(request, timeout=10) as response:
        response.read()


def stream_url() -> str:
    """Build the ntfy JSON stream URL, resuming from the last seen timestamp."""
    since = str(_last_ts) if _last_ts else NTFY_SINCE
    return f"{NTFY_BASE_URL}/{NTFY_TOPIC}/json?since={since}"


def connect_and_forward() -> None:
    """Open the ntfy stream and forward messages until it closes."""
    global _last_ts

    request = urllib.request.Request(stream_url())
    if NTFY_TOKEN:
        request.add_header("Authorization", f"Bearer {NTFY_TOKEN}")

    log(f"connecting to {stream_url()}")
    with urllib.request.urlopen(request, timeout=None) as response:
        for raw in response:
            if not raw.strip():
                continue
            try:
                message = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if message.get("event") != "message":
                continue

            try:
                timestamp = int(message.get("time", 0))
            except (TypeError, ValueError):
                timestamp = 0

            # First connection: ignore the replay window before the process started.
            if not _last_ts and timestamp and timestamp < _started_at - 5:
                continue
            # Duplicate protection on reconnect.
            if timestamp and timestamp <= _last_ts:
                continue

            title = message.get("title") or NTFY_TOPIC
            body = message.get("message") or ""
            level = map_level(message.get("priority", 3))
            try:
                bark_push(title, body, level, message.get("click"))
            except Exception as exc:  # noqa: BLE001
                log(f"bark push failed: {exc}")
            else:
                log(f"forwarded: {title!r}")
            if timestamp:
                _last_ts = timestamp


def main() -> None:
    if not BARK_KEY:
        log("BARK_KEY is not set, exiting")
        sys.exit(1)
    log(f"watching {NTFY_BASE_URL}/{NTFY_TOPIC} -> {BARK_SERVER}")

    while True:
        try:
            connect_and_forward()
            log("ntfy stream closed, reconnecting")
        except KeyboardInterrupt:
            log("stopped")
            return
        except Exception as exc:  # noqa: BLE001
            log(f"ntfy stream error: {exc}")
        time.sleep(RECONNECT_DELAY)


if __name__ == "__main__":
    main()
