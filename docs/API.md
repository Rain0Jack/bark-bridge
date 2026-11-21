# bark-bridge v2 设计契约（后端 / WebUI 共用）

本文是 `bark-bridge.py`（后端）与 `webui.html`（前端）之间的**唯一契约**。后端按此实现，
前端只依赖本文，不依赖后端源码细节。若实现中发现契约需要调整，必须同时更新本文。

- 版本：`2.0.0`
- 运行时：Python 3.11+ 标准库（无第三方依赖），单进程，SQLite 持久化
- 传输：HTTP/1.1 + JSON（UTF-8），同源，不使用 CORS

## 1. 运行模型

```
ntfy 服务器 ──JSON 流──► 订阅工作线程 × N ──HTTP /push──► Bark 服务器 ──APNs──► iPhone/iPad
                              │
                       用户/设备/订阅/权限 全部持久化在 SQLite
                              │
       浏览器 ◄──HTTP/JSON──► 内置 Web 服务器（WebUI + REST API）
```

- 一个进程同时提供 **转发引擎** 和 **WebUI/API**，默认端口 `8080`。
- 每条订阅（ntfy topic）一个工作线程，断线自动重连，用最后消息时间戳续传。
- 配置改动（增删改订阅/设备）通过 API 写入数据库后**最多 1~2 秒内自动生效**，无需重启。
- 无外部依赖：仅 `sqlite3`、`http.server`、`urllib`、`hashlib`、`hmac`、`threading` 等标准库。

## 2. 认证与安全

- 首次运行（无任何用户）时 `GET /api/health` 返回 `needs_setup: true`，前端显示「创建管理员」表单，
  `POST /api/setup` 创建第一个管理员后关闭该入口。若设置了环境变量 `ADMIN_PASSWORD`，启动时自动创建
  `ADMIN_USER` 管理员，`needs_setup` 为 `false`。
- 登录成功后下发两个 Cookie（`Path=/`，`SameSite=Lax`）：
  - `bb_session`：`HttpOnly`，HMAC-SHA256 签名，载荷含用户 id、过期时间、密码版本；`Max-Age=SESSION_TTL`。
  - `bb_csrf`：非 HttpOnly，随机值，供前端读取。
- **所有** `POST/PUT/DELETE/PATCH` 请求（`/api/login`、`/api/setup` 除外）必须带请求头
  `X-CSRF-Token: <bb_csrf cookie 的值>`，否则 `403 csrf_failed`。
- 密码：PBKDF2-HMAC-SHA256（210000 轮，16 字节随机盐），只存哈希；修改密码后旧会话立即失效。
- 登录失败限流：同一 `IP|用户名` 5 分钟内最多 8 次失败，超限 `429 rate_limited`。
- 安全响应头：`X-Content-Type-Options: nosniff`、`X-Frame-Options: DENY`、`Referrer-Policy: no-referrer`、
  `Content-Security-Policy: default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; img-src data:; connect-src 'self'; base-uri 'none'; form-action 'none'`。
- 角色：`admin` / `user`。`user` 只能读写自己的设备与订阅；`admin` 可读写全部，并可管理用户。
- Bark Key、ntfy Token 属于凭据：`bark_key` 仅返回给有权限的登录用户；`token` **永不回传**，
  只回传 `has_token: bool`。

## 3. 统一响应格式

成功（`200`/`201`，永远返回 JSON 对象，不返回裸数组）：

```json
{"ok": true, "data": { ... }}
```

失败（`4xx`/`5xx`）：

```json
{"ok": false, "error": "面向用户的中文提示", "code": "invalid_request"}
```

`code` 取值：`invalid_request` `unauthorized` `forbidden` `not_found` `conflict` `rate_limited`
`csrf_failed` `last_admin` `invalid_credentials` `needs_setup` `payload_too_large`
`method_not_allowed` `push_failed` `internal`。

未认证访问受保护接口返回 `401` + `code: "unauthorized"`，前端据此回到登录页。
请求头 `Content-Type: application/json` 必需；请求体上限 64 KiB（`payload_too_large`）。
若响应 `Content-Type` 为文本且客户端接受 gzip，后端可返回 `Content-Encoding: gzip`。

## 4. 数据结构

### 4.1 User

```json
{"id": 1, "username": "admin", "role": "admin", "is_active": true, "created_at": 1730000000}
```

规则：`username` 匹配 `^[A-Za-z0-9_.-]{2,32}$`；密码长度 8~128；角色仅 `admin`/`user`；
不能停用/删除自己，也不能删除或降级最后一个启用的管理员（`last_admin`）。

### 4.2 Device（一台 iOS 设备 = 一个 Bark Key）

```json
{
  "id": 1, "owner_id": 1, "owner": "admin", "name": "iPad",
  "bark_key": "AbCdEf123456", "server": "https://api.day.app", "group": "ntfy",
  "level": "", "icon": "", "is_active": true,
  "created_at": 1730000000, "updated_at": 1730000000,
  "pushed_count": 0, "last_push_at": 0, "last_error": ""
}
```

- `name` 1~64 字符；`bark_key` 4~128 个非空白字符；`server` 必须 `http://`/`https://` 开头（去掉尾部 `/`）；
  `group` 0~64；`level` ∈ `""`、`active`、`timeSensitive`、`passive`、`critical`（空 = 按 ntfy 优先级映射）；
  `icon` 为空或 URL。
- `pushed_count` / `last_push_at` / `last_error` 为运行统计（重启后 `pushed_count` 从 0 开始，其余来自内存）。

### 4.3 Subscription（一条 ntfy topic 订阅，可推送多台设备）

```json
{
  "id": 1, "owner_id": 1, "owner": "admin", "name": "告警",
  "ntfy_url": "http://127.0.0.1:40265", "topic": "alerts", "has_token": true,
  "since": "1m", "priority_min": 0, "keyword": "", "exclude_keyword": "",
  "title_prefix": "", "is_active": true, "device_ids": [1, 2],
  "created_at": 1730000000, "updated_at": 1730000000,
  "stats": {"state": "streaming", "last_error": "", "forwarded": 12, "last_message_at": 1730000000}
}
```

- `name` 1~64；`ntfy_url` 缺省用设置里的 `default_ntfy_url`；`topic` 1~64，只允许
  `^[A-Za-z0-9_.-]{1,64}$`（ntfy topic 本身不含 `/`）；`since` ∈ `1m`/`10m`/`1h`/`all` 等 ntfy 支持的值（1~32 字符）；
  `priority_min` 0~5（0 = 不过滤）；`keyword`/`exclude_keyword` 0~256（空格分隔多个词）；`title_prefix` 0~32。
- `owner_id` 只在创建（`POST`）时生效，供管理员为其他用户创建记录；`PUT` 不支持转移归属，会忽略该字段。
- `device_ids`：目标设备，必须是当前用户自己的（管理员可跨用户），否则 `403 forbidden`。
- `stats.state` ∈ `stopped` `connecting` `streaming` `reconnecting` `error`；**只读**，由引擎填充。
- 首次连接用 `since` 回溯；之后用引擎里记录的最后时间戳续传。
- 消息过滤（按顺序）：`event` 必须为 `message`；按 ntfy 消息 `id` 去重（同一条消息不会重复转发）；
  `time` 不早于游标（同一秒内的新消息不会被丢弃）；`priority >= priority_min`；
  `keyword` 的每个空格分隔词都要出现在「标题+正文」中（不区分大小写）；命中任一 `exclude_keyword` 词则丢弃。
- 游标（最后处理的消息时间戳）落库节流写入（≥5 秒一次，流关闭时强制写），重启后用它续传；
  同一秒内已转发的消息在重连时按 `id` 去重，不会重复推送。
- 正文是 JSON 文档（`JSON_BODY_PARSE=1`，默认开）：若 `message` 以 `{` 开头且能解析出 `title`/`message`/`body`/`text`
  等字段，则展开成真正的标题与正文，并把其中的 `priority`/`click`/`attach` 一并接管
  （ani-rss 之类把 JSON POST 到 topic 根路径的场景，避免 iOS 上显示一坨 JSON）。
- 附件与封面（`SEND_IMAGE=1`，默认开）：ntfy 的 `attachment.url`（或 JSON 正文里的 `attach`/`image`/`icon`）
  若是图片（`.png/.jpg/.jpeg/.webp/.gif/.heic/.bmp/.avif`），作为 Bark 的 `image` 字段下发；
  非图片附件则把链接追加到正文。
- 转发到设备 `d` 的 Bark 载荷：

```json
{"device_key": "d.bark_key", "title": "前缀 + 标题(最多100字)", "body": "正文(最多 MAX_BODY 字)",
 "level": "d.level 或 按优先级映射", "group": "d.group", "url": "ntfy click（若有）",
 "icon": "d.icon（若有）", "image": "附件/封面图片 URL（若有）"}
```

  优先级映射：`priority >= 4 → timeSensitive`，否则 `active`。设备 `level` 非空时覆盖映射。
- 多设备分发：同一消息并发推送到所有目标设备（线程池，默认 4 并发），单台失败不影响其他设备。

## 5. REST API

下表中的「权限」：`公开` = 无需登录；`登录` = 任意登录用户；`管理员` = `role=admin`。
列表接口对 `user` 只返回自己的记录；对 `admin` 返回全部（含 `owner_id`/`owner`）。

### 5.1 系统与认证

| 方法 | 路径 | 权限 | 说明 |
| --- | --- | --- | --- |
| GET | `/api/health` | 公开 | `{version, uptime, now, needs_setup}` |
| POST | `/api/setup` | 公开（仅无用户时） | `{username?, password}` → `{user, csrf}`，同时登录；已有用户时 `409 conflict` |
| POST | `/api/login` | 公开 | `{username, password}` → `{user, csrf}`；失败 `401 invalid_credentials`，限流 `429` |
| POST | `/api/logout` | 公开 | 清除 Cookie → `{}` |
| GET | `/api/me` | 登录 | `{user, csrf, version, is_admin}` |
| POST | `/api/password` | 登录 | `{old_password, new_password}` → `{}`（其他会话立即失效） |
| GET | `/api/settings` | 登录 | `{version, defaults:{ntfy_url, bark_server, since}, limits:{max_body}, features:{multi_device, users, activity}}` |
| PUT | `/api/settings` | 管理员 | `{default_ntfy_url?, default_bark_server?, default_since?}` → `{defaults}`，持久化 |

### 5.2 设备

| 方法 | 路径 | 权限 | 说明 |
| --- | --- | --- | --- |
| GET | `/api/devices` | 登录 | `{devices:[Device]}` |
| POST | `/api/devices` | 登录 | `{name, bark_key, server?, group?, level?, icon?, is_active?, owner_id?}` → `201 {device}` |
| PUT | `/api/devices/{id}` | 登录（本人/管理员） | 同上，字段可省略（省略 = 不变）；`bark_key` 省略表示不改 → `{device}` |
| DELETE | `/api/devices/{id}` | 登录（本人/管理员） | `{}`，同时解除订阅关联 |
| POST | `/api/devices/{id}/test` | 登录（本人/管理员） | 推一条测试通知 → `{sent: true, detail: "..."}`；推送失败 `502` |

### 5.3 订阅

| 方法 | 路径 | 权限 | 说明 |
| --- | --- | --- | --- |
| GET | `/api/subscriptions` | 登录 | `{subscriptions:[Subscription]}` |
| POST | `/api/subscriptions` | 登录 | `{name, ntfy_url?, topic, token?, since?, priority_min?, keyword?, exclude_keyword?, title_prefix?, is_active?, device_ids?, owner_id?}` → `201 {subscription}` |
| PUT | `/api/subscriptions/{id}` | 登录（本人/管理员） | 字段可省略；`token` **省略/null = 不变**，`""` = 清除 → `{subscription}` |
| DELETE | `/api/subscriptions/{id}` | 登录（本人/管理员） | `{}`，工作线程在 2 秒内停止 |
| POST | `/api/subscriptions/{id}/test` | 登录（本人/管理员） | `{title?, body?}` → 向该订阅的所有目标设备推测试消息 → `{sent: n, failed: m}` |

### 5.4 用户管理（管理员）

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/users` | `{users:[{id, username, role, is_active, created_at, device_count, subscription_count}]}` |
| POST | `/api/users` | `{username, password, role?, is_active?}` → `201 {user}` |
| PUT | `/api/users/{id}` | `{username?, password?, role?, is_active?}`（改密码会让该用户所有会话失效）→ `{user}` |
| DELETE | `/api/users/{id}` | `{}`（级联删除其设备/订阅；不能删自己、不能删最后一个管理员） |

### 5.5 实时状态与活动日志

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/status` | `{version, uptime, subscriptions:[{id, state, last_error, forwarded, last_message_at}], totals:{subscriptions_active, devices_active, forwarded_since_start}}` |
| GET | `/api/activity?since=0&limit=100` | `{items:[Activity], last_id}`，最多保留 200 条（内存环形缓冲，重启清空） |

Activity 条目：

```json
{"id": 42, "ts": 1730000000.5, "kind": "push", "subscription_id": 1, "subscription": "告警",
 "device_id": 2, "device": "iPhone", "ok": true, "title": "磁盘告警", "error": ""}
```

`kind` ∈ `push`（转发到某设备）、`test`（手动测试）、`stream`（流状态变化，此时 `device_id=null`、
`ok=false` 表示异常）。前端每 2 秒用 `since=<上次 last_id>` 增量拉取，实现「实时日志」。

### 5.6 静态页面

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/` | `webui.html`（`text/html; charset=utf-8`，`Cache-Control: no-cache`） |
| GET | `/favicon.ico` | `204` |
| 其他 | `/...` | `404` JSON（`not_found`） |

### 5.7 在线更新（管理员）

从 GitHub 仓库拉取最新代码并在进程内完成替换与重启。仅管理员可用，默认开启（`UPDATE_ENABLED=0` 可关闭）。

- 代码来源：先试 `https://codeload.github.com/{repo}/tar.gz/refs/heads/{branch}`，404 时再试 `refs/tags/{branch}`，
  因此 `UPDATE_BRANCH` 也可以填 tag（如 `v2.1.0`）；只取其中的 `bark-bridge.py` 与 `webui.html`；
  提交信息（可选展示）来自 `https://api.github.com/repos/{repo}/commits/{branch}`，取不到不影响更新判断。
- 「有新版本」判定：远端 `bark-bridge.py` 的 SHA-256 与本地不同（同版本号但内容变了也算），且远端版本不低于本地。
  远端仍是旧版 v1（脚本里没有 `__version__`）时按 `0.0.0` 处理，只提示「远端版本更低、可能没推送」，**拒绝降级**。
- 检查结果缓存 10 分钟（`GET` 用缓存，`POST /api/update/check` 强制刷新）；缓存写在 settings 表，重启后仍有效。
  启动后约 6 秒会后台预热检查一次，之后每 6 小时一次（`UPDATE_AUTOCHECK=0` 可关闭自动检查，仍可手动检查）。
- 应用更新前会校验新文件：`bark-bridge.py` 必须能被 `compile()` 通过、`webui.html` 必须含 `<script` 与 `/api/`；
  旧文件备份到 `<DATA_DIR>/update-backup-<YYYYmmdd-HHMMSS>/`（含 `meta.json`），写入用临时文件 + `os.replace` 原子替换。
- 替换成功后进程用 `os.execv` 原地重启（POSIX 上 PID 不变；Windows 会启动新进程，PID 变化、端口不变），
  环境变量保留，约 1~2 秒不可用；数据库/配置不受影响。
- 若程序目录只读或进程无写权限（例如 Docker 只读挂载/单文件挂载），`apply.supported=false`，`reason` 给出中文提示；
  在线更新要求**目录挂载**（如 `- ./:/app`），不要用 `:ro` 单文件挂载。

`GET /api/update`（管理员，可用缓存）与 `POST /api/update/check`（管理员，强制刷新）返回：

```json
{"current_version": "2.0.0", "current_hash": "ab12cd34", "repo": "Rain0Jack/bark-bridge",
 "branch": "main", "enabled": true, "checked_at": 1730000000, "cached": true,
 "remote": {"available": true, "behind": false, "version": "2.1.0", "hash": "ff00aa11",
            "commit": "9f2c1ab", "subject": "feat: 支持多设备",
            "committed_at": "2026-11-21T10:00:00Z", "missing": []},
 "apply": {"supported": true, "reason": ""},
 "warning": "", "error": "",
 "last_applied": {"from_version": "2.0.0", "to_version": "2.1.0", "at": 1730000000,
                  "backup_dir": "/app/data/update-backup-20261121-210500", "commit": "9f2c1ab"}}
```

- `remote` 在还没有有效检查结果时为 `null`；`error` 非空表示检查失败（网络/仓库/解析），前端展示提示但保留旧缓存。
- `remote.behind=true` 表示远端版本比本地低（通常是本地改了还没 push），此时 `available=false`，`warning` 给出中文说明。
- `remote.missing` 非空表示远端源码包缺少文件（例如仓库还是 v1、没有 `webui.html`）：同样 `available=false` +
  `behind=true`，`warning` 提示先把新版本 push 到 GitHub，避免把旧版「更新」下来。
- `current_hash` / `remote.hash` 为各自 `bark-bridge.py` 的 SHA-256 前 8 位。
- `last_applied` 为最近一次成功应用更新的记录（没有则为 `null`；重启后仍保留，可用于「已更新到 vX」提示）。

`POST /api/update/apply`（管理员）：

- 请求体可省略；先强制检查：已是最新 → `400 invalid_request`（"已是最新版本"）；远端更低 → `400 invalid_request`（拒绝降级）；
  检查失败 → `502 update_failed`（带原因）。
- 成功时立即返回 `202`：

```json
{"restarting": true, "from_version": "2.0.0", "to_version": "2.1.0",
 "backup_dir": "/app/data/update-backup-20261121-210500"}
```

随后约 0.5 秒内进程重启，前端轮询 `/api/health` 直到版本变化（或服务恢复）即视为完成。

新增错误码：`update_disabled`（400，更新已关闭）、`update_unsupported`（502，无法写入程序文件）、
`update_failed`（502，下载/校验/替换失败）。活动日志新增 `kind` = `update`。

## 6. 环境变量（部署配置，非 WebUI 可改项）

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `PORT` | `8080` | WebUI/API 端口 |
| `HOST` | `0.0.0.0` | 监听地址 |
| `DATA_DIR` | 脚本同目录 `data/` | SQLite 与运行数据目录（容器内目录挂载时落在 `/app/data`） |
| `WEBUI_FILE` | 脚本同目录 `webui.html` | WebUI 文件路径 |
| `APP_FILE` | 运行中的脚本路径 | 在线更新要替换的程序文件（测试/特殊部署可覆盖） |
| `JSON_BODY_PARSE` | `1` | 正文是 JSON 文档时自动展开（ani-rss 等把 JSON 发到 topic 根路径的场景） |
| `SEND_IMAGE` | `1` | ntfy 附件/封面是图片时作为 Bark 通知图片转发（`image` 字段） |
| `UPDATE_ENABLED` | `1` | `0` = 关闭 WebUI 在线更新 |
| `UPDATE_AUTOCHECK` | `1` | `0` = 不在启动后/每 6 小时自动检查（仍可手动点「检查更新」；离线部署与测试用） |
| `UPDATE_REPO` | `Rain0Jack/bark-bridge` | GitHub 仓库 `owner/name` |
| `UPDATE_BRANCH` | `main` | 跟踪的分支，也可以填 tag（如 `v2.1.0`） |
| `UPDATE_API_BASE` | `https://api.github.com` | 提交信息 API 根地址（可覆盖，测试用） |
| `UPDATE_CODELOAD_BASE` | `https://codeload.github.com` | 源码包下载根地址（可覆盖，测试用） |
| `ADMIN_USER` | `admin` | 首次启动自动创建的管理员用户名 |
| `ADMIN_PASSWORD` | 空 | 设置后首启自动建管理员；留空则由 WebUI 引导创建 |
| `BARK_SERVER` | `https://api.day.app` | 新设备默认 Bark 服务器 |
| `NTFY_BASE_URL` | `http://127.0.0.1:40265` | 新订阅默认 ntfy 地址 |
| `SESSION_TTL` | `604800` | 会话有效期（秒） |
| `MAX_BODY` | `2000` | Bark 正文截断长度 |
| `RECONNECT_DELAY` | `5` | 断线重连基数（秒，实际指数退避 5/10/20/40/60） |
| `PUSH_WORKERS` | `4` | 并发推送线程数 |
| `ACTIVITY_SIZE` | `200` | 活动日志环形缓冲条数 |
| `ACCESS_LOG` | `0` | `1` 时打印每条 HTTP 访问日志 |
| 兼容旧版 | `NTFY_TOPIC` `NTFY_TOKEN` `NTFY_SINCE` `BARK_KEY` `BARK_GROUP` `BARK_LEVEL` `BARK_ICON` | 仅首次初始化（空库）时，用这些变量播种 `admin` 的默认设备与订阅，让老部署升级后继续工作 |

## 7. SQLite 结构（后端实现参考）

```sql
users(id INTEGER PRIMARY KEY, username TEXT NOT NULL UNIQUE, password_hash TEXT NOT NULL,
      role TEXT NOT NULL DEFAULT 'user', is_active INTEGER NOT NULL DEFAULT 1,
      created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL);

devices(id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        name TEXT NOT NULL, bark_key TEXT NOT NULL, server TEXT NOT NULL,
        grp TEXT NOT NULL DEFAULT 'ntfy', level TEXT NOT NULL DEFAULT '',
        icon TEXT NOT NULL DEFAULT '', is_active INTEGER NOT NULL DEFAULT 1,
        created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL);

subscriptions(id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        name TEXT NOT NULL, ntfy_url TEXT NOT NULL, topic TEXT NOT NULL, token TEXT NOT NULL DEFAULT '',
        since TEXT NOT NULL DEFAULT '1m', priority_min INTEGER NOT NULL DEFAULT 0,
        keyword TEXT NOT NULL DEFAULT '', exclude_keyword TEXT NOT NULL DEFAULT '',
        title_prefix TEXT NOT NULL DEFAULT '', is_active INTEGER NOT NULL DEFAULT 1,
        cursor_ts INTEGER NOT NULL DEFAULT 0, forwarded INTEGER NOT NULL DEFAULT 0,
        created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL);

subscription_devices(subscription_id INTEGER NOT NULL REFERENCES subscriptions(id) ON DELETE CASCADE,
        device_id INTEGER NOT NULL REFERENCES devices(id) ON DELETE CASCADE,
        PRIMARY KEY (subscription_id, device_id));

settings(key TEXT PRIMARY KEY, value TEXT NOT NULL);
-- settings 内部键：secret（会话签名密钥，自动生成）、default_ntfy_url、default_bark_server、
-- default_since、legacy_pending / legacy_seeded（v1 环境变量一次性迁移标记）；
-- 结构版本记在 PRAGMA user_version（当前 = 1）。
```

- `PRAGMA journal_mode=WAL, synchronous=NORMAL, foreign_keys=ON, busy_timeout=5000`。
- 每个线程持有自己的连接（`threading.local`），写操作用一个全局锁串行化（低并发，足够）。
- `cursor_ts` 用于重启后续传，`forwarded` 为累计转发条数；更新做节流（≥5 秒写一次）。

## 8. 前端（webui.html）要求

- 单文件、零外部资源（不引用 CDN/字体/图片），离线可用；内联 CSS + 原生 JS（ES2020），无构建步骤；
  文件体积目标 < 75 KB。
- 中文界面，深/浅色自动适配（`prefers-color-scheme`），移动端可用（≥360px 宽）。
- 页面结构：
  1. 登录页 / 首次「创建管理员」页（依据 `/api/health` 的 `needs_setup`）；
  2. 主界面顶部导航：`概览`、`设备`、`订阅`、`用户`（仅管理员）、`日志`；
  3. 概览：订阅流状态卡片（streaming/reconnecting/error 颜色区分）、累计转发数、快速测试按钮；
  4. 设备：列表 + 新增/编辑/删除弹窗（名称、Bark Key、服务器、分组、级别、图标、启用），
     支持「复制推送地址」「测试推送」；Key 默认打码，可点按显示；
  5. 订阅：列表 + 新增/编辑/删除（名称、ntfy 地址、topic、token、since、优先级过滤、关键词/排除词、
     标题前缀、启用、**多选目标设备**），显示 `has_token` 状态（留空 = 不变，清空需显式按钮）；
  6. 用户（管理员）：列表 + 新增/编辑（改密、角色、启用/停用）+ 删除；
  7. 日志：实时滚动（2 秒增量轮询 `/api/activity`），按订阅/设备/成功失败着色，可暂停、可清屏。
- 所有请求 `credentials: "same-origin"`；写请求自动带 `X-CSRF-Token`；`401` 时回到登录页并提示；
  错误用顶部 toast 显示服务端 `error` 文本。
- 轮询仅在页面可见时进行（`document.visibilityState`），隐藏时暂停；刷新/重建列表后不中断日志游标。
- 无第三方 JS 库；不使用 `innerHTML` 拼接服务端字符串以外的未转义内容（文本用 `textContent`，防 XSS）。
