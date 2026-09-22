"""macOS 提醒事项内部采集（替代外部 launchd 推送脚本）。

直接用 osascript 读取 Reminders.app，返回 {"_push_apple": <推送体>}，
由 app.py 的 collect_source 走与 /api/apple-sync 相同的处理。
"""
import json
import os
import subprocess

_SCRIPT_DIR = os.path.join(os.path.dirname(__file__), "apple_scripts")
_REMINDERS_JXA = os.path.join(_SCRIPT_DIR, "read_reminders.js")


def collect(cfg):
    if not (cfg or {}).get("reminders", {}).get("enabled", False):
        return None
    if not os.path.exists(_REMINDERS_JXA):
        return None
    try:
        r = subprocess.run(["osascript", "-l", "JavaScript", _REMINDERS_JXA],
                           capture_output=True, text=True, timeout=15)
    except Exception:
        return None
    if r.returncode != 0 or not r.stdout.strip():
        return None
    try:
        data = json.loads(r.stdout.strip())
    except Exception:
        return None
    return {"_push_apple": data}
