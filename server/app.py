"""主服务:FastAPI app。串起 配置热重载 → 数据采集 → 整合 → 渲染 → 出图,
并提供 Kindle 取图、实时预览、push 接收、设置网页 API。

配置即页面:渲染哪些页由 active_pages(cfg) 决定(数据源没配的页不渲染)。
诚实降级:采集/渲染单点失败只跳过该项,保留旧页;全失败杀僵尸下轮恢复。
"""
import io
import json
import os
import re
import shutil
import socket
import subprocess
import threading
import time
import base64
import hashlib
import random
from contextlib import asynccontextmanager
from datetime import datetime, timezone, timedelta

from fastapi import FastAPI, Body, Request
from fastapi.responses import Response, HTMLResponse, JSONResponse

from server.config import schema
from server.config.loader import ConfigManager
from server.render import styles, pipeline, contract
from server.render.build_context import prep_context
from server.sources import weather, ccusage_cli, homeassistant, metrics, mstodo, rss, downloader, lyrics, apple_music, apple_reminders, codex_quota
from server.sources.ccusage_merge import merge_all_devices
from server import actions

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _resolve_config_path(env=None, new_default=None, old_default=None):
    """配置文件路径 —— **外置到仓库外**(`~/.config/kindle-dashboard/config.yaml`),
    这样 git 升级 / 重装 / 删库重拉都不丢用户配置(凭据/城市/设备全在)。
    `KINDLE_CONFIG` 环境变量可覆盖。
    自动迁移:外置位置还没有、但仓库内有旧 `config.yaml` 时,搬出来一份(老用户无感)。"""
    env = env if env is not None else os.environ.get("KINDLE_CONFIG")
    if env:
        return env
    new = new_default or os.path.expanduser("~/.config/kindle-dashboard/config.yaml")
    old = old_default if old_default is not None else os.path.join(REPO_ROOT, "config.yaml")
    if not os.path.exists(new) and old and os.path.exists(old):
        try:
            os.makedirs(os.path.dirname(new), exist_ok=True)
            shutil.copy2(old, new)
            print(f"[config] 已把配置迁移到 {new}(以后升级/重装不再丢设置)")
        except Exception as e:
            print(f"[config] 迁移失败,沿用旧路径 {old}: {e}")
            return old
    return new


CONFIG_PATH = _resolve_config_path()
WEB_DIR = os.path.join(REPO_ROOT, "web")
DATA_DIR = os.environ.get("KINDLE_DATA_DIR", os.path.join(REPO_ROOT, "data"))
APPLE_REMINDERS_CACHE = os.environ.get(
    "KINDLE_APPLE_REMINDERS_CACHE", os.path.join(DATA_DIR, "apple_reminders.json"))
CCUSAGE_DEVICES_CACHE = os.path.join(DATA_DIR, "ccusage_devices.json")
MUSIC_CACHE = os.environ.get("KINDLE_MUSIC_CACHE", os.path.join(DATA_DIR, "music.json"))
MUSIC_ARTWORK_DIR = os.environ.get("KINDLE_MUSIC_ARTWORK_DIR", os.path.join(DATA_DIR, "music_artwork"))
MUSIC_ARTWORK_INDEX = os.path.join(MUSIC_ARTWORK_DIR, "index.json")

# 推送 agent 脚本(被监控机 curl 下载):白名单路径,纯文本下发
AGENT_FILES = {
    "install.sh":        os.path.join(REPO_ROOT, "installers", "push-agent", "install_agent.sh"),
    "push_agent.sh":     os.path.join(REPO_ROOT, "installers", "push-agent", "push_agent.sh"),
    "collect_linux.sh":  os.path.join(REPO_ROOT, "server", "sources", "collectors", "collect_linux.sh"),
    "collect_macos.sh":  os.path.join(REPO_ROOT, "server", "sources", "collectors", "collect_macos.sh"),
    "install.ps1":       os.path.join(REPO_ROOT, "installers", "push-agent", "install_agent.ps1"),
    "push_agent.ps1":    os.path.join(REPO_ROOT, "installers", "push-agent", "push_agent.ps1"),
    "collect_windows.ps1": os.path.join(REPO_ROOT, "server", "sources", "collectors", "collect_windows.ps1"),
    # Mac 独立推送安装器(NAS 部署用,不需要 clone 仓库)
    "install_reminders.sh": os.path.join(REPO_ROOT, "installers", "mac-push", "install_reminders.sh"),
    "install_ccusage.sh":   os.path.join(REPO_ROOT, "installers", "mac-push", "install_ccusage.sh"),
    "install_quota.sh":     os.path.join(REPO_ROOT, "installers", "mac-push", "install_quota.sh"),
    "install_music.sh":     os.path.join(REPO_ROOT, "installers", "mac-push", "install_music.sh"),
    "read_reminders.js":    os.path.join(REPO_ROOT, "installers", "macos", "reminders", "read_reminders.js"),
    "read_music.js":        os.path.join(REPO_ROOT, "installers", "macos", "music", "read_music.js"),
    "claude_statusline.py": os.path.join(REPO_ROOT, "installers", "macos", "quota", "claude_statusline.py"),
    "codex_quota.py":       os.path.join(REPO_ROOT, "installers", "macos", "quota", "codex_quota.py"),
}

# 采集器模块名 → (配置段, 字段, 默认秒)。间隔放在各源自己的配置段里(随源卡),不再集中。
SOURCE_INTERVAL = {"weather":       ("weather", "interval", 600),
                   "ccusage_cli":   ("ai_usage", "interval", 300),
                   "homeassistant": ("home_assistant", "interval", 60),
                   "metrics":       ("devices", "interval", 30),
                   "mstodo":        ("mstodo", "interval", 600),
                   "rss":           ("news", "interval", 1800),
                   "downloader":    ("downloaders", "interval", 15),
                   "apple_music":    ("music", "interval", 10),
                   "apple_reminders": ("reminders", "interval", 300),
                   "codex_quota":   ("ai_usage", "codex_quota_interval", 600)}
# 渲染间隔放在「服务」段
RENDER_INTERVAL = ("server", "render_interval", 30)

cm = ConfigManager(CONFIG_PATH)

cache = {}
cache_lock = threading.Lock()

# 歌词缓存:track_id → [{t,text}](换歌才联网查一次,后台线程不阻塞推送)。
# 空列表 [] 也缓存,表示"查过但没有",避免每帧重查;_LYRICS_INFLIGHT 防并发重复查。
_LYRICS_CACHE = {}
_LYRICS_INFLIGHT = set()
_LYRICS_LOCK = threading.Lock()
_LYRICS_CACHE_MAX = 64


def _lyrics_get(track_id):
    """读歌词缓存(供 _music_payload 挂到 payload)。未缓存 → []。"""
    if not track_id:
        return []
    with _LYRICS_LOCK:
        return list(_LYRICS_CACHE.get(track_id, []))


def _lyrics_worker(track_id, name, artist, duration, album):
    """后台线程:查歌词 → 缓存 → 补进当前 cache['music'] 并落盘(若仍是这首)。"""
    try:
        result = lyrics.fetch_lyrics(name, artist, duration, album)
    except Exception as e:
        print(f"[lyrics] 查询出错 {name}:{e}")
        result = []
    with _LYRICS_LOCK:
        if len(_LYRICS_CACHE) >= _LYRICS_CACHE_MAX:   # 简单淘汰:清掉最早插入的一半(dict 保序)
            for k in list(_LYRICS_CACHE)[: _LYRICS_CACHE_MAX // 2]:
                _LYRICS_CACHE.pop(k, None)
        _LYRICS_CACHE[track_id] = result
        _LYRICS_INFLIGHT.discard(track_id)
    with cache_lock:
        m = cache.get("music")
        if isinstance(m, dict) and (m.get("track_id") or "") == track_id:
            m["lyrics"] = result
            try:
                _atomic_json_write(MUSIC_CACHE, _music_payload_for_disk(m))
            except Exception:
                pass
    print(f"[lyrics] {name} — {artist}:{len(result)} 行")


def _maybe_fetch_lyrics(payload):
    """换歌(track_id 未缓存且未在查)时,起后台线程查歌词。开关关 / 无曲目 → 跳过。"""
    if not payload.get("has_track"):
        return
    if not (cm.get().get("music", {}) or {}).get("lyrics_enabled", True):
        return
    track_id = payload.get("track_id") or ""
    name = payload.get("name") or ""
    if not track_id or not name or name == "--":
        return
    with _LYRICS_LOCK:
        if track_id in _LYRICS_CACHE or track_id in _LYRICS_INFLIGHT:
            return
        _LYRICS_INFLIGHT.add(track_id)
    threading.Thread(
        target=_lyrics_worker,
        args=(track_id, name, payload.get("artist") or "",
              payload.get("duration") or None, payload.get("album") or ""),
        daemon=True,
    ).start()

RENDERED = {}            # {page_key: png bytes}
RENDER_ORDER = []        # 当前轮播顺序
LEGACY_FRAMES = {}       # 安卓 4.x 图片相框出口:{page_key: (800x600 彩色 png bytes, 渲染时刻)}
                         # 惰性按需:只在真有老安卓拉 /app-legacy/frame.png 时渲当前那一页(带 TTL 缓存)。
LEGACY_FRAMES_LOCK = threading.Lock()
RENDER_LOCK = threading.Lock()
CURRENT = {"style": None}
page_state = {"i": 0, "last": 0.0}
KINDLE_PULL = {"last": 0.0, "ip": ""}   # Kindle 最近一次拉帧时间/IP（比 SSH 更可靠的在播证据）
PAUSED = {"on": False}   # 暂停时：停止所有数据源采集、照片预取/处理与翻页，Kindle 停在当前帧（服务仍可访问）
CLEAR_CMD = {"on": False, "ts": 0.0}   # 一键清屏指令：设置页点「一键清屏」置位，Kindle 下一次拉帧读到即执行（读后复位）
legacy_page_state = {"i": 0, "last": 0.0}

SOURCES = (weather, ccusage_cli, homeassistant, metrics, mstodo, rss, downloader, apple_music, apple_reminders, codex_quota)
CONFIG_SAVE_SYNC_SOURCES = (weather,)


def _atomic_json_write(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)
    os.replace(tmp, path)


def _apple_payload(data):
    reminders = data.get("reminders", [])
    if not isinstance(reminders, list):
        reminders = []
    return {
        "updated_at": data.get("updated_at") or datetime.now(timezone.utc).isoformat(),
        "reminders": reminders,
    }


def _fmt_push_hm(ts):
    try:
        ts = float(ts)
    except (TypeError, ValueError):
        ts = time.time()
    return datetime.fromtimestamp(ts, _tz(cm.get())).strftime("%H:%M")


def _music_cfg_int(key, default, lo=0, hi=10000):
    try:
        value = int(((cm.get().get("music", {}) or {}).get(key, default)) or default)
    except (TypeError, ValueError):
        value = default
    return max(lo, min(hi, value))


def _music_load_artwork_index():
    try:
        with open(MUSIC_ARTWORK_INDEX, encoding="utf-8") as f:
            data = json.load(f) or {}
    except FileNotFoundError:
        return []
    except Exception as e:
        print(f"[music-push] 读取封面缓存索引失败:{e}")
        return []
    items = data.get("items") if isinstance(data, dict) else []
    return items if isinstance(items, list) else []


def _music_write_artwork_index(items):
    _atomic_json_write(MUSIC_ARTWORK_INDEX, {"items": items})


def _music_cache_artwork(art_hash, blob, mime, meta, sampled_at):
    if not blob:
        return ""
    if mime not in ("image/jpeg", "image/png", "image/webp"):
        mime = "image/jpeg"
    if not art_hash:
        art_hash = hashlib.sha256(blob).hexdigest()
    safe_hash = re.sub(r"[^A-Za-z0-9_.-]", "_", art_hash)[:96]
    ext = {"image/png": ".png", "image/webp": ".webp"}.get(mime, ".jpg")
    filename = f"{safe_hash}{ext}"
    os.makedirs(MUSIC_ARTWORK_DIR, exist_ok=True)
    path = os.path.join(MUSIC_ARTWORK_DIR, filename)
    if not os.path.exists(path):
        with open(path, "wb") as f:
            f.write(blob)

    items = _music_load_artwork_index()
    by_hash = {it.get("hash"): it for it in items if isinstance(it, dict)}
    old = by_hash.get(art_hash) or {}
    item = {
        "hash": art_hash,
        "path": filename,
        "mime": mime,
        "first_seen": old.get("first_seen") or sampled_at,
        "last_seen": sampled_at,
        "name": meta.get("name") or old.get("name", ""),
        "artist": meta.get("artist") or old.get("artist", ""),
        "album": meta.get("album") or old.get("album", ""),
    }
    by_hash[art_hash] = item
    items = sorted(by_hash.values(), key=lambda it: float(it.get("last_seen") or 0), reverse=True)
    limit = _music_cfg_int("artwork_cache_limit", 120, 0, 500)
    if limit:
        keep, drop = items[:limit], items[limit:]
    else:
        keep, drop = [], items
    for it in drop:
        try:
            p = os.path.join(MUSIC_ARTWORK_DIR, it.get("path", ""))
            if p.startswith(MUSIC_ARTWORK_DIR) and os.path.isfile(p):
                os.remove(p)
        except Exception:
            pass
    _music_write_artwork_index(keep)
    return art_hash


def _music_artwork_wall():
    """静态目标(Kindle/web-simple)的封面墙:每次调用**真随机**抽 count 张 → data URI。
    渲染循环每帧重抽(render_all 注入),所以 Kindle 每次刷到屏保都是新的一批(不再 5 分钟才换)。
    封面转 base64 内嵌,因为 Kindle 出图是 file:// 临时 HTML,相对/HTTP 图不稳。"""
    count = _music_cfg_int("artwork_wall_count", 20, 1, 40)
    items = [it for it in _music_load_artwork_index() if isinstance(it, dict) and it.get("path")]
    if not items:
        return []
    k = min(count, len(items))
    out = []
    for it in random.sample(items, k):
        path = os.path.join(MUSIC_ARTWORK_DIR, it.get("path", ""))
        try:
            with open(path, "rb") as f:
                raw = base64.b64encode(f.read()).decode()
        except Exception:
            continue
        mime = it.get("mime") or "image/jpeg"
        out.append({
            "url": f"data:{mime};base64,{raw}",
            "hash": it.get("hash", ""),
            "album": it.get("album", ""),
            "artist": it.get("artist", ""),
        })
    return out


# 动态目标(/app、/app-legacy)的「渐换封面墙」状态:服务端维护一组当前显示的封面,
# 每 artwork_swap_interval 秒漂移 1~artwork_swap_max 张(Apple TV 式)。所有动态客户端
# 轮询看到同一组、同步漂移;只有变动的几张 <img> 的 URL 变 → 浏览器命中缓存不闪。
_DYN_WALL = {"tiles": [], "ts": 0.0}    # tiles: 当前显示的 hash 有序列表;ts: 上次漂移时刻
_DYN_WALL_LOCK = threading.Lock()


def _music_artwork_pool(limit):
    """全部缓存封面的轻量元数据(只 hash/album/artist,**不读图不 base64**),按 last_seen 倒序取前 limit。
    给动态版漂移墙当「池子」用;真正的图经 /music/artwork/<hash> 单独按需拉。"""
    items = [it for it in _music_load_artwork_index()
             if isinstance(it, dict) and it.get("hash") and it.get("path")]
    items.sort(key=lambda it: float(it.get("last_seen") or 0), reverse=True)
    return items[:max(0, limit)]


def _wall_show_count(n):
    """按封面数退到的实际显示格子数——**必须与 build_context._music_wall_layout 的档位一致**
    (1/2/4/9/16/20 向下取),否则漂移会误以为在操作没显示出来的格子(如 5 张实际只显示 4)。"""
    if n <= 0:
        return 0
    if n == 1:
        return 1
    if n < 4:
        return 2
    if n < 9:
        return 4
    if n < 16:
        return 9
    if n < 20:
        return 16
    return 20


def _dyn_wall_tiles():
    """动态版当前应显示的封面墙(漂移后)。返回 [{hash, album, artist, url:""}](url 空,模板按 hash 拼 HTTP URL)。
    显示格子数按布局档位退档(与静态墙一致),多出来的封面留作「备用池」。
    规则(只要显示数 ≥2 就一直在动):到点(now-ts>=interval)随机挑 1~swap_max 个格子,逐格——
      · 备用池里有「还没显示的图」→ 换成新图(真·渐换;如 5 张显示 4、第 5 张轮流上场);
      · 没有新图(池子==显示数,已满铺,如正好 4 张)→ 与另一个随机格子对调位置(画面照动、不重复显示);
    只有 1 张时无格可换/可调,静止(物理必然)。"""
    count = _music_cfg_int("artwork_wall_count", 20, 1, 40)
    pool_size = _music_cfg_int("artwork_pool_size", 40, 1, 200)
    interval = _music_cfg_int("artwork_swap_interval", 4, 1, 600)
    swap_max = _music_cfg_int("artwork_swap_max", 3, 1, 40)
    pool = _music_artwork_pool(pool_size)
    if not pool:
        return []
    by_hash = {it["hash"]: it for it in pool}
    pool_hashes = list(by_hash.keys())
    # 显示数按档位退档(如 5 张→显示 4),剩下的就是漂移可用的备用图。avail 先夹到 count 上限。
    show_n = _wall_show_count(min(count, len(pool_hashes)))
    now = time.time()
    with _DYN_WALL_LOCK:
        cur = [h for h in _DYN_WALL["tiles"] if h in by_hash]    # 丢掉已被缓存淘汰的
        if len(cur) < show_n:                                    # 初次/缓存新增:补满
            spare = [h for h in pool_hashes if h not in cur]
            random.shuffle(spare)
            cur += spare[:show_n - len(cur)]
            _DYN_WALL["ts"] = now
        elif len(cur) > show_n:                                  # 缓存缩水:截断
            cur = cur[:show_n]
        if show_n >= 2 and now - _DYN_WALL["ts"] >= interval:    # 到点漂移
            k = random.randint(1, min(swap_max, show_n))
            for slot in random.sample(range(show_n), k):
                shown = set(cur)
                spare = [h for h in pool_hashes if h not in shown]
                if spare:                                        # 有新图:换上
                    cur[slot] = random.choice(spare)
                elif show_n >= 2:                                # 满铺:与另一格对调位置
                    others = [i for i in range(show_n) if i != slot]
                    j = random.choice(others)
                    cur[slot], cur[j] = cur[j], cur[slot]
            _DYN_WALL["ts"] = now
        _DYN_WALL["tiles"] = list(cur)
    return [{"hash": h, "album": by_hash[h].get("album", ""),
             "artist": by_hash[h].get("artist", ""), "url": ""} for h in cur]


def _inject_dyn_wall(snap):
    """动态路由(/app、/app-legacy)渲染前,把屏保封面墙换成服务端漂移墙(就地改 snap)。
    只在屏保态(停播/暂停)替换;有歌在放时模板根本不显墙,替不替都无影响,故只在该态省一次漂移计算。"""
    mus = snap.get("music")
    if not isinstance(mus, dict):
        return
    if mus.get("has_track") and mus.get("state") != "paused":
        return
    tiles = _dyn_wall_tiles()
    if tiles:
        mus = dict(mus)
        mus["artwork_wall"] = tiles
        snap["music"] = mus


def _music_payload(data, previous=None):
    """Mac Music agent 上报 → 扁平 cache["music"]。
    封面第一版用 data URI,因为 Kindle 渲染链路是 file:// 临时 HTML,相对 HTTP 图片不稳定。
    agent 只在封面 hash 变化时上传 artwork_data;hash 不变时沿用 previous.artwork_url。"""
    previous = previous or {}
    sampled_at = data.get("sampled_at") or time.time()
    updated_at = _fmt_push_hm(sampled_at)
    has_track = bool(data.get("has_track"))
    if not has_track:
        return {
            "available": True,
            "has_track": False,
            "state": "stopped",
            "sampled_at": sampled_at,
            "state_since": previous.get("state_since") if previous.get("state") == "stopped" else sampled_at,
            "artwork_wall": _music_artwork_wall(),
            "updated_at": updated_at,
        }

    def num(name, default=0):
        try:
            return float(data.get(name, default) or default)
        except (TypeError, ValueError):
            return default

    rep = (data.get("repeat") or "off").strip().lower()
    if rep not in ("off", "all", "one"):
        rep = "off"
    st = (data.get("state") or "stopped").strip().lower()
    if st not in ("playing", "paused", "stopped"):
        st = "stopped"
    art_url = ""
    art_hash = data.get("artwork_hash") or ""
    if data.get("has_artwork"):
        raw = data.get("artwork_data") or ""
        mime = (data.get("artwork_mime") or "image/jpeg").split(";", 1)[0]
        if raw:
            if len(raw) > 3_000_000:
                raise ValueError("artwork too large")
            try:
                blob = base64.b64decode(raw, validate=True)
                if mime not in ("image/jpeg", "image/png", "image/webp"):
                    mime = "image/jpeg"
                art_hash = _music_cache_artwork(art_hash, blob, mime, data, sampled_at) or art_hash
                art_url = f"data:{mime};base64,{raw}"
            except Exception:
                art_url = ""
        elif art_hash and art_hash == previous.get("artwork_hash"):
            art_url = previous.get("artwork_url", "") or ""
    track_id = data.get("track_id") or data.get("persistent_id") or ""
    prev_same_state = previous.get("has_track") and previous.get("state") == st
    prev_same_track = (previous.get("track_id") or previous.get("persistent_id") or "") == track_id
    state_since = previous.get("state_since") if (prev_same_state and prev_same_track) else sampled_at
    payload = {
        "available": True,
        "has_track": True,
        "state": st,
        "state_since": state_since or sampled_at,
        "track_id": track_id,
        "persistent_id": data.get("persistent_id") or "",
        "database_id": data.get("database_id") or 0,
        "name": data.get("name") or "--",
        "artist": data.get("artist") or "",
        "album": data.get("album") or "",
        "album_artist": data.get("album_artist") or "",
        "composer": data.get("composer") or "",
        "genre": data.get("genre") or "",
        "year": str(data.get("year") or ""),
        "duration": num("duration", 0),
        "position": num("position", 0),
        "sampled_at": sampled_at,
        "shuffle": bool(data.get("shuffle", False)),
        "repeat": rep,
        "track_number": int(num("track_number", 0)),
        "track_count": int(num("track_count", 0)),
        "loved": bool(data.get("loved", False)),
        "play_count": int(num("play_count", 0)),
        "artwork_hash": art_hash,
        "artwork_url": art_url,
        "artwork_wall": _music_artwork_wall(),
        "updated_at": updated_at,
        "lyrics": _lyrics_get(track_id),
    }
    return payload


def _music_payload_for_disk(payload):
    disk = dict(payload or {})
    # artwork_wall 是渲染用 data URI 列表,可能很大;封面本体已在 MUSIC_ARTWORK_DIR,
    # 重启时 _load_music_cache 会从索引重建,不要写进 music.json。
    disk.pop("artwork_wall", None)
    return disk


def _load_apple_reminders_cache():
    """服务重启后回放上次 Apple 提醒,避免等下一轮 launchd 推送前首页空掉。"""
    try:
        with open(APPLE_REMINDERS_CACHE, encoding="utf-8") as f:
            payload = json.load(f) or {}
    except FileNotFoundError:
        return
    except Exception as e:
        print(f"[apple-sync] 读取本地缓存失败:{e}")
        return
    reminders = payload.get("reminders") or []
    if not isinstance(reminders, list):
        return
    with cache_lock:
        cache["reminders"] = reminders
        cache["apple_updated"] = payload.get("updated_at")
    print(f"[apple-sync] 已加载本地缓存 {len(reminders)} 条")


def _load_music_cache():
    """服务重启后回放上一帧 Music 状态,避免音乐页先空掉。"""
    try:
        with open(MUSIC_CACHE, encoding="utf-8") as f:
            payload = json.load(f) or {}
    except FileNotFoundError:
        return
    except Exception as e:
        print(f"[music-push] 读取本地缓存失败:{e}")
        return
    if not isinstance(payload, dict):
        return
    payload["artwork_wall"] = _music_artwork_wall()
    with cache_lock:
        cache["music"] = payload
    tid = payload.get("track_id") or ""        # 种回歌词缓存,重启后同曲不重查
    if tid and isinstance(payload.get("lyrics"), list):
        with _LYRICS_LOCK:
            _LYRICS_CACHE[tid] = payload["lyrics"]
    print(f"[music-push] 已加载本地缓存:{'有曲目' if payload.get('has_track') else '无播放'}")


def _is_local_host(host):
    host = (host or "").strip().lower().strip("[]")
    return host in {"localhost", "0.0.0.0", "::", "::1"} or host.startswith("127.")


def _valid_lan_ip(ip):
    return bool(ip and not ip.startswith(("127.", "169.254.")) and ip != "0.0.0.0")


def _lan_priority(ip):
    """局域网地址优先级(越小越优先)。常见家用/办公 LAN 段(RFC1918)优先;
    VPN/代理 TUN(Clash 等用 198.18.0.0/15)、CGNAT(100.64/10)等垫底,
    避免开着代理时把 198.18.0.1 这种虚拟网卡地址当成看板局域网地址。"""
    if ip.startswith("192.168."):
        return 0
    if ip.startswith("10."):
        return 1
    if ip.startswith("172."):
        try:
            if 16 <= int(ip.split(".")[1]) <= 31:    # 172.16.0.0/12
                return 2
        except (ValueError, IndexError):
            pass
    return 9   # 198.18.x(代理 TUN)/100.64.x(CGNAT)等非典型 LAN,排最后


def _lan_ips():
    """尽力找本机可被局域网访问的 IPv4;第一个优先用于生成远程 agent 命令。"""
    ips = []

    def add(ip):
        if _valid_lan_ip(ip) and ip not in ips:
            ips.append(ip)

    for target in ("8.8.8.8", "1.1.1.1"):
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect((target, 80))
            add(s.getsockname()[0])
            s.close()
            break
        except Exception:
            try:
                s.close()
            except Exception:
                pass

    try:
        for ip in socket.gethostbyname_ex(socket.gethostname())[2]:
            add(ip)
    except Exception:
        pass

    for cmd in (("ip", "-4", "-o", "addr", "show", "scope", "global"), ("ifconfig",)):
        try:
            out = subprocess.check_output(cmd, text=True, stderr=subprocess.DEVNULL, timeout=2)
        except Exception:
            continue
        for ip in re.findall(r"\binet\s+(\d+\.\d+\.\d+\.\d+)", out):
            add(ip)
    # 按家用 LAN 段优先排序(稳定排序保留同级原有顺序):真实局域网 IP 排前,
    # 代理/VPN 的 198.18.x 之类垫底,recommended=ips[0] 才不会误选虚拟网卡地址。
    ips.sort(key=_lan_priority)
    return ips


def _local_hostname_url(scheme, port):
    """本机 mDNS `.local` 地址(如 http://Xxx.local:8585)。
    支持 mDNS 的设备(多数 Mac / Linux / 手机)用它当看板地址,可**绕开 IP 漂移**(IP 变了名字不变);
    不支持 mDNS 的设备(如部分 Kindle busybox)忽略即可。取不到名字则返回 ''(不臆造)。"""
    name = ""
    try:                                  # macOS:LocalHostName 就是 .local 名(不含后缀)
        r = subprocess.run(["scutil", "--get", "LocalHostName"],
                           capture_output=True, text=True, timeout=2)
        if r.returncode == 0:
            name = r.stdout.strip()
    except Exception:
        pass
    if not name:                          # 非 macOS:仅当主机名本身就是 .local 才用,不凭空造
        try:
            h = socket.gethostname().strip()
            if h.endswith(".local"):
                name = h[:-6]
        except Exception:
            pass
    if not name or any(c in name for c in " \t/\\"):
        return ""
    return f"{scheme}://{name}.local:{port}"


_load_apple_reminders_cache()
_load_music_cache()


def _load_ccusage_devices_cache():
    """服务重启后回放各设备推送的 ccusage 数据,重算合并结果,避免重启后空窗。"""
    try:
        with open(CCUSAGE_DEVICES_CACHE, encoding="utf-8") as f:
            by_device = json.load(f) or {}
    except FileNotFoundError:
        return
    except Exception as e:
        print(f"[ccusage-push] 读取设备缓存失败:{e}")
        return
    if not isinstance(by_device, dict) or not by_device:
        return
    merged = merge_all_devices(by_device)
    with cache_lock:
        cache.setdefault("ccusage_by_device", {}).update(by_device)
        cache["ccusage"] = merged
    print(f"[ccusage-push] 已加载 {len(by_device)} 台设备的缓存")


_load_ccusage_devices_cache()


def _tz(cfg):
    name = cfg.get("server", {}).get("timezone", "Asia/Shanghai")
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo(name)
    except Exception:
        return timezone(timedelta(hours=8))


# ---------- 采集 + 渲染 ----------
def _merge(frag):
    if not frag:
        return
    with cache_lock:
        for k, v in frag.items():
            if k == "devices_metrics":
                # 只更新本轮成功项;单台临时失败保留上一帧(不删)。改名/删除的旧指标由
                # _prune_pull_device_cache(配置保存时)剪——别在这无脑删非 push 项,否则 A 成功 B 失败时 B 凭空消失。
                cache.setdefault("devices_metrics", {}).update(v)
            else:
                cache[k] = v


def _prune_pull_device_cache(cfg):
    """配置保存后剪掉已改名/已删除的本机或 SSH 指标;push 设备继续保留供发现区使用。"""
    machines = ((cfg or {}).get("devices", {}) or {}).get("machines", []) or []
    keep = {
        (m.get("id") or "").strip() or (m.get("name") or "").strip()
        for m in machines
        if (m.get("mode") or "local") != "push"
    }
    keep = {x for x in keep if x}
    with cache_lock:
        cur = cache.get("devices_metrics") or {}
        for key, val in list(cur.items()):
            if not val.get("updated_at") and key not in keep:
                cur.pop(key, None)


def _route_internal_push(data):
    """内部采集源（apple_music/apple_reminders/codex_quota）返回的推送体，
    走与外部 push 端点相同的处理逻辑，保证缓存结构一致。"""
    if not data:
        return False
    touched = False
    if "_push_music" in data:
        raw = data["_push_music"]
        try:
            with cache_lock:
                prev = cache.get("music") or {}
                payload = _music_payload(raw, prev)
                cache["music"] = payload
            _maybe_fetch_lyrics(payload)
            try:
                _atomic_json_write(MUSIC_CACHE, _music_payload_for_disk(payload))
            except Exception:
                pass
            touched = True
        except Exception as e:
            print(f"[collect] apple_music payload: {e}")
    if "_push_apple" in data:
        raw = data["_push_apple"]
        try:
            payload = _apple_payload(raw)
            with cache_lock:
                cache["reminders"] = payload["reminders"]
                cache["apple_updated"] = payload["updated_at"]
            try:
                _atomic_json_write(APPLE_REMINDERS_CACHE, payload)
            except Exception:
                pass
            touched = True
        except Exception as e:
            print(f"[collect] apple_reminders payload: {e}")
    if "_push_rate_limits" in data:
        raw = data["_push_rate_limits"]
        try:
            if raw.get("source") == "codex":
                with cache_lock:
                    cache["codex_rate_limits"] = raw.get("rate_limits")
            else:
                with cache_lock:
                    cache["rate_limits"] = raw.get("rate_limits")
            touched = True
        except Exception as e:
            print(f"[collect] codex_quota payload: {e}")
    return touched


def collect_source(src, cfg):
    try:
        data = src.collect(cfg)
        if data and any(k.startswith("_push_") for k in data):
            ok = _route_internal_push(data)
            return data if ok else None
        _merge(data)
        return data            # 真拿到数据(非 None/非空)→ 真值;失败/无源 → None/空(供冷启动快速重试判断)
    except Exception as e:
        print(f"[collect] {src.__name__}: {e}")
    return None


def _page_meta(cfg, style, page_key):
    """页脚动态页码:按「实际启用页顺序」(active_pages,反映用户手动排序)算 (当前页号, 总页数)。
    只数该 style 真有模板的页;page_key 不在其中兜底 1。"""
    pages = [p for p in schema.active_pages(cfg) if styles.has_page(style, p)]
    total = len(pages) or 1
    try:
        return pages.index(page_key) + 1, total
    except ValueError:
        return 1, total


def render_all(cfg):
    now = datetime.now(_tz(cfg))
    style = styles.pick_style(cfg, now.date())
    if not style:
        return
    rc = pipeline.RenderConfig.from_config(cfg)
    pages = schema.active_pages(cfg)
    with cache_lock:
        snap = dict(cache)
    # 静态封面墙每帧重抽:Kindle 每次刷到音乐屏保都是新的随机一批(屏保页内容变是有意的;
    # 其余页/有歌曲页零回归)。只在该重画一组时换 music.artwork_wall,不动别的。
    mus = snap.get("music")
    if isinstance(mus, dict) and mus.get("artwork_wall"):
        mus = dict(mus)
        mus["artwork_wall"] = _music_artwork_wall()
        snap["music"] = mus
    ctx = prep_context(now, snap, cfg)
    new = {}
    for pk in pages:
        if not styles.has_page(style, pk):
            continue
        if pk == "photo":
            continue  # photo页由frame端点动态渲染,不在这里静态渲染
        ctx["page_no"], ctx["page_total"] = _page_meta(cfg, style, pk)
        try:
            html = styles.render_page(style, pk, ctx)
        except Exception as e:
            print(f"[render] {pk}: {e}")
            continue
        try:
            new[pk] = pipeline.render_html_to_png(html, rc)
        except Exception as e:
            print(f"[render] {pk}: {e}")
    # legacy(安卓 4.x 图片相框)彩图不在这里渲——改成 /app-legacy/frame.png 惰性按需(只渲在看的那页)。
    # 没老安卓设备连着时零开销;详见 app_legacy_frame。
    if not new:
        if pages:        # 有启用页却全失败=真僵尸,才清理;无启用页(还没配数据源)不算失败,别瞎杀
            pipeline.kill_stale_chrome()
            print("[render] 全部失败,已杀僵尸,下轮恢复")
        return
    with RENDER_LOCK:
        for pk in new:
            RENDERED[pk] = new[pk]
        RENDER_ORDER[:] = [p for p in pages if p in RENDERED or p == "photo"]
        CURRENT["style"] = style


def _interval(cfg, section, field, default):
    """取某配置段某字段的间隔(秒)。缺失/非法 → 回落默认;最低 5 秒防忙转。"""
    try:
        v = int((cfg.get(section, {}) or {}).get(field))
    except (TypeError, ValueError):
        v = 0
    return max(5, v) if v else default


def source_loop(src):
    """每个数据源一条独立线程,按各自间隔采集,互不阻塞(慢源如 ccusage 不再拖累渲染)。
    **冷启动快速重试**:本源还没成功拿到过数据时(如启动瞬间网络没就绪、RSS 拉空),
    用 ≤30s 短间隔重试、不空等整个采集间隔(否则 RSS 30 分钟/次会让 news 页空置半小时)。
    重试窗口限前 5 分钟,避免没配的源长期空转。"""
    section, field, default = SOURCE_INTERVAL.get(src.__name__.rsplit(".", 1)[-1], RENDER_INTERVAL)
    t0 = time.time()
    got = False
    while True:
        if PAUSED["on"]:
            time.sleep(1); continue
        cfg = cm.get()
        if collect_source(src, cfg):
            got = True
        interval = _interval(cfg, section, field, default)
        if not got and (time.time() - t0) < 300:
            interval = min(interval, 30)
        time.sleep(interval)


def render_loop():
    """渲染独立线程:按 render 间隔从缓存出图,不受采集快慢影响(时钟永不冻)。
    并负责热重载配置(唯一调 maybe_reload 的线程,避免多线程竞争)。"""
    while True:
        if PAUSED["on"]:
            time.sleep(1); continue
        cm.maybe_reload()
        cfg = cm.get()
        try:
            render_all(cfg)
        except Exception as e:
            print(f"[render_all] {e}")
        time.sleep(_interval(cfg, *RENDER_INTERVAL))


def _ensure_access_token():
    """首次启动若没令牌则生成一个,写进 config 并打印带令牌的设置页链接。
    令牌保护设置/配置接口(见 _auth 中间件);Kindle 拉图、设备上报不受影响。"""
    if (cm.get().get("server", {}) or {}).get("access_token"):
        return
    import secrets
    tok = secrets.token_urlsafe(16)
    cm.force_set("server", "access_token", tok)   # 绕过全量校验,别被 config 别处的错误挡掉令牌生成
    port = (cm.get().get("server", {}) or {}).get("port", 8585)
    print("[auth] 已生成设置页访问令牌(只此一份,请记下)。用此链接打开设置页:")
    print(f"       http://<本机IP>:{port}/setup?token={tok}")


def _log_rotate_loop():
    """日志看门狗:每小时把 data/*.log 超 5MB 的截断到只保留最近约 1MB,
    防 launchd 重定向的 service/menubar/codex-quota/reminders 日志长期跑爆盘。"""
    import glob
    MAX, KEEP = 5 * 1024 * 1024, 1 * 1024 * 1024
    while True:
        try:
            for f in glob.glob(os.path.join(DATA_DIR, "*.log")):
                try:
                    if os.path.getsize(f) > MAX:
                        with open(f, "rb") as fp:
                            fp.seek(-KEEP, os.SEEK_END)
                            tail = fp.read()
                        with open(f, "wb") as fp:
                            fp.write("...(日志已轮转,仅保留最近部分)...\n".encode() + tail)
                except Exception:
                    pass
        except Exception:
            pass
        time.sleep(3600)


# ---------- mDNS 局域网广播(让安卓 App 自动发现看板服务器,免手敲 IP) ----------
def _primary_ipv4():
    """本机在局域网里的主 IPv4(连一下外网取出网网卡地址,不真发包)。取不到返回 ''。"""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return ""


_mdns = {"zc": None, "info": None}


def _start_mdns(port):
    """广播 `_kindledash._tcp.local.` 服务,TXT 带 path=/app + name=主机名。
    zeroconf 没装 / 取不到 IP / 注册失败都**静默跳过**(不影响服务起来;App 端永远保留手填/扫码兜底)。
    NAS Docker 桥接网络下 mDNS 出不了容器,需 host network 才有效(见 docs)。"""
    try:
        from zeroconf import Zeroconf, ServiceInfo
    except Exception as e:
        print(f"[mDNS] 未安装 zeroconf,跳过局域网广播(pip install zeroconf 后重启即可):{e}")
        return
    ip = _primary_ipv4()
    if not ip:
        print("[mDNS] 取不到本机 IP,跳过广播")
        return
    try:
        host = (socket.gethostname() or "kindle-dashboard").split(".")[0]
        safe = "".join(c for c in host if c.isalnum() or c in "-_") or "dashboard"
        info = ServiceInfo(
            "_kindledash._tcp.local.",
            f"{safe}._kindledash._tcp.local.",
            addresses=[socket.inet_aton(ip)],
            port=int(port),
            properties={"path": "/app", "name": host},
            server=f"{safe}.local.",
        )
        zc = Zeroconf()
        zc.register_service(info)
        _mdns["zc"], _mdns["info"] = zc, info
        print(f"[mDNS] 已广播 _kindledash._tcp → {ip}:{port}(名:{host})")
    except Exception as e:
        print(f"[mDNS] 注册失败,跳过:{e}")


def _stop_mdns():
    zc = _mdns.get("zc")
    if not zc:
        return
    try:
        if _mdns.get("info"):
            zc.unregister_service(_mdns["info"])
        zc.close()
    except Exception:
        pass
    _mdns["zc"] = _mdns["info"] = None


@asynccontextmanager
async def lifespan(_app):
    _ensure_access_token()
    pipeline.kill_stale_chrome()   # 清上一轮残留的渲染 Chrome(重启即自动扫除僵尸,免手动 pkill)
    port = (cm.get().get("server", {}) or {}).get("port", 8585)
    _start_mdns(port)
    for src in SOURCES:
        threading.Thread(target=source_loop, args=(src,), daemon=True).start()
    threading.Thread(target=render_loop, daemon=True).start()
    threading.Thread(target=_log_rotate_loop, daemon=True).start()
    yield
    _stop_mdns()


app = FastAPI(lifespan=lifespan)


# ---------- 访问鉴权:令牌保护设置/配置接口;Kindle 拉图/设备上报/health 豁免 ----------
from fastapi import Request  # noqa: E402

# 豁免前缀:Kindle 只拉 frame.png / page/* / clear-cmd(清屏指令,Kindle 带不了令牌);agent 下发、health、setup 空壳页都放行
_AUTH_EXEMPT_PREFIXES = ("/kindle/frame.png", "/kindle/page/", "/kindle/photo.png", "/kindle/clear-cmd", "/agent/", "/health", "/setup")
# 豁免精确路径:设备主动上报的接口(push 进来,Kindle/agent 调,带不了令牌)
_AUTH_EXEMPT_EXACT = {"/", "/api/device-metrics", "/api/apple-sync", "/api/music",
                      "/api/rate-limits", "/api/kindle-status", "/api/ccusage",
                      "/qrcode.js"}   # 公共 MIT QR 库(设置页 <script src> 加载,无密钥,豁免)


@app.middleware("http")
async def _auth(request: Request, call_next):
    token = (cm.get().get("server", {}) or {}).get("access_token") or ""
    if token:  # 设了令牌才校验;空=放行(向后兼容,首次启动会自动生成)
        path = request.url.path
        exempt = path in _AUTH_EXEMPT_EXACT or any(path.startswith(p) for p in _AUTH_EXEMPT_PREFIXES)
        if not exempt:
            given = (request.query_params.get("token")
                     or request.headers.get("X-Access-Token")
                     or request.cookies.get("kd_token") or "")
            if given != token:
                return JSONResponse(
                    {"ok": False, "error": "需要访问令牌:请用 install 打印的 /setup?token=... 链接打开设置页。"},
                    status_code=401)
    return await call_next(request)


@app.get("/")
def root():
    from fastapi.responses import RedirectResponse
    return RedirectResponse("/setup")


# ---------- Kindle 取图 ----------
def _placeholder():
    from PIL import Image, ImageDraw
    img = Image.new("L", (600, 800), 255)
    ImageDraw.Draw(img).text((230, 380), "Loading...", fill=120)
    b = io.BytesIO(); img.save(b, format="PNG"); return b.getvalue()


def _legacy_placeholder():
    from PIL import Image, ImageDraw
    img = Image.new("RGB", (800, 600), (236, 232, 222))
    d = ImageDraw.Draw(img)
    d.text((350, 290), "Loading...", fill=(120, 120, 120))
    b = io.BytesIO(); img.save(b, format="PNG"); return b.getvalue()


@app.get("/kindle/frame.png")
def kindle_frame(request: Request = None):
    KINDLE_PULL["last"] = time.time()
    try:
        if request is not None:
            KINDLE_PULL["ip"] = request.client.host
    except Exception:
        pass
    cfg = cm.get()
    PAGE_DURATIONS = {"photo": 60}
    default_dur = 10
    if PAUSED["on"]:
        # 暂停：冻结当前页，不翻页、不重渲照片、不碰 NAS
        with RENDER_LOCK:
            frozen = RENDERED.get("photo_live") if (RENDER_ORDER and RENDER_ORDER[page_state["i"] % len(RENDER_ORDER)] == "photo") else None
            if not frozen and RENDER_ORDER:
                frozen = RENDERED.get(RENDER_ORDER[page_state["i"] % len(RENDER_ORDER)])
        return Response(frozen or _placeholder(), media_type="image/png")
    with RENDER_LOCK:
        order = list(RENDER_ORDER)
        if order:
            now_ts = time.time()
            cur_page = order[page_state["i"] % len(order)]
            dur = PAGE_DURATIONS.get(cur_page, default_dur)
            if page_state["last"] == 0.0:
                page_state["last"] = now_ts
            elif now_ts - page_state["last"] >= dur:
                page_state["i"] = (page_state["i"] + 1) % len(order)
                page_state["last"] = now_ts
            cur_page = order[page_state["i"] % len(order)]
            # 电子相册:20秒换一张,60秒内换3张;缓存渲染结果避免重复渲染
            if cur_page == "photo":
                photo_png = RENDERED.get("photo_live")
                photo_last = page_state.get("photo_last", 0)
                if not photo_png or now_ts - photo_last >= 20:
                    from server.render.build_context import _build_photo
                    from server.render.styles import render_page
                    photo_data = _build_photo(cfg)
                    if photo_data.get("data"):
                        ctx = {"photo": photo_data, "now": time.strftime("%m/%d %H:%M"), "time_hm": time.strftime("%H:%M"), "lang": "zh"}
                        style = styles.pick_style(cfg)
                        html = render_page(style, "photo", ctx)
                        _rc = pipeline.RenderConfig.from_config(cfg)
                        photo_png = pipeline.render_html_to_png(html, _rc)
                        RENDERED["photo_live"] = photo_png
                        page_state["photo_last"] = now_ts
                png = photo_png or RENDERED.get("photo") or _placeholder()
            else:
                png = RENDERED.get(cur_page)
        else:
            png = None
    return Response(png or _placeholder(), media_type="image/png")


@app.get("/kindle/page/{page_key}.png")
def kindle_page(page_key: str):
    with RENDER_LOCK:
        png = RENDERED.get(page_key)
    return Response(png or _placeholder(), media_type="image/png")


@app.get("/kindle/photo.png")
def kindle_photo():
    from server.photo_source import get_photo
    png = get_photo(advance=True)
    return Response(png or _placeholder(), media_type="image/png")


@app.get("/kindle/clear-cmd")
def kindle_clear_cmd():
    """一键清屏指令(供 Kindle 拉帧循环轮询):返回 "1" = 执行清屏(黑闪清屏后全刷重绘当前帧),
    返回 "0" = 无指令正常拉帧。**读取即复位**(每条指令只执行一次);设了访问令牌也放行
    (Kindle 拉帧带不了令牌,见 _AUTH_EXEMPT_PREFIXES)。"""
    on = CLEAR_CMD["on"]
    CLEAR_CMD["on"] = False
    return Response("1" if on else "0", media_type="text/plain")


def _legacy_render_page(cfg, pk):
    """把当前风格的某页渲成一张 800x600 **彩色** PNG(给安卓 4.x 图片相框,老平板当相框显示)。
    target=legacy_photo:借 static 版式(不渲点不了的控件/歌词、封面墙走 data URI 故 file:// 出图能渲),
    但还原彩色封面 + 暖白底(见 styles.LEGACY_PHOTO_CSS);彩色、不旋转、不灰度。**Kindle 路径不碰。**"""
    now = datetime.now(_tz(cfg))
    style = styles.pick_style(cfg, now.date())
    if not style or not styles.has_page(style, pk):
        return None
    with cache_lock:
        snap = dict(cache)
    mus = snap.get("music")        # 屏保封面墙跟 Kindle 一样每次重抽(图片模式也跟着变批)
    if isinstance(mus, dict) and mus.get("artwork_wall"):
        mus = dict(mus)
        mus["artwork_wall"] = _music_artwork_wall()
        snap["music"] = mus
    ctx = prep_context(now, snap, cfg)
    ctx["page_no"], ctx["page_total"] = _page_meta(cfg, style, pk)
    html = styles.render_page(style, pk, ctx, target="legacy_photo")
    rc = pipeline.RenderConfig.from_config(cfg)
    # 按 2x 高清渲染(base 800×600 矢量 + device-scale-factor=2 → 输出 1600×1200,字体/斜线锐利),
    # pad 端用 CSS 把高清图 downscale 铺满 → 比直接出 800×600 再放大清楚得多(同 Kindle 按机型分辨率出图的道理)。
    legacy_rc = pipeline.RenderConfig(
        width=pipeline.BASE_W * 2, height=pipeline.BASE_H * 2,
        base_width=pipeline.BASE_W, base_height=pipeline.BASE_H,
        rotate=0, grayscale=False, timeout=rc.timeout, chrome_bin=rc.chrome_bin)
    return pipeline.render_html_to_png(html, legacy_rc)


@app.get("/app-legacy/frame.png")
def app_legacy_frame():
    """安卓 4.x 出口:给 WebView 一张 800x600 彩色 PNG(老平板当相框)。**惰性按需**:
    只在本接口被拉时渲当前在看的那一页,按 render_interval 做 TTL 缓存(同一页这段时间内只渲一次);
    没老安卓设备来拉就完全不渲(开源用户大多没 4.2 机器,零额外开销)。翻页节奏所有客户端共享、与 web-simple 一致。
    鉴权仍走 /app-legacy 的 token,不豁免。"""
    cfg = cm.get()
    page_interval = _interval(cfg, "server", "page_interval", 20)
    ttl = _interval(cfg, "server", "render_interval", 30)
    style = styles.pick_style(cfg)
    active = [p for p in schema.active_pages(cfg) if styles.has_page(style, p)] if style else []
    if not active:
        return Response(_legacy_placeholder(), media_type="image/png", headers={"Cache-Control": "no-store"})
    now_ts = time.time()
    with LEGACY_FRAMES_LOCK:        # 翻页:所有 legacy 客户端共享一个节奏(同 web-simple)
        if legacy_page_state["last"] == 0.0:
            legacy_page_state["last"] = now_ts
        elif now_ts - legacy_page_state["last"] >= page_interval:
            legacy_page_state["i"] = (legacy_page_state["i"] + 1) % len(active)
            legacy_page_state["last"] = now_ts
        pk = active[legacy_page_state["i"] % len(active)]
        cached = LEGACY_FRAMES.get(pk)
    png = None
    if cached and (now_ts - cached[1] < ttl):
        png = cached[0]            # 缓存还新鲜,直接用(同页这一轮内不重渲)
    else:
        try:
            png = _legacy_render_page(cfg, pk)
        except Exception as e:
            print(f"[legacy-frame] {pk}: {e}")
        if png:
            with LEGACY_FRAMES_LOCK:
                LEGACY_FRAMES[pk] = (png, now_ts)
        elif cached:
            png = cached[0]        # 渲染失败保留旧帧(诚实降级)
    return Response(png or _legacy_placeholder(), media_type="image/png",
                    headers={"Cache-Control": "no-store"})


@app.get("/kindle/preview.png")
def kindle_preview(page: str, style: str = ""):
    """实时预览:即时渲染指定页/风格(不走缓存)。设置网页用。"""
    cfg = cm.get()
    s = style or styles.pick_style(cfg)
    if not s:
        return Response(b"no style", media_type="text/plain", status_code=400)
    now = datetime.now(_tz(cfg))
    with cache_lock:
        ctx = prep_context(now, dict(cache), cfg)
    ctx["page_no"], ctx["page_total"] = _page_meta(cfg, s, page)
    try:
        html = styles.render_page(s, page, ctx)
        rc = pipeline.RenderConfig.from_config(cfg)
        rc.rotate = 0   # 预览用横屏正立(电脑上看舒服;= Kindle 横放后实际所见)。frame.png 仍按配置旋转。
        png = pipeline.render_html_to_png(html, rc)
    except Exception as e:
        return Response(f"render error: {e}".encode(), media_type="text/plain", status_code=500)
    return Response(png, media_type="image/png")


@app.get("/health")
def health():
    cfg = cm.get()
    return {"status": "ok", "style": CURRENT["style"],
            "rendered": sorted(RENDERED.keys()),
            "active_pages": schema.active_pages(cfg)}


# ---------- push 接收 ----------
@app.post("/api/device-metrics")
async def device_metrics(data: dict = Body(...)):
    key = (data.get("id") or data.get("hostname") or "").strip() or "unknown"
    m = data.get("metrics") or {}
    m = dict(m)
    m["hostname"] = data.get("hostname") or key
    m["updated_at"] = time.time()
    with cache_lock:
        dm = cache.setdefault("devices_metrics", {})
        if key not in dm and len(dm) >= 64:   # 防(无鉴权上报口被)恶意/异常 push 大量不同 id 撑爆内存
            return JSONResponse({"status": "rejected", "error": "device count limit"}, status_code=429)
        dm[key] = m
    return {"status": "ok", "key": key}


@app.post("/api/apple-sync")
async def apple_sync(data: dict = Body(...)):
    payload = _apple_payload(data)
    with cache_lock:
        cache["reminders"] = payload["reminders"]
        cache["apple_updated"] = payload["updated_at"]
    try:
        _atomic_json_write(APPLE_REMINDERS_CACHE, payload)
    except Exception as e:
        print(f"[apple-sync] 写本地缓存失败:{e}")
    if payload["reminders"] and not (cm.get().get("reminders", {}) or {}).get("enabled"):
        cm.force_set("reminders", "enabled", True)
    return {"status": "ok"}


@app.post("/api/rate-limits")
async def rate_limits(data: dict = Body(...)):
    source = data.get("source", "claude")
    with cache_lock:
        if source == "codex":
            cache["codex_rate_limits"] = data.get("rate_limits")
        else:
            cache["rate_limits"] = data.get("rate_limits")
    return {"status": "ok"}


@app.post("/api/kindle-status")
async def kindle_status(data: dict = Body(...)):
    with cache_lock:
        cache["kindle_battery"] = data.get("battery")
        cache["kindle_charging"] = data.get("charging", False)
    return {"status": "ok"}


@app.post("/api/ccusage")
async def api_ccusage_push(data: dict = Body(...)):
    """接收设备推送的 ccusage 数据(Mac/其他机器上的 Claude/Codex 日志用量)。
    支持多设备:每台按 id 存储,合并后写入 cache["ccusage"] 供 build_context 消费。"""
    dev_id = (data.get("id") or "").strip() or "unknown"
    cc_data = data.get("cc") or {}
    codex_data = data.get("codex") or {}
    if not isinstance(cc_data, dict):
        cc_data = {}
    if not isinstance(codex_data, dict):
        codex_data = {}
    with cache_lock:
        by_device = cache.setdefault("ccusage_by_device", {})
        if dev_id not in by_device and len(by_device) >= 64:
            return JSONResponse({"status": "rejected", "error": "device count limit"}, status_code=429)
        by_device[dev_id] = {"cc": cc_data, "codex": codex_data}
        merged = merge_all_devices(by_device)
        cache["ccusage"] = merged
    try:
        _atomic_json_write(CCUSAGE_DEVICES_CACHE, by_device)
    except Exception as e:
        print(f"[ccusage-push] 落盘失败：{e}")
    if not (cm.get().get("ai_usage", {}) or {}).get("enabled"):
        cm.force_set("ai_usage", "enabled", True)
    return {"status": "ok", "id": dev_id}


@app.post("/api/music")
async def api_music_push(data: dict = Body(...)):
    """接收 Mac Music agent 推送的当前播放状态。"""
    if not isinstance(data, dict):
        return JSONResponse({"status": "rejected", "error": "invalid payload"}, status_code=400)
    try:
        with cache_lock:
            prev = cache.get("music") or {}
            payload = _music_payload(data, prev)
            cache["music"] = payload
    except ValueError as e:
        return JSONResponse({"status": "rejected", "error": str(e)}, status_code=413)
    _maybe_fetch_lyrics(payload)   # 换歌则后台查歌词(不阻塞本次推送响应)
    try:
        _atomic_json_write(MUSIC_CACHE, _music_payload_for_disk(payload))
    except Exception as e:
        print(f"[music-push] 落盘失败:{e}")
    if not (cm.get().get("music", {}) or {}).get("enabled"):
        cm.force_set("music", "enabled", True)
    return {"status": "ok", "has_track": bool(payload.get("has_track")), "artwork": bool(payload.get("artwork_url")),
            "artwork_wall": len(payload.get("artwork_wall") or [])}


@app.get("/music/artwork/{art_hash}")
def music_artwork(art_hash: str):
    """按 hash 返回一张缓存专辑封面(动态版渐换墙的 <img> 走这里,而非 data URI)。
    封面按 hash 不可变 → 长缓存 immutable,轮询重渲染时浏览器命中缓存、不闪不重拉。
    经 _auth 令牌保护(不豁免);按索引里的 hash 精确匹配 + abspath 限定在 MUSIC_ARTWORK_DIR 内,杜绝路径穿越。"""
    for it in _music_load_artwork_index():
        if isinstance(it, dict) and it.get("hash") == art_hash:
            path = os.path.join(MUSIC_ARTWORK_DIR, it.get("path", ""))
            if os.path.abspath(path).startswith(os.path.abspath(MUSIC_ARTWORK_DIR)) and os.path.isfile(path):
                try:
                    with open(path, "rb") as f:
                        blob = f.read()
                except Exception:
                    break
                return Response(blob, media_type=it.get("mime") or "image/jpeg",
                                headers={"Cache-Control": "public, max-age=86400, immutable"})
            break
    return Response(b"", media_type="image/jpeg", status_code=404)


# ---------- 安卓 App:活的交互 HTML(/app 外壳 + /app/page/* 单页)----------
# 都经 _auth 令牌保护(不是 Kindle 拉图,绝不豁免)。一套 styles 模板,target=android 出活 HTML。
APP_PAGE_TEXTS = {
    "zh": {"reconnecting": "连接中断,重连中…", "confirm_stop": "确认停止打印?此操作不可恢复。",
           "action_failed": "操作失败", "net_error": "网络错误", "no_pages": "还没启用任何页面,请先在设置页配置数据源。",
           # ↓ HA 控制面板(底部 sheet)静态文案(spec ha-page-interaction-spec.md §3.5,中央注入)
           "close": "关闭", "p_open": "打开", "p_close": "关闭", "p_stop": "停止",
           "p_on": "开", "p_off": "关", "p_play": "播放/暂停", "p_prev": "上一首", "p_next": "下一首",
           "p_volup": "音量+", "p_voldown": "音量−", "p_mute": "静音", "p_apply": "应用", "p_set": "设置",
           "p_mode": "模式", "p_current": "当前", "p_target": "目标", "p_position": "位置",
           "p_code": "密码(可选)", "p_arm_home": "在家布防", "p_arm_away": "离家布防",
           "p_arm_night": "夜间布防", "p_disarm": "撤防", "confirm_disarm": "确认撤防?将解除安防警戒。"},
    "en": {"reconnecting": "Disconnected, reconnecting…", "confirm_stop": "Stop the print? This cannot be undone.",
           "action_failed": "Action failed", "net_error": "Network error", "no_pages": "No pages enabled yet — configure a data source in setup first.",
           "close": "Close", "p_open": "Open", "p_close": "Close", "p_stop": "Stop",
           "p_on": "On", "p_off": "Off", "p_play": "Play/Pause", "p_prev": "Prev", "p_next": "Next",
           "p_volup": "Vol+", "p_voldown": "Vol−", "p_mute": "Mute", "p_apply": "Apply", "p_set": "Set",
           "p_mode": "Mode", "p_current": "Now", "p_target": "Target", "p_position": "Position",
           "p_code": "code (optional)", "p_arm_home": "Arm Home", "p_arm_away": "Arm Away",
           "p_arm_night": "Arm Night", "p_disarm": "Disarm", "confirm_disarm": "Disarm the alarm system?"},
}


# —— 三档梯度 UA 分流(施工图 §2.3):/app 按 User-Agent 自动把不同浏览器送到对的看板出口 ——
# 古董(Kindle 自带 WebKit / NetFront / 安卓 0~2.x)→ /web-simple(无 JS 静态图)
# 老安卓(3.x/4.x 老 AOSP WebKit,能跑 float+ES5 跑不了 grid)→ /app-legacy(float-CSS 活页)
# 其余(现代 Chromium / 现代安卓 WebView)→ /app(现状全功能)
# 正则只是起点,真机校准:安卓4.2 必落 legacy、Kindle 自带浏览器必落 simple(附录B)。
# 装壳不靠 UA:安卓壳按 SDK_INT 直接加载对应路径(更准),浏览器直接开才走这里。
_UA_ANCIENT = re.compile(r"(Kindle|Silk|NetFront|Obigo|UCWEB|Android [0-2]\.)", re.I)
_UA_OLD_ANDROID = re.compile(r"Android [34]\.", re.I)


def _classify_ua(ua: str) -> str:
    """返回 'app'(现代)/ 'legacy'(老安卓)/ 'simple'(古董)。古董优先(Kindle Fire 也带 Android 4.)。"""
    ua = ua or ""
    if _UA_ANCIENT.search(ua):
        return "simple"
    if _UA_OLD_ANDROID.search(ua):
        return "legacy"
    return "app"


def _tier_redirect(request: Request):
    """按 ?force= 覆盖或 UA 自动判断,给出该跳转的目标(透传 token);现代档返回 None(不跳,原样给 /app)。"""
    from fastapi.responses import RedirectResponse
    force = request.query_params.get("force")
    tier = force if force in ("app", "legacy", "simple") else _classify_ua(request.headers.get("user-agent", ""))
    if tier == "app":
        return None
    tok = request.query_params.get("token", "") or ""
    target = "/app-legacy" if tier == "legacy" else "/web-simple"
    if tok:
        from urllib.parse import quote
        target += "?token=" + quote(tok)
    return RedirectResponse(target, status_code=302)


@app.get("/app", response_class=HTMLResponse)
def app_shell(request: Request):
    """安卓 App 外壳页:页签 + 轮询/动作 JS + 自适应缩放。页面内容靠 /app/page/* 轮询拉。
    入口处先按 UA 三档分流(老安卓→/app-legacy、古董→/web-simple);现代浏览器/WebView 不跳,原样给本页。"""
    redir = _tier_redirect(request)
    if redir is not None:
        return redir
    cfg = cm.get()
    lang = (cfg.get("server", {}) or {}).get("language", "zh")
    style = styles.pick_style(cfg)
    active = [p for p in schema.active_pages(cfg) if styles.has_page(style, p)]
    pages = [{"key": p, "title": contract.PAGES[p]["title"]} for p in active]
    token = request.query_params.get("token", "") or ""
    app_cfg = {
        "token": token,
        "interval": _interval(cfg, "server", "app_poll_interval", 5),     # 当前页轮询刷新(数据)
        "page_interval": _interval(cfg, "server", "page_interval", 20),    # 自动轮播翻页(复用 Kindle 翻页间隔)
        "style": style,
        "default": active[0] if active else "home",
        "pages": pages,
        "texts": APP_PAGE_TEXTS.get(lang, APP_PAGE_TEXTS["zh"]),
    }
    path = os.path.join(WEB_DIR, "app.html")
    if not os.path.exists(path):
        return HTMLResponse("<h1>App 外壳未安装</h1>", status_code=404)
    with open(path, encoding="utf-8") as f:
        shell = f.read()
    return HTMLResponse(shell.replace("__APP_CONFIG__", json.dumps(app_cfg, ensure_ascii=False)))


@app.get("/app/page/{page_key}", response_class=HTMLResponse)
def app_page(page_key: str, request: Request):
    """用现有模板渲染单页活 HTML 片段(彩色 + 可点控件,target=android)。不走 Chromium 截图。"""
    cfg = cm.get()
    style = styles.pick_style(cfg)
    if not style or not styles.has_page(style, page_key):
        return HTMLResponse("", status_code=404)
    now = datetime.now(_tz(cfg))
    with cache_lock:
        snap = dict(cache)
    _inject_dyn_wall(snap)                # 动态版屏保:服务端漂移墙(模板按 hash 走 /music/artwork)
    ctx = prep_context(now, snap, cfg)
    ctx["app_token"] = request.query_params.get("token", "") or ""
    # 安卓 App 把**本机(手机/平板)电量**经 query 传来 → 覆盖页脚电量。
    # 否则页脚显示的是 Kindle 经 /api/kindle-status 上报的电量(App 跑在手机上,该显示手机的)。
    # /app/page 永远不是 Kindle 在请求(Kindle 走 /kindle/*),所以页脚电量只认本机经 query 传来的;
    # 传不到(普通浏览器/拿不到电量权限)就清空 → 模板 `battery.has` 为假、不显示,**绝不退回显示 Kindle 的电量**。
    kb = request.query_params.get("kbatt")
    bat = {"level": "--", "charging": False, "has": False}
    if kb not in (None, ""):
        try:
            bat = {"level": int(kb), "charging": request.query_params.get("kchg") == "1", "has": True}
        except (ValueError, TypeError):
            pass
    ctx["battery"] = bat
    ctx["page_no"], ctx["page_total"] = _page_meta(cfg, style, page_key)
    try:
        html = styles.render_page(style, page_key, ctx, target="android")
    except Exception as e:
        return HTMLResponse(f"<!doctype html><body>render error: {e}", status_code=500)
    # 看板画布固定 800×600(与 Kindle 一致):App/浏览器端对整块画布等比缩放居中铺满,
    # 不再按屏比重排内容(避免任意宽度下的溢出适配,见 web/app.html 的 fit())。
    return HTMLResponse(html)


@app.get("/web-simple", response_class=HTMLResponse)
def web_simple(request: Request):
    """古董浏览器(Kindle 自带 WebKit)降级页:无 JS,静态 HTML 嵌当前看板整图 + meta refresh 整页定时重载。
    `/app` 那套现代 JS(fetch/Fullscreen/Wake Lock)在古董浏览器跑不动 → 给它这个最朴素的页。
    页面外壳经 _auth 令牌(绝不豁免);内嵌的 `/kindle/frame.png` 本就在 _AUTH_EXEMPT 名单(给 Kindle 拉图),
    复用即可、不另渲染。整图每 page_interval 秒由 frame.png 自动翻页,本页同节奏整页重载跟上。"""
    from html import escape as _esc
    cfg = cm.get()
    interval = _interval(cfg, "server", "page_interval", 20)
    tok = request.query_params.get("token", "") or ""
    # meta refresh 显式带上当前令牌(古董浏览器对「无 url 的 refresh 是否保留 query」行为不一,显式最稳)。
    refresh_url = "/web-simple" + (f"?token={_esc(tok, quote=True)}" if tok else "")
    # 整图加时间戳破缓存:整页重载会重新请求 img,古董浏览器易缓存 PNG;frame.png 忽略未知 query,加 ?t= 无副作用。
    img_src = f"/kindle/frame.png?t={int(time.time())}"
    page = (
        "<!DOCTYPE html>\n<html><head>\n"
        '<meta charset="UTF-8">\n'
        f'<meta http-equiv="refresh" content="{interval};url={_esc(refresh_url, quote=True)}">\n'
        '<meta name="viewport" content="width=device-width,initial-scale=1">\n'
        "<title>Kindle Dashboard</title>\n"
        "<style>html,body{margin:0;padding:0;background:#000;height:100%;}"
        ".wrap{text-align:center;}img{max-width:100%;height:auto;display:inline-block;}</style>\n"
        "</head><body><div class=\"wrap\">"
        f'<img src="{img_src}" alt="dashboard"></div></body></html>'
    )
    return HTMLResponse(page)


# ---------- 老系统(安卓 4.x 古董 WebView)降级出口 ----------
# 默认 /app-legacy = 图片相框:复用当前 7 套风格的服务端彩色 PNG,端上只显示 <img>。
# 旧 ES5 活页壳保留在 /app-legacy-live + styles/legacy/*,用于调试/回滚/沉淀经验。
# 都经 _auth 令牌(绝不豁免,绝不进 _AUTH_EXEMPT_*——能暴露看板数据)。
@app.get("/app-legacy", response_class=HTMLResponse)
def app_legacy_shell(request: Request):
    """老系统降级外壳:图片相框模式。
    Android 4.2 WebView 不再承载业务布局,只显示服务端预渲染的 800x600 彩色 PNG。"""
    cfg = cm.get()
    lang = (cfg.get("server", {}) or {}).get("language", "zh")
    style = styles.pick_style(cfg)
    active = [p for p in schema.active_pages(cfg) if styles.has_page(style, p)]
    pages = [{"key": p, "title": contract.PAGES[p]["title"]} for p in active]
    token = request.query_params.get("token", "") or ""
    texts = APP_PAGE_TEXTS.get(lang, APP_PAGE_TEXTS["zh"])
    app_cfg = {
        "token": token,
        "interval": _interval(cfg, "server", "app_poll_interval", 5),
        "page_interval": _interval(cfg, "server", "page_interval", 20),
        "default": active[0] if active else "home",
        "pages": pages,
        "texts": {"reconnecting": texts.get("reconnecting", ""), "no_pages": texts.get("no_pages", "")},
    }
    path = os.path.join(WEB_DIR, "app-legacy.html")
    if not os.path.exists(path):
        return HTMLResponse("<h1>legacy 外壳未安装</h1>", status_code=404)
    with open(path, encoding="utf-8") as f:
        shell = f.read()
    # no-store:外壳含 <style>(legacy 主题 CSS),老 WebView 会缓存住 → 改了样式设备不更新。
    return HTMLResponse(shell.replace("__APP_CONFIG__", json.dumps(app_cfg, ensure_ascii=False)),
                        headers={"Cache-Control": "no-store"})


@app.get("/app-legacy-live", response_class=HTMLResponse)
def app_legacy_live_shell(request: Request):
    """旧 legacy 活页壳保留入口(调试/回滚用)。
    默认 /app-legacy 已切到图片相框模式;这个入口保留 ES5 + /app-legacy/page/* 的老实现和经验教训。"""
    cfg = cm.get()
    lang = (cfg.get("server", {}) or {}).get("language", "zh")
    active = [p for p in schema.active_pages(cfg) if styles.has_page("legacy", p)]
    pages = [{"key": p, "title": contract.PAGES[p]["title"]} for p in active]
    token = request.query_params.get("token", "") or ""
    texts = APP_PAGE_TEXTS.get(lang, APP_PAGE_TEXTS["zh"])
    app_cfg = {
        "token": token,
        "interval": _interval(cfg, "server", "app_poll_interval", 5),
        "page_interval": _interval(cfg, "server", "page_interval", 20),
        "default": active[0] if active else "home",
        "pages": pages,
        "texts": {"reconnecting": texts.get("reconnecting", ""), "no_pages": texts.get("no_pages", "")},
    }
    path = os.path.join(WEB_DIR, "app-legacy-live.html")
    if not os.path.exists(path):
        return HTMLResponse("<h1>legacy live 外壳未安装</h1>", status_code=404)
    with open(path, encoding="utf-8") as f:
        shell = f.read()
    return HTMLResponse(shell.replace("__APP_CONFIG__", json.dumps(app_cfg, ensure_ascii=False)),
                        headers={"Cache-Control": "no-store"})


@app.get("/app-legacy/page/{page_key}", response_class=HTMLResponse)
def app_legacy_page(page_key: str, request: Request):
    """用 legacy 模板渲染单页 float-CSS 片段(target=legacy,纯 float+ES5 友好,不注入现代 android 主题)。"""
    cfg = cm.get()
    if not styles.has_page("legacy", page_key):
        return HTMLResponse("", status_code=404)
    now = datetime.now(_tz(cfg))
    with cache_lock:
        snap = dict(cache)
    _inject_dyn_wall(snap)                # 动态版屏保:服务端漂移墙(模板按 hash 走 /music/artwork)
    ctx = prep_context(now, snap, cfg)
    ctx["app_token"] = request.query_params.get("token", "") or ""
    # legacy 跑在老安卓/浏览器上,不是 Kindle → 页脚电量只认本机经 query 传来的(老 WebView 多半给不了),
    # 给不到就清空(不退回显示 Kindle 的电量,模板 battery.has 为假即不显示)。
    kb = request.query_params.get("kbatt")
    bat = {"level": "--", "charging": False, "has": False}
    if kb not in (None, ""):
        try:
            bat = {"level": int(kb), "charging": request.query_params.get("kchg") == "1", "has": True}
        except (ValueError, TypeError):
            pass
    ctx["battery"] = bat
    ctx["page_no"], ctx["page_total"] = _page_meta(cfg, "legacy", page_key)
    try:
        html = styles.render_page("legacy", page_key, ctx, target="legacy")
    except Exception as e:
        return HTMLResponse(f"<div>render error: {e}</div>", status_code=500)
    return HTMLResponse(html, headers={"Cache-Control": "no-store"})


# ---------- 动作接口(安卓 App 控制设备)----------
# 全部经 _auth 令牌保护(能改你家设备,绝不豁免、绝不进 _AUTH_EXEMPT_*);只接受白名单操作。
@app.post("/api/action/ha")
async def action_ha(data: dict = Body(...)):
    """控制一个 HA 实体。body {entity_id, action, value?}。
    action/value 经 actions._resolve_action 白名单解析(开关/锁/窗帘/空调/媒体/选择/数值/按钮/安防)。"""
    try:
        actions.ha_action(cm.get(), data.get("entity_id", ""),
                          data.get("action", ""), data.get("value"))
        return {"ok": True}
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=400)


@app.post("/api/action/torrent")
async def action_torrent(data: dict = Body(...)):
    """种子暂停/恢复。body {client(下载器名), id(hash 或数字), action(pause/resume)}。"""
    try:
        actions.torrent_action(cm.get(), data.get("client", ""), data.get("id", ""), data.get("action", ""))
        return {"ok": True}
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=400)


def _action_state(cfg):
    """汇总所有可控目标的当前状态(供 /action-test 做「点前 → 点后」对照)。
    各部分独立降级(铁律3):某源连不上只影响该部分、其余照常,绝不抛错。"""
    out = {"ha": {"configured": False}, "printer": {"configured": False},
           "torrents": {"configured": False}}

    ha = (cfg or {}).get("home_assistant", {}) or {}
    ha_url = (ha.get("url") or "").strip().rstrip("/")
    ha_token = (ha.get("token") or "").strip()
    ha_ready = bool(ha_url and ha_token)

    pr_cfg = (cfg or {}).get("printer", {}) or {}
    pr_prefix = (pr_cfg.get("entity_prefix") or "").strip()
    want_printer = bool(pr_cfg.get("enabled") and pr_prefix)

    # HA states 只拉一次,实体清单 + 打印机共用(失败标记成异常,各部分各自报错降级)
    states = None
    if ha_ready:
        try:
            states = homeassistant._fetch_states(ha_url, ha_token)
        except Exception as e:
            states = e

    def _ha_err():
        if not ha_ready:
            return "Home Assistant 未配置(缺地址或令牌)"
        if isinstance(states, Exception):
            return f"读取 HA 状态失败:{states}"
        return None

    # HA 可控实体全量清单(按 kind 分组 + 只读传感器单列)
    if ha_ready:
        sec = {"configured": True}
        err = _ha_err()
        if err:
            sec["error"] = err
            sec["groups"] = {}; sec["readonly"] = []; sec["counts"] = {}
        else:
            try:
                inv = actions.controllable_inventory(cfg)
                sec.update(inv)
            except Exception as e:
                sec["error"] = f"读取 HA 实体失败:{e}"
                sec["groups"] = {}; sec["readonly"] = []; sec["counts"] = {}
        out["ha"] = sec

    if want_printer:
        sec = {"configured": True}
        err = _ha_err()
        if err:
            sec["error"] = err
        else:
            try:
                pr = homeassistant._build_printer(states, pr_prefix)
                sec.update({"online": pr["online"], "status": pr.get("status") or "",
                            "stage": pr.get("stage") or "", "progress": pr.get("progress") or 0,
                            "task": pr.get("task") or "--"})
            except Exception as e:
                sec["error"] = f"解析打印机状态失败:{e}"
        out["printer"] = sec

    clients = [c for c in (((cfg or {}).get("downloaders", {}) or {}).get("clients", []) or [])
               if isinstance(c, dict) and (c.get("host") or "").strip()]
    if clients:
        sec = {"configured": True, "items": [], "errors": []}
        for c in clients:
            cname = c.get("name") or c.get("host") or "?"
            try:
                d = downloader._ADAPTERS.get(c.get("type", "qbittorrent"), downloader._qb_fetch)(c)
            except Exception as e:
                sec["errors"].append(f"{cname}: {e}")
                continue
            for t in d.get("torrents", []):
                sec["items"].append({"client": cname, "id": t.get("id", ""),
                                     "name": t.get("name", ""), "state": t.get("state", ""),
                                     "progress": t.get("progress", 0)})
        out["torrents"] = sec

    return out


@app.get("/api/action-state")
def api_action_state():
    """所有可控目标的当前状态紧凑 JSON(供 /action-test 做「点前/点后」对照)。
    经令牌(绝不豁免——暴露设备名/状态);各源独立降级。"""
    try:
        return {"ok": True, **_action_state(cm.get())}
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)


@app.get("/action-test", response_class=HTMLResponse)
def action_test_page():
    """动作接口真机测试控制台。经令牌(绝不进 _AUTH_EXEMPT_*——能改你家设备)。"""
    path = os.path.join(WEB_DIR, "action-test.html")
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            return HTMLResponse(f.read())
    return HTMLResponse("<h1>测试页未安装</h1>", status_code=404)


# ---------- 设置网页 API ----------
@app.get("/api/schema")
def api_schema():
    lang = (cm.get().get("server", {}) or {}).get("language", "zh")
    return JSONResponse(schema.to_json(lang))


@app.get("/api/config")
def api_get_config():
    return JSONResponse({"config": cm.redacted(), "status": cm.status()})


@app.post("/api/config")
async def api_save_config(data: dict = Body(...)):
    errors = cm.save(data.get("config") or data)
    if errors:
        return JSONResponse({"ok": False, "errors": errors}, status_code=400)
    cfg = cm.get()
    _prune_pull_device_cache(cfg)
    for src in CONFIG_SAVE_SYNC_SOURCES:
        collect_source(src, cfg)
    return {"ok": True, "status": cm.status()}


@app.get("/api/styles")
def api_styles():
    return {"styles": styles.list_styles(), "pages": list(contract.PAGES.keys())}


@app.get("/agent/{name}")
def agent_file(name: str):
    """下发推送 agent 脚本(被监控机 curl 下载安装)。白名单,纯文本。
    .ps1 文件加 UTF-8 BOM——Windows PowerShell 5.x 无 BOM 按系统 GBK 解码,中文乱码致解析失败。"""
    path = AGENT_FILES.get(name)
    if not path or not os.path.exists(path):
        return Response("not found", media_type="text/plain", status_code=404)
    with open(path, "rb") as f:
        raw = f.read()
    if name.endswith(".ps1") and not raw.startswith(b"\xef\xbb\xbf"):
        raw = b"\xef\xbb\xbf" + raw
    return Response(raw, media_type="text/plain; charset=utf-8")


@app.get("/api/city-search")
def api_city_search(q: str = ""):
    """城市搜索:用已保存的天气 host/key 调 GeoAPI,返回候选城市供设置网页选择。
    key 只在服务端使用,不回传前端。"""
    w = cm.get().get("weather", {})
    host = (w.get("host") or "").strip()
    key = (w.get("key") or "").strip()
    if not (host and key):
        return JSONResponse(
            {"ok": False, "error": "请先填写并【保存】天气的 API Host 和 Key,再搜索城市。"},
            status_code=400)
    if not (q or "").strip():
        return {"ok": True, "results": []}
    try:
        results = weather.search_city(host, key, q)
    except Exception as e:
        return JSONResponse({"ok": False, "error": f"搜索失败:{e}"}, status_code=502)
    return {"ok": True, "results": results}


@app.get("/api/ha-entities")
def api_ha_entities(q: str = "", domain: str = ""):
    """HA 实体搜索:用已保存的 HA 地址/令牌拉实体,返回候选供设置网页选择。
    令牌只在服务端使用,不回传前端。"""
    ha = cm.get().get("home_assistant", {})
    url = (ha.get("url") or "").strip()
    token = (ha.get("token") or "").strip()
    if not (url and token):
        return JSONResponse(
            {"ok": False, "error": "请先填写并【保存】Home Assistant 的地址和令牌,再选实体。"},
            status_code=400)
    try:
        result = homeassistant.list_entities(url, token, q, domain)
    except Exception as e:
        return JSONResponse({"ok": False, "error": f"读取实体失败:{e}"}, status_code=502)
    return {"ok": True, **result}


@app.get("/api/printers")
def api_printers():
    """扫描 HA 中可作为 3D 打印机页数据源的打印机。"""
    ha = cm.get().get("home_assistant", {})
    url = (ha.get("url") or "").strip()
    token = (ha.get("token") or "").strip()
    if not (url and token):
        return JSONResponse(
            {"ok": False, "error": "请先填写并【保存】Home Assistant 的地址和令牌,再扫描打印机。"},
            status_code=400)
    try:
        result = homeassistant.list_printers(url, token)
    except Exception as e:
        return JSONResponse({"ok": False, "error": f"扫描打印机失败:{e}"}, status_code=502)
    return {"ok": True, **result}


@app.get("/api/server-url")
def api_server_url(request: Request):
    """给设置页生成远程 agent 命令:localhost 打开设置页时,自动换成局域网地址。"""
    cfg = cm.get()
    origin = str(request.base_url).rstrip("/")
    host = request.url.hostname or ""
    port = request.url.port or int(cfg.get("server", {}).get("port", 8585))
    scheme = request.url.scheme or "http"
    lan_urls = [f"{scheme}://{ip}:{port}" for ip in _lan_ips()]
    use_lan = _is_local_host(host) and lan_urls
    recommended = lan_urls[0] if use_lan else origin
    candidates = []
    for url in [recommended, origin] + lan_urls:
        if url and url not in candidates:
            candidates.append(url)
    local_url = _local_hostname_url(scheme, port)   # mDNS .local:支持的设备可选,绕开 IP 漂移
    if local_url and local_url not in candidates:
        candidates.append(local_url)
    return {
        "origin": origin,
        "recommended": recommended,
        "candidates": candidates,
        "is_loopback": _is_local_host(host),
    }


# ---------- Microsoft To Do 登录(设备码流程) ----------
@app.get("/api/mstodo/state")
def api_mstodo_state():
    """是否已连接 + 账号名(非敏感),供设置页初始渲染。"""
    return {"ok": True, **mstodo.state()}


@app.post("/api/mstodo/login/start")
def api_mstodo_login_start():
    """发起设备码登录,返回用户要输入的 code 和网址。"""
    try:
        return {"ok": True, **mstodo.login_start(cm.get())}
    except Exception as e:
        return JSONResponse({"ok": False, "error": f"发起登录失败:{e}"}, status_code=502)


@app.get("/api/mstodo/login/status")
def api_mstodo_login_status(session: str = ""):
    """轮询登录状态;成功时落地后置 mstodo.enabled=true(只置一次)。"""
    st = mstodo.login_status(session)
    if st.get("state") == "success" and not cm.get().get("mstodo", {}).get("enabled"):
        cm.save({"mstodo": {"enabled": True}})
    return {"ok": True, **st}


@app.post("/api/mstodo/logout")
def api_mstodo_logout():
    """断开连接:删 token + 置 mstodo.enabled=false。"""
    mstodo.logout()
    cm.save({"mstodo": {"enabled": False}})
    return {"ok": True}


@app.get("/api/discovered-devices")
def api_discovered():
    """已上报数据的设备 + 每台可勾选的指标条(供设置网页生成勾选 UI)。"""
    with cache_lock:
        dm = dict(cache.get("devices_metrics", {}))
    out = []
    for key, raw in sorted(dm.items()):
        fields = ["cpu", "mem", "net", "disk_io"]
        fields += [f"vol:{v['name']}" for v in raw.get("disks", [])]
        out.append({"key": key, "hostname": raw.get("hostname", key),
                    "fields": fields, "updated_at": raw.get("updated_at")})
    return {"devices": out}


@app.get("/qrcode.js")
def qrcode_lib():
    """前端二维码库(qrcode-generator,MIT,Kazuhiko Arase)。设置页生成 App 配置二维码用,本地内联离线可用。"""
    path = os.path.join(WEB_DIR, "qrcode.js")
    if not os.path.exists(path):
        return Response("// qrcode.js missing", media_type="application/javascript", status_code=404)
    with open(path, encoding="utf-8") as f:
        return Response(f.read(), media_type="application/javascript")


@app.get("/setup", response_class=HTMLResponse)
def setup_page():
    path = os.path.join(WEB_DIR, "setup.html")
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            return HTMLResponse(f.read())
    return HTMLResponse("<h1>设置页未安装</h1>", status_code=404)


# ---------- 系统向导（设置页「系统向导」面板）----------
from server import systemctl

@app.get("/api/system/status")
def api_system_status():
    try:
        return {"ok": True, "paused": PAUSED["on"], "clear_pending": CLEAR_CMD["on"],
                **systemctl.status(cm.get())}
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)


@app.post("/api/system/pause")
def api_system_pause(payload: dict = None):
    """暂停/恢复。暂停时停止所有数据源采集、照片预取处理与翻页，Kindle 停在当前帧。"""
    try:
        on = bool((payload or {}).get("paused"))
    except Exception:
        return JSONResponse({"ok": False, "error": "参数错误"}, status_code=400)
    PAUSED["on"] = on
    print(f"[control] 看板已{'暂停' if on else '恢复'}")
    return {"ok": True, "paused": on}


@app.post("/api/kindle/clear")
def api_kindle_clear():
    """一键清屏(设置页「系统向导」按钮):置位清屏指令,Kindle 下一次拉帧(≤拉图间隔)时
    黑闪清屏、全刷重绘当前帧,随后继续正常轮播。不触碰轮播状态(不清除/重置当前帧)。"""
    CLEAR_CMD["on"] = True
    CLEAR_CMD["ts"] = time.time()
    print("[control] 已下发 Kindle 一键清屏指令")
    return {"ok": True}


@app.post("/api/system/mount-nas")
def api_system_mount_nas():
    ok, msg = systemctl.mount_nas()
    return {"ok": ok, "msg": msg}


@app.post("/api/system/setup-usbnet")
def api_system_setup_usbnet():
    ok, msg = systemctl.setup_usbnet()
    return {"ok": ok, "msg": msg}


@app.post("/api/system/test-kindle")
def api_system_test_kindle():
    return systemctl.test_kindle()


@app.post("/api/system/restart-photo")
def api_system_restart_photo():
    """重置照片批次状态，下次渲染时重新建批。"""
    try:
        from server.render import build_context
        build_context._photo_state["batch"] = []
        build_context._photo_state["local_idx"] = 0
        build_context._photo_building = False
        return {"ok": True, "msg": "照片服务将在下一轮重新加载"}
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)
