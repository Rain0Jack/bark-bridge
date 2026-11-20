# bark-bridge

把自建 [ntfy](https://ntfy.sh) 的消息转发到 [Bark](https://bark.day.app)，让 iPhone / iPad 获得可靠的 APNs 推送。

> **为什么需要它？** 官方 ntfy iOS App 没有前台服务，无法在后台维持长连接，自建服务器又需要中继到 ntfy.sh 才能借用其推送配置。Bark 是独立的 APNs 推送 App，作为"弹窗通道"稳定可靠。ntfy 继续做统一入口（脚本、Web、历史记录），Bark 只负责把通知推送到 Apple 设备。

零第三方依赖，Python 3.11+ 标准库即可运行；提供 Docker / systemd / 源码三种部署方式。

## 架构

```
你的脚本/监控 ──HTTP──► 自建 ntfy ──JSON 流──► bark-bridge ──HTTP──► Bark(api.day.app) ──APNs──► iPad / iPhone
```

## 特性

- **零依赖**：只用 Python 标准库，镜像小、启动快；
- **自动重连**：断线后按最后一条消息的时间戳 `since=<ts>` 续传，不漏消息、不重复；
- **优先级映射**：`Priority >= 4` 自动映射为 Bark 的 `timeSensitive`，可穿透专注模式；
- **点击跳转**：ntfy 的 `Click` 头映射为 Bark 通知的跳转 URL；
- **分组/图标/正文截断**可配置；
- Docker（挂载脚本或自建镜像）与 systemd 两种部署方式。

## 快速开始（Docker）

```bash
git clone https://github.com/Rain0Jack/bark-bridge.git
cd bark-bridge
cp .env.example .env
vim .env          # 填 NTFY_TOKEN 与 BARK_KEY
docker compose up -d
docker logs -f bark-bridge
```

日志出现下面这行即启动成功：

```
watching http://127.0.0.1:40265/alerts -> https://api.day.app
```

### 获取两个必要的值

1. **ntfy token**（自建 ntfy 开启了认证时）：

   ```bash
   docker exec -it ntfy ntfy token add admin --label "bark-bridge"
   ```

2. **Bark Key**：iOS 安装 [Bark](https://apps.apple.com/app/bark-customed-notifications/id1403753865) 后，首页的推送地址形如
   `https://api.day.app/AbCdEf123456/`，其中 `AbCdEf123456` 就是 `BARK_KEY`。

### 测试

```bash
curl -H "Authorization: Bearer $(grep NTFY_TOKEN .env | cut -d= -f2)" \
     -H "Title: 桥接测试" -H "Priority: 4" \
     -d "这条会出现在 iPad 上" \
     http://127.0.0.1:40265/alerts
```

iPad 收到 Bark 推送，且 `docker logs bark-bridge` 出现 `forwarded: '桥接测试'`。

## 快速开始（systemd，不用 Docker）

```bash
sudo mkdir -p /opt/bark-bridge /etc/bark-bridge
sudo install -m 755 bark-bridge.py /opt/bark-bridge/bark-bridge.py
sudo install -m 644 .env.example /etc/bark-bridge/env
sudo chmod 600 /etc/bark-bridge/env
sudo vim /etc/bark-bridge/env

sudo install -m 644 systemd/bark-bridge.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now bark-bridge
sudo journalctl -u bark-bridge -f
```

## 使用自建镜像

```bash
docker compose -f docker-compose.build.yml up -d --build
```

## 配置项

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `NTFY_BASE_URL` | `http://127.0.0.1:40265` | ntfy 服务地址（容器内 `127.0.0.1` 指宿主机，前提是 host 网络） |
| `NTFY_TOPIC` | `alerts` | 要监听的 topic |
| `NTFY_TOKEN` | 空 | ntfy Bearer token，服务器开启认证时必填 |
| `NTFY_SINCE` | `1m` | 首次连接的回溯窗口，支持 `1m` / `30s` / `all` |
| `BARK_KEY` | 空 | **必填**，Bark 设备 Key |
| `BARK_SERVER` | `https://api.day.app` | Bark 服务器地址，自建时改这里 |
| `BARK_GROUP` | `ntfy` | 通知分组名，同组通知会折叠 |
| `BARK_LEVEL` | 空 | 固定 Bark 级别（`active`/`timeSensitive`/`passive`）；留空按优先级映射 |
| `BARK_ICON` | 空 | 通知图标 URL |
| `MAX_BODY` | `2000` | 正文截断长度 |
| `RECONNECT_DELAY` | `5` | ntfy 断线重连间隔（秒） |

## 消息映射

| ntfy | Bark |
| --- | --- |
| `Title` | 推送标题（为空时用 topic 名） |
| 消息正文 | `body`，超过 `MAX_BODY` 截断 |
| `Priority: 4/5` | `level=timeSensitive` |
| `Priority: 1~3` | `level=active` |
| `Click` 头 | `url`（点击通知打开） |
| 固定 | `group=BARK_GROUP`、可选 `icon` |

## 常见问题

| 现象 | 原因 / 处理 |
| --- | --- |
| 日志 `HTTP Error 401` | `NTFY_TOKEN` 错误或无权限，用 `curl` 手动验证 JSON 流 |
| 日志 `bark push failed: 400` | `BARK_KEY` 错误或 `BARK_SERVER` 地址不对 |
| iPad 不弹通知 | Bark App 通知权限、专注模式；`Priority >= 4` 才能穿透专注 |
| 消息重复 | 多实例在跑，或重启窗口重叠；脚本已按时间戳去重，检查是否启动了多个容器 |
| 收不到 ntfy 流 | topic 没授权（403），`ntfy access <user> <topic> read-only` 授权 |
| 容器连不上 ntfy | 非 host 网络时 `127.0.0.1` 指向容器自身，改用宿主机 IP 或 `host.docker.internal` |

## 安全与隐私

- `.env` 已在 `.gitignore` 中，**不要提交真实 token / Bark Key**；
- ntfy token 建议一条服务一个，用 `ntfy token add --label` 便于吊销；
- Bark Key 泄露后可在 App 内重置；
- 默认经 `api.day.app` 推送，标题和正文会经过 Bark 官方服务器。敏感内容建议只推"有告警，详见 ntfy"，正文留在自建服务器；也可自建 [bark-server](https://github.com/Finb/bark-server)（需要 Apple 开发者账号的 APNs 证书）。

## License

[MIT](LICENSE)
