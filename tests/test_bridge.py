#!/usr/bin/env python3
"""bark-bridge v2 端到端测试（纯标准库 unittest）。

覆盖 docs/API.md 契约：
  * 首次 setup / 登录 / CSRF / 401 / 失败限流 / 改密使旧会话失效
  * 用户权限隔离（普通用户只能读写自己的设备与订阅）
  * 设备 CRUD（bark_key 省略不变、测试推送）
  * 订阅 CRUD（token 省略不变 / "" 清除、device_ids 归属校验）
  * 多设备 fanout、优先级映射、priority_min / keyword / exclude_keyword 过滤
  * API 热更新（运行中新增订阅 / 新增目标设备后自动生效）
  * 断线重连与时间戳去重
  * /api/status 与 /api/activity 增量
  * 静态页、404 / 错误格式、安全响应头
  * 在线更新：假 GitHub 源码包 + commits API、检查/强制刷新/权限、
    更新关闭、下载/校验失败、apply 后原地重启并保留数据库与会话
  * JSON 正文展开（JSON_BODY_PARSE）、附件/封面图片映射（SEND_IMAGE）

测试自包含，不访问外网：在测试进程内起假 ntfy（分块 JSON 流）与假 Bark
（记录 POST /push 载荷）服务器，用 sys.executable 子进程启动 bark-bridge.py。

运行（PATH 里没有 python，用 DSH 自带解释器）：
  C:\\Users\\16094\\.dsh\\dsh-runtimes\\dsh-primary-runtime\\dependencies\\python\\python.exe \
      -m unittest discover -s tests -v
"""

from __future__ import annotations

import atexit
import hashlib
import http.cookiejar
import http.server
import io
import json
import os
import queue
import re
import shutil
import signal
import socket
import subprocess
import sys
import tarfile
import threading
import time
import unittest
import urllib.error
import urllib.parse
import urllib.request
import uuid

# ---------------------------------------------------------------- 常量与基础工具

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
APP_PATH = os.path.join(ROOT, "bark-bridge.py")
WEBUI_PATH = os.path.join(ROOT, "webui.html")

# 临时数据目录默认放在 tests/.tmp 下（已被 .gitignore 覆盖）。某些受限沙箱里
# tests/ 目录对子进程只读，此时自动回退到工作区根目录下的 .tmp-tests/（同样被
# .gitignore 的 `.tmp*/` 覆盖），保证测试仍可运行；也可用环境变量
# BB_TEST_TMP_ROOT 显式指定。
def _resolve_tmp_root() -> str:
    preferred = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".tmp")
    fallback = os.path.join(ROOT, ".tmp-tests")
    candidates = [preferred]
    if os.path.abspath(fallback) != os.path.abspath(preferred):
        candidates.append(fallback)
    for candidate in candidates:
        try:
            os.makedirs(candidate, exist_ok=True)
            probe = os.path.join(candidate, ".write-probe")
            with open(probe, "w", encoding="utf-8") as handle:
                handle.write("ok")
            os.remove(probe)
            return candidate
        except OSError:
            continue
    return preferred


TMP_ROOT = os.path.abspath(os.environ.get("BB_TEST_TMP_ROOT") or _resolve_tmp_root())
os.makedirs(TMP_ROOT, exist_ok=True)

ADMIN_PASSWORD = "AdminPass123!"
DEFAULT_TIMEOUT = 12.0
POLL_INTERVAL = 0.1
API_VERSION = "2.0.0"

_DEFAULT_PASSWORD = object()


def remove_tree(path, attempts=6, delay=0.15) -> bool:
    """删除目录并在 Windows 文件句柄延迟释放时重试；返回是否已删除。"""
    for _ in range(attempts):
        shutil.rmtree(path, ignore_errors=True)
        if not os.path.exists(path):
            return True
        time.sleep(delay)
    return not os.path.exists(path)


def cleanup_tmp_root() -> None:
    """清理 TMP_ROOT：逐个删除本次运行创建的目录，最后尝试删空根目录。

    对历史遗留、当前沙箱身份无权删除的目录静默跳过（不阻塞测试进程退出）。
    """
    try:
        entries = os.listdir(TMP_ROOT)
    except OSError:
        return
    for name in entries:
        path = os.path.join(TMP_ROOT, name)
        remove_tree(path)
        if os.path.isdir(path):
            try:
                os.rmdir(path)
            except OSError:
                pass
    try:
        os.rmdir(TMP_ROOT)
    except OSError:
        pass


atexit.register(cleanup_tmp_root)


def free_port() -> int:
    """绑定 0 端口取一个空闲端口，尽量避免与其它进程冲突。"""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def wait_for(predicate, timeout=DEFAULT_TIMEOUT, interval=POLL_INTERVAL, message="条件"):
    """轮询等待 predicate() 返回真值；超时抛出带诊断信息的 AssertionError。"""
    deadline = time.monotonic() + timeout
    last_error = None
    while time.monotonic() < deadline:
        try:
            result = predicate()
        except Exception as exc:  # noqa: BLE001 - 轮询期间的临时异常不应直接失败
            last_error = exc
            result = None
        if result:
            return result
        time.sleep(interval)
    suffix = f"（最后一次异常：{last_error!r}）" if last_error is not None else ""
    raise AssertionError(f"等待超时（{timeout:g}s）：{message}{suffix}")


def records_matching(records, predicate):
    return [record for record in records if predicate(record["payload"])]


def make_temp_dir(prefix="bb-test-"):
    """在 TMP_ROOT 下创建独立临时目录。

    不用 ``tempfile.mkdtemp``：它用 mode 0700 建目录，在部分 Windows
    受限沙箱（如 DSH workspace-write）下会导致该目录内无法再写文件。
    """
    path = os.path.join(TMP_ROOT, prefix + uuid.uuid4().hex[:12])
    os.makedirs(path, exist_ok=True)
    return path


def make_app_dirs(root):
    """由测试进程预创建 DATA_DIR 子目录并返回其路径。

    应用子进程只会往里写 SQLite 文件；这些文件继承测试进程创建目录的 ACL，
    因此测试结束后 rmtree 可以正常删除（若让子进程自己建目录，在受限沙箱下
    目录可能属于另一个不可解析的 SID，导致清理失败）。
    """
    data_dir = os.path.join(root, "data")
    os.makedirs(data_dir, exist_ok=True)
    return data_dir


# ---------------------------------------------------------------- 假 ntfy 服务器


class _QuietThreadingHTTPServer(http.server.ThreadingHTTPServer):
    """ThreadingHTTPServer 子类：客户端异常断开时不打印无意义 traceback。

    被测应用在重连/退出时会直接关闭到假服务器的长连接，socketserver 默认会把
    ConnectionResetError 打到测试进程 stderr，干扰测试输出。
    """

    daemon_threads = True

    def handle_error(self, request, client_address):
        error = sys.exc_info()[1]
        if isinstance(error, (ConnectionResetError, ConnectionAbortedError, BrokenPipeError)):
            return
        super().handle_error(request, client_address)


class _NtfyConnection:
    """一条已建立的 ntfy JSON 流连接。"""

    __slots__ = ("topic", "since", "stop", "queue")

    def __init__(self, topic: str, since: str):
        self.topic = topic
        self.since = since
        self.stop = threading.Event()
        self.queue: "queue.Queue[dict]" = queue.Queue()


class _NtfyHandler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "FakeNtfy/1.0"

    def log_message(self, fmt, *args):  # noqa: A003 - 基类签名
        pass

    def do_GET(self):  # noqa: N802 - 基类签名
        fake = self.server.fake  # type: ignore[attr-defined]
        self.close_connection = True
        parsed = urllib.parse.urlparse(self.path)
        parts = [part for part in parsed.path.split("/") if part]
        if len(parts) < 2 or parts[-1] != "json":
            body = b"[]"
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        topic = parts[-2]
        query = urllib.parse.parse_qs(parsed.query)
        since = (query.get("since") or [""])[0]
        conn, backlog = fake.register(topic, since, self.headers.get("Authorization", ""))

        try:
            self.send_response(200)
            self.send_header("Content-Type", "application/x-ndjson; charset=utf-8")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            for message in backlog:
                self._write_chunk((json.dumps(message, ensure_ascii=False) + "\n").encode("utf-8"))
            last_write = time.monotonic()
            while not conn.stop.is_set():
                try:
                    message = conn.queue.get(timeout=0.1)
                except queue.Empty:
                    # 真实 ntfy 会周期性发送 keepalive；这里也发，既贴近真实行为，
                    # 又能让服务端及时发现客户端已断开（否则读线程会一直阻塞）。
                    if time.monotonic() - last_write >= 0.5:
                        keepalive = {
                            "id": uuid.uuid4().hex[:16],
                            "time": int(time.time()),
                            "event": "keepalive",
                            "topic": topic,
                        }
                        self._write_chunk(
                            (json.dumps(keepalive, ensure_ascii=False) + "\n").encode("utf-8")
                        )
                        last_write = time.monotonic()
                    continue
                self._write_chunk((json.dumps(message, ensure_ascii=False) + "\n").encode("utf-8"))
                last_write = time.monotonic()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            fake.unregister(conn)
            try:
                self.wfile.write(b"0\r\n\r\n")
                self.wfile.flush()
            except OSError:
                pass

    def _write_chunk(self, data: bytes) -> None:
        self.wfile.write(("%X\r\n" % len(data)).encode("ascii") + data + b"\r\n")
        self.wfile.flush()


class FakeNtfy:
    """极简 ntfy JSON 流服务器。

    * ``GET /<topic>/json?since=<...>`` 分块流式返回 JSON 行；
    * ``publish()`` 向所有已连接的同 topic 客户端推送消息（同时记录历史）；
    * ``drop_connections()`` 主动断开所有连接，用于测试重连；
    * ``replay_all = True`` 让重连时无视 since 重放全部历史，用于测试引擎去重。
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._messages: "dict[str, list[dict]]" = {}
        self._connections: "list[_NtfyConnection]" = []
        self._requests: "list[dict]" = []
        self._last_time = int(time.time())
        self.replay_all = False
        self._closed = False
        self._server = _QuietThreadingHTTPServer(("127.0.0.1", 0), _NtfyHandler)
        self._server.fake = self  # type: ignore[attr-defined]
        self.port = int(self._server.server_address[1])
        self.url = f"http://127.0.0.1:{self.port}"
        self._thread = threading.Thread(
            target=self._server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True
        )
        self._thread.start()

    # -- 连接管理 ---------------------------------------------------------
    def register(self, topic: str, since: str, authorization: str):
        conn = _NtfyConnection(topic, since)
        with self._lock:
            backlog = list(self._messages.get(topic, []))
            if not self.replay_all:
                backlog = self._filter_since(backlog, since)
            self._connections.append(conn)
            self._requests.append({"topic": topic, "since": since, "authorization": authorization})
        return conn, backlog

    def unregister(self, conn: _NtfyConnection) -> None:
        with self._lock:
            if conn in self._connections:
                self._connections.remove(conn)

    @staticmethod
    def _filter_since(messages, since: str):
        try:
            cursor = int(str(since).strip())
        except (TypeError, ValueError):
            # 首次连接使用 since=1m/all 等语义；测试在订阅创建后才发布消息，
            # 所以这里不回放历史即可。
            return []
        return [message for message in messages if int(message.get("time", 0)) > cursor]

    def active_connections(self, topic=None) -> int:
        with self._lock:
            connections = [conn for conn in self._connections if not conn.stop.is_set()]
        if topic is None:
            return len(connections)
        return len([conn for conn in connections if conn.topic == topic])

    def requests_for(self, topic=None) -> "list[dict]":
        with self._lock:
            requests = list(self._requests)
        if topic is None:
            return requests
        return [item for item in requests if item["topic"] == topic]

    def drop_connections(self) -> None:
        with self._lock:
            connections = list(self._connections)
        for conn in connections:
            conn.stop.set()

    # -- 消息发布 ---------------------------------------------------------
    def publish(self, topic, *, title=None, message=None, priority=3, event="message", time_=None, **extra):
        with self._lock:
            if time_ is None:
                time_ = max(self._last_time + 1, int(time.time()))
                self._last_time = int(time_)
            else:
                time_ = int(time_)
                self._last_time = max(self._last_time, time_)
            payload = {
                "id": uuid.uuid4().hex[:16],
                "time": time_,
                "event": event,
                "topic": topic,
            }
            if title is not None:
                payload["title"] = title
            if message is not None:
                payload["message"] = message
            if priority is not None:
                payload["priority"] = priority
            payload.update(extra)
            self._messages.setdefault(topic, []).append(payload)
            connections = [
                conn
                for conn in self._connections
                if conn.topic == topic and not conn.stop.is_set()
            ]
        for conn in connections:
            conn.queue.put(payload)
        return payload

    def history(self, topic) -> "list[dict]":
        with self._lock:
            return list(self._messages.get(topic, []))

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self.drop_connections()
        try:
            self._server.shutdown()
        except Exception:  # noqa: BLE001
            pass
        self._server.server_close()


# ---------------------------------------------------------------- 假 Bark 服务器


class _BarkHandler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "FakeBark/1.0"

    def log_message(self, fmt, *args):  # noqa: A003
        pass

    def do_POST(self):  # noqa: N802
        fake = self.server.fake  # type: ignore[attr-defined]
        self.close_connection = True
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except (TypeError, ValueError):
            length = 0
        raw = self.rfile.read(length) if length else b""
        try:
            payload = json.loads(raw.decode("utf-8"))
            if not isinstance(payload, dict):
                raise ValueError("payload 不是 JSON 对象")
            parse_ok = True
        except (ValueError, UnicodeDecodeError):
            payload = {"_raw": raw.decode("utf-8", "replace")}
            parse_ok = False
        ok = parse_ok and payload.get("device_key") not in fake.fail_keys
        fake.record(payload, ok, self.path)
        status = 200 if ok else 500
        body = json.dumps(
            {"code": status, "message": "success" if ok else "mock failure"},
            ensure_ascii=False,
        ).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
        self.wfile.flush()


class FakeBark:
    """极简 Bark 服务器：记录 POST /push 的 JSON 载荷，可模拟指定 key 失败。"""

    def __init__(self):
        self._lock = threading.Lock()
        self._records: "list[dict]" = []
        self.fail_keys: "set[str]" = set()
        self._closed = False
        self._server = _QuietThreadingHTTPServer(("127.0.0.1", 0), _BarkHandler)
        self._server.fake = self  # type: ignore[attr-defined]
        self.port = int(self._server.server_address[1])
        self.url = f"http://127.0.0.1:{self.port}"
        self._thread = threading.Thread(
            target=self._server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True
        )
        self._thread.start()

    def record(self, payload: dict, ok: bool, path: str) -> None:
        with self._lock:
            self._records.append({"payload": payload, "ok": ok, "path": path})

    def records(self) -> "list[dict]":
        with self._lock:
            return list(self._records)

    def payloads(self, ok=None) -> "list[dict]":
        records = self.records()
        if ok is None:
            return [record["payload"] for record in records]
        return [record["payload"] for record in records if record["ok"] is ok]

    def clear(self) -> None:
        with self._lock:
            self._records.clear()

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self._server.shutdown()
        except Exception:  # noqa: BLE001
            pass
        self._server.server_close()


# ---------------------------------------------------------------- 假 GitHub


class _GithubHandler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "FakeGithub/1.0"

    def log_message(self, fmt, *args):  # noqa: A003
        pass

    def do_GET(self):  # noqa: N802
        fake = self.server.github  # type: ignore[attr-defined]
        self.close_connection = True
        parsed = urllib.parse.urlsplit(self.path)
        parts = [part for part in parsed.path.split("/") if part]
        mode = fake.mode

        # commits API：/repos/{repo}/commits/{branch}
        if parts[:1] == ["repos"] and "commits" in parts:
            index = parts.index("commits")
            repo = "/".join(parts[1:index])
            branch = "/".join(parts[index + 1:])
            fake.record_commit_request(parsed.path, repo, branch, mode)
            if mode == "404":
                return self._json(404, {"message": "Not Found"})
            return self._json(200, fake.commit_payload())

        # codeload 源码包：/{repo}/tar.gz/refs/heads/{branch}
        if "tar.gz" in parts:
            index = parts.index("tar.gz")
            repo = "/".join(parts[:index])
            tail = parts[index + 1:]
            fake.record_tar_request(parsed.path, repo, "/".join(tail), mode)
            if mode == "404":
                return self._json(404, {"message": "Not Found"})
            if mode == "garbage":
                return self._bytes(200, b"this is not a gzip archive", "application/gzip")
            return self._bytes(200, fake.tar_bytes(), "application/gzip")

        return self._json(404, {"message": "Not Found"})

    def _bytes(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except OSError:
            pass

    def _json(self, status: int, payload: dict) -> None:
        self._bytes(status, json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                    "application/json; charset=utf-8")


class FakeGithub:
    """假 GitHub：codeload 源码包 + commits API，用于在线更新测试。

    * ``GET /{repo}/tar.gz/refs/heads/{branch}`` → 内存生成的 gzip tarball
      （含 bark-bridge.py 与 webui.html，版本号被改成 ``remote_version``）
    * ``GET /repos/{repo}/commits/{branch}`` → ``{sha, commit:{message, author}}``

    ``mode`` 取值：
      * ``ok``            正常远端（版本号 9.9.9）
      * ``404``           两个端点都返回 404
      * ``garbage``       源码包返回非 gzip 垃圾字节（下载/解包失败）
      * ``broken_python`` 包内 bark-bridge.py 有语法错误（compile 校验失败）
      * ``broken_webui``  包内 webui.html 缺少 ``<script`` / ``/api/``（校验失败）
      * ``same``          包内容与本地当前文件一致（用于「已是最新」）
      * ``missing_webui`` 包内只有 v1 的 bark-bridge.py（无 webui.html、无 __version__）
      * ``poison``        合法可编译但启动即退出的新版本（用于重启后清理孤儿进程）
    """

    def __init__(self, repo="owner/repo", branch="main", remote_version="9.9.9"):
        self.repo = repo
        self.branch = branch
        self.remote_version = remote_version
        self.subject = "feat: 在线更新测试版本"
        self.commit_sha = "9f2c1ab" + "0" * 33
        self.committed_at = "2026-11-21T10:00:00Z"
        self.mode = "ok"

        with open(APP_PATH, "rb") as handle:
            self.local_python = handle.read()
        with open(WEBUI_PATH, "rb") as handle:
            self.local_webui = handle.read()
        match = re.search(rb'__version__ = "([^"]+)"', self.local_python)
        if match is None:
            raise RuntimeError("当前 bark-bridge.py 中找不到 __version__")
        self.current_version = match.group(1).decode("utf-8")
        self.remote_python = (
            self.local_python[: match.start(1)]
            + remote_version.encode("utf-8")
            + self.local_python[match.end(1):]
        )
        self.remote_webui = self.local_webui

        self._lock = threading.Lock()
        self.tar_requests: "list[dict]" = []
        self.commit_requests: "list[dict]" = []
        self._closed = False
        self._server = _QuietThreadingHTTPServer(("127.0.0.1", 0), _GithubHandler)
        self._server.github = self  # type: ignore[attr-defined]
        self.port = int(self._server.server_address[1])
        self.url = f"http://127.0.0.1:{self.port}"
        self._thread = threading.Thread(
            target=self._server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True
        )
        self._thread.start()

    # -- 响应内容 ---------------------------------------------------------
    def commit_payload(self) -> dict:
        return {
            "sha": self.commit_sha,
            "commit": {
                "message": self.subject + "\n\n测试用的提交说明",
                "author": {"name": "QA", "email": "qa@example.com", "date": self.committed_at},
            },
        }

    def _tar(self, files: dict) -> bytes:
        prefix = f"{self.repo.split('/')[-1]}-{self.branch}"
        buffer = io.BytesIO()
        with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
            for name, data in files.items():
                info = tarfile.TarInfo(f"{prefix}/{name}")
                info.size = len(data)
                info.mtime = int(time.time())
                info.mode = 0o644
                archive.addfile(info, io.BytesIO(data))
        return buffer.getvalue()

    def tar_bytes(self) -> bytes:
        with self._lock:
            mode = self.mode
        if mode == "same":
            files = {"bark-bridge.py": self.local_python, "webui.html": self.local_webui}
        elif mode == "broken_python":
            files = {
                "bark-bridge.py": b"def broken(:\n" + self.remote_python,
                "webui.html": self.remote_webui,
            }
        elif mode == "broken_webui":
            files = {
                "bark-bridge.py": self.remote_python,
                "webui.html": b"<html><body>no script and no api here</body></html>",
            }
        elif mode == "missing_webui":
            # 模拟真实仓库还是 v1：只有 bark-bridge.py，没有 webui.html 与 __version__。
            files = {
                "bark-bridge.py": (
                    b"#!/usr/bin/env python3\n"
                    b"# legacy v1 script without __version__ marker\n"
                    b"print('legacy v1')\n"
                ),
            }
        elif mode == "poison":
            # 版本号更高、compile() 可通过，但启动即退出：用于让更新重启后的
            # 新进程自行结束（Windows 沙箱不允许测试进程按 PID 终止进程）。
            files = {
                "bark-bridge.py": b'__version__ = "9.9.10"\nraise SystemExit(0)\n',
                "webui.html": self.remote_webui,
            }
        else:
            files = {"bark-bridge.py": self.remote_python, "webui.html": self.remote_webui}
        return self._tar(files)

    # -- 请求记录 ---------------------------------------------------------
    def record_tar_request(self, path, repo, tail, mode) -> None:
        with self._lock:
            self.tar_requests.append({"path": path, "repo": repo, "tail": tail, "mode": mode})

    def record_commit_request(self, path, repo, branch, mode) -> None:
        with self._lock:
            self.commit_requests.append(
                {"path": path, "repo": repo, "branch": branch, "mode": mode}
            )

    def requests(self) -> "tuple[list[dict], list[dict]]":
        with self._lock:
            return list(self.tar_requests), list(self.commit_requests)

    def remote_hash(self) -> str:
        return self.normalized_hash(self.remote_python)

    def local_hash(self) -> str:
        return self.normalized_hash(self.local_python)

    @staticmethod
    def normalized_hash(data: bytes) -> str:
        # 后端对 CRLF 做了归一化，这里保持同样的判定口径。
        return hashlib.sha256(data.replace(b"\r\n", b"\n")).hexdigest()[:8]

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self._server.shutdown()
        except Exception:  # noqa: BLE001
            pass
        self._server.server_close()


# ---------------------------------------------------------------- HTTP 客户端


class Response:
    def __init__(self, status: int, headers, body: bytes):
        self.status = status
        self.headers = headers
        self.body = body
        try:
            self.json = json.loads(body.decode("utf-8")) if body else None
        except (UnicodeDecodeError, json.JSONDecodeError):
            self.json = None

    @property
    def text(self) -> str:
        return self.body.decode("utf-8", "replace")

    @property
    def data(self):
        return (self.json or {}).get("data")

    @property
    def code(self):
        return (self.json or {}).get("code")

    def header(self, name, default=None):
        return self.headers.get(name, default)

    def header_all(self, name) -> "list[str]":
        return self.headers.get_all(name) or []


_UNSET = object()


class ApiClient:
    """带 CookieJar 的极简 API 客户端；写请求自动附带 X-CSRF-Token。"""

    def __init__(self, base_url: str):
        self.base = base_url.rstrip("/")
        self.jar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.jar))

    def cookie(self, name):
        for cookie in self.jar:
            if cookie.name == name:
                return cookie.value
        return None

    @property
    def csrf(self):
        return self.cookie("bb_csrf")

    def request(
        self,
        method,
        path,
        payload=_UNSET,
        raw_body=None,
        headers=None,
        csrf=True,
        timeout=15.0,
    ):
        data = None
        if payload is not _UNSET:
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        if raw_body is not None:
            data = raw_body
        request = urllib.request.Request(self.base + path, data=data, method=method.upper())
        request_headers = dict(headers or {})
        if data is not None and not any(key.lower() == "content-type" for key in request_headers):
            request_headers["Content-Type"] = "application/json; charset=utf-8"
        if csrf and method.upper() in ("POST", "PUT", "PATCH", "DELETE"):
            token = self.csrf
            if token and not any(key.lower() == "x-csrf-token" for key in request_headers):
                request_headers["X-CSRF-Token"] = token
        for key, value in request_headers.items():
            request.add_header(key, value)
        try:
            response = self.opener.open(request, timeout=timeout)
            body = response.read()
            status, response_headers = response.status, response.headers
        except urllib.error.HTTPError as exc:
            body = exc.read()
            status, response_headers = exc.code, exc.headers
        return Response(status, response_headers, body)

    def get(self, path, **kwargs):
        return self.request("GET", path, **kwargs)

    def post(self, path, payload=_UNSET, **kwargs):
        return self.request("POST", path, payload=payload, **kwargs)

    def put(self, path, payload=_UNSET, **kwargs):
        return self.request("PUT", path, payload=payload, **kwargs)

    def delete(self, path, **kwargs):
        return self.request("DELETE", path, **kwargs)


# ---------------------------------------------------------------- 应用子进程


class AppServer:
    """用子进程启动 bark-bridge.py，使用独立临时 DATA_DIR 与随机端口。"""

    def __init__(
        self,
        *,
        ntfy_url: str,
        bark_url: str,
        admin_user: str = "admin",
        admin_password=ADMIN_PASSWORD,
        extra_env=None,
        ready_timeout: float = 25.0,
        app_file: str | None = None,
        webui_file: str | None = None,
        cwd: str | None = None,
    ):
        self.app_file = app_file or APP_PATH
        if not os.path.exists(self.app_file):
            raise RuntimeError(f"找不到后端文件：{self.app_file}")
        self.webui_file = webui_file or WEBUI_PATH
        self.cwd = cwd or ROOT
        self.tmpdir = make_temp_dir()
        self.data_dir = make_app_dirs(self.tmpdir)
        self.port = free_port()
        self.base = f"http://127.0.0.1:{self.port}"
        self.log_path = os.path.join(self.tmpdir, "app.log")
        self._log = open(self.log_path, "wb")
        self._stopped = False
        # 在线更新成功后进程会重启：POSIX 下 os.execv 原地替换（PID 不变），
        # Windows 下 CRT 实现为新进程 + 旧进程退出，需要额外记录新 PID 以便清理。
        self._extra_pids: "set[int]" = set()

        env = dict(os.environ)
        for key in (
            "PORT",
            "HOST",
            "DATA_DIR",
            "WEBUI_FILE",
            "ADMIN_USER",
            "ADMIN_PASSWORD",
            "BARK_SERVER",
            "NTFY_BASE_URL",
            "SESSION_TTL",
            "MAX_BODY",
            "RECONNECT_DELAY",
            "PUSH_WORKERS",
            "ACTIVITY_SIZE",
            "ACCESS_LOG",
            "APP_FILE",
            "UPDATE_ENABLED",
            "UPDATE_REPO",
            "UPDATE_BRANCH",
            "UPDATE_API_BASE",
            "UPDATE_CODELOAD_BASE",
            "UPDATE_AUTOCHECK",
            "JSON_BODY_PARSE",
            "SEND_IMAGE",
            # 旧版兼容播种变量，避免宿主机环境影响测试。
            "NTFY_TOPIC",
            "NTFY_TOKEN",
            "NTFY_SINCE",
            "BARK_KEY",
            "BARK_GROUP",
            "BARK_LEVEL",
            "BARK_ICON",
        ):
            env.pop(key, None)
        env.update(
            {
                "PORT": str(self.port),
                "HOST": "127.0.0.1",
                "DATA_DIR": self.data_dir,
                "WEBUI_FILE": self.webui_file,
                "APP_FILE": self.app_file,
                "ADMIN_USER": admin_user,
                "ADMIN_PASSWORD": "" if admin_password is None else str(admin_password),
                "BARK_SERVER": bark_url,
                "NTFY_BASE_URL": ntfy_url,
                "SESSION_TTL": "3600",
                "MAX_BODY": "2000",
                "RECONNECT_DELAY": "1",
                "PUSH_WORKERS": "4",
                "ACTIVITY_SIZE": "200",
                "ACCESS_LOG": "0",
                # 测试默认关闭「启动后 6 秒 + 每 6 小时」的后台自动检查，避免访问
                # 真实 GitHub（无外网依赖 + 不拖慢测试）；需要验证预热的用例单独开。
                "UPDATE_AUTOCHECK": "0",
                "PYTHONUNBUFFERED": "1",
                "PYTHONIOENCODING": "utf-8",
                "PYTHONUTF8": "1",
            }
        )
        if extra_env:
            env.update({str(key): str(value) for key, value in extra_env.items()})

        self.proc = subprocess.Popen(
            [sys.executable, self.app_file],
            cwd=self.cwd,
            env=env,
            stdout=self._log,
            stderr=subprocess.STDOUT,
        )
        try:
            self.wait_ready(ready_timeout)
        except Exception:
            self.stop()
            raise

    # -- 生命周期 ---------------------------------------------------------
    def wait_ready(self, timeout: float) -> None:
        deadline = time.monotonic() + timeout
        last_error = None
        while time.monotonic() < deadline:
            if self.proc.poll() is not None:
                raise RuntimeError(
                    f"应用启动失败（退出码 {self.proc.returncode}）：\n{self.log_tail()}"
                )
            try:
                with urllib.request.urlopen(self.base + "/api/health", timeout=1.0) as response:
                    response.read()
                    if response.status == 200:
                        return
            except Exception as exc:  # noqa: BLE001 - 端口尚未监听时属正常
                last_error = exc
            time.sleep(0.05)
        raise RuntimeError(f"应用在 {timeout:g}s 内未就绪（{last_error!r}）：\n{self.log_tail()}")

    def log_tail(self, limit: int = 8000) -> str:
        try:
            self._log.flush()
            with open(self.log_path, "rb") as handle:
                data = handle.read()
            return data[-limit:].decode("utf-8", "replace")
        except OSError:
            return ""

    # -- 进程发现与清理（用于更新重启） ----------------------------------
    def listening_pids(self) -> "list[int]":
        """返回正在监听本应用端口的进程 PID（Windows 用 netstat -ano）。"""
        if os.name != "nt":
            return []
        try:
            completed = subprocess.run(
                ["netstat", "-ano", "-p", "TCP"],
                capture_output=True,
                text=True,
                timeout=10,
            )
        except (OSError, subprocess.SubprocessError):
            return []
        pids: "list[int]" = []
        suffix = ":" + str(self.port)
        for line in completed.stdout.splitlines():
            fields = line.split()
            if len(fields) < 5 or fields[0].upper() != "TCP":
                continue
            if not fields[1].endswith(suffix) or fields[3].upper() != "LISTENING":
                continue
            try:
                pid = int(fields[4])
            except ValueError:
                continue
            if pid not in pids:
                pids.append(pid)
        return pids

    def adopt_listening_processes(self) -> "list[int]":
        """更新重启后把监听端口的新进程（PID 与旧句柄不同）纳入清理列表。"""
        pids = [pid for pid in self.listening_pids() if pid != self.proc.pid]
        self._extra_pids.update(pids)
        return pids

    @staticmethod
    def _terminate_pid(pid: int) -> None:
        try:
            os.kill(pid, signal.SIGTERM)
            return
        except OSError:
            pass
        try:
            subprocess.run(
                ["taskkill", "/F", "/PID", str(pid)],
                capture_output=True,
                timeout=10,
            )
        except (OSError, subprocess.SubprocessError):
            pass

    def _wait_port_free(self, timeout: float) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if not self.listening_pids():
                return True
            time.sleep(0.1)
        return False

    def stop(self) -> None:
        if self._stopped:
            return
        self._stopped = True
        original_exited = self.proc.poll() is not None
        if not original_exited:
            self.proc.terminate()  # Windows 上是 TerminateProcess，干净收尾由测试断言
            try:
                self.proc.wait(timeout=8)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                try:
                    self.proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    pass
        # 重启（例如应用了在线更新）后旧句柄已退出，新进程需要按端口发现并终止。
        extra_pids = set(self._extra_pids)
        if original_exited and not extra_pids:
            extra_pids.update(self.listening_pids())
        for pid in extra_pids:
            self._terminate_pid(pid)
        if extra_pids:
            if not self._wait_port_free(8.0):
                for pid in self.listening_pids():
                    self._terminate_pid(pid)
                self._wait_port_free(4.0)
        try:
            self._log.close()
        except OSError:
            pass
        remove_tree(self.tmpdir)


# ---------------------------------------------------------------- 测试基类


class BridgeTestCase(unittest.TestCase):
    """提供假服务器、应用子进程、登录与轮询断言的公共基类。"""

    maxDiff = None

    def setUp(self):
        self.ntfy = FakeNtfy()
        self.addCleanup(self.ntfy.close)
        self.bark = FakeBark()
        self.addCleanup(self.bark.close)

    # -- 应用与客户端 -----------------------------------------------------
    def start_app(
        self,
        *,
        admin_password=_DEFAULT_PASSWORD,
        admin_user="admin",
        extra_env=None,
        app_file=None,
        webui_file=None,
        cwd=None,
    ):
        if admin_password is _DEFAULT_PASSWORD:
            admin_password = ADMIN_PASSWORD
        app = AppServer(
            ntfy_url=self.ntfy.url,
            bark_url=self.bark.url,
            admin_user=admin_user,
            admin_password=admin_password,
            extra_env=extra_env,
            app_file=app_file,
            webui_file=webui_file,
            cwd=cwd,
        )
        self.addCleanup(app.stop)
        return app

    def client(self, app) -> ApiClient:
        return ApiClient(app.base)

    def login(self, app, username="admin", password=None, client=None) -> ApiClient:
        if password is None:
            password = ADMIN_PASSWORD
        if client is None:
            client = self.client(app)
        response = client.post("/api/login", {"username": username, "password": password})
        self.assertEqual(response.status, 200, f"登录失败：{response.text}")
        self.assertTrue((response.json or {}).get("ok"), response.text)
        return client

    def make_user(self, admin: ApiClient, username, password="UserPass123!", role="user", is_active=True):
        response = admin.post(
            "/api/users",
            {"username": username, "password": password, "role": role, "is_active": is_active},
        )
        self.assertEqual(response.status, 201, f"创建用户失败：{response.text}")
        return response.data["user"]

    # -- 资源构造 ---------------------------------------------------------
    def unique(self, prefix: str) -> str:
        return f"{prefix}-{uuid.uuid4().hex[:8]}"

    def create_device(self, client: ApiClient, name, *, bark_key=None, **overrides):
        payload = {
            "name": name,
            "bark_key": bark_key or ("key-" + uuid.uuid4().hex[:12]),
            "server": self.bark.url,
            "group": "ntfy",
        }
        payload.update(overrides)
        response = client.post("/api/devices", payload)
        self.assertEqual(response.status, 201, f"创建设备失败：{response.text}")
        return response.data["device"]

    def create_subscription(self, client: ApiClient, device_ids, topic=None, **overrides):
        payload = {
            "name": self.unique("sub"),
            "ntfy_url": self.ntfy.url,
            "topic": topic or self.unique("topic"),
            "since": "all",
            "device_ids": list(device_ids),
        }
        payload.update(overrides)
        response = client.post("/api/subscriptions", payload)
        self.assertEqual(response.status, 201, f"创建订阅失败：{response.text}")
        return response.data["subscription"]

    # -- 查询辅助 ---------------------------------------------------------
    def get_subscription(self, client: ApiClient, sub_id):
        response = client.get("/api/subscriptions")
        self.assertEqual(response.status, 200, response.text)
        for subscription in response.data.get("subscriptions", []):
            if subscription["id"] == sub_id:
                return subscription
        return None

    def get_device(self, client: ApiClient, device_id):
        response = client.get("/api/devices")
        self.assertEqual(response.status, 200, response.text)
        for device in response.data.get("devices", []):
            if device["id"] == device_id:
                return device
        return None

    def wait_subscription_state(self, client, sub_id, states=("streaming",), timeout=DEFAULT_TIMEOUT):
        def probe():
            subscription = self.get_subscription(client, sub_id)
            if subscription and subscription.get("stats", {}).get("state") in states:
                return subscription
            return None

        return wait_for(probe, timeout=timeout, message=f"订阅 {sub_id} 状态进入 {states}")

    def get_status(self, client: ApiClient):
        response = client.get("/api/status")
        self.assertEqual(response.status, 200, response.text)
        self.assertTrue((response.json or {}).get("ok"), response.text)
        return response.data

    def get_activity(self, client: ApiClient, since=0, limit=100):
        response = client.get(f"/api/activity?since={since}&limit={limit}")
        self.assertEqual(response.status, 200, response.text)
        data = response.data
        self.assertIn("items", data, response.text)
        self.assertIn("last_id", data, response.text)
        self.assertIsInstance(data["items"], list)
        return data

    # -- 推送等待 ---------------------------------------------------------
    def wait_pushes(self, predicate=lambda payload: True, count=1, timeout=DEFAULT_TIMEOUT, description=None):
        description = description or f"Bark 收到 {count} 条成功推送"

        def probe():
            matched = records_matching(self.bark.records(), lambda payload: predicate(payload))
            matched = [record for record in matched if record["ok"]]
            if len(matched) >= count:
                return [record["payload"] for record in matched]
            return None

        return wait_for(probe, timeout=timeout, message=description)

    def wait_push_attempt(self, predicate=lambda payload: True, timeout=DEFAULT_TIMEOUT, description=None):
        description = description or "Bark 收到推送尝试"

        def probe():
            matched = records_matching(self.bark.records(), predicate)
            if matched:
                return matched
            return None

        return wait_for(probe, timeout=timeout, message=description)

    def assert_no_push(self, predicate=lambda payload: True, duration=1.2, description="不应产生推送"):
        deadline = time.monotonic() + duration
        while time.monotonic() < deadline:
            matched = records_matching(self.bark.records(), predicate)
            if matched:
                self.fail(f"{description}，但 Bark 收到了：{matched}")
            time.sleep(0.1)

    # -- 错误断言 ---------------------------------------------------------
    def assert_error(self, response, status=None, code=None):
        if status is not None:
            if isinstance(status, (tuple, list, set, frozenset)):
                self.assertIn(response.status, status, response.text)
            else:
                self.assertEqual(response.status, status, response.text)
        self.assertIsNotNone(response.json, f"错误响应不是 JSON：{response.text!r}")
        self.assertFalse(response.json.get("ok"), response.text)
        self.assertIsInstance(response.json.get("error"), str, response.text)
        self.assertTrue(response.json.get("error"), response.text)
        if code is not None:
            if isinstance(code, (tuple, list, set, frozenset)):
                self.assertIn(response.json.get("code"), code, response.text)
            else:
                self.assertEqual(response.json.get("code"), code, response.text)


class AdminAppCase(BridgeTestCase):
    """每个用例获得一个全新应用 + 已登录的 admin 客户端。"""

    def setUp(self):
        super().setUp()
        self.app = self.start_app()
        self.admin = self.login(self.app)


# ================================================================ 认证与系统


class TestAuth(BridgeTestCase):
    def test_health_needs_setup_when_no_users(self):
        app = self.start_app(admin_password=None)
        client = self.client(app)
        response = client.get("/api/health")
        self.assertEqual(response.status, 200, response.text)
        self.assertTrue(response.json["ok"], response.text)
        data = response.data
        self.assertTrue(data["needs_setup"], response.text)
        self.assertEqual(data["version"], API_VERSION)
        self.assertGreaterEqual(float(data["uptime"]), 0)
        self.assertIsInstance(data["now"], (int, float))

    def test_setup_creates_admin_then_conflicts(self):
        app = self.start_app(admin_password=None)
        client = self.client(app)

        # 非法 setup 不应创建用户。
        bad = client.post("/api/setup", {"username": "x", "password": "Short1!"})
        self.assert_error(bad, status=400, code="invalid_request")
        bad = client.post("/api/setup", {"username": "root", "password": "short"})
        self.assert_error(bad, status=400, code="invalid_request")
        self.assertTrue(client.get("/api/health").data["needs_setup"])

        response = client.post("/api/setup", {"username": "root", "password": "RootPass123!"})
        self.assertIn(response.status, (200, 201), response.text)
        data = response.data
        self.assertEqual(data["user"]["username"], "root")
        self.assertEqual(data["user"]["role"], "admin")
        self.assertEqual(data["csrf"], client.csrf)
        self.assertIsNotNone(client.cookie("bb_session"))

        me = client.get("/api/me")
        self.assertEqual(me.status, 200, me.text)
        self.assertEqual(me.data["user"]["username"], "root")
        self.assertTrue(me.data["is_admin"])

        # 已有用户后再次 setup 必须 409。
        second = self.client(app).post("/api/setup", {"username": "other", "password": "OtherPass123!"})
        self.assert_error(second, status=409, code="conflict")
        self.assertFalse(self.client(app).get("/api/health").data["needs_setup"])

    def test_health_with_env_admin_no_setup(self):
        app = self.start_app()
        data = self.client(app).get("/api/health").data
        self.assertFalse(data["needs_setup"])
        self.assertEqual(data["version"], API_VERSION)

    def test_login_me_cookies_and_logout(self):
        app = self.start_app()
        client = self.client(app)
        response = client.post("/api/login", {"username": "admin", "password": ADMIN_PASSWORD})
        self.assertEqual(response.status, 200, response.text)
        self.assertEqual(response.data["user"]["username"], "admin")
        self.assertEqual(response.data["user"]["role"], "admin")
        self.assertEqual(response.data["csrf"], client.csrf)

        set_cookies = "\n".join(response.header_all("Set-Cookie"))
        self.assertIn("bb_session=", set_cookies)
        self.assertIn("bb_csrf=", set_cookies)
        session_cookie = next(
            (line for line in set_cookies.split("\n") if line.startswith("bb_session=")), ""
        )
        csrf_cookie = next(
            (line for line in set_cookies.split("\n") if line.startswith("bb_csrf=")), ""
        )
        self.assertIn("HttpOnly", session_cookie)
        self.assertNotIn("HttpOnly", csrf_cookie)
        self.assertIn("SameSite=Lax", set_cookies)
        self.assertIn("Path=/", set_cookies)

        me = client.get("/api/me")
        self.assertEqual(me.status, 200, me.text)
        self.assertEqual(me.data["version"], API_VERSION)
        self.assertTrue(me.data["is_admin"])

        logout = client.post("/api/logout", {})
        self.assertEqual(logout.status, 200, logout.text)
        self.assertEqual(client.get("/api/me").status, 401)

    def test_unauthorized_protected_endpoints(self):
        app = self.start_app()
        client = self.client(app)
        for path in (
            "/api/me",
            "/api/settings",
            "/api/devices",
            "/api/subscriptions",
            "/api/status",
            "/api/activity",
            "/api/users",
        ):
            response = client.get(path)
            self.assert_error(response, status=401, code="unauthorized")
        # 未登录的写请求同样是 401（不应先报 CSRF）。
        response = client.post(
            "/api/devices", {"name": "x", "bark_key": "abcd1234"}, csrf=False
        )
        self.assert_error(response, status=401, code="unauthorized")

    def test_csrf_required_for_writes(self):
        app = self.start_app()
        client = self.login(app)
        body = {"name": "iPhone", "bark_key": "TestKey0001", "server": self.bark.url}

        missing = client.post("/api/devices", body, csrf=False)
        self.assert_error(missing, status=403, code="csrf_failed")

        wrong = client.post("/api/devices", body, headers={"X-CSRF-Token": "wrong-token"})
        self.assert_error(wrong, status=403, code="csrf_failed")

        # GET 不需要 CSRF。
        self.assertEqual(client.get("/api/devices").status, 200)

        ok = client.post("/api/devices", body)
        self.assertEqual(ok.status, 201, ok.text)

    def test_wrong_password_rate_limited(self):
        app = self.start_app()
        client = self.client(app)
        for _ in range(8):
            response = client.post("/api/login", {"username": "admin", "password": "wrong-password"})
            self.assert_error(response, status=401, code="invalid_credentials")
        limited = client.post("/api/login", {"username": "admin", "password": "wrong-password"})
        self.assert_error(limited, status=429, code="rate_limited")
        # 其它用户名不应被同一 key 的计数牵连。
        other = client.post("/api/login", {"username": "someone", "password": "wrong-password"})
        self.assert_error(other, status=401, code="invalid_credentials")

    def test_change_password_invalidates_other_sessions(self):
        app = self.start_app()
        first = self.login(app)
        second = self.login(app)
        self.assertEqual(second.get("/api/me").status, 200)

        response = first.post(
            "/api/password", {"old_password": ADMIN_PASSWORD, "new_password": "NewPass456!"}
        )
        self.assertEqual(response.status, 200, response.text)
        self.assertEqual(second.get("/api/me").status, 401)

        old_login = self.client(app).post("/api/login", {"username": "admin", "password": ADMIN_PASSWORD})
        self.assert_error(old_login, status=401, code="invalid_credentials")

        new_client = self.client(app)
        new_login = new_client.post(
            "/api/login", {"username": "admin", "password": "NewPass456!"}
        )
        self.assertEqual(new_login.status, 200, new_login.text)

    def test_change_password_requires_correct_old_password(self):
        app = self.start_app()
        client = self.login(app)
        response = client.post(
            "/api/password", {"old_password": "not-the-password", "new_password": "NewPass456!"}
        )
        self.assert_error(response, status=(400, 401), code=("invalid_credentials", "invalid_request"))
        # 旧密码仍然有效。
        self.assertEqual(
            self.client(app)
            .post("/api/login", {"username": "admin", "password": ADMIN_PASSWORD})
            .status,
            200,
        )

    def test_app_terminates_cleanly(self):
        app = self.start_app()
        self.assertIsNone(app.proc.poll())
        app.proc.terminate()
        try:
            returncode = app.proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.fail("terminate 后 10 秒内进程未退出")
        self.assertIsNotNone(returncode)


# ================================================================ 权限隔离


class TestPermissions(AdminAppCase):
    def setUp(self):
        super().setUp()
        self.admin_id = self.admin.get("/api/me").data["user"]["id"]
        self.alice_user = self.make_user(self.admin, "alice")
        self.alice = self.login(self.app, "alice", "UserPass123!")
        self.alice_id = self.alice.get("/api/me").data["user"]["id"]

    def test_user_device_and_subscription_isolation(self):
        admin_device = self.create_device(self.admin, "Admin-iPhone")
        alice_device = self.create_device(self.alice, "Alice-iPad")

        alice_devices = self.alice.get("/api/devices").data["devices"]
        self.assertEqual([device["id"] for device in alice_devices], [alice_device["id"]])
        self.assertEqual(alice_devices[0]["owner_id"], self.alice_id)
        self.assertEqual(alice_devices[0]["owner"], "alice")

        admin_devices = self.admin.get("/api/devices").data["devices"]
        admin_ids = {device["id"] for device in admin_devices}
        self.assertIn(admin_device["id"], admin_ids)
        self.assertIn(alice_device["id"], admin_ids)

        admin_sub = self.create_subscription(self.admin, [admin_device["id"]])
        alice_sub = self.create_subscription(self.alice, [alice_device["id"]])

        alice_subs = self.alice.get("/api/subscriptions").data["subscriptions"]
        self.assertEqual([sub["id"] for sub in alice_subs], [alice_sub["id"]])
        self.assertEqual(alice_subs[0]["owner_id"], self.alice_id)

        admin_subs = self.admin.get("/api/subscriptions").data["subscriptions"]
        admin_sub_ids = {sub["id"] for sub in admin_subs}
        self.assertIn(admin_sub["id"], admin_sub_ids)
        self.assertIn(alice_sub["id"], admin_sub_ids)

    def test_user_cannot_modify_others_records(self):
        admin_device = self.create_device(self.admin, "Admin-iPhone")
        admin_sub = self.create_subscription(self.admin, [admin_device["id"]])

        put_device = self.alice.put(f"/api/devices/{admin_device['id']}", {"name": "hacked"})
        self.assert_error(put_device, status=(403, 404), code=("forbidden", "not_found"))
        delete_device = self.alice.delete(f"/api/devices/{admin_device['id']}")
        self.assert_error(delete_device, status=(403, 404), code=("forbidden", "not_found"))
        test_device = self.alice.post(f"/api/devices/{admin_device['id']}/test", {})
        self.assert_error(test_device, status=(403, 404), code=("forbidden", "not_found"))
        self.assertIsNotNone(self.get_device(self.admin, admin_device["id"]))

        put_sub = self.alice.put(f"/api/subscriptions/{admin_sub['id']}", {"title_prefix": "x"})
        self.assert_error(put_sub, status=(403, 404), code=("forbidden", "not_found"))
        delete_sub = self.alice.delete(f"/api/subscriptions/{admin_sub['id']}")
        self.assert_error(delete_sub, status=(403, 404), code=("forbidden", "not_found"))
        self.assertIsNotNone(self.get_subscription(self.admin, admin_sub["id"]))

        # 普通用户不能把别人的设备设为订阅目标。
        # 契约 docs/API.md §4.3：必须是当前用户自己的设备，否则 403 forbidden。
        cross = self.alice.post(
            "/api/subscriptions",
            {
                "name": "cross",
                "ntfy_url": self.ntfy.url,
                "topic": self.unique("topic"),
                "device_ids": [admin_device["id"]],
            },
        )
        self.assert_error(cross, status=403, code="forbidden")

    def test_admin_can_create_records_for_user(self):
        alice_device = self.create_device(self.alice, "Alice-iPad")
        response = self.admin.post(
            "/api/subscriptions",
            {
                "name": "for-alice",
                "ntfy_url": self.ntfy.url,
                "topic": self.unique("topic"),
                "since": "all",
                "device_ids": [alice_device["id"]],
                "owner_id": self.alice_id,
            },
        )
        self.assertEqual(response.status, 201, response.text)
        subscription = response.data["subscription"]
        self.assertEqual(subscription["owner_id"], self.alice_id)
        self.assertEqual(subscription["owner"], "alice")
        self.assertIn(subscription["id"], [s["id"] for s in self.alice.get("/api/subscriptions").data["subscriptions"]])

    def test_non_admin_cannot_manage_users(self):
        self.assert_error(self.alice.get("/api/users"), status=403, code="forbidden")
        self.assert_error(
            self.alice.post("/api/users", {"username": "bob2", "password": "BobPass123!"}),
            status=403,
            code="forbidden",
        )
        self.assert_error(
            self.alice.put(f"/api/users/{self.admin_id}", {"role": "user"}),
            status=403,
            code="forbidden",
        )
        self.assert_error(
            self.alice.put("/api/settings", {"default_ntfy_url": "http://evil.example.com"}),
            status=403,
            code="forbidden",
        )

    def test_last_admin_protected(self):
        demote = self.admin.put(f"/api/users/{self.admin_id}", {"role": "user"})
        self.assert_error(
            demote,
            status=(400, 403, 409),
            code=("last_admin", "invalid_request", "forbidden", "conflict"),
        )
        deactivate = self.admin.put(f"/api/users/{self.admin_id}", {"is_active": False})
        self.assert_error(
            deactivate,
            status=(400, 403, 409),
            code=("last_admin", "invalid_request", "forbidden", "conflict"),
        )
        delete_self = self.admin.delete(f"/api/users/{self.admin_id}")
        self.assert_error(
            delete_self,
            status=(400, 403, 409),
            code=("last_admin", "invalid_request", "forbidden", "conflict"),
        )
        users = self.admin.get("/api/users")
        self.assertEqual(users.status, 200, users.text)
        admin_record = next(user for user in users.data["users"] if user["id"] == self.admin_id)
        self.assertEqual(admin_record["role"], "admin")
        self.assertTrue(admin_record["is_active"])

    def test_admin_delete_user_cascades(self):
        bob = self.make_user(self.admin, "bob")
        bob_client = self.login(self.app, "bob", "UserPass123!")
        bob_device = self.create_device(bob_client, "Bob-Phone")
        bob_sub = self.create_subscription(bob_client, [bob_device["id"]])

        response = self.admin.delete(f"/api/users/{bob['id']}")
        self.assertEqual(response.status, 200, response.text)
        self.assertEqual(bob_client.get("/api/me").status, 401)

        users = self.admin.get("/api/users").data["users"]
        self.assertNotIn(bob["id"], [user["id"] for user in users])
        devices = self.admin.get("/api/devices").data["devices"]
        self.assertNotIn(bob_device["id"], [device["id"] for device in devices])
        subscriptions = self.admin.get("/api/subscriptions").data["subscriptions"]
        self.assertNotIn(bob_sub["id"], [sub["id"] for sub in subscriptions])

    def test_user_management_validation(self):
        self.assert_error(
            self.admin.post("/api/users", {"username": "x", "password": "ValidPass123!"}),
            status=400,
            code="invalid_request",
        )
        self.assert_error(
            self.admin.post("/api/users", {"username": "validname", "password": "short"}),
            status=400,
            code="invalid_request",
        )
        self.assert_error(
            self.admin.post(
                "/api/users", {"username": "roleuser", "password": "ValidPass123!", "role": "root"}
            ),
            status=400,
            code="invalid_request",
        )
        self.assert_error(
            self.admin.post("/api/users", {"username": "alice", "password": "ValidPass123!"}),
            status=409,
            code="conflict",
        )


# ================================================================ 设备


class TestDevices(AdminAppCase):
    def test_create_and_list_devices(self):
        response = self.admin.post(
            "/api/devices",
            {
                "name": "iPhone 15",
                "bark_key": "TestKey0001",
                "server": self.bark.url + "/",
                "group": "work",
                "level": "passive",
                "icon": "https://example.com/icon.png",
            },
        )
        self.assertEqual(response.status, 201, response.text)
        device = response.data["device"]
        self.assertIsInstance(device["id"], int)
        self.assertEqual(device["name"], "iPhone 15")
        self.assertEqual(device["bark_key"], "TestKey0001")
        self.assertEqual(device["server"], self.bark.url)  # 去掉尾部 /
        self.assertEqual(device["group"], "work")
        self.assertEqual(device["level"], "passive")
        self.assertEqual(device["icon"], "https://example.com/icon.png")
        self.assertTrue(device["is_active"])
        self.assertEqual(device["owner_id"], self.admin.get("/api/me").data["user"]["id"])
        self.assertEqual(device["owner"], "admin")
        self.assertIsInstance(device["created_at"], int)
        self.assertGreater(device["created_at"], 0)
        self.assertEqual(device["pushed_count"], 0)
        self.assertEqual(device["last_push_at"], 0)
        self.assertEqual(device["last_error"], "")
        self.assertNotIn("token", device)

        devices = self.admin.get("/api/devices").data["devices"]
        self.assertIn(device["id"], [item["id"] for item in devices])

    def test_partial_update_keeps_omitted_fields(self):
        device = self.create_device(
            self.admin, "iPhone", bark_key="OriginalKey01", group="work", level="active"
        )
        response = self.admin.put(f"/api/devices/{device['id']}", {"name": "Renamed"})
        self.assertEqual(response.status, 200, response.text)
        updated = response.data["device"]
        self.assertEqual(updated["name"], "Renamed")
        self.assertEqual(updated["bark_key"], "OriginalKey01")  # 省略 = 不变
        self.assertEqual(updated["group"], "work")
        self.assertEqual(updated["level"], "active")

        response = self.admin.put(f"/api/devices/{device['id']}", {"bark_key": "NewKey0002"})
        self.assertEqual(response.status, 200, response.text)
        self.assertEqual(response.data["device"]["bark_key"], "NewKey0002")
        self.assertEqual(response.data["device"]["name"], "Renamed")

        response = self.admin.put(f"/api/devices/{device['id']}", {"group": ""})
        self.assertEqual(response.status, 200, response.text)
        self.assertEqual(response.data["device"]["group"], "")

    def test_device_validation(self):
        valid = {"name": "iPhone", "bark_key": "ValidKey01", "server": self.bark.url}
        cases = [
            {"name": ""},
            {"name": "x" * 65},
            {"bark_key": "abc"},  # < 4
            {"bark_key": " " * 8},  # 空白
            {"server": "ftp://example.com"},
            {"server": "bark.example.com"},
            {"level": "bogus"},
            {"icon": "not-a-url"},
            {"group": "x" * 65},
        ]
        for override in cases:
            payload = dict(valid)
            payload.update(override)
            response = self.admin.post("/api/devices", payload)
            self.assert_error(response, status=400, code="invalid_request")
        # 合法设备仍可创建。
        self.assertEqual(self.admin.post("/api/devices", valid).status, 201)

    def test_device_test_push(self):
        device = self.create_device(self.admin, "iPhone", bark_key="PushTestKey1", group="alerts")
        self.bark.clear()
        response = self.admin.post(f"/api/devices/{device['id']}/test", {})
        self.assertEqual(response.status, 200, response.text)
        data = response.data
        self.assertTrue(data["sent"], response.text)
        self.assertIsInstance(data.get("detail"), str)

        payloads = self.wait_pushes(
            lambda payload: payload.get("device_key") == "PushTestKey1", count=1
        )
        payload = payloads[0]
        self.assertEqual(payload["group"], "alerts")
        self.assertIn("test", json.dumps(payload, ensure_ascii=False).lower())

    def test_device_not_found(self):
        self.assert_error(
            self.admin.put("/api/devices/999999", {"name": "x"}), status=404, code="not_found"
        )
        self.assert_error(self.admin.delete("/api/devices/999999"), status=404, code="not_found")
        self.assert_error(
            self.admin.post("/api/devices/999999/test", {}), status=404, code="not_found"
        )

    def test_device_delete_unlinks_subscription(self):
        device = self.create_device(self.admin, "iPhone")
        subscription = self.create_subscription(self.admin, [device["id"]])
        response = self.admin.delete(f"/api/devices/{device['id']}")
        self.assertEqual(response.status, 200, response.text)
        self.assertIsNone(self.get_device(self.admin, device["id"]))
        current = self.get_subscription(self.admin, subscription["id"])
        self.assertIsNotNone(current)
        self.assertEqual(current["device_ids"], [])


# ================================================================ 订阅


class TestSubscriptions(AdminAppCase):
    def test_create_and_token_never_returned(self):
        device = self.create_device(self.admin, "iPhone")
        response = self.admin.post(
            "/api/subscriptions",
            {
                "name": "告警",
                "ntfy_url": self.ntfy.url,
                "topic": "alerts",
                "token": "tk_secret_token",
                "since": "all",
                "priority_min": 2,
                "keyword": "磁盘 告警",
                "exclude_keyword": "测试",
                "title_prefix": "[生产]",
                "device_ids": [device["id"]],
            },
        )
        self.assertEqual(response.status, 201, response.text)
        subscription = response.data["subscription"]
        self.assertEqual(subscription["name"], "告警")
        self.assertEqual(subscription["topic"], "alerts")
        self.assertEqual(subscription["priority_min"], 2)
        self.assertEqual(subscription["keyword"], "磁盘 告警")
        self.assertEqual(subscription["exclude_keyword"], "测试")
        self.assertEqual(subscription["title_prefix"], "[生产]")
        self.assertEqual(subscription["device_ids"], [device["id"]])
        self.assertTrue(subscription["has_token"])
        self.assertNotIn("token", subscription)
        self.assertEqual(subscription["owner_id"], self.admin.get("/api/me").data["user"]["id"])
        stats = subscription["stats"]
        self.assertIn(stats["state"], ("stopped", "connecting", "streaming", "reconnecting", "error"))
        self.assertIn("last_error", stats)
        self.assertIn("forwarded", stats)
        self.assertIn("last_message_at", stats)

        listed = self.admin.get("/api/subscriptions").data["subscriptions"]
        stored = next(item for item in listed if item["id"] == subscription["id"])
        self.assertNotIn("token", stored)
        self.assertTrue(stored["has_token"])

    def test_update_token_semantics(self):
        device = self.create_device(self.admin, "iPhone")
        subscription = self.create_subscription(self.admin, [device["id"]], token="tk_first")

        response = self.admin.put(f"/api/subscriptions/{subscription['id']}", {"title_prefix": "x"})
        self.assertEqual(response.status, 200, response.text)
        self.assertTrue(response.data["subscription"]["has_token"])  # 省略 = 不变

        response = self.admin.put(f"/api/subscriptions/{subscription['id']}", {"token": None})
        self.assertEqual(response.status, 200, response.text)
        self.assertTrue(response.data["subscription"]["has_token"])  # null = 不变

        response = self.admin.put(f"/api/subscriptions/{subscription['id']}", {"token": ""})
        self.assertEqual(response.status, 200, response.text)
        self.assertFalse(response.data["subscription"]["has_token"])  # "" = 清除
        self.assertNotIn("token", response.data["subscription"])

        response = self.admin.put(
            f"/api/subscriptions/{subscription['id']}", {"token": "tk_second"}
        )
        self.assertEqual(response.status, 200, response.text)
        self.assertTrue(response.data["subscription"]["has_token"])

    def test_update_device_ids(self):
        first = self.create_device(self.admin, "iPhone")
        second = self.create_device(self.admin, "iPad")
        subscription = self.create_subscription(self.admin, [first["id"]])

        response = self.admin.put(
            f"/api/subscriptions/{subscription['id']}",
            {"device_ids": [first["id"], second["id"]]},
        )
        self.assertEqual(response.status, 200, response.text)
        self.assertEqual(sorted(response.data["subscription"]["device_ids"]), sorted([first["id"], second["id"]]))

        missing = self.admin.put(
            f"/api/subscriptions/{subscription['id']}", {"device_ids": [999999]}
        )
        self.assert_error(missing, status=400, code="invalid_request")

    def test_subscription_validation(self):
        device = self.create_device(self.admin, "iPhone")
        valid = {
            "name": "告警",
            "ntfy_url": self.ntfy.url,
            "topic": self.unique("topic"),
            "device_ids": [device["id"]],
        }
        cases = [
            {"name": ""},
            {"topic": "bad topic!"},
            {"topic": ""},
            {"priority_min": 6},
            {"priority_min": -1},
            {"since": "x" * 33},
            {"keyword": "x" * 257},
            {"exclude_keyword": "x" * 257},
            {"title_prefix": "x" * 33},
            {"ntfy_url": "ftp://example.com"},
        ]
        for override in cases:
            payload = dict(valid)
            payload.update(override)
            response = self.admin.post("/api/subscriptions", payload)
            self.assert_error(response, status=400, code="invalid_request")
        self.assertEqual(self.admin.post("/api/subscriptions", valid).status, 201)

    def test_subscription_test_push_multiple_devices(self):
        first = self.create_device(self.admin, "iPhone", bark_key="SubTestKey01")
        second = self.create_device(self.admin, "iPad", bark_key="SubTestKey02")
        subscription = self.create_subscription(self.admin, [first["id"], second["id"]])
        self.bark.clear()

        response = self.admin.post(
            f"/api/subscriptions/{subscription['id']}/test",
            {"title": "测试标题", "body": "测试正文"},
        )
        self.assertEqual(response.status, 200, response.text)
        self.assertEqual(response.data["sent"], 2, response.text)
        self.assertEqual(response.data["failed"], 0, response.text)

        payloads = self.wait_pushes(lambda payload: payload.get("body") == "测试正文", count=2)
        keys = {payload["device_key"] for payload in payloads}
        self.assertEqual(keys, {"SubTestKey01", "SubTestKey02"})

    def test_delete_subscription(self):
        device = self.create_device(self.admin, "iPhone")
        subscription = self.create_subscription(self.admin, [device["id"]])
        response = self.admin.delete(f"/api/subscriptions/{subscription['id']}")
        self.assertEqual(response.status, 200, response.text)
        self.assertIsNone(self.get_subscription(self.admin, subscription["id"]))
        self.assert_error(
            self.admin.delete(f"/api/subscriptions/{subscription['id']}"),
            status=404,
            code="not_found",
        )


# ================================================================ 转发引擎


class TestEngine(AdminAppCase):
    def setUp(self):
        super().setUp()
        self.topic = self.unique("topic")

    # -- 辅助 -------------------------------------------------------------
    def make_device(self, name, **overrides):
        return self.create_device(self.admin, name, **overrides)

    def make_sub(self, devices, **overrides):
        device_ids = [device["id"] if isinstance(device, dict) else device for device in devices]
        return self.create_subscription(self.admin, device_ids, topic=self.topic, **overrides)

    def connect(self, subscription=None):
        wait_for(
            lambda: self.ntfy.active_connections(self.topic) >= 1,
            timeout=DEFAULT_TIMEOUT,
            message=f"订阅工作线程连接 ntfy（topic={self.topic}）",
        )
        if subscription is not None:
            self.wait_subscription_state(self.admin, subscription["id"])

    def publish(self, **kwargs):
        return self.ntfy.publish(self.topic, **kwargs)

    # -- 用例 -------------------------------------------------------------
    def test_fanout_to_multiple_devices(self):
        first = self.make_device("iPhone", bark_key="FanKey000001", icon="https://example.com/i.png")
        second = self.make_device("iPad", bark_key="FanKey000002")
        inactive = self.make_device("Watch", bark_key="FanKey000003", is_active=False)
        subscription = self.make_sub([first, second, inactive])
        self.connect(subscription)
        self.bark.clear()

        self.publish(
            title="磁盘告警",
            message="磁盘使用率 95%",
            priority=5,
            click="https://example.com/alert/1",
        )
        payloads = self.wait_pushes(lambda payload: payload.get("title") == "磁盘告警", count=2)
        by_key = {payload["device_key"]: payload for payload in payloads}
        self.assertEqual(set(by_key), {"FanKey000001", "FanKey000002"})
        for key in ("FanKey000001", "FanKey000002"):
            payload = by_key[key]
            self.assertEqual(payload["level"], "timeSensitive")
            self.assertEqual(payload["group"], "ntfy")
            self.assertEqual(payload["body"], "磁盘使用率 95%")
            self.assertEqual(payload["url"], "https://example.com/alert/1")
        self.assertEqual(by_key["FanKey000001"].get("icon"), "https://example.com/i.png")
        self.assertNotIn("icon", by_key["FanKey000002"])

        # 停用的设备不应收到推送。
        self.assert_no_push(lambda payload: payload.get("device_key") == "FanKey000003", duration=0.8)

        # 无 click 的消息不应带 url 字段；priority=3 映射为 active。
        self.publish(title="无链接消息", message="正文", priority=3)
        payloads = self.wait_pushes(lambda payload: payload.get("title") == "无链接消息", count=2)
        for payload in payloads:
            self.assertNotIn("url", payload)
            self.assertEqual(payload["level"], "active")

        # 设备运行统计（pushed_count）应更新。
        for device in (first, second):
            wait_for(
                lambda device=device: (self.get_device(self.admin, device["id"]) or {}).get(
                    "pushed_count", 0
                )
                >= 1,
                timeout=DEFAULT_TIMEOUT,
                message=f"设备 {device['name']} 的 pushed_count 递增",
            )

    def test_priority_mapping_and_device_level_override(self):
        mapped = self.make_device("Mapped", bark_key="PrioKey00001")
        overridden = self.make_device("Overridden", bark_key="PrioKey00002", level="passive")
        subscription = self.make_sub([mapped, overridden])
        self.connect(subscription)
        self.bark.clear()

        self.publish(title="低优先级", message="p2", priority=2)
        payloads = self.wait_pushes(lambda payload: payload.get("title") == "低优先级", count=2)
        by_key = {payload["device_key"]: payload for payload in payloads}
        self.assertEqual(by_key["PrioKey00001"]["level"], "active")
        self.assertEqual(by_key["PrioKey00002"]["level"], "passive")

        self.bark.clear()
        self.publish(title="高优先级", message="p5", priority=5)
        payloads = self.wait_pushes(lambda payload: payload.get("title") == "高优先级", count=2)
        by_key = {payload["device_key"]: payload for payload in payloads}
        self.assertEqual(by_key["PrioKey00001"]["level"], "timeSensitive")
        self.assertEqual(by_key["PrioKey00002"]["level"], "passive")

    def test_priority_min_filter(self):
        device = self.make_device("iPhone")
        subscription = self.make_sub([device], priority_min=3)
        self.connect(subscription)
        self.bark.clear()

        self.publish(title="被过滤的低优先级", message="p2", priority=2)
        self.publish(title="通过的高优先级", message="p4", priority=4)
        payloads = self.wait_pushes(lambda payload: payload.get("title") == "通过的高优先级", count=1)
        self.assertEqual(payloads[0]["level"], "timeSensitive")
        self.assert_no_push(lambda payload: payload.get("title") == "被过滤的低优先级", duration=0.8)

    def test_keyword_filter(self):
        device = self.make_device("iPhone")
        subscription = self.make_sub([device], keyword="Disk Alert")
        self.connect(subscription)
        self.bark.clear()

        self.publish(title="DISK usage", message="alert triggered at 95%", priority=3)
        self.publish(title="memory usage", message="memory is high", priority=3)
        payloads = self.wait_pushes(lambda payload: payload.get("title") == "DISK usage", count=1)
        self.assertEqual(payloads[0]["body"], "alert triggered at 95%")
        self.assert_no_push(lambda payload: "memory" in str(payload.get("title", "")), duration=0.8)

    def test_exclude_keyword_filter(self):
        device = self.make_device("iPhone")
        subscription = self.make_sub([device], keyword="磁盘", exclude_keyword="测试 忽略")
        self.connect(subscription)
        self.bark.clear()

        self.publish(title="磁盘正常", message="ok", priority=3)
        self.publish(title="磁盘测试", message="excluded by 测试", priority=3)
        self.publish(title="磁盘忽略", message="excluded by 忽略", priority=3)
        self.wait_pushes(lambda payload: payload.get("title") == "磁盘正常", count=1)
        self.assert_no_push(lambda payload: payload.get("title") in ("磁盘测试", "磁盘忽略"), duration=0.8)

    def test_event_filtering(self):
        device = self.make_device("iPhone")
        subscription = self.make_sub([device])
        self.connect(subscription)
        self.bark.clear()

        self.publish(title="keepalive", message="", priority=3, event="open")
        self.publish(title="真实消息", message="hello", priority=3, event="message")
        self.wait_pushes(lambda payload: payload.get("title") == "真实消息", count=1)
        self.assert_no_push(lambda payload: payload.get("title") == "keepalive", duration=0.8)

    def test_title_prefix_and_body_truncation(self):
        device = self.make_device("iPhone")
        subscription = self.make_sub([device], title_prefix="【生产】")
        self.connect(subscription)
        self.bark.clear()

        long_title = "标题" * 100
        long_body = "B" * 2600
        self.publish(title=long_title, message=long_body, priority=3)
        payloads = self.wait_pushes(lambda payload: "【生产】" in str(payload.get("title", "")), count=1)
        payload = payloads[0]
        self.assertTrue(payload["title"].startswith("【生产】"), payload["title"])
        self.assertLessEqual(len(payload["title"]), len("【生产】") + 100, payload["title"])
        self.assertLessEqual(len(payload["body"]), 2000, len(payload["body"]))

    def test_single_device_failure_does_not_block_others(self):
        good = self.make_device("Good", bark_key="FailKey00001")
        bad = self.make_device("Bad", bark_key="FailKey00002")
        subscription = self.make_sub([good, bad])
        self.connect(subscription)
        self.bark.fail_keys.add("FailKey00002")
        self.bark.clear()

        self.publish(title="失败隔离", message="x", priority=3)
        # 正常设备应成功收到。
        self.wait_pushes(lambda payload: payload.get("device_key") == "FailKey00001", count=1)
        # 失败设备应有失败尝试记录。
        self.wait_push_attempt(lambda payload: payload.get("device_key") == "FailKey00002")
        failed = [
            record
            for record in self.bark.records()
            if record["payload"].get("device_key") == "FailKey00002" and not record["ok"]
        ]
        self.assertTrue(failed)

    def test_status_and_activity_incremental(self):
        first = self.make_device("iPhone")
        second = self.make_device("iPad")
        subscription = self.make_sub([first, second])
        self.connect(subscription)
        self.bark.clear()

        self.publish(title="消息一", message="one", priority=3)
        self.publish(title="消息二", message="two", priority=5)
        self.wait_pushes(lambda payload: payload.get("title") in ("消息一", "消息二"), count=4)

        status = self.get_status(self.admin)
        self.assertEqual(status["version"], API_VERSION)
        self.assertGreaterEqual(float(status["uptime"]), 0)
        entries = {entry["id"]: entry for entry in status["subscriptions"]}
        self.assertIn(subscription["id"], entries)
        self.assertEqual(entries[subscription["id"]]["state"], "streaming")
        self.assertIn("last_error", entries[subscription["id"]])
        totals = status["totals"]
        self.assertGreaterEqual(totals["subscriptions_active"], 1)
        self.assertGreaterEqual(totals["devices_active"], 2)
        # forwarded 以「消息条数」计（一条消息 fanout 到多台设备只 +1）。
        self.assertGreaterEqual(totals["forwarded_since_start"], 2)

        activity = self.get_activity(self.admin, since=0, limit=100)
        items = activity["items"]
        self.assertGreaterEqual(activity["last_id"], 1)
        self.assertTrue(items)
        ids = [item["id"] for item in items]
        self.assertEqual(ids, sorted(ids))
        push_items = [item for item in items if item["kind"] == "push"]
        self.assertTrue(push_items)
        for item in push_items:
            for field in ("id", "ts", "kind", "subscription_id", "subscription", "device_id", "device", "ok", "title", "error"):
                self.assertIn(field, item)
            self.assertIsInstance(item["ts"], (int, float))
            self.assertTrue(item["ok"])
        titles = {item["title"] for item in push_items}
        self.assertTrue({"消息一", "消息二"} & titles)

        # 增量拉取：since=last_id 不应重复旧记录，新推送后能拿到新记录。
        first_last_id = activity["last_id"]
        empty = self.get_activity(self.admin, since=first_last_id)
        self.assertTrue(all(item["id"] > first_last_id for item in empty["items"]))
        self.bark.clear()
        self.publish(title="消息三", message="three", priority=3)
        self.wait_pushes(lambda payload: payload.get("title") == "消息三", count=2)

        def new_items():
            data = self.get_activity(self.admin, since=first_last_id)
            matched = [item for item in data["items"] if item["id"] > first_last_id]
            return matched or None

        matched = wait_for(new_items, timeout=DEFAULT_TIMEOUT, message="增量活动日志出现新记录")
        self.assertTrue(any(item["title"] == "消息三" for item in matched))

        limited = self.get_activity(self.admin, since=0, limit=1)
        self.assertLessEqual(len(limited["items"]), 1)

    def test_reconnect_no_duplicate_push(self):
        device = self.make_device("iPhone", bark_key="DedupKey0001")
        subscription = self.make_sub([device])
        self.connect(subscription)
        self.bark.clear()

        first_message = self.publish(title="第一条", message="first", priority=3)
        self.wait_pushes(lambda payload: payload.get("title") == "第一条", count=1)

        # 让假 ntfy 在重连时无视 since 重放全部历史，验证引擎自身按时间戳去重。
        self.ntfy.replay_all = True
        self.ntfy.drop_connections()

        wait_for(
            lambda: len(self.ntfy.requests_for(self.topic)) >= 2,
            timeout=DEFAULT_TIMEOUT,
            message="断线后订阅工作线程重连 ntfy",
        )
        reconnect = self.ntfy.requests_for(self.topic)[-1]
        self.assertTrue(str(reconnect["since"]).strip().isdigit(), reconnect)
        self.assertGreater(int(reconnect["since"]), 0)

        second_message = self.publish(title="第二条", message="second", priority=3)
        self.assertGreater(int(second_message["time"]), int(first_message["time"]))
        self.wait_pushes(lambda payload: payload.get("title") == "第二条", count=1)
        time.sleep(0.6)
        duplicates = [
            payload for payload in self.bark.payloads(ok=True) if payload.get("title") == "第一条"
        ]
        self.assertEqual(len(duplicates), 1, f"重放导致重复推送：{duplicates}")
        self.assertEqual(self.ntfy.active_connections(self.topic), 1)

    def test_hot_reload_new_subscription_without_restart(self):
        device = self.make_device("iPhone")
        pid = self.app.proc.pid
        uptime_before = float(self.client(self.app).get("/api/health").data["uptime"])

        subscription = self.make_sub([device])
        started = time.monotonic()
        self.connect(subscription)
        self.assertLess(time.monotonic() - started, 6.0, "新增订阅后 6 秒内未建立 ntfy 连接")

        self.bark.clear()
        self.publish(title="热更新订阅", message="x", priority=3)
        self.wait_pushes(lambda payload: payload.get("title") == "热更新订阅", count=1)

        self.assertIsNone(self.app.proc.poll())
        self.assertEqual(self.app.proc.pid, pid)
        self.assertGreaterEqual(float(self.client(self.app).get("/api/health").data["uptime"]), uptime_before)

    def test_hot_reload_add_target_device(self):
        first = self.make_device("iPhone", bark_key="HotKey000001")
        subscription = self.make_sub([first])
        self.connect(subscription)
        self.bark.clear()

        self.publish(title="加设备前", message="x", priority=3)
        self.wait_pushes(lambda payload: payload.get("device_key") == "HotKey000001", count=1)

        second = self.make_device("iPad", bark_key="HotKey000002")
        started = time.monotonic()
        response = self.admin.put(
            f"/api/subscriptions/{subscription['id']}",
            {"device_ids": [first["id"], second["id"]]},
        )
        self.assertEqual(response.status, 200, response.text)

        self.bark.clear()
        self.publish(title="加设备后", message="y", priority=3)
        payloads = self.wait_pushes(
            lambda payload: payload.get("title") == "加设备后", count=2, timeout=8.0
        )
        elapsed = time.monotonic() - started
        self.assertLess(elapsed, 6.0, "修改目标设备后 6 秒内未生效")
        self.assertEqual(
            {payload["device_key"] for payload in payloads}, {"HotKey000001", "HotKey000002"}
        )

    def test_delete_subscription_stops_worker(self):
        device = self.make_device("iPhone", bark_key="StopKey000001")
        subscription = self.make_sub([device])
        self.connect(subscription)

        self.bark.clear()
        self.publish(title="删除前", message="x", priority=3)
        self.wait_pushes(lambda payload: payload.get("title") == "删除前", count=1)

        response = self.admin.delete(f"/api/subscriptions/{subscription['id']}")
        self.assertEqual(response.status, 200, response.text)
        wait_for(
            lambda: self.ntfy.active_connections(self.topic) == 0,
            timeout=DEFAULT_TIMEOUT,
            message="删除订阅后工作线程断开 ntfy 连接",
        )

        self.bark.clear()
        self.ntfy.publish(self.topic, title="删除后", message="y", priority=3)
        self.assert_no_push(lambda payload: payload.get("title") == "删除后", duration=1.5)
        status = self.get_status(self.admin)
        self.assertNotIn(subscription["id"], [entry["id"] for entry in status["subscriptions"]])


# ================================================================ JSON 正文 / 图片映射


class TestMessageMapping(BridgeTestCase):
    """JSON 正文展开（JSON_BODY_PARSE）与附件/封面图片映射（SEND_IMAGE）。"""

    def setUp(self):
        super().setUp()
        self.topic = self.unique("topic")

    def start_mapping_app(self, *, extra_env=None, subscription_overrides=None):
        app = self.start_app(extra_env=extra_env)
        admin = self.login(app)
        device = self.create_device(
            admin, "MappingPhone", bark_key="MapKey-" + uuid.uuid4().hex[:12]
        )
        subscription = self.create_subscription(
            admin, [device["id"]], topic=self.topic, **(subscription_overrides or {})
        )
        wait_for(
            lambda: self.ntfy.active_connections(self.topic) >= 1,
            timeout=DEFAULT_TIMEOUT,
            message="订阅工作线程连接 ntfy",
        )
        self.wait_subscription_state(admin, subscription["id"])
        self.bark.clear()
        return app, admin, device, subscription

    def publish(self, **kwargs):
        return self.ntfy.publish(self.topic, **kwargs)

    def test_json_body_is_unwrapped(self):
        app, admin, device, subscription = self.start_mapping_app()
        nested = {
            "title": "番剧更新",
            "message": "第3话",
            "priority": 5,
            "click": "https://x/ep3",
            "attach": "https://img.example.com/cover.jpg",
        }
        self.publish(message=json.dumps(nested, ensure_ascii=False), priority=3)

        payload = self.wait_pushes(lambda item: item.get("title") == "番剧更新", count=1)[0]
        self.assertEqual(payload["title"], "番剧更新")
        self.assertEqual(payload["body"], "第3话")
        self.assertEqual(payload["level"], "timeSensitive", "JSON 里的 priority=5 应被接管")
        self.assertEqual(payload["url"], "https://x/ep3")
        self.assertEqual(payload["image"], "https://img.example.com/cover.jpg")

        # 活动日志应记录展开后的标题，便于排查。
        def probe():
            data = self.get_activity(admin)
            for item in data["items"]:
                if item.get("kind") == "push" and item.get("title") == "番剧更新":
                    return item
            return None

        activity = wait_for(probe, timeout=DEFAULT_TIMEOUT, message="活动日志出现展开后的标题")
        self.assertTrue(activity["ok"], activity)

    def test_json_body_parse_disabled(self):
        app, admin, device, subscription = self.start_mapping_app(
            extra_env={"JSON_BODY_PARSE": "0"}
        )
        raw = json.dumps(
            {"title": "番剧更新", "message": "第3话", "priority": 5, "click": "https://x/ep3"},
            ensure_ascii=False,
        )
        self.publish(message=raw, priority=3)

        payload = self.wait_pushes(lambda item: item.get("title") == self.topic, count=1)[0]
        self.assertEqual(payload["title"], self.topic)
        self.assertEqual(payload["body"], raw, "关闭 JSON 解析后正文应保持原始 JSON 字符串")
        self.assertEqual(payload["level"], "active")
        self.assertNotIn("url", payload)
        self.assertNotIn("image", payload)

    def test_attachment_image_and_non_image(self):
        self.start_mapping_app()
        # 图片附件（attachment.url）→ Bark image 字段
        self.publish(
            title="封面通知",
            message="第4话",
            priority=3,
            attachment={"url": "https://img.example.com/cover.jpg", "name": "cover.jpg"},
        )
        payload = self.wait_pushes(lambda item: item.get("title") == "封面通知", count=1)[0]
        self.assertEqual(payload["image"], "https://img.example.com/cover.jpg")
        self.assertEqual(payload["body"], "第4话")
        self.assertNotIn("url", payload)

        # 非图片附件（.pdf）→ 链接追加进正文、不带 image
        self.publish(
            title="PDF附件",
            message="第5话",
            priority=3,
            attachment={"url": "https://example.com/doc.pdf"},
        )
        payload = self.wait_pushes(lambda item: item.get("title") == "PDF附件", count=1)[0]
        self.assertNotIn("image", payload)
        self.assertIn("附件：https://example.com/doc.pdf", payload["body"])
        self.assertTrue(payload["body"].startswith("第5话"), payload["body"])

        # 顶层 attach 字符串同样作为图片来源
        self.publish(
            title="attach字段",
            message="第6话",
            priority=3,
            attach="https://img.example.com/pic.png",
        )
        payload = self.wait_pushes(lambda item: item.get("title") == "attach字段", count=1)[0]
        self.assertEqual(payload["image"], "https://img.example.com/pic.png")

    def test_send_image_disabled(self):
        self.start_mapping_app(extra_env={"SEND_IMAGE": "0"})
        self.publish(
            title="封面关闭",
            message="正文",
            priority=3,
            attachment={"url": "https://img.example.com/cover.jpg"},
        )
        payload = self.wait_pushes(lambda item: item.get("title") == "封面关闭", count=1)[0]
        self.assertNotIn("image", payload)
        self.assertEqual(payload["body"], "正文")

        self.publish(
            title="PDF关闭",
            message="正文2",
            priority=3,
            attachment={"url": "https://example.com/doc.pdf"},
        )
        payload = self.wait_pushes(lambda item: item.get("title") == "PDF关闭", count=1)[0]
        self.assertNotIn("image", payload)
        self.assertEqual(payload["body"], "正文2", "SEND_IMAGE=0 时不应追加附件链接")

    def test_json_priority_drives_priority_min_filter(self):
        self.start_mapping_app(subscription_overrides={"priority_min": 4})
        low = json.dumps(
            {"title": "低优先JSON", "message": "p2", "priority": 2}, ensure_ascii=False
        )
        high = json.dumps(
            {"title": "高优先JSON", "message": "p5", "priority": 5}, ensure_ascii=False
        )
        self.publish(message=low, priority=3)
        self.publish(message=high, priority=3)

        self.wait_pushes(lambda item: item.get("title") == "高优先JSON", count=1)
        self.assert_no_push(lambda item: item.get("title") == "低优先JSON", duration=0.8)


# ================================================================ 静态页与错误


class TestStaticAndErrors(BridgeTestCase):
    def setUp(self):
        super().setUp()
        self.app = self.start_app()
        self.client_app = self.client(self.app)

    def test_index_returns_webui(self):
        if not os.path.exists(WEBUI_PATH):
            self.skipTest("webui.html 尚未提供，跳过静态页断言")
        response = self.client_app.get("/")
        self.assertEqual(response.status, 200, response.text)
        content_type = response.header("Content-Type", "")
        self.assertIn("text/html", content_type.lower())
        self.assertIn("charset=utf-8", content_type.lower())
        self.assertIn("no-cache", response.header("Cache-Control", "").lower())
        self.assertGreater(len(response.body), 200)
        self.assertIn(b"<html", response.body.lower())
        self.assertIn(b"bark", response.body.lower())
        # 前端确实引用了契约里的 API，且是中文登录界面。
        self.assertIn(b"/api/", response.body)
        self.assertIn("登录".encode("utf-8"), response.body)

    def test_favicon_204(self):
        response = self.client_app.get("/favicon.ico")
        self.assertEqual(response.status, 204, response.text)
        self.assertEqual(response.body, b"")

    def test_404_formats(self):
        for path in ("/api/does-not-exist", "/no-such-page"):
            response = self.client_app.get(path)
            self.assert_error(response, status=404, code="not_found")
        post = self.client_app.post("/api/nope", {})
        self.assert_error(post, status=404, code="not_found")

    def test_invalid_json_body(self):
        response = self.client_app.request(
            "POST",
            "/api/login",
            raw_body=b"{not-valid-json",
            headers={"Content-Type": "application/json"},
        )
        self.assert_error(response, status=400, code="invalid_request")

    def test_payload_too_large(self):
        oversized = b'{"username":"admin","password":"' + b"A" * (70 * 1024) + b'"}'
        response = self.client_app.request(
            "POST",
            "/api/login",
            raw_body=oversized,
            headers={"Content-Type": "application/json"},
        )
        self.assert_error(response, status=(400, 413), code="payload_too_large")

    def test_security_headers(self):
        response = self.client_app.get("/api/health")
        self.assertEqual(response.header("X-Content-Type-Options"), "nosniff")
        self.assertEqual(response.header("X-Frame-Options"), "DENY")
        self.assertEqual(response.header("Referrer-Policy"), "no-referrer")

        index = self.client_app.get("/")
        csp = index.header("Content-Security-Policy", "")
        self.assertIn("default-src 'none'", csp)
        self.assertIn("connect-src 'self'", csp)


# ================================================================ 设置


class TestSettings(AdminAppCase):
    def test_get_and_update_settings(self):
        response = self.admin.get("/api/settings")
        self.assertEqual(response.status, 200, response.text)
        data = response.data
        self.assertEqual(data["version"], API_VERSION)
        defaults = data["defaults"]
        for key in ("ntfy_url", "bark_server", "since"):
            self.assertIn(key, defaults)
        limits = data["limits"]
        self.assertIn("max_body", limits)
        self.assertIsInstance(limits["max_body"], int)
        features = data["features"]
        self.assertTrue(features["multi_device"])
        self.assertTrue(features["users"])
        self.assertTrue(features["activity"])

        update = self.admin.put(
            "/api/settings",
            {
                "default_ntfy_url": "http://127.0.0.1:12345",
                "default_bark_server": "https://bark.example.com",
                "default_since": "10m",
            },
        )
        self.assertEqual(update.status, 200, update.text)
        updated = update.data
        updated_defaults = updated.get("defaults", updated)
        self.assertEqual(updated_defaults["ntfy_url"], "http://127.0.0.1:12345")
        self.assertEqual(updated_defaults["bark_server"], "https://bark.example.com")
        self.assertEqual(updated_defaults["since"], "10m")

        persisted = self.admin.get("/api/settings").data["defaults"]
        self.assertEqual(persisted["ntfy_url"], "http://127.0.0.1:12345")
        self.assertEqual(persisted["bark_server"], "https://bark.example.com")
        self.assertEqual(persisted["since"], "10m")

    def test_non_admin_cannot_update_settings(self):
        self.make_user(self.admin, "carol")
        carol = self.login(self.app, "carol", "UserPass123!")
        self.assertEqual(carol.get("/api/settings").status, 200)
        self.assert_error(
            carol.put("/api/settings", {"default_ntfy_url": "http://evil.example.com"}),
            status=403,
            code="forbidden",
        )


# ================================================================ 在线更新


class TestUpdate(BridgeTestCase):
    """在线更新端到端：假 GitHub + 程序副本 + 更新后原地重启。

    每个用例把仓库里的 bark-bridge.py / webui.html 复制到临时目录，通过
    ``APP_FILE`` / ``WEBUI_FILE`` 让子进程运行副本；``UPDATE_*`` 环境变量把
    GitHub API 与 codeload 指向测试进程内的假服务器，全程不访问外网。
    """

    def setUp(self):
        super().setUp()
        self.github = FakeGithub()
        self.addCleanup(self.github.close)

        self.app_dir = make_temp_dir("bb-update-")
        self.addCleanup(remove_tree, self.app_dir)
        self.app_copy = os.path.join(self.app_dir, "bark-bridge.py")
        self.webui_copy = os.path.join(self.app_dir, "webui.html")
        shutil.copyfile(APP_PATH, self.app_copy)
        shutil.copyfile(WEBUI_PATH, self.webui_copy)
        with open(self.app_copy, "rb") as handle:
            self.local_python = handle.read()
        with open(self.webui_copy, "rb") as handle:
            self.local_webui = handle.read()

    # -- 辅助 -------------------------------------------------------------
    def start_update_app(self, *, extra_env=None):
        env = {
            "APP_FILE": self.app_copy,
            "WEBUI_FILE": self.webui_copy,
            "UPDATE_ENABLED": "1",
            "UPDATE_REPO": self.github.repo,
            "UPDATE_BRANCH": self.github.branch,
            "UPDATE_API_BASE": self.github.url,
            "UPDATE_CODELOAD_BASE": self.github.url,
        }
        if extra_env:
            env.update(extra_env)
        return self.start_app(
            extra_env=env,
            app_file=self.app_copy,
            webui_file=self.webui_copy,
            cwd=self.app_dir,
        )

    def wait_update_ready(self, client, timeout=15.0):
        """等待 /api/update 返回远端检查结果（启动预热或首次按需检查）。"""

        def probe():
            response = client.get("/api/update")
            if response.status != 200:
                return None
            data = response.data or {}
            return data if data.get("remote") is not None else None

        return wait_for(probe, timeout=timeout, message="GET /api/update 返回远端检查结果")

    def wait_health_version(self, app, version, timeout=30.0):
        """轮询 /api/health 直到版本变为 version；重启瞬间的连接重置可容忍。"""

        def probe():
            try:
                with urllib.request.urlopen(app.base + "/api/health", timeout=1.0) as response:
                    payload = json.loads(response.read().decode("utf-8"))
            except Exception:  # noqa: BLE001 - execv 重启期间连接会被重置
                return None
            data = payload.get("data") or {}
            return data if data.get("version") == version else None

        try:
            return wait_for(
                probe, timeout=timeout, interval=0.1,
                message=f"/api/health 版本变为 {version}",
            )
        except AssertionError as exc:
            raise AssertionError(f"{exc}\n应用日志尾部:\n{app.log_tail()}") from None

    def assert_files_unchanged(self):
        with open(self.app_copy, "rb") as handle:
            self.assertEqual(handle.read(), self.local_python, "bark-bridge.py 不应被破坏")
        with open(self.webui_copy, "rb") as handle:
            self.assertEqual(handle.read(), self.local_webui, "webui.html 不应被破坏")

    def existing_backup_dir(self, app, backup_dir):
        candidates = [str(backup_dir)]
        if not os.path.isabs(str(backup_dir)):
            candidates.append(os.path.join(app.data_dir, str(backup_dir)))
        candidates.append(
            os.path.join(app.data_dir, os.path.basename(str(backup_dir).rstrip("/\\")))
        )
        for candidate in candidates:
            if os.path.isdir(candidate):
                return candidate
        self.fail(f"备份目录不存在：{backup_dir!r}（DATA_DIR={app.data_dir}）")

    def retire_restarted_process(self, app, admin):
        """Windows 下让更新重启产生的新进程自行退出，便于测试目录清理。

        测试沙箱不允许测试进程按 PID 终止进程（os.kill / taskkill 均 Access
        denied），而 Windows 上 os.execv 会留下一个独立的新进程，它会占住
        DATA_DIR / cwd 导致临时目录删不掉。这里让假 GitHub 再发布一个「版本号
        更高、编译通过但启动即退出」的合法包，触发一次重启让新进程自行结束。
        POSIX 上 os.execv 原地替换（PID 不变），无需该步骤。
        """
        if os.name != "nt":
            return
        self.github.mode = "poison"
        response = admin.post("/api/update/apply", {})
        self.assertEqual(response.status, 202, f"触发清理重启失败：{response.text}")
        wait_for(
            lambda: not app.listening_pids(),
            timeout=20.0,
            message="更新重启出的测试进程应自行退出",
        )

    # -- 用例 -------------------------------------------------------------
    def test_get_update_status(self):
        app = self.start_update_app()
        admin = self.login(app)
        data = self.wait_update_ready(admin)

        self.assertEqual(data["current_version"], self.github.current_version, data)
        self.assertEqual(data["current_hash"], self.github.local_hash(), data)
        self.assertEqual(data["repo"], self.github.repo, data)
        self.assertEqual(data["branch"], self.github.branch, data)
        self.assertTrue(data["enabled"], data)
        self.assertIsInstance(data["checked_at"], int, data)
        self.assertGreater(data["checked_at"], 0, data)
        self.assertIsInstance(data["cached"], bool, data)
        self.assertIsInstance(data["error"], str, data)

        remote = data["remote"]
        self.assertTrue(remote["available"], remote)
        self.assertEqual(remote["version"], self.github.remote_version, remote)
        self.assertEqual(remote["hash"], self.github.remote_hash(), remote)
        self.assertNotEqual(data["current_hash"], remote["hash"], remote)
        self.assertTrue(str(remote["commit"]).startswith("9f2c1ab"), remote)
        self.assertEqual(remote["subject"], self.github.subject, remote)
        self.assertEqual(remote.get("committed_at"), self.github.committed_at, remote)

        apply_info = data["apply"]
        self.assertTrue(apply_info["supported"], data)
        self.assertEqual(apply_info["reason"], "", data)

        # GET 命中缓存：短时间内再次获取不应再请求远端。
        tar_before, commit_before = self.github.requests()
        again = admin.get("/api/update")
        self.assertEqual(again.status, 200, again.text)
        self.assertTrue(again.data["cached"], again.text)
        tar_after, commit_after = self.github.requests()
        self.assertEqual(len(tar_after), len(tar_before), "缓存期内 GET 不应重新下载源码包")
        self.assertEqual(len(commit_after), len(commit_before), "缓存期内 GET 不应重新请求 commits")

    def test_force_check_refreshes_cache(self):
        app = self.start_update_app()
        admin = self.login(app)
        self.wait_update_ready(admin)

        tar_before, _ = self.github.requests()
        response = admin.post("/api/update/check", {})
        self.assertEqual(response.status, 200, response.text)
        data = response.data
        self.assertFalse(data["cached"], f"强制检查不应返回缓存：{response.text}")
        self.assertTrue(data["remote"]["available"], data)
        self.assertEqual(data["remote"]["version"], self.github.remote_version, data)
        tar_after, _ = self.github.requests()
        self.assertGreater(len(tar_after), len(tar_before), "强制检查应重新下载源码包")

        cached = admin.get("/api/update")
        self.assertEqual(cached.status, 200, cached.text)
        self.assertTrue(cached.data["cached"], cached.text)

    def test_update_permissions(self):
        app = self.start_update_app()
        anonymous = self.client(app)
        self.assert_error(anonymous.get("/api/update"), status=401, code="unauthorized")
        self.assert_error(anonymous.post("/api/update/check", {}), status=401, code="unauthorized")
        self.assert_error(anonymous.post("/api/update/apply", {}), status=401, code="unauthorized")

        admin = self.login(app)
        self.make_user(admin, "updater")
        user = self.login(app, "updater", "UserPass123!")
        self.assert_error(user.get("/api/update"), status=403, code="forbidden")
        self.assert_error(user.post("/api/update/check", {}), status=403, code="forbidden")
        self.assert_error(user.post("/api/update/apply", {}), status=403, code="forbidden")
        self.assertIsNone(app.proc.poll())

    def test_update_disabled(self):
        app = self.start_update_app(extra_env={"UPDATE_ENABLED": "0"})
        admin = self.login(app)
        response = admin.get("/api/update")
        self.assertEqual(response.status, 200, response.text)
        self.assertFalse(response.data["enabled"], response.text)
        self.assert_error(
            admin.post("/api/update/apply", {}), status=400, code="update_disabled"
        )
        self.assert_files_unchanged()
        self.assertIsNone(app.proc.poll())

    def test_apply_remote_404_fails_cleanly(self):
        app = self.start_update_app()
        admin = self.login(app)
        self.wait_update_ready(admin)

        self.github.mode = "404"
        response = admin.post("/api/update/apply", {})
        self.assert_error(response, status=502, code="update_failed")
        self.assert_files_unchanged()
        self.assertIsNone(app.proc.poll(), "更新失败后进程应继续运行")

        health = self.client(app).get("/api/health")
        self.assertEqual(health.status, 200, health.text)
        self.assertEqual(health.data["version"], self.github.current_version)

        update = admin.get("/api/update")
        self.assertEqual(update.status, 200, update.text)
        self.assertTrue(update.data["error"], f"检查失败时 error 应非空：{update.text}")

    def test_apply_corrupt_packages_fail_cleanly(self):
        app = self.start_update_app()
        admin = self.login(app)
        self.wait_update_ready(admin)

        for mode in ("garbage", "broken_python", "broken_webui"):
            self.github.mode = mode
            response = admin.post("/api/update/apply", {})
            self.assert_error(response, status=502, code="update_failed")
            self.assert_files_unchanged()
            self.assertIsNone(app.proc.poll(), f"mode={mode} 更新失败后进程应继续运行")

        health = self.client(app).get("/api/health")
        self.assertEqual(health.status, 200, health.text)
        self.assertEqual(health.data["version"], self.github.current_version)

    def test_apply_success_restarts_with_new_version(self):
        app = self.start_update_app()
        admin = self.login(app)
        data = self.wait_update_ready(admin)
        self.assertTrue(data["remote"]["available"], data)

        device = self.create_device(admin, "UpdatePhone", bark_key="UpdateKey001")
        pid_before = app.proc.pid
        self.assertEqual(admin.get("/api/me").status, 200)

        response = admin.post("/api/update/apply", {})
        self.assertEqual(response.status, 202, response.text)
        result = response.data
        self.assertTrue(result["restarting"], result)
        self.assertEqual(result["from_version"], self.github.current_version, result)
        self.assertEqual(result["to_version"], self.github.remote_version, result)
        self.assertIsInstance(result["backup_dir"], str, result)
        self.assertTrue(result["backup_dir"], result)

        # 原地重启：约 0.3s 不可用后 /api/health 报告新版本。
        health = self.wait_health_version(app, self.github.remote_version, timeout=30.0)
        self.assertEqual(health["version"], self.github.remote_version, health)
        if os.name == "nt":
            # Windows 上 CPython 的 os.execv 由 CRT 实现为「启动新进程 + 旧进程退出」，
            # PID 会变化；契约里「PID 不变」的原地重启语义只在 POSIX 上成立。
            self.assertIsNotNone(app.proc.poll(), "Windows 上更新后旧进程应已退出")
            new_pids = app.adopt_listening_processes()
            self.assertTrue(new_pids, "更新重启后应有新进程监听同一端口")
        else:
            self.assertIsNone(app.proc.poll(), "重启后应用进程应存活")
            self.assertEqual(app.proc.pid, pid_before, "os.execv 原地重启后 PID 应保持不变")

        # 程序文件与 WebUI 已被替换为远端内容。
        with open(self.app_copy, "rb") as handle:
            self.assertEqual(handle.read(), self.github.remote_python, "bark-bridge.py 未更新")
        with open(self.webui_copy, "rb") as handle:
            self.assertEqual(handle.read(), self.github.remote_webui, "webui.html 未更新")

        # 旧文件已备份到 <DATA_DIR>/update-backup-*/。
        backup_dir = self.existing_backup_dir(app, result["backup_dir"])
        backup_app = os.path.join(backup_dir, "bark-bridge.py")
        self.assertTrue(os.path.isfile(backup_app), f"备份目录缺少旧 bark-bridge.py：{backup_dir}")
        with open(backup_app, "rb") as handle:
            self.assertEqual(handle.read(), self.local_python, "备份的旧 bark-bridge.py 内容不对")
        backup_webui = os.path.join(backup_dir, "webui.html")
        self.assertTrue(os.path.isfile(backup_webui), f"备份目录缺少旧 webui.html：{backup_dir}")
        with open(backup_webui, "rb") as handle:
            self.assertEqual(handle.read(), self.local_webui, "备份的旧 webui.html 内容不对")

        # 会话密钥与数据都在同一 DB：重启后旧 Cookie 仍有效、设备仍存在。
        me = admin.get("/api/me")
        self.assertEqual(me.status, 200, me.text)
        self.assertEqual(me.data["version"], self.github.remote_version, me.text)
        self.assertIsNotNone(self.get_device(admin, device["id"]), "重启后设备数据应保留")

        # 更新完成后远端不再「有新版本」。
        update = admin.get("/api/update")
        self.assertEqual(update.status, 200, update.text)
        self.assertEqual(update.data["current_version"], self.github.remote_version, update.text)
        self.assertFalse(update.data["remote"]["available"], update.text)
        self.assertEqual(update.data["remote"]["version"], self.github.remote_version, update.text)
        self.assertEqual(update.data["remote"]["hash"], self.github.remote_hash(), update.text)

        # 已是最新时再次 apply → 400 invalid_request。
        self.assert_error(
            admin.post("/api/update/apply", {}), status=400, code="invalid_request"
        )

        # Windows 清理：让重启出的新进程自行退出（详见 helper 注释）。
        self.retire_restarted_process(app, admin)

    def test_apply_when_already_up_to_date(self):
        self.github.mode = "same"
        app = self.start_update_app()
        admin = self.login(app)
        data = self.wait_update_ready(admin)
        self.assertFalse(data["remote"]["available"], data)
        self.assertEqual(data["current_hash"], data["remote"]["hash"], data)

        self.assert_error(
            admin.post("/api/update/apply", {}), status=400, code="invalid_request"
        )
        self.assert_files_unchanged()
        self.assertIsNone(app.proc.poll())

    def test_remote_repository_missing_webui(self):
        """真实仓库还是 v1（缺 webui.html、无 __version__）时应给出 warning 而不是报错。"""
        self.github.mode = "missing_webui"
        app = self.start_update_app()
        admin = self.login(app)
        data = self.wait_update_ready(admin)

        remote = data["remote"]
        self.assertFalse(remote["available"], data)
        self.assertTrue(remote["behind"], data)
        self.assertEqual(remote["missing"], ["webui.html"], data)
        self.assertEqual(remote["version"], "0.0.0", data)
        self.assertTrue(data.get("warning"), f"缺少 webui.html 时应有 warning：{data}")
        self.assertEqual(data.get("error"), "", data)  # 不是错误，只是「远端落后」
        self.assert_files_unchanged()

        response = admin.post("/api/update/apply", {})
        self.assert_error(response, status=400, code="invalid_request")
        self.assertIn("webui", response.json.get("error", ""), response.text)
        self.assert_files_unchanged()
        self.assertIsNone(app.proc.poll())


if __name__ == "__main__":
    unittest.main(verbosity=2)
