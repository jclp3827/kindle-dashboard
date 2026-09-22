#!/bin/sh
# Kindle 端拉图显示脚本(推送到 /mnt/us/)。服务地址从 dashboard.conf 读,不写死。
# 装机时由主机端 install.sh 写入 /mnt/us/dashboard.conf:
#   SERVER_URL=http://<服务IP>:<端口>
. /mnt/us/dashboard.conf 2>/dev/null
SERVER_URL="${SERVER_URL:-http://192.168.1.100:8585}"
SERVER_URL_ALT="${SERVER_URL_ALT:-}"   # 备用地址(Mac 的 .local mDNS 名);主地址(IP)失效时自动切
INTERVAL="${INTERVAL:-20}"
CLEAR_EVERY="${CLEAR_EVERY:-10}"

# ── 逃生舱(防变砖)──────────────────────────────────────────────
# 盘根目录有 dashboard.off(或 dashboard.off.txt)就【不启动看板】,保持正常 Kindle 界面。
# 用途:看板占了屏又连不上(没开 USBNetwork、WiFi 也没连)时,把 Kindle 插任意电脑——
# 默认就是 U 盘模式(不需要 USBNetwork/WiFi),根目录即 /mnt/us,在里面新建一个空文件
# 名为 dashboard.off,重启 Kindle 就回到正常界面;想恢复看板把该文件删掉即可。
if ls /mnt/us/dashboard.off* >/dev/null 2>&1; then
    /sbin/initctl start framework 2>/dev/null || true
    /sbin/initctl start pmond 2>/dev/null || true
    exit 0
fi

PIDFILE="/mnt/us/dashboard.pid"
BASE="$SERVER_URL"                     # 当前实际使用的服务地址;拉图连续失败会在主/备间轮换自愈

report_battery() {
    batt=$(lipc-get-prop com.lab126.powerd battLevel 2>/dev/null)
    chg=$(lipc-get-prop com.lab126.powerd isCharging 2>/dev/null)
    [ -z "$batt" ] && return
    if [ "$chg" = "1" ]; then chg_b="true"; else chg_b="false"; fi
    curl -s -m 5 -X POST "$BASE/api/kindle-status" -H "Content-Type: application/json" \
        -d "{\"battery\":$batt,\"charging\":$chg_b}" >/dev/null 2>&1
}

ensure_wifi() {
    # 完整重连,确保拿到【有效 IP】(不只是 WiFi 关联)。
    # 关键(2026-06-09 真机):路由器重启后 wpa_state 常已是 COMPLETED,但 DHCP 租约失效、IP 没刷新,
    # 光 `wpa_cli reconnect` 不会重新 DHCP → Kindle 有关联却没有效 IP、一直拉不到图、停在旧画面。
    # 解法:关无线→开无线,让系统走【完整重连流程(含重新 DHCP)】。被调用时(首屏 / 主循环连续失败)一律执行。
    timeout 8 lipc-set-prop com.lab126.cmd wirelessEnable 0 2>/dev/null
    sleep 3
    timeout 8 lipc-set-prop com.lab126.cmd wirelessEnable 1 2>/dev/null
    i=0
    while [ $i -lt 6 ]; do                       # 最多等 ~30s 直到重新关联(含 DHCP 拿 IP)
        sleep 5
        state=$(timeout 5 wpa_cli status 2>/dev/null | grep 'wpa_state=' | cut -d= -f2)
        [ "$state" = "COMPLETED" ] && break
        timeout 5 wpa_cli reassociate 2>/dev/null   # 关联卡住时再推一把(reassociate 比 reconnect 彻底)
        i=$((i + 1))
    done
    lipc-set-prop com.lab126.powerd preventScreenSaver 1 2>/dev/null
}

# SSH 开机自启(2026-06-09):看板会杀掉系统界面独占屏幕,USB/SSH 也跟着关 → 看板占屏时
# 没法远程刷/排错,形成"重启后连不上"的死结。这里在看板启动时顺带拉起 dropbear(SSH 服务,
# 监听 22;WiFi 连上即可远程 SSH,USB 网络模式也可)。只要看板在跑 SSH 就一直开着,以后不管
# Kindle 怎么重启,都能一行命令远程刷。dropbear -R 自动生成临时 host key;install 用
# StrictHostKeyChecking=no,host key 变化不影响。
start_ssh() {
    { pidof dropbear || pidof dropbearmulti; } >/dev/null 2>&1 && return 0   # 已在跑就不重复起
    for d in /usr/sbin/dropbear /usr/bin/dropbear; do
        [ -x "$d" ] && { "$d" -R -p 22 >/dev/null 2>&1; return 0; }
    done
    # 越狱 USBNetwork 插件自带的多合一二进制(KT3 等常见)
    DM=/mnt/us/usbnet/bin/dropbearmulti
    [ -x "$DM" ] && "$DM" dropbear -R -p 22 >/dev/null 2>&1
}

# 杀旧实例
if [ -f "$PIDFILE" ]; then
    kill $(cat "$PIDFILE") 2>/dev/null
    rm -f "$PIDFILE"
fi
echo $$ > "$PIDFILE"

# 1. 杀看门狗 pmond(否则杀界面后会自动重启)
kill $(pidof pmond) 2>/dev/null
sleep 1
# 2. 杀图形界面进程,独占屏幕
for p in cvm awesome blanket lxinit KPPMainApp pillowd JunoStatusBarDriver kfxreader mesquite; do
    kill $(pidof $p) 2>/dev/null
done
sleep 2
# 3. 防休眠 + 开机自启 SSH(看板占屏时也能远程刷,免去"重启后连不上")
lipc-set-prop com.lab126.powerd preventScreenSaver 1 2>/dev/null
start_ssh
# 4. 首屏
ensure_wifi
report_battery
curl -s -m 10 "$BASE/kindle/frame.png" -o /mnt/us/frame.png
fbink -c -f
fbink -g file=/mnt/us/frame.png -W GC16 -f

# 5. 主循环 + WiFi 看门狗
count=0; fail=0
while true; do
    sleep "$INTERVAL"
    # 一键清屏(设置页「系统向导」按钮):读到服务端指令 → 拉最新帧 → 黑闪清屏 → 全刷重绘当前帧 → 继续正常轮播。
    # 指令读取即复位(每条只执行一次);不碰轮播状态,清完从当前帧继续。
    if [ "$(curl -s -m 5 "$BASE/kindle/clear-cmd" 2>/dev/null)" = "1" ]; then
        curl -s -m 10 "$BASE/kindle/frame.png" -o /mnt/us/frame.png
        if [ -s /mnt/us/frame.png ]; then
            fbink -c -f >/dev/null 2>&1
            fbink -g file=/mnt/us/frame.png -W GC16 -f >/dev/null 2>&1
        fi
        count=$((count + 1))
        [ $((count % 3)) -eq 0 ] && report_battery
        continue
    fi
    curl -s -m 10 "$BASE/kindle/frame.png" -o /mnt/us/frame_new.png
    if [ -s /mnt/us/frame_new.png ]; then
        mv /mnt/us/frame_new.png /mnt/us/frame.png
        fail=0
    else
        fail=$((fail + 1))
        [ $((fail % 3)) -eq 0 ] && ensure_wifi   # 连续3次(~60s)失败先重连 WiFi
        # 再失败可能是 Mac 局域网 IP 变了(DHCP):在主(IP)/备(.local)地址间轮换,通了就留在新地址
        if [ -n "$SERVER_URL_ALT" ] && [ $((fail % 6)) -eq 0 ]; then
            if [ "$BASE" = "$SERVER_URL" ]; then BASE="$SERVER_URL_ALT"; else BASE="$SERVER_URL"; fi
        fi
    fi
    count=$((count + 1))
    [ $((count % 3)) -eq 0 ] && report_battery
    if [ $((count % CLEAR_EVERY)) -eq 0 ]; then
        fbink -g file=/mnt/us/frame.png -W GC16 -f >/dev/null 2>&1   # 定期全刷去残影
    else
        fbink -g file=/mnt/us/frame.png -W REAGL >/dev/null 2>&1
    fi
done   # 循环内 fbink 静默,避免 dashboard.log 在小存储的 Kindle 上长期累积
