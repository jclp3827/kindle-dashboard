#!/bin/zsh
# kindle看板 启动器：双击 App 时由 AppleScript 调用。
# 只做最小的事：服务没起就拉起（后台），就绪后打开设置页。不开机自启、不常驻。
REPO="$HOME/kindle-dashboard-repo"
PORT=8585
LOG="$HOME/.kindle-dashboard-app.log"
CFG="$HOME/.config/kindle-dashboard/config.yaml"

# 本地健康检查务必绕过代理
export no_proxy="127.0.0.1,localhost,192.168.0.0/16,::1"
export NO_PROXY="$no_proxy"

# 读 token
TOKEN=$(grep -m1 'access_token:' "$CFG" 2>/dev/null | sed 's/^.*access_token:[[:space:]]*//; s/[[:space:]]*$//; s/^"//; s/"$//')
URL="http://127.0.0.1:$PORT/setup"
[ -n "$TOKEN" ] && URL="${URL}?token=${TOKEN}"

healthy(){ curl -s --noproxy '*' -m 2 "http://127.0.0.1:$PORT/health" >/dev/null 2>&1; }

if healthy; then
  open "$URL"
  exit 0
fi

# 服务未运行：后台拉起（带代理，供天气/额度等外网接口）
export http_proxy="http://127.0.0.1:7890"
export https_proxy="http://127.0.0.1:7890"
export HTTP_PROXY="$http_proxy"
export HTTPS_PROXY="$https_proxy"
mkdir -p "$(dirname "$LOG")"
# 日志轮转：超过 1MB 只保留最后 200 行，避免长期追加无限增长
if [ -f "$LOG" ] && [ $(stat -f%z "$LOG" 2>/dev/null || echo 0) -gt 1048576 ]; then
  tail -200 "$LOG" > "$LOG.tmp" 2>/dev/null && mv "$LOG.tmp" "$LOG"
fi
cd "$REPO" || exit 1
nohup "$REPO/.venv/bin/python" -m server.run >> "$LOG" 2>&1 &
disown

# 后台等待就绪后自动打开浏览器（最多 40 秒），不阻塞 App
nohup zsh -c '
for i in $(seq 1 40); do
  if curl -s --noproxy "*" -m 2 "http://127.0.0.1:'$PORT'/health" >/dev/null 2>&1; then
    open "'$URL'"
    exit 0
  fi
  sleep 1
done
' >/dev/null 2>&1 &
disown

exit 0
