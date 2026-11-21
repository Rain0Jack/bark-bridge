#!/usr/bin/env bash
# bark-bridge one-command updater (NAS / any Docker host).
#
# 用法 1（推荐，SSH 到 NAS 粘一条命令；目录换成你的部署目录）：
#   curl -fsSL https://raw.githubusercontent.com/Rain0Jack/bark-bridge/main/scripts/update.sh | sudo bash -s -- /root/dpanel/compose/bark-bridge
#
# 用法 2（仓库内执行）：
#   sudo ./scripts/update.sh /path/to/deploy [--branch main] [--no-restart] [--no-migrate]
#
# 做了什么：备份现有文件 → 从 GitHub 下载最新源码包（分支或 tag）→ 校验语法 →
# 把旧的单文件只读挂载迁移成目录挂载（WebUI 在线更新需要）→ 原子替换
# bark-bridge.py / webui.html → docker compose up -d --force-recreate → 轮询 /api/health。
# 不会改动你的 data/、.env 或 compose 里内联的 environment 配置。
set -euo pipefail

REPO="${BB_REPO:-Rain0Jack/bark-bridge}"
BRANCH="${BB_BRANCH:-main}"
TARGET=""
MIGRATE=1
RESTART=1

usage() {
  cat <<'EOF'
用法: update.sh [部署目录] [--branch 分支或tag] [--repo owner/name] [--no-migrate] [--no-restart]
  --branch   要更新的分支或 tag（默认 main）
  --repo     GitHub 仓库，默认 Rain0Jack/bark-bridge
  --no-migrate   不自动把旧的 :ro 单文件挂载改成目录挂载
  --no-restart   只更新文件，不重启容器
EOF
}

while [ $# -gt 0 ]; do
  case "$1" in
    --branch) BRANCH="${2:?--branch 需要一个值}"; shift 2 ;;
    --repo) REPO="${2:?--repo 需要一个值}"; shift 2 ;;
    --no-migrate) MIGRATE=0; shift ;;
    --no-restart) RESTART=0; shift ;;
    -h|--help) usage; exit 0 ;;
    *) TARGET="$1"; shift ;;
  esac
done

say() { printf '\033[1;32m[bark-bridge]\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[bark-bridge]\033[0m %s\n' "$*"; }
die() { printf '\033[1;31m[bark-bridge]\033[0m %s\n' "$*" >&2; exit 1; }

[ -n "$TARGET" ] || TARGET="$(pwd)"
cd "$TARGET" || die "进入目录失败：$TARGET"
[ -f bark-bridge.py ] || die "当前目录没有 bark-bridge.py：$(pwd)（请把部署目录作为参数传入）"

COMPOSE=""
for candidate in docker-compose.yml docker-compose.yaml compose.yml compose.yaml; do
  if [ -f "$candidate" ]; then COMPOSE="$candidate"; break; fi
done
[ -n "$COMPOSE" ] || die "当前目录没有 docker-compose.yml：$(pwd)"

if [ "$(id -u)" -ne 0 ] && command -v docker >/dev/null 2>&1 && ! docker info >/dev/null 2>&1; then
  die "当前用户没有 docker 权限，请用 sudo 运行"
fi

STAMP="$(date +%Y%m%d-%H%M%S)"
BACKUP=".update-backups/$STAMP"
mkdir -p "$BACKUP"
for file in bark-bridge.py webui.html "$COMPOSE"; do
  [ -f "$file" ] && cp -p "$file" "$BACKUP/" || true
done
say "已备份到 $TARGET/$BACKUP"

TMP="$(mktemp -d)"
cleanup() { rm -rf "$TMP"; }
trap cleanup EXIT INT TERM

say "下载 https://github.com/$REPO@$BRANCH ..."
if ! curl -fsSL --retry 3 --connect-timeout 20 \
      "https://codeload.github.com/$REPO/tar.gz/refs/heads/$BRANCH" -o "$TMP/src.tar.gz"; then
  curl -fsSL --retry 3 --connect-timeout 20 \
    "https://codeload.github.com/$REPO/tar.gz/refs/tags/$BRANCH" -o "$TMP/src.tar.gz" \
    || die "下载失败：$REPO@$BRANCH（分支/tag 不存在或网络不通）"
fi

tar -xzf "$TMP/src.tar.gz" -C "$TMP"
SRC="$(dirname "$(find "$TMP" -maxdepth 2 -name bark-bridge.py | head -n 1)")"
[ -f "$SRC/bark-bridge.py" ] || die "源码包里没有 bark-bridge.py"
[ -f "$SRC/webui.html" ] || die "源码包里没有 webui.html"

if command -v python3 >/dev/null 2>&1; then
  python3 -m py_compile "$SRC/bark-bridge.py" || die "新 bark-bridge.py 语法校验失败，已中止（现有文件未改动）"
elif command -v docker >/dev/null 2>&1; then
  docker run --rm -v "$SRC:/src:ro" python:3.12-alpine \
    python3 -m py_compile /src/bark-bridge.py || die "新 bark-bridge.py 语法校验失败，已中止（现有文件未改动）"
else
  warn "没有 python3/docker，跳过语法校验"
fi

NEW_VERSION="$(sed -n 's/^__version__ = "\([^"]*\)".*/\1/p' "$SRC/bark-bridge.py" | head -n 1)"

if [ "$MIGRATE" = 1 ] && grep -qE '^[[:space:]]*-[[:space:]]*\./bark-bridge\.py:/app/bark-bridge\.py' "$COMPOSE"; then
  warn "检测到单文件只读挂载，迁移为目录挂载（WebUI 在线更新需要；原 compose 已备份）"
  awk '
    /^[[:space:]]*-[[:space:]]*\.\/bark-bridge\.py:/ { next }
    /^[[:space:]]*-[[:space:]]*\.\/webui\.html:/ { next }
    /^[[:space:]]*-[[:space:]]*\.\/data:\/app\/data/ { next }
    /^[[:space:]]*volumes:[[:space:]]*$/ { print; print "      - ./:/app"; next }
    { print }
  ' "$COMPOSE" > "$TMP/compose.new"
  mv "$TMP/compose.new" "$COMPOSE"
fi

install -m 644 "$SRC/bark-bridge.py" "$TARGET/bark-bridge.py.new"
install -m 644 "$SRC/webui.html" "$TARGET/webui.html.new"
mv "$TARGET/bark-bridge.py.new" "$TARGET/bark-bridge.py"
mv "$TARGET/webui.html.new" "$TARGET/webui.html"
say "代码已更新（远端版本 ${NEW_VERSION:-未知}）"

if [ "$RESTART" = 1 ] && command -v docker >/dev/null 2>&1; then
  if docker compose version >/dev/null 2>&1; then
    docker compose -f "$COMPOSE" up -d --force-recreate
  else
    docker-compose -f "$COMPOSE" up -d --force-recreate
  fi
  PORT="$(sed -n 's/^[[:space:]]*PORT:[[:space:]]*\([0-9]*\).*/\1/p' "$COMPOSE" | head -n 1)"
  PORT="${PORT:-8080}"
  say "等待服务恢复（http://127.0.0.1:$PORT/api/health）..."
  for _ in $(seq 1 30); do
    if curl -fsS "http://127.0.0.1:$PORT/api/health" >/dev/null 2>&1; then
      RUNNING_VERSION="$(curl -fsS "http://127.0.0.1:$PORT/api/health" \
        | sed -n 's/.*"version":"\([^"]*\)".*/\1/p')"
      say "更新完成，当前运行版本：${RUNNING_VERSION:-未知}"
      say "WebUI: http://127.0.0.1:$PORT/ （首次升级 v2 请先在页面创建管理员）"
      exit 0
    fi
    sleep 1
  done
  warn "服务 30 秒内没有恢复，请查看日志：docker compose -f $COMPOSE logs --tail=100"
  exit 1
fi

say "文件已更新（未重启服务）"
