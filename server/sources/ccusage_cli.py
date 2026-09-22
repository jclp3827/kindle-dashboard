"""AI 用量采集:本机直接跑 ccusage CLI,无任何中间服务。

跑本地 ccusage(npm 包,读 Claude/Codex 日志)产出看板需要的结构。看板服务跑在哪台机器,
就读那台机器的 Claude/Codex 用量;不依赖 ccusage-web 之类的中间件。
ccusage 输出字段已与看板对接:claude 给 date/totalTokens/totalCost,codex 给 date/totalTokens/costUSD
(build_context 的 _norm 已兼容 costUSD)。
install.sh 选"启用 AI 用量"时自动装 Node + ccusage 并置 ai_usage.enabled=true(见 installers/macos)。

持久化:每次成功采集落盘到 data/ccusage_cache.json;服务重启 / 本轮没采到时回放上次结果,
不空窗(ccusage 每次解析全量日志约 10 秒,重启后第一轮出图就有历史数据)。
"""
import json
import os
import shutil
import subprocess

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_DATA_DIR = os.environ.get("KINDLE_DATA_DIR", os.path.join(REPO, "data"))
_PERSIST = os.environ.get("KINDLE_CCUSAGE_CACHE", os.path.join(_DATA_DIR, "ccusage_cache.json"))


def _save(frag):
    try:
        os.makedirs(os.path.dirname(_PERSIST), exist_ok=True)
        tmp = _PERSIST + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(frag, f)
        os.replace(tmp, _PERSIST)
    except Exception as e:
        print(f"[ccusage_cli] 落盘失败: {e}")


def _load():
    try:
        with open(_PERSIST, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def _bin():
    cands = [
        os.path.join(REPO, ".node", "bin", "ccusage"),     # install 装到项目本地
        shutil.which("ccusage"),
        os.path.expanduser("~/.npm-global/bin/ccusage"),
        "/usr/local/bin/ccusage", "/opt/homebrew/bin/ccusage",
    ]
    for c in cands:
        if c and os.path.exists(c):
            return c
    return ""


def _env():
    """确保 node 在 PATH(ccusage 是 node 脚本;launchd 环境 PATH 受限,本地 node 要补进去)。"""
    env = dict(os.environ)
    extra = [os.path.join(REPO, ".node", "bin"), os.path.expanduser("~/bin"), "/usr/local/bin", "/opt/homebrew/bin"]
    env["PATH"] = os.pathsep.join(extra) + os.pathsep + env.get("PATH", "")
    return env


def _cmd(binp, agent, tz):
    """构造 ccusage 命令。必带 --timezone(否则按本机时区切天,跨时区会错位,见 CLAUDE.md 坑)。"""
    cmd = [binp, agent, "daily", "--json"]
    if tz:
        cmd += ["--timezone", tz]
    return cmd


def _daily(binp, agent, tz):
    try:
        r = subprocess.run(_cmd(binp, agent, tz),
                           capture_output=True, text=True, timeout=40, env=_env())
        if not r.stdout.strip():
            return []
        return (json.loads(r.stdout) or {}).get("daily", [])
    except Exception as e:
        print(f"[ccusage_cli] {agent}: {e}")
        return []


def collect(cfg: dict):
    a = (cfg or {}).get("ai_usage", {})
    if not a.get("enabled"):
        return None
    binp = _bin()
    if not binp:
        return _load()      # ccusage 没找到(可能 launchd PATH 抖动)→ 回放上次,不空窗
    tz = ((cfg or {}).get("server", {}) or {}).get("timezone") or "Asia/Shanghai"
    cc = _daily(binp, "claude", tz)
    cx = _daily(binp, "codex", tz)
    if not cc and not cx:
        return _load()      # 这轮没采到 → 回放上次结果
    frag = {"ccusage": {"ok": True, "cc": {"daily": cc}, "codex": {"daily": cx}}}
    _save(frag)             # 成功采集 → 落盘,供重启/失败时回放
    return frag
