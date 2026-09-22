"""Codex 额度内部采集（替代 crontab + codex_quota_push.sh）。

运行 installers/macos/quota/codex_quota.py（需代理），把结果转成
/​api/rate-limits 相同的推送体，返回 {"_push_rate_limits": ...}，
由 app.py 的 collect_source 写入 cache["codex_rate_limits"]。
"""
import json
import os
import subprocess

_QUOTA_PY = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "installers", "macos", "quota", "codex_quota.py")


def collect(cfg):
    ai = (cfg or {}).get("ai_usage", {}) or {}
    if not ai.get("enabled", True):
        return None
    if not os.path.exists(_QUOTA_PY):
        return None
    env = dict(os.environ)
    # 代理：优先配置，回落本地 Clash
    proxy = ai.get("codex_proxy") or "http://127.0.0.1:7890"
    if proxy:
        env["CODEX_QUOTA_PROXY"] = proxy
        env["http_proxy"] = proxy
        env["https_proxy"] = proxy
    try:
        r = subprocess.run(["python3", _QUOTA_PY], capture_output=True,
                           text=True, timeout=30, env=env)
    except Exception:
        return None
    out = (r.stdout or "").strip()
    if not out:
        return None
    try:
        q = json.loads(out)
    except Exception:
        return None
    if "error" in q or "primary" not in q:
        return None
    payload = {
        "source": "codex",
        "rate_limits": {
            "five_hour": {"used_percentage": q["secondary"].get("usedPercent", 0),
                          "resets_at": q["secondary"].get("resetsAt", 0)},
            "seven_day": {"used_percentage": q["primary"].get("usedPercent", 0),
                          "resets_at": q["primary"].get("resetsAt", 0)},
        },
    }
    return {"_push_rate_limits": payload}
