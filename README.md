# bark-bridge

把自建 [ntfy](https://ntfy.sh) 的 topic 消息转发到 iOS 的 [Bark](https://bark.day.app) 推送，支持**多台 iOS 设备、多用户、可在线编辑的订阅**，并内置一个实时管理的 WebUI。

> **为什么需要它？** 官方 ntfy iOS App 没有前台服务，无法在后台维持长连接，自建服务器又需要中继到 ntfy.sh 才能借用其推送配置。Bark 是独立的 APNs 推送 App，作为"弹窗通道"稳定可靠。ntfy 继续做统一入口（脚本、Web、历史记录），Bark 只负责把通知推送到 Apple 设备。

零第三方依赖（Python 3.11+ 标准库），单进程 ≈ 30 MB 内存，提供 Docker / systemd / 源码三种部署方式。

## v2 做了什么

v1 只能「一个 Bark Key + 一个 topic」。v2 是一个完整的小型多用户推送网关：

| 能力 | 说明 |
| --- | --- |
| **多设备** | 一台 iOS 设备 = 一条 Bark 记录（iPad / iPhone / 家人手机…），一条订阅可**同时 fanout 到多台设备**，并发推送、单台失败不影响其它设备 |
| **多用户** | `admin` / `user` 两种角色，用户只能看到和操作自己的设备与订阅；管理员负责开号、停用、重置密码 |
| **订阅可编辑** | ntfy 地址、topic、token、回溯窗口、优先级下限、关键词/排除词、标题前缀、目标设备，全部在 WebUI 里改，**1~2 秒内生效，无需重启** |
| **WebUI 实时管理** | 单文件、零外链、深/浅色自适应、移动端可用；概览状态卡片、设备/订阅/用户 CRUD、2 秒增量实时日志 |
| **在线更新** | 管理员在 WebUI 点「更新」即可从 GitHub 仓库拉取最新代码、自动备份、原子替换并原地重启（约 1~2 秒）；NAS 上也可以用一条命令更新 |
| **少踩坑的映射** | 正文是 JSON 文档时自动展开（ani-rss 等把 JSON 发到 topic 根路径的场景）；图片附件/番剧封面自动作为 Bark 通知图片转发 |
| **仍然极轻** | 单进程，无 Redis/无 Node/无第三方库；源码 `bark-bridge.py` + `webui.html` 合计约 175 KB，镜像基于 `python:3.12-alpine` |
| **可靠投递** | 断线自动重连、按落库游标续传；按消息 `id` 去重，同一秒内的多条消息不丢、重连不重复 |

## 架构

```
你的脚本/监控 ──HTTP──► 自建 ntfy ──JSON 流──► bark-bridge ──HTTP /push──► Bark 服务器 ──APNs──► iPad / iPhone
                                              │
                          SQLite：用户 / 设备 / 订阅 / 游标 / 会话密钥
                                              │
                                   浏览器 ◄── WebUI + REST API（默认 :8080）
```

- 一条启用中的订阅 = 一个 ntfy 流工作线程；消息到达后按目标设备并发推送（默认 4 并发）。
- 所有配置存 SQLite（WAL），WebUI/API 的修改由监督线程在 1.5 秒内对账，只重启变化过的流。
- 同一个进程同时提供转发引擎和 WebUI/API，没有额外服务、没有消息队列。

## 快速开始（Docker）

```bash
git clone https://github.com/Rain0Jack/bark-bridge.git
cd bark-bridge
cp .env.example .env      # 一般只需要改 PORT / NTFY_BASE_URL
docker compose up -d
```

浏览器打开 `http://<主机IP>:8080/`，第一次会提示**创建管理员**，之后：

1. **加设备**：iPhone/iPad 安装 [Bark](https://apps.apple.com/app/bark-customed-notifications/id1403753865)，首页推送地址形如
   `https://api.day.app/AbCdEf123456/`，`AbCdEf123456` 就是 Bark Key；WebUI「设备 → 新增」填名称 + Key，
   有多台 iOS 设备就加多条。
2. **加订阅**：「订阅 → 新增」填 ntfy 地址、topic、ntfy token（服务器开认证时）、回溯窗口，
   再**勾选目标设备**（可多选）。保存后卡片状态变成绿色的 `streaming` 即在工作。
3. **验证**：点订阅卡片上的「测试推送」，或往 topic 发一条：

   ```bash
   curl -H "Title: 桥接测试" -H "Priority: 4" -d "这条会出现在 iPad 上" \
        http://127.0.0.1:40265/alerts
   ```

   目标设备收到通知，WebUI「日志」页出现对应记录。

> `network_mode: host`：容器内的 `127.0.0.1` 就是宿主机，所以默认的 `NTFY_BASE_URL=http://127.0.0.1:40265`
> 可以直接访问本机 ntfy；WebUI 也直接监听 `.env` 里的 `PORT`，无需端口映射。ntfy 在别的机器/容器网络时，
> 改 `NTFY_BASE_URL` 并删掉 compose 里的 `network_mode: host`，再自行加 `ports: ["8080:8080"]`。

## WebUI 使用

| 页面 | 功能 |
| --- | --- |
| 概览 | 每条订阅的流状态（streaming/connecting/reconnecting/error/stopped）、启用订阅与设备数、累计转发、运行时长；每卡可一键测试推送；5 秒自动刷新（仅前台） |
| 设备 | 增删改（名称 / Bark Key / Bark 服务器 / 分组 / 级别 / 图标 / 启用）；Key 默认打码；复制推送地址；测试推送 |
| 订阅 | 增删改；多选目标设备；token 留空 = 不修改、显式「清除 Token」才清空；优先级下限、关键词、排除词、标题前缀、since 回溯窗口 |
| 用户 | 仅管理员：开号、改密、改角色、启用/停用、删除（不能操作自己，且始终保留至少一个启用的管理员） |
| 日志 | 2 秒增量轮询的实时日志（成功/失败/流状态/测试/更新着色），可暂停、清屏、立即刷新；页面隐藏时自动停止轮询 |
| 更新 | 仅管理员：顶栏版本徽标点开，检查新版本 → 一键更新 → 自动备份、替换、重启并等待服务恢复；显示当前/远端版本、commit 与更新时间 |

## 在线更新

### 方式一：WebUI 一键更新（推荐）

管理员登录后点顶栏版本徽标打开「在线更新」：

1. 点「检查更新」→ 显示远端最新版本、commit 短 sha 与提交说明（检查结果缓存 10 分钟，启动后与每 6 小时也会自动检查）；
2. 点「立即更新」→ 二次确认后：下载 GitHub 源码包 → 校验 `bark-bridge.py` 可编译 → 备份旧文件到
   `<DATA_DIR>/update-backup-<时间戳>/` → 原子替换 `bark-bridge.py` / `webui.html` → 原地重启（约 1~2 秒不可用）；
3. 前端自动轮询 `/api/health`，等到版本变化即提示「已更新到 vX」并刷新页面。数据库、用户、设备、订阅、配置都不受影响。

前提：程序目录**可写**。Docker 部署必须是目录挂载（本仓库 `docker-compose.yml` 已改好）：

```yaml
    volumes:
      - ./:/app          # 目录挂载，WebUI 才能替换文件
```

不要用 `./bark-bridge.py:/app/bark-bridge.py:ro` 这类单文件只读挂载，否则 WebUI 会提示「程序目录不可写，无法在线更新」。

### 方式二：NAS / Docker 主机上一条命令更新

SSH 到 NAS，把目录换成你的部署目录（下面示例是 DPanel 的 `/root/dpanel/compose/bark-bridge`）：

```bash
curl -fsSL https://raw.githubusercontent.com/Rain0Jack/bark-bridge/main/scripts/update.sh | sudo bash -s -- /root/dpanel/compose/bark-bridge
```

脚本会：备份现有文件到 `.update-backups/<时间戳>/` → 下载 GitHub 源码包（分支或 tag）→ `py_compile` 校验 →
把旧的单文件只读挂载自动迁移成目录挂载（WebUI 在线更新需要，原 compose 会备份）→ 替换
`bark-bridge.py` / `webui.html` → `docker compose up -d --force-recreate` → 轮询 `/api/health` 直到服务恢复。
**不会改** 你的 `data/`、`.env` 或 compose 里内联的 `environment` 配置。

常用参数：`--branch v2.1.0`（更新到某个 tag）、`--no-restart`（只换文件）、`--no-migrate`（不动 compose）。
在仓库目录里也可以直接 `sudo bash scripts/update.sh <部署目录>`。

### 更新失败怎么办

- 备份都在 `<DATA_DIR>/update-backup-*`（WebUI 方式）或部署目录 `.update-backups/`（脚本方式），
  把 `bark-bridge.py` / `webui.html` 换回去再 `docker compose restart` 即可回退；
- 远端版本比本地低时会提示「远端版本更低」，通常是本地改完还没 `git push`，WebUI 会拒绝降级；
- 更新期间约 1~2 秒不可用，正在转发的消息会按游标续传，不会丢。

## 从 v1 升级

v1 的 `.env`（`BARK_KEY` / `NTFY_TOPIC` / `NTFY_TOKEN` / `BARK_GROUP` / `BARK_LEVEL` / `BARK_ICON`）**无需手动迁移**：

- 若同时设置了 `ADMIN_PASSWORD`：首次启动自动创建管理员，并把旧变量播种成该管理员的「默认设备 + 一条订阅」；
- 若没设 `ADMIN_PASSWORD`：保持旧变量不动，首次打开 WebUI 创建管理员后自动完成同样的迁移；
- 迁移只发生一次（记在数据库里），之后一切以 WebUI / 数据库为准，旧变量可以删掉。

数据库在 `./data/bark-bridge.db`，升级前先 `cp -r data data.bak` 即可回退。

v1 的 `docker-compose.yml` 用的是单文件只读挂载，升级到 v2 时需要改成目录挂载（否则 WebUI 在线更新不可用，
数据目录也不会自动出现在宿主机）：

```diff
     volumes:
-      - ./bark-bridge.py:/app/bark-bridge.py:ro
-      - ./webui.html:/app/webui.html:ro
-      - ./data:/app/data
+      - ./:/app
```

改完 `sudo docker compose up -d --force-recreate`。用上面的更新脚本升级时会自动完成这一步。

## 部署方式

### Docker（构建独立镜像）

```bash
docker compose -f docker-compose.build.yml up -d --build
```

### systemd（不用 Docker）

```bash
sudo mkdir -p /opt/bark-bridge /etc/bark-bridge
sudo install -m 755 bark-bridge.py /opt/bark-bridge/bark-bridge.py
sudo install -m 644 webui.html /opt/bark-bridge/webui.html
sudo install -m 644 .env.example /etc/bark-bridge/env
sudo chmod 600 /etc/bark-bridge/env
sudo vim /etc/bark-bridge/env

sudo install -m 644 systemd/bark-bridge.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now bark-bridge
sudo journalctl -u bark-bridge -f
```

数据默认在 `/var/lib/bark-bridge/`（systemd `StateDirectory` 自动创建）。`WEBUI_FILE` 默认取脚本同目录的 `webui.html`。

### 反代（Nginx / DPanel / Caddy，可选）

想让手机在外面用 `https://bark.你的域名.top` 打开 WebUI 时，把反向代理指到 `127.0.0.1:8080` 即可（host 网络下容器与宿主机同网）：

- **DPanel**：新建反代站点，上游填 `127.0.0.1:8080`（或 DPanel 里该容器的 `*.pod.dpanel.local`），
  申请 ACME 证书并开启 HTTPS；WebUI 是普通 HTTP/1.1 + JSON，不需要 WebSocket 配置。
- **Nginx**：

  ```nginx
  location / {
      proxy_pass http://127.0.0.1:8080;
      proxy_set_header Host $host;
      proxy_set_header X-Real-IP $remote_addr;
      proxy_cache off;            # 实时日志接口不要缓存
      proxy_buffering off;
  }
  ```

- **Caddy**：`bark.example.com { reverse_proxy 127.0.0.1:8080 }`。

> 反代暴露到公网前，务必用强密码并考虑只允许内网/VPN 访问；容器内 `HOST` 保持 `0.0.0.0`、`PORT` 默认 `8080`，不要加 `ports:`。

### 源码直接跑

```bash
python3 -m venv .venv && . .venv/bin/activate   # 可选，纯标准库
cp .env.example .env
python3 bark-bridge.py
```

## 配置项

大部分配置在 **WebUI 里改**，`.env` 只负责进程级参数与首次播种。完整列表见 [.env.example](.env.example)。

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `PORT` | `8080` | WebUI / API 端口 |
| `HOST` | `0.0.0.0` | 监听地址（只给本机用可改 `127.0.0.1`） |
| `DATA_DIR` | 脚本同目录下的 `data/` | SQLite 数据目录，**必须持久化** |
| `ADMIN_USER` | `admin` | 管理员用户名 |
| `ADMIN_PASSWORD` | 空 | 设置后首启自动建管理员；留空则由 WebUI 引导创建 |
| `NTFY_BASE_URL` | `http://127.0.0.1:40265` | 新建订阅的默认 ntfy 地址 |
| `NTFY_SINCE` | `1m` | 新建订阅的默认回溯窗口 |
| `BARK_SERVER` | `https://api.day.app` | 新建设备的默认 Bark 服务器 |
| `JSON_BODY_PARSE` | `1` | 正文是 JSON 文档时自动展开（ani-rss 等场景） |
| `SEND_IMAGE` | `1` | 图片附件/封面作为 Bark 通知图片转发 |
| `SESSION_TTL` | `604800` | 会话有效期（秒） |
| `MAX_BODY` | `2000` | Bark 正文截断长度 |
| `RECONNECT_DELAY` | `5` | 断线重连基数，按 5/10/20/40/60 指数退避 |
| `PUSH_WORKERS` | `4` | 并发推送线程数 |
| `ACTIVITY_SIZE` | `200` | 实时日志保留条数 |
| `ACCESS_LOG` | `0` | `1` = 打印每条 HTTP 访问日志 |
| `UPDATE_ENABLED` | `1` | `0` = 关闭 WebUI 在线更新 |
| `UPDATE_REPO` | `Rain0Jack/bark-bridge` | 在线更新来源仓库 `owner/name` |
| `UPDATE_BRANCH` | `main` | 跟踪分支，也可以填 tag（如 `v2.1.0`） |
| `APP_FILE` / `WEBUI_FILE` | 运行中的脚本 / 同目录 webui.html | 仅特殊部署或测试需要 |

## 消息映射

| ntfy | Bark |
| --- | --- |
| `Title` | 推送标题（为空用 topic 名；超过 100 字截断；可再加标题前缀） |
| 消息正文 | `body`，超过 `MAX_BODY` 截断 |
| `Priority: 4/5` | `level=timeSensitive`（可穿透专注模式） |
| `Priority: 1~3` | `level=active` |
| `Click` 头 | `url`（点击通知打开） |
| 附件 / 封面（图片 URL） | `image`（Bark 通知图片，iOS 15+）；非图片附件把链接追加到正文 |
| 正文是 JSON 文档 | 自动展开出真正的 `title`/`message`，并接管其中的 `priority`/`click`/`attach`（`JSON_BODY_PARSE=1`） |
| 固定 | `group`（同组折叠）、可选 `icon`；设备记录里的级别可覆盖优先级映射 |

订阅级过滤（按顺序）：`event` 必须是 `message` → 按消息 `id` 去重 → `time` 不早于游标 → `priority >= priority_min` →
`keyword` 的每个词都要出现在「标题+正文」→ 命中任一 `exclude_keyword` 词则丢弃。

## HTTP API

WebUI 使用的就是同一套 JSON API（登录后可 `curl` 调用），契约见 **[docs/API.md](docs/API.md)**：
`/api/health`、`/api/login`、`/api/devices`、`/api/subscriptions`、`/api/users`、`/api/status`、`/api/activity` 等。
认证用 HMAC 签名 Cookie + `X-CSRF-Token` 双提交校验。

## 体积与资源占用

| 指标 | 实测 |
| --- | --- |
| 源码 | `bark-bridge.py` ≈ 106 KB + `webui.html` ≈ 69 KB（含全部前端） |
| 第三方依赖 | **0**（标准库：`sqlite3` / `http.server` / `urllib` / `tarfile` / `hashlib` / `threading`） |
| 进程内存 | 常驻 RSS ≈ 30 MB（Windows 实测；Linux/alpine 通常更低），线程数 5 起（每条订阅 +1） |
| 空闲 CPU | 10 秒内 ≈ 0（监督线程 1.5 秒一次轻量对账，更新检查每 6 小时一次，流线程阻塞在 ntfy 长连接上） |
| 磁盘 | 一个 SQLite 文件（WAL），用户/设备/订阅元数据通常 < 100 KB；更新备份每次约 175 KB |

## 常见问题

| 现象 | 原因 / 处理 |
| --- | --- |
| 打不开 WebUI | 检查 `PORT`/`HOST`、防火墙；host 网络下端口直接监听在宿主机 |
| 打不开登录页且日志有 `webui.html 缺失` | `webui.html` 没和脚本放一起，或 `WEBUI_FILE` 指错 |
| 日志 `HTTP Error 401`（ntfy） | 订阅里的 ntfy token 错误或该用户没有 topic 读权限，`ntfy access <user> <topic> ro` |
| 日志 `push failed ... 400` | 设备 Bark Key 错误或 Bark 服务器地址不对，用「测试推送」验证 |
| 多台设备只收到一台 | 订阅里没勾选另一台设备；或那台设备被停用；看「日志」页对应设备是否失败 |
| 消息重复 | 多实例在跑（同一份数据目录只能一个进程），或 Windows/Docker 两份部署；按 `id` 去重只在单进程内生效 |
| 收不到 ntfy 流 | topic 没授权（403）；容器非 host 网络时 `127.0.0.1` 指向容器自身，改用宿主机 IP |
| 403 `csrf_failed` | 页面长期未刷新导致 cookie 失效，刷新页面重新登录即可 |
| 在线更新提示「程序目录不可写」 | Docker 用了单文件 `:ro` 挂载：改成目录挂载 `- ./:/app`（见「在线更新」）后重启 |
| 在线更新提示「远端版本更低」 | 本地改了代码但还没 `git push`；GitHub 上还是旧版，先 push（或按 tag 更新） |
| 更新后 1~2 秒打不开页面 | 正常现象：进程正在原地重启，前端会自动等到恢复 |
| 更新失败 | 旧文件在 `<DATA_DIR>/update-backup-*`（WebUI）或部署目录 `.update-backups/`（脚本），换回后 restart 即可 |
| 忘记管理员密码 | 用另一个管理员在「用户」页重置；只剩唯一管理员时按下面的方法直接改库（会清空所有会话） |

只记得唯一管理员密码时，停掉服务后直接改库：

```bash
python3 - <<'PY'
import base64, hashlib, secrets, sqlite3

new_password = b"new-password-here"
salt = secrets.token_bytes(16)
digest = hashlib.pbkdf2_hmac("sha256", new_password, salt, 210000)
b64 = lambda raw: base64.urlsafe_b64encode(raw).decode().rstrip("=")
stored = f"pbkdf2_sha256$210000${b64(salt)}${b64(digest)}"

db = sqlite3.connect("data/bark-bridge.db")
row = db.execute("SELECT id FROM users WHERE role='admin' ORDER BY id LIMIT 1").fetchone()
if row:
    db.execute("UPDATE users SET password_hash=? WHERE id=?", (stored, row[0]))
    db.commit()
print("updated admin #", row[0] if row else None)
PY
```

改完重启服务即可用新密码登录（旧会话因密码版本变化自动失效）。

## 开发与测试

```bash
python3 -m py_compile bark-bridge.py
python3 -m unittest discover -s tests -v   # 50 个用例：假 ntfy + 假 Bark，无外网依赖，约 1~3 分钟
```

`tests/test_bridge.py` 会在测试进程内起假 ntfy JSON 流与假 Bark 收集服务器，用子进程启动 `bark-bridge.py`，
覆盖多设备 fanout、过滤、热更新、去重续传、权限隔离、CSRF、限流等；临时目录自动创建与清理。

CI（GitHub Actions）会跑：`py_compile` → WebUI 内联脚本 `node --check` → 完整 unittest → 两个 compose 文件的 `config` 校验。

## 安全与隐私

- **不要直接暴露到公网**：默认只提供 HTTP。公网访问请放在 Caddy/Nginx 后并开 HTTPS，或只在内网/VPN 使用。
- `.env` 与 `data/` 已在 `.gitignore` 中；数据库里存有 Bark Key、ntfy token（明文）与密码哈希，别提交、别外传。
- 密码用 PBKDF2-HMAC-SHA256（210000 轮）加盐存储；改密后旧会话立即失效；登录失败有频率限制。
- 会话 Cookie `HttpOnly` + `SameSite=Lax`，写请求要求 `X-CSRF-Token`；WebUI 无外部资源、自动加 CSP。
- Bark Key / ntfy token 只发给有权限的登录用户（普通用户看不到别人的）。
- 默认经 `api.day.app` 推送，标题和正文会经过 Bark 官方服务器；敏感内容建议只推「有告警，详见 ntfy」，
  或自建 [bark-server](https://github.com/Finb/bark-server)（需要 Apple 开发者账号的 APNs 证书）。

## License

[MIT](LICENSE)
