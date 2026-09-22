#!/bin/zsh
# kindle看板 服务管理（被菜单栏 App 调用，也可手动执行）
#   service.sh start   后台启动服务（幂等，已在跑则不重复启动，不打开浏览器）
#   service.sh stop    停止服务（采集/渲染/拉取/推送全部停止，Kindle 停在最后一帧）
#   service.sh status  打印 running / stopped
#
# 注意：本脚本会被菜单栏 App 以最小环境（无 shell profile）调起，
# 因此必须显式设置 PATH，命令尽量用绝对路径，且 stop 要能在最小环境下生效。

# 最小环境下也能找到 /usr/bin 下的工具
export PATH="/usr/bin:/bin:/usr/sbin:/sbin:/opt/homebrew/bin:$PATH"

REPO="$HOME/kindle-dashboard-repo"
PORT=8585
LOG="$HOME/.kindle-dashboard-app.log"
PIDFILE="$HOME/.kindle-dashboard-app.pid"
# 精确匹配「python -m server.run」服务进程，不会误杀 pkill 自身或其它 Python
PATTERN=" -m server.run"

# 本地健康检查务必绕过代理
export no_proxy="127.0.0.1,localhost,192.168.0.0/16,::1"
export NO_PROXY="$no_proxy"

healthy(){ /usr/bin/curl -s --noproxy '*' -m 2 "http://127.0.0.1:$PORT/health" >/dev/null 2>&1; }

service_pids(){ /usr/bin/pgrep -f "$PATTERN" 2>/dev/null; }

case "$1" in
  start)
    if healthy; then
      service_pids | head -1 > "$PIDFILE"
      exit 0
    fi
    # 服务外网请求（天气/额度）走代理
    export http_proxy="http://127.0.0.1:7890"
    export https_proxy="http://127.0.0.1:7890"
    export HTTP_PROXY="$http_proxy"
    export HTTPS_PROXY="$https_proxy"
    /bin/mkdir -p "$(/usr/bin/dirname "$LOG")"
    # 日志轮转：超过 1MB 只保留最后 200 行
    if [ -f "$LOG" ] && [ $(/usr/bin/stat -f%z "$LOG" 2>/dev/null || echo 0) -gt 1048576 ]; then
      /usr/bin/tail -200 "$LOG" > "$LOG.tmp" 2>/dev/null && /bin/mv "$LOG.tmp" "$LOG"
    fi
    cd "$REPO" || exit 1
    /usr/bin/nohup "$REPO/.venv/bin/python" -m server.run >> "$LOG" 2>&1 &
    echo $! > "$PIDFILE"
    /usr/bin/disown 2>/dev/null
    ;;
  stop)
    # 优先用 pidfile，兜底用精确模式匹配
    PIDS=""
    if [ -f "$PIDFILE" ]; then
      PID=$(/bin/cat "$PIDFILE" 2>/dev/null)
      if [ -n "$PID" ] && /bin/kill -0 "$PID" 2>/dev/null; then PIDS="$PID"; fi
    fi
    [ -z "$PIDS" ] && PIDS="$(service_pids)"
    if [ -n "$PIDS" ]; then
      # 先优雅退出（最多等 5 秒），再强杀（uvicorn 有时不响应 SIGTERM）
      /bin/kill -TERM $PIDS 2>/dev/null
      i=0
      while [ $i -lt 5 ]; do
        /bin/sleep 1
        LEFT="$(service_pids)"
        [ -z "$LEFT" ] && break
        i=$((i+1))
      done
      LEFT="$(service_pids)"
      if [ -n "$LEFT" ]; then /bin/kill -KILL $LEFT 2>/dev/null; fi
    fi
    /bin/rm -f "$PIDFILE"
    ;;
  status)
    if healthy; then echo "running"; else echo "stopped"; fi
    ;;
  *)
    echo "usage: $0 {start|stop|status}"
    exit 1
    ;;
esac
exit 0
