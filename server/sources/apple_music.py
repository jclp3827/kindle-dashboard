"""macOS Music.app 内部采集（替代外部 launchd 推送脚本）。

服务运行在用户 GUI 会话中，直接用 osascript 读取 Music.app 当前播放信息和封面，
不再需要 sync_music.sh / com.kindle-dashboard.music 启动项。
返回 {"_push_music": <推送体>}，由 app.py 的 collect_source 走与 /api/music 相同的处理。
"""
import base64
import hashlib
import os
import subprocess
import tempfile

_SCRIPT_DIR = os.path.join(os.path.dirname(__file__), "apple_scripts")
_MUSIC_JXA = os.path.join(_SCRIPT_DIR, "read_music.js")

# 封面导出 AppleScript（与原 sync_music.sh 一致）
_ARTWORK_OSA = r'''
set outPath to system attribute "KINDLE_MUSIC_ARTWORK_PATH"
try
  tell application "Music"
    if it is running and player state is not stopped then
      set t to current track
      if (count of artworks of t) > 0 then
        set artData to raw data of artwork 1 of t
        set f to open for access POSIX file outPath with write permission
        set eof f to 0
        write artData to f
        close access f
      end if
    end if
  end tell
on error
  try
    close access POSIX file outPath
  end try
end try
'''


def _run(cmd, timeout=20, env=None):
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, env=env)
    except Exception:
        return None


def collect(cfg):
    if not (cfg or {}).get("music", {}).get("enabled", True):
        return None
    if not os.path.exists(_MUSIC_JXA):
        return None

    # 1. 元数据（JXA）
    r = _run(["osascript", "-l", "JavaScript", _MUSIC_JXA], timeout=15)
    if r is None or r.returncode != 0 or not r.stdout.strip():
        # 未授权 / Music 未运行：返回停止态，让页面显示封面墙
        return {"_push_music": {"has_track": False, "state": "stopped",
                                "sampled_at": None}}
    import json
    try:
        data = json.loads(r.stdout.strip())
    except Exception:
        return None

    # 2. 封面（仅在播放时）
    if data.get("has_track"):
        art_fd, art_path = tempfile.mkstemp(suffix=".jpg", prefix="music_art_")
        os.close(art_fd)
        try:
            os.unlink(art_path)
        except OSError:
            pass
        env = dict(os.environ)
        env["KINDLE_MUSIC_ARTWORK_PATH"] = art_path
        try:
            subprocess.run(["osascript"], input=_ARTWORK_OSA, capture_output=True,
                           text=True, timeout=15, env=env)
        except Exception:
            pass
        if os.path.exists(art_path) and os.path.getsize(art_path) > 0:
            with open(art_path, "rb") as f:
                blob = f.read()
            art_hash = "sha256:" + hashlib.sha256(blob).hexdigest()
            data["has_artwork"] = True
            data["artwork_hash"] = art_hash
            data["artwork_mime"] = "image/jpeg"
            data["artwork_data"] = base64.b64encode(blob).decode()
        else:
            data["has_artwork"] = False
        try:
            os.unlink(art_path)
        except OSError:
            pass

    return {"_push_music": data}
