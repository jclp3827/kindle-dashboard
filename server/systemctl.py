"""系统向导：状态检测与维护动作（NAS 挂载、USB 网络、Kindle 推送、照片缓存）。

全部为本地、只读或带超时的操作，供设置页「系统向导」面板调用。
USB 网络配置需要管理员权限，通过 osascript 弹出系统授权框（不存密码）。
"""
import os
import subprocess
import threading
import time

KINDLE_IP = "192.168.15.244"
MAC_USB_IP = "192.168.15.201"
NAS_HOST = "minibase"
NAS_IP = "192.168.50.78"
NAS_SHARE = "smb://minibase/摄影图片"
NAS_MOUNT = "/Volumes/摄影图片"
PHOTO_CACHE = os.path.expanduser("~/kindle-photo-cache")
SSH_KEY = os.path.expanduser("~/.ssh/kindle_key_rsa")

# 照片总数扫描较慢（网络盘），后台线程定期刷新，状态接口只读缓存（不阻塞）
_photo_count_cache = {"ts": 0, "mounted": False, "count": 0, "scanning": False}
_photo_count_lock = threading.Lock()


def _count_photos_worker():
    """后台扫描 NAS 照片总数，写缓存。"""
    with _photo_count_lock:
        if _photo_count_cache["scanning"]:
            return
        _photo_count_cache["scanning"] = True
    try:
        cmd = ("find '%s' -type f \\( -iname '*.jpg' -o -iname '*.jpeg' "
               "-o -iname '*.nef' -o -iname '*.png' \\) 2>/dev/null | wc -l") % NAS_MOUNT
        r = _run(["bash", "-c", cmd], timeout=60)
        count = _photo_count_cache["count"]
        if r is not None:
            try:
                count = int(r.stdout.strip())
            except ValueError:
                pass
        _photo_count_cache.update(ts=time.time(), mounted=True, count=count)
    finally:
        _photo_count_cache["scanning"] = False


def _ensure_photo_count(mounted):
    """挂载后若缓存过期（>10 分钟），后台触发一次扫描，立即返回旧值。"""
    if not mounted:
        _photo_count_cache["mounted"] = False
        return _photo_count_cache["count"]
    now = time.time()
    stale = (not _photo_count_cache["mounted"]) or (now - _photo_count_cache["ts"] > 600)
    if stale and not _photo_count_cache["scanning"]:
        threading.Thread(target=_count_photos_worker, daemon=True).start()
    return _photo_count_cache["count"]


def _run(cmd, timeout=10, env=None):
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, env=env)
    except subprocess.TimeoutExpired:
        return None
    except Exception:
        return None


def _mounted():
    r = _run(["/sbin/mount"], timeout=5)
    return r is not None and "摄影图片" in r.stdout


def _en11_info():
    """返回 USB 网卡 en11 状态。"""
    r = _run(["/sbin/ifconfig", "en11"], timeout=5)
    if r is None or r.returncode != 0:
        return {"exists": False, "ip": "", "configured": False}
    ip = ""
    for line in r.stdout.splitlines():
        if "inet " in line and "inet6" not in line:
            parts = line.split()
            if len(parts) >= 2:
                ip = parts[1]
    return {
        "exists": True,
        "ip": ip,
        "configured": ip == MAC_USB_IP,
    }


def _ping(host):
    r = _run(["/sbin/ping", "-c", "1", "-t", "2", host], timeout=5)
    return r is not None and r.returncode == 0


def _ssh(args, timeout=10):
    """经 RSA 密钥连 Kindle。"""
    if not os.path.exists(SSH_KEY):
        return None
    base = ["ssh", "-i", SSH_KEY,
            "-o", "StrictHostKeyChecking=no",
            "-o", "KexAlgorithms=+diffie-hellman-group1-sha1",
            "-o", "HostKeyAlgorithms=+ssh-rsa",
            "-o", "ConnectTimeout=5",
            "-o", "BatchMode=yes",
            f"root@{KINDLE_IP}"]
    return _run(base + args, timeout=timeout)


def _ssh_ok():
    r = _ssh(["echo ok"])
    if r is not None and r.returncode == 0 and "ok" in r.stdout:
        return True, ""
    if not os.path.exists(SSH_KEY):
        return False, "未找到 SSH 密钥"
    return False, "SSH 连接失败"



def _pulling():
    """Kindle 最近 30 秒内拉过 frame.png → 正在正常显示。"""
    try:
        from server.app import KINDLE_PULL
        last = KINDLE_PULL.get("last", 0)
        if last and (time.time() - last) < 30:
            return True, int(time.time() - last), KINDLE_PULL.get("ip", "")
    except Exception:
        pass
    return False, None, ""


def _kindle_loop():
    """检查 Kindle 上 loop.sh 是否在跑。"""
    r = _ssh(["p=$(cat /tmp/loop.pid 2>/dev/null); kill -0 $p 2>/dev/null && echo running || echo stopped"])
    if r is not None and r.returncode == 0:
        return "running" in r.stdout
    return None


def status(cfg):
    """汇总系统状态。"""
    nas_mounted = _mounted()
    photo_count = _ensure_photo_count(nas_mounted)

    # 照片缓存
    cache_imgs = 0
    if os.path.isdir(PHOTO_CACHE):
        cache_imgs = len([f for f in os.listdir(PHOTO_CACHE) if f.startswith("p_") and f.endswith(".png")])
    photo_idx = None
    state_file = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                              "data", "photo_state.json")
    try:
        import json
        with open(state_file) as f:
            photo_idx = json.load(f).get("photo_idx")
    except Exception:
        pass

    # USB 网络
    en11 = _en11_info()
    pulling, pull_ago, pull_ip = _pulling()
    if not en11.get("configured"):
        kindle_ping, ssh_ok, ssh_err, loop = False, False, "USB 网络未就绪", None
    else:
        kindle_ping = _ping(KINDLE_IP)
        if not kindle_ping and not pulling:
            ssh_ok, ssh_err, loop = False, "ping 不通 Kindle（确认 USB 已连、USBNetwork 已开启）", None
        else:
            ssh_ok, ssh_err = _ssh_ok()
            loop = _kindle_loop() if ssh_ok else None
    # 正在拉帧即视为推送正常（即使 SSH 因 tmpfs 密钥丢失而不通）
    if pulling:
        loop = True

    # 数据源（从缓存判断）
    from server.app import cache, cache_lock
    with cache_lock:
        snap = dict(cache)
    sources = {
        "weather": bool(snap.get("weather") or snap.get("weather_now")),
        "ccusage": bool(snap.get("ccusage")),
        "codex_quota": bool(snap.get("codex_rate_limits")),
        "music": isinstance(snap.get("music"), dict),
        "reminders": bool(snap.get("reminders")),
        "news": bool(snap.get("news") or snap.get("news_items")),
    }

    return {
        "nas": {
            "mounted": nas_mounted,
            "mount": NAS_MOUNT,
            "photo_count": photo_count,
            "counting": _photo_count_cache.get("scanning", False),
            "host_reachable": _ping(NAS_IP),
        },
        "usbnet": en11,
        "kindle": {
            "ip": KINDLE_IP,
            "ping": kindle_ping,
            "ssh": ssh_ok,
            "ssh_err": ssh_err,
            "loop": loop,
            "pulling": pulling,
            "pull_ago": pull_ago,
            "pull_ip": pull_ip,
        },
        "photo": {
            "cache_count": cache_imgs,
            "photo_idx": photo_idx,
            "cache_dir": PHOTO_CACHE,
        },
        "sources": sources,
    }


def mount_nas():
    """挂载 NAS 照片共享。返回 (ok, message)。"""
    if _mounted():
        return True, "NAS 已挂载"
    if not _ping(NAS_IP):
        return False, "NAS 不在线（192.168.50.78 ping 不通），请确认 NAS 已开机"
    os.makedirs(NAS_MOUNT, exist_ok=True)
    # 客人免密挂载
    r = _run(["mount_smbfs", "-o", "guest", f"smb://{NAS_IP}/摄影图片", NAS_MOUNT], timeout=15)
    if r is not None and r.returncode == 0 and _mounted():
        _photo_count_cache["ts"] = 0
        _ensure_photo_count(True)
        return True, "NAS 挂载成功"
    # 退回到 minibase 主机名
    r = _run(["mount_smbfs", "-o", "guest", NAS_SHARE, NAS_MOUNT], timeout=15)
    if r is not None and r.returncode == 0 and _mounted():
        _photo_count_cache["ts"] = 0
        _ensure_photo_count(True)
        return True, "NAS 挂载成功"
    err = (r.stderr or r.stdout or "").strip() if r is not None else "挂载超时"
    if not err:
        err = "客人挂载被拒绝，可在 Finder 手动连接"
    return False, f"挂载失败：{err}。可在 Finder 用「前往→连接服务器」输入 {NAS_SHARE}"


def setup_usbnet():
    """通过系统授权弹窗配置 en11（需要管理员密码）。返回 (ok, message)。"""
    en11 = _en11_info()
    if not en11["exists"]:
        return False, "未检测到 en11 网卡。请确认 Kindle 已通过 USB 连接、并在 KUAL 中开启 USBNetwork"
    cmd = f"/sbin/ifconfig en11 inet {MAC_USB_IP} netmask 255.255.255.0 up"
    osa = f'do shell script "{cmd}" with administrator privileges'
    r = _run(["osascript", "-e", osa], timeout=60)
    if r is not None and r.returncode == 0:
        time.sleep(1)
        info = _en11_info()
        if info["configured"]:
            return True, f"USB 网络已配置（Mac {MAC_USB_IP}）"
        return False, "授权完成但 en11 未生效"
    err = (r.stderr or "").strip() if r is not None else "授权超时"
    if "User canceled" in err or "取消" in err:
        return False, "已取消授权"
    return False, f"配置失败：{err}"


def test_kindle():
    """完整链路检测。优先看是否在拉帧（最可靠），再 USB→ping→SSH→推送脚本。"""
    en11 = _en11_info()
    if not en11["exists"]:
        return {"ok": False, "stage": "en11", "msg": "未检测到 en11，请先配置 USB 网络"}
    if not en11["configured"]:
        return {"ok": False, "stage": "en11", "msg": "en11 未配置为 192.168.15.201"}
    pulling, ago, _ = _pulling()
    if pulling:
        return {"ok": True, "stage": "pulling",
                "msg": f"链路正常，Kindle 正在拉取画面（{ago} 秒前）", "loop": True}
    if not _ping(KINDLE_IP):
        return {"ok": False, "stage": "ping", "msg": f"ping 不通 Kindle（{KINDLE_IP}），请确认 USBNetwork 已开启"}
    ssh_ok, ssh_err = _ssh_ok()
    if not ssh_ok:
        return {"ok": False, "stage": "ssh", "msg": f"SSH 不通（Kindle 重启后密钥可能丢失）：{ssh_err}"}
    loop = _kindle_loop()
    if loop is False:
        # 尝试启动 loop.sh
        _ssh(["nohup /mnt/us/loop.sh >/dev/null 2>&1 &"], timeout=10)
        time.sleep(2)
        loop = _kindle_loop()
    return {
        "ok": bool(loop),
        "stage": "loop",
        "msg": "链路正常，Kindle 正在拉取画面" if loop else "SSH 正常但推送脚本未运行（已尝试启动）",
        "loop": loop,
    }
