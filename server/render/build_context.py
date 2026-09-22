"""数据整合层:把各数据源的原始 cache 整理成符合数据契约的 render context。

prep_context(now, cache) 是唯一入口:输入聚合 cache(各采集器写入),输出 contract 定义的 ctx。
缺数据时各段降级为占位值(诚实降级),与 contract.empty_context() 同构。
device 当前固定 nas/mac 两槽(1:1 沿用,保证 style_a 模板),动态机器字典见 data-contract.md 的 P0 待办。
"""
import math
import time
import calendar as cal_mod
from datetime import datetime, date, timedelta, timezone
from functools import lru_cache
from lunardate import LunarDate
from server.render import contract

WEEKDAYS_CN = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]
WEEKDAYS_EN = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
# 打印机状态 / 速度档位的双语映射(数据层产出,模板直接显示)
PRINTER_STATUS = {
    "zh": {"running": "打印中", "idle": "空闲", "finish": "打印完成", "failed": "打印失败",
           "pause": "已暂停", "prepare": "准备中", "slicing": "切片中", "offline": "离线"},
    "en": {"running": "Printing", "idle": "Idle", "finish": "Done", "failed": "Failed",
           "pause": "Paused", "prepare": "Preparing", "slicing": "Slicing", "offline": "Offline"},
}
PRINTER_SPEED = {
    "zh": {"standard": "标准", "silent": "静音", "sport": "运动", "ludicrous": "狂暴"},
    "en": {"standard": "Standard", "silent": "Silent", "sport": "Sport", "ludicrous": "Ludicrous"},
}
DL_STATE = {
    "zh": {"downloading": "下载中", "seeding": "做种", "paused": "暂停", "checking": "校验",
           "queued": "排队", "stalled": "卡住", "error": "错误", "other": "其他"},
    "en": {"downloading": "Downloading", "seeding": "Seeding", "paused": "Paused", "checking": "Checking",
           "queued": "Queued", "stalled": "Stalled", "error": "Error", "other": "Other"},
}
# 音乐播放状态 / 随机 / 循环 的双语映射(数据层产出,模板直接显示;别在模板写死中文)
MUSIC_STATE = {
    "zh": {"playing": "播放中", "paused": "已暂停", "stopped": "已停止"},
    "en": {"playing": "Playing", "paused": "Paused", "stopped": "Stopped"},
}
MUSIC_REPEAT = {
    "zh": {"off": "循环关", "all": "列表循环", "one": "单曲循环"},
    "en": {"off": "Repeat off", "all": "Repeat all", "one": "Repeat one"},
}
MUSIC_SHUFFLE = {
    "zh": {True: "随机开", False: "随机关"},
    "en": {True: "Shuffle on", False: "Shuffle off"},
}
LUNAR_MONTHS = ["", "正月", "二月", "三月", "四月", "五月", "六月",
                "七月", "八月", "九月", "十月", "冬月", "腊月"]
LUNAR_DAYS = ["", "初一", "初二", "初三", "初四", "初五", "初六", "初七", "初八", "初九", "初十",
              "十一", "十二", "十三", "十四", "十五", "十六", "十七", "十八", "十九", "二十",
              "廿一", "廿二", "廿三", "廿四", "廿五", "廿六", "廿七", "廿八", "廿九", "三十"]
ZODIAC = ["鼠", "牛", "虎", "兔", "龙", "蛇", "马", "羊", "猴", "鸡", "狗", "猪"]
STEMS = ["甲", "乙", "丙", "丁", "戊", "己", "庚", "辛", "壬", "癸"]
BRANCHES = ["子", "丑", "寅", "卯", "辰", "巳", "午", "未", "申", "酉", "戌", "亥"]
# 24 节气按太阳视黄经每 15° 定义。这里存每个节气通常所在月份与目标黄经,
# 日期由 solar_terms_for_year(year) 按年份计算,不再维护单年硬编码表。
SOLAR_TERM_POINTS = [
    (1, "小寒", 285), (1, "大寒", 300), (2, "立春", 315), (2, "雨水", 330),
    (3, "惊蛰", 345), (3, "春分", 0),   (4, "清明", 15),  (4, "谷雨", 30),
    (5, "立夏", 45),  (5, "小满", 60),  (6, "芒种", 75),  (6, "夏至", 90),
    (7, "小暑", 105), (7, "大暑", 120), (8, "立秋", 135), (8, "处暑", 150),
    (9, "白露", 165), (9, "秋分", 180), (10, "寒露", 195), (10, "霜降", 210),
    (11, "立冬", 225), (11, "小雪", 240), (12, "大雪", 255), (12, "冬至", 270),
]
HOLIDAYS = {(1,1):"元旦",(2,14):"情人节",(3,8):"妇女节",(4,5):"清明",(5,1):"劳动",
            (5,4):"青年",(6,1):"儿童",(10,1):"国庆",(10,2):"国庆",(10,3):"国庆",(12,25):"圣诞"}
LUNAR_HOLIDAYS = {(1,1):"春节",(1,15):"元宵",(5,5):"端午",(7,7):"七夕",(8,15):"中秋",(9,9):"重阳",(12,30):"除夕"}


def _julian_day(y, m, d, hour=0):
    if m <= 2:
        y -= 1
        m += 12
    a = y // 100
    b = 2 - a + a // 4
    return int(365.25 * (y + 4716)) + int(30.6001 * (m + 1)) + d + b - 1524.5 + hour / 24


def _solar_longitude(jd):
    t = (jd - 2451545.0) / 36525
    l0 = 280.46646 + 36000.76983 * t + 0.0003032 * t * t
    m = math.radians(357.52911 + 35999.05029 * t - 0.0001537 * t * t)
    c = ((1.914602 - 0.004817 * t - 0.000014 * t * t) * math.sin(m)
         + (0.019993 - 0.000101 * t) * math.sin(2 * m)
         + 0.000289 * math.sin(3 * m))
    omega = math.radians(125.04 - 1934.136 * t)
    return (l0 + c - 0.00569 - 0.00478 * math.sin(omega)) % 360


def _angle_delta(cur, target):
    return (cur - target + 540) % 360 - 180


def _next_month(y, m):
    return (y + 1, 1) if m == 12 else (y, m + 1)


def _jd_to_cn_date(jd):
    return (datetime(1970, 1, 1) + timedelta(days=jd - 2440587.5, hours=8)).date()


def _term_date(year, month, target):
    y2, m2 = _next_month(year, month)
    lo = _julian_day(year, month, 1, -8)
    hi = _julian_day(y2, m2, 1, -8)
    for _ in range(48):
        mid = (lo + hi) / 2
        if _angle_delta(_solar_longitude(mid), target) < 0:
            lo = mid
        else:
            hi = mid
    return _jd_to_cn_date((lo + hi) / 2)


@lru_cache(maxsize=128)
def solar_terms_for_year(year):
    out = []
    for month, name, target in SOLAR_TERM_POINTS:
        d = _term_date(year, month, target)
        out.append((d.month, d.day, name))
    return tuple(out)


def solar_terms_by_month_day(year):
    return {(m, d): name for m, d, name in solar_terms_for_year(year)}


def _merge_daily(a, b):
    """按日期合并两个 daily 数组,相同日期的 totalTokens/totalCost 相加。"""
    merged = {}
    for item in a:
        d = item["date"]
        merged[d] = {"date": d, "totalTokens": item.get("totalTokens", 0),
                     "totalCost": item.get("totalCost", 0)}
    for item in b:
        d = item["date"]
        if d in merged:
            merged[d]["totalTokens"] += item.get("totalTokens", 0)
            merged[d]["totalCost"] += item.get("totalCost", 0)
        else:
            merged[d] = {"date": d, "totalTokens": item.get("totalTokens", 0),
                         "totalCost": item.get("totalCost", 0)}
    return sorted(merged.values(), key=lambda x: x["date"])


def fmt_cost(c):
    if c >= 1000: return f"${c:,.0f}"
    if c >= 100: return f"${c:.1f}"
    return f"${c:.2f}"

def fmt_tok(n):
    if n >= 1e9: return f"{n/1e9:.1f}B"
    if n >= 1e6: return f"{n/1e6:.0f}M"
    if n >= 1e3: return f"{n/1e3:.0f}K"
    return str(int(n))


def reminder_due_date(value, tzinfo=None):
    """把 Apple/To Do 的 dueDate 归一成看板本地日期。"""
    s = (value or "").strip()
    if not s:
        return None
    try:
        if "T" not in s:
            return date.fromisoformat(s[:10])
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        if dt.tzinfo:
            return dt.astimezone(tzinfo or timezone(timedelta(hours=8))).date()
        return dt.date()
    except Exception:
        try:
            return date.fromisoformat(s[:10])
        except Exception:
            return None


def fmt_bytes(n):
    if n >= 1e12: return f"{n/1e12:.1f}T"
    if n >= 1e9: return f"{n/1e9:.1f}G"
    if n >= 1e6: return f"{n/1e6:.0f}M"
    if n >= 1e3: return f"{n/1e3:.0f}K"
    return str(int(n))

def fmt_mem(n):
    if n >= 1024**3: return f"{n/1024**3:.1f}G"
    if n >= 1024**2: return f"{n/1024**2:.0f}M"
    return f"{n/1024:.0f}K"

def fmt_speed(n):
    if n >= 1e6: return f"{n/1e6:.1f} MB/s"
    if n >= 1e3: return f"{n/1e3:.0f} KB/s"
    return f"{int(n)} B/s" if n else "0"

def fmt_countdown(resets_at, lang="zh"):
    secs = int(resets_at - time.time())
    if lang == "en":
        if secs <= 0: return "soon"
        h, m = secs // 3600, (secs % 3600) // 60
        if h >= 24:
            d, rh = divmod(h, 24)
            return f"in {d}d {rh}h"
        if h > 0: return f"in {h}h {m}m"
        return f"in {m}m"
    if secs <= 0: return "即将刷新"
    h, m = secs // 3600, (secs % 3600) // 60
    if h >= 24:
        d, rh = divmod(h, 24)
        return f"{d}天{rh}小时后"
    if h > 0: return f"{h}小时{m}分后"
    return f"{m}分钟后"


def _fmt_eta(secs, lang="zh"):
    """剩余时间秒 → 文字。异常值(qB 8640000=∞、负数、0、None)→ "—"。"""
    try:
        secs = int(secs)
    except (TypeError, ValueError):
        return "—"
    if secs <= 0 or secs >= 8640000:
        return "—"
    h, m = secs // 3600, (secs % 3600) // 60
    if lang == "en":
        return f"{h}h {m}m" if h else f"{m}m"
    return f"{h}时{m}分" if h else f"{m}分"


def _fmt_mmss(secs):
    """秒 → "m:ss"(歌曲时长/进度;时长≥1小时显 "h:mm:ss")。异常/0 → "0:00"。
    取整向下截断(与播放器一致:314.76s → 5:14,不进位成 5:15)。"""
    try:
        secs = int(float(secs))
    except (TypeError, ValueError):
        return "0:00"
    if secs < 0:
        secs = 0
    h, m, s = secs // 3600, (secs % 3600) // 60, secs % 60
    if h:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m}:{s:02d}"


def _music_wall_layout(items):
    items = list(items or [])
    n = len(items)
    if n <= 0:
        return [], 0, 0, "empty"
    if n == 1:
        return items[:1], 1, 1, "one"
    if n < 4:
        return items[:2], 2, 1, "two"
    if n < 9:
        return items[:4], 2, 2, "four"
    if n < 16:
        return items[:9], 3, 3, "nine"
    if n < 20:
        return items[:16], 4, 4, "sixteen"
    return items[:20], 5, 4, "twenty"


def _sanitize_lyrics(raw):
    """歌词原始列表 → 干净的 [{t: float, text: str}],按 t 升序、丢弃坏行。
    诚实降级:任何异常/空/非列表 → 返回 [](模板据此回退现状播放器)。"""
    out = []
    for ln in (raw or []):
        if not isinstance(ln, dict):
            continue
        try:
            t = float(ln.get("t"))
        except (TypeError, ValueError):
            continue
        text = str(ln.get("text") or "").strip()
        if t < 0 or not text:
            continue
        out.append({"t": round(t, 2), "text": text})
    out.sort(key=lambda x: x["t"])
    return out


def _rel_time(ts, now, lang="zh"):
    """资讯发布时间 → 相对时间(本地化)。ts<=0 / 解析不出 → 空串。"""
    try:
        ts = float(ts)
    except (TypeError, ValueError):
        ts = 0
    if ts <= 0:
        return ""
    secs = now.timestamp() - ts
    en = (lang == "en")
    if secs < 3600:
        return "just now" if en else "刚刚"
    if secs < 86400:
        h = int(secs // 3600)
        return f"{h}h ago" if en else f"{h}小时前"
    d = datetime.fromtimestamp(ts, now.tzinfo)
    return d.strftime("%m/%d") if en else d.strftime("%m.%d")


def get_lunar(d):
    lu = LunarDate.fromSolarDate(d.year, d.month, d.day)
    gz = STEMS[(lu.year-4)%10] + BRANCHES[(lu.year-4)%12]
    return lu, LUNAR_MONTHS[lu.month], LUNAR_DAYS[lu.day], gz, ZODIAC[(lu.year-4)%12]


def solar_term_text(d):
    terms = solar_terms_for_year(d.year)
    cur = up = None
    for m, day, name in terms:
        td = date(d.year, m, day)
        if td <= d: cur = (name, td)
        elif up is None: up = (name, td)
    if cur and (d - cur[1]).days == 0:
        return f"今日{cur[0]}"
    if up:
        return f"{up[0]}还有{(up[1]-d).days}天"
    # 当年最后一个节气(冬至)之后到年底:下一个是明年小寒,跨年算,别返回空
    nxt = solar_terms_for_year(d.year + 1)
    if nxt:
        m, day, name = nxt[0]
        return f"{name}还有{(date(d.year + 1, m, day) - d).days}天"
    return ""


def build_calendar(today, lang="zh"):
    weeks = cal_mod.Calendar(firstweekday=0).monthdayscalendar(today.year, today.month)
    terms = solar_terms_by_month_day(today.year)
    en = (lang == "en")
    out = []
    for wk in weeks:
        row = []
        for i, dnum in enumerate(wk):
            if dnum == 0:
                row.append(None); continue
            hol = HOLIDAYS.get((today.month, dnum))
            try:
                lu = LunarDate.fromSolarDate(today.year, today.month, dnum)
                lstr = LUNAR_MONTHS[lu.month] if lu.day == 1 else LUNAR_DAYS[lu.day]
                lhol = LUNAR_HOLIDAYS.get((lu.month, lu.day))
            except Exception:
                lstr, lhol = "", None
            term = terms.get((today.month, dnum))
            sub = hol or lhol or term or lstr
            row.append({
                "d": dnum,
                # 英文版:日历只留公历数字,格子副文本(农历/节气/中国节日)与节日标注都不渲染
                "l": "" if en else sub,
                "today": dnum == today.day,
                "holiday": False if en else bool(hol or lhol or term),
                "weekend": i >= 5,
            })
        out.append(row)
    return out



_photo_state = {"idx": 0, "batch": [], "batch_time": 0, "path": "", "photo_idx": 0, "local_idx": 0, "batch_start": 0}
# Load persisted photo_idx
try:
    import os as _os, json as _json
    _p = _os.path.join(_os.path.dirname(_os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))), "data", "photo_state.json")
    if _os.path.exists(_p):
        d = _json.load(open(_p))
        _photo_state["photo_idx"] = d.get("photo_idx", d.get("batch_idx", 0))
except Exception as _e:
    print(f"[photo] load state error: {_e}")

def _refresh_photo_batch(cfg, start_idx=None):
    """从NAS预取一批照片，处理好存本地缓存目录。按实际消耗位置推进，不跳图。"""
    import os, subprocess, time
    from PIL import Image, ImageOps
    pcfg = (cfg or {}).get("photo", {}) or {}
    path = pcfg.get("path", "")
    gray = pcfg.get("grayscale", True)
    if not path:
        return
    SKIP = {"_重复待确认", "#recycle", ".DS_Store"}
    cache_dir = os.path.expanduser("~/kindle-photo-cache")
    os.makedirs(cache_dir, exist_ok=True)

    # 收集文件列表（NAS不可达时保留旧列表，不清空）
    need_scan = (
        _photo_state.get("files") is None
        or _photo_state.get("files_mtime", 0) < time.time() - 600
        or _photo_state.get("path") != path
    )
    if need_scan:
        EXTS = {".jpg",".jpeg",".png",".nef"}
        files = []
        if os.path.isdir(path):
            try:
                for root, dirs, names in os.walk(path, timeout=10):
                    dirs[:] = [d for d in dirs if d not in SKIP and not d.startswith(".")]
                    for n in names:
                        if os.path.splitext(n)[1].lower() in EXTS:
                            files.append(os.path.join(root, n))
            except Exception:
                pass
        files.sort()
        if files:
            _photo_state["files"] = files
            _photo_state["files_mtime"] = time.time()
            _photo_state["path"] = path
        elif _photo_state.get("files"):
            # NAS不可达，保留旧列表
            pass
        else:
            return
    files = _photo_state["files"]
    if not files:
        return

    BATCH = 180
    if start_idx is None:
        start_idx = _photo_state.get("photo_idx", 0)
    start_idx = start_idx % len(files)
    batch = []
    tw, th = 1024, 758
    for i in range(BATCH):
        src = files[(start_idx + i) % len(files)]
        try:
            out = os.path.join(cache_dir, f"p_{i:04d}.png")
            if src.lower().endswith(".nef"):
                subprocess.run(["sips","-s","format","jpeg","--resampleHeightWidthMax","2048",src,"--out",os.path.join(cache_dir, "nef_tmp.jpg")], check=True, capture_output=True)
                src2 = os.path.join(cache_dir, "nef_tmp.jpg")
            else:
                src2 = src
            im = Image.open(src2)
            im = ImageOps.exif_transpose(im)
            im = im.convert("L" if gray else "RGB")
            sr = im.width/im.height; tr = tw/th
            if sr > tr: nw,nh = tw, round(im.height*tw/im.width)
            else: nh,nw = th, round(im.width*th/im.height)
            im = im.resize((nw,nh), Image.LANCZOS)
            canvas = Image.new("L" if gray else "RGB", (tw,th), 255)
            canvas.paste(im, ((tw-nw)//2, (th-nh)//2))
            canvas.save(out, "PNG")
            batch.append(out)
        except:
            pass
    _photo_state["batch"] = batch
    _photo_state["batch_start"] = start_idx
    _photo_state["photo_idx"] = start_idx
    _photo_state["batch_time"] = time.time()
    _photo_state["local_idx"] = 0
    _save_photo_state()


def _save_photo_state():
    """持久化照片进度。"""
    import os, json
    try:
        pdir = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "data")
        os.makedirs(pdir, exist_ok=True)
        with open(os.path.join(pdir, "photo_state.json"), "w") as f:
            json.dump({"photo_idx": _photo_state.get("photo_idx", 0)}, f)
    except Exception as e:
        print(f"[photo] save state error: {e}")


_photo_building = False

def _build_photo(cfg):
    """从本地缓存读照片，不碰NAS。按实际消耗推进，用完前预取，不跳图。"""
    import base64, os, time, threading, glob
    global _photo_building
    # 重启后快速恢复：磁盘上有缓存就直接用
    if not _photo_state["batch"]:
        cached = sorted(glob.glob(os.path.expanduser("~/kindle-photo-cache/p_*.png")))
        if len(cached) >= 10:
            _photo_state["batch"] = cached
            _photo_state["local_idx"] = 0
            _photo_state["batch_time"] = time.time()
            _photo_state["batch_start"] = _photo_state.get("photo_idx", 0)
    if not _photo_state["batch"]:
        # 首次需要建批
        if not _photo_building:
            _photo_building = True
            def _bg():
                global _photo_building
                try: _refresh_photo_batch(cfg)
                except Exception as e: print(f"[photo] batch build error: {e}")
                finally: _photo_building = False
            threading.Thread(target=_bg, daemon=True).start()
        return {"data": "", "caption": "加载中..."}
    batch = _photo_state["batch"]
    local = _photo_state.get("local_idx", 0)
    # 用到80%时预取下一批（NAS不可达则跳过，循环播放缓存）
    import os as _os
    photo_path = (cfg or {}).get("photo", {}).get("path", "")
    nas_ok = _os.path.isdir(photo_path)
    if nas_ok and local >= len(batch) * 0.8 and not _photo_building:
        _photo_building = True
        next_start = _photo_state.get("batch_start", 0) + local
        def _bg():
            global _photo_building
            try: _refresh_photo_batch(cfg, next_start)
            except Exception as e: print(f"[photo] batch build error: {e}")
            finally: _photo_building = False
        threading.Thread(target=_bg, daemon=True).start()
    # 批次用完但新批还没好，循环用旧批（不黑屏）
    idx = local % len(batch)
    _photo_state["local_idx"] = local + 1
    _photo_state["photo_idx"] = _photo_state.get("batch_start", 0) + local
    # 每10张持久化一次
    if local % 10 == 0:
        _save_photo_state()
    try:
        with open(batch[idx], "rb") as f:
            data = base64.b64encode(f.read()).decode()
        return {"data": data, "caption": os.path.basename(batch[idx])}
    except Exception as e:
        return {"data": "", "caption": str(e)}


def prep_context(now, cache, cfg=None):
    today = now.date()
    lang = ((cfg or {}).get("server", {}) or {}).get("language", "zh")
    en = (lang == "en")
    lu, lmon, lday, gz, zod = get_lunar(today)

    # ---- Home ----
    wn = cache.get("weather_now") or {}
    wd = cache.get("weather_daily") or []
    today_wd = wd[0] if wd else {}
    tmr_wd = wd[1] if len(wd) > 1 else {}
    # 苹果提醒(Mac 推)+ Microsoft To Do(服务端拉)合并;两源独立,谁挂不影响谁。
    reminders = (cache.get("reminders") or []) + (cache.get("reminders_mstodo") or [])
    pending = [r for r in reminders if not r.get("completed")]
    today_str = today.isoformat()
    cutoff = (today + timedelta(days=14)).isoformat()
    overdue, today_items, upcoming = [], [], []
    for r in pending:
        due_date = reminder_due_date(r.get("dueDate"), now.tzinfo)
        if not due_date:
            upcoming.append({"title": r.get("title",""), "dt": ""})
            continue
        due = due_date.isoformat()
        if due < today_str:
            overdue.append({"title": r.get("title",""), "dt": due_date.strftime("%m.%d")})
        elif due == today_str:
            today_items.append({"title": r.get("title",""), "dt": ""})
        elif due <= cutoff:
            delta = (due_date - today).days
            if en:
                tag = "Tomorrow" if delta == 1 else (f"+{delta}d" if delta <= 7 else due_date.strftime("%m.%d"))
            else:
                tag = "明天" if delta == 1 else (f"+{delta}天" if delta <= 7 else due_date.strftime("%m.%d"))
            upcoming.append({"title": r.get("title",""), "dt": tag})
    overdue.sort(key=lambda x: x["dt"]); upcoming.sort(key=lambda x: x["dt"])

    home = {
        "date_md": now.strftime("%m/%d"),
        "date_dot": now.strftime("%m.%d"),
        "weekday": (WEEKDAYS_EN if en else WEEKDAYS_CN)[now.weekday()],
        # 中国文化专属元素:英文版一律置空(模板据此隐藏),公历日期/星期保留
        "lunar": "" if en else f"{lmon}{lday}",
        "ganzhi": "" if en else f"{gz}{zod}年",
        "term": "" if en else solar_term_text(today),
        "year": today.year, "month": today.month,
        "weather": {
            "city": cache.get("weather_city", ""),
            "temp": wn.get("temp","--"), "cond": wn.get("text","--"),
            "feels": wn.get("feelsLike","--"), "humidity": wn.get("humidity","--"),
            "wind": f"{wn.get('windDir','')}{wn.get('windScale','')}" + ("" if en else "级"),
            "today_range": f"{today_wd.get('tempMin','--')}–{today_wd.get('tempMax','--')}°" if today_wd else "--",
            "tmr_range": f"{tmr_wd.get('tempMin','--')}–{tmr_wd.get('tempMax','--')}°" if tmr_wd else "--",
            "tmr_cond": tmr_wd.get("textDay",""),
        },
        "calendar": build_calendar(today, lang),
        "reminders": {"overdue": overdue, "today": today_items,
                      "upcoming": upcoming, "total": len(pending)},
    }

    # ---- AI ----
    # ccusage 本机直采(ccusage_cli):cc/codex 的 daily 数组,本机日志直接读出
    def _norm(d):
        return {"date": d["date"], "totalTokens": d.get("totalTokens", 0),
                "totalCost": d.get("totalCost", d.get("costUSD", 0))}
    cc = (cache.get("ccusage") or {}).get("cc", {}).get("daily", [])
    cx = [_norm(d) for d in (cache.get("ccusage") or {}).get("codex", {}).get("daily", [])]
    cc_today = next((d for d in cc if d["date"]==today_str), None)
    cx_today = next((d for d in cx if d["date"]==today_str), None)
    s7 = (today - timedelta(days=7)).isoformat()
    s30 = (today - timedelta(days=30)).isoformat()
    cc7 = sum(d["totalTokens"] for d in cc if d["date"]>=s7)
    cx7 = sum(d["totalTokens"] for d in cx if d["date"]>=s7)
    cc30 = sum(d["totalTokens"] for d in cc if d["date"]>=s30)
    cx30 = sum(d["totalTokens"] for d in cx if d["date"]>=s30)
    ccall = sum(d["totalTokens"] for d in cc)
    cxall = sum(d["totalTokens"] for d in cx)
    rl = cache.get("rate_limits") or {}
    five = rl.get("five_hour") or {}
    week = rl.get("seven_day") or {}
    cx_rl = cache.get("codex_rate_limits") or {}
    cx_five = cx_rl.get("five_hour") or {}
    cx_week = cx_rl.get("seven_day") or {}
    # 7天柱状图
    chart = []
    days = [(today - timedelta(days=i)) for i in range(6,-1,-1)]
    vals = []
    for d in days:
        ds = d.isoformat()
        ccv = next((x["totalTokens"] for x in cc if x["date"]==ds), 0)
        cxv = next((x["totalTokens"] for x in cx if x["date"]==ds), 0)
        vals.append((ccv, cxv))
    mx = max((a+b) for a,b in vals) or 1
    for d,(ccv,cxv) in zip(days, vals):
        chart.append({
            "day": d.strftime("%d"),
            "cc_h": round(ccv/mx*100), "cx_h": round(cxv/mx*100),
            "val": fmt_tok(ccv+cxv) if (ccv+cxv)>0 else "",
        })
    ai = {
        "five_pct": int(five.get("used_percentage") or 0),
        "five_reset": fmt_countdown(five["resets_at"], lang) if five.get("resets_at") else "--",
        "week_pct": int(week.get("used_percentage") or 0),
        "week_reset": fmt_countdown(week["resets_at"], lang) if week.get("resets_at") else "--",
        "today_cost": fmt_cost((cc_today["totalCost"] if cc_today else 0)+(cx_today["totalCost"] if cx_today else 0)),
        "cc_cost": fmt_cost(cc_today["totalCost"]) if cc_today else "$0",
        "cc_tok": fmt_tok(cc_today["totalTokens"]) if cc_today else "0",
        "cx_cost": fmt_cost(cx_today["totalCost"]) if cx_today else "$0",
        "cx_tok": fmt_tok(cx_today["totalTokens"]) if cx_today else "0",
        "cx_five_pct": int(cx_five.get("used_percentage") or 0),
        "cx_five_reset": fmt_countdown(cx_five["resets_at"], lang) if cx_five.get("resets_at") else "--",
        "cx_week_pct": int(cx_week.get("used_percentage") or 0),
        "cx_week_reset": fmt_countdown(cx_week["resets_at"], lang) if cx_week.get("resets_at") else "--",
        "tok_7d": fmt_tok(cc7+cx7), "tok_30d": fmt_tok(cc30+cx30), "tok_all": fmt_tok(ccall+cxall),
        "chart": chart,
    }
    # 自定义价:今日官方价 × 倍率(Claude/Codex 各一档,中转站对账用)。
    # 两档都=1.0 视为未配置,不显示自定义价(诚实降级,避免与官方价重复)。
    ai_cfg = (cfg or {}).get("ai_usage", {}) or {}
    # 额度面板显示哪一个(both/claude/codex):只用一家时隐藏另一块,不影响 token 统计/趋势图。
    quota_show = ai_cfg.get("quota_show") or "both"
    ai["show_cc_quota"] = quota_show in ("both", "claude")
    ai["show_cx_quota"] = quota_show in ("both", "codex")
    # 整列额度面板是否显示;none(中转站用户)→ 页面收成"趋势图为主+花费"。
    ai["show_quota_panel"] = ai["show_cc_quota"] or ai["show_cx_quota"]
    def _rate(key):
        try:
            return float(ai_cfg.get(key, 1.0))
        except (TypeError, ValueError):
            return 1.0
    cc_rate, cx_rate = _rate("claude_rate"), _rate("codex_rate")
    if cc_rate != 1.0 or cx_rate != 1.0:
        custom = ((cc_today["totalCost"] if cc_today else 0) * cc_rate
                  + (cx_today["totalCost"] if cx_today else 0) * cx_rate)
        ai["custom_total"] = f"¥{custom:.2f}"
    else:
        ai["custom_total"] = ""
    ai["custom_name"] = ""

    # ---- Device ----
    def dev(d, fields=None):
        if not d: return None
        mt = d.get("mem_total",1) or 1
        # 用 .get 容错:disks 来自设备上报(无鉴权口),异常/恶意 payload 缺字段也不能让渲染/发现接口崩
        vols = []
        for v in d.get("disks", []) or []:
            if not isinstance(v, dict):
                continue
            nm = v.get("name") or ""
            vols.append({"key": nm, "name": (("Total" if en else "总容量") if nm == "/" else nm), "pct": v.get("pct", 0),
                         "used": fmt_bytes(v.get("used", 0)), "total": fmt_bytes(v.get("total", 0))})
        # 字段显示控制:fields 留空=全显示;否则按勾选过滤(cpu/mem/net/disk_io/vol:<挂载点>)
        sel = set(fields or [])
        all_on = not sel
        if not all_on:
            vols = [v for v in vols if f"vol:{v.get('key', v['name'])}" in sel]
        return {
            "cpu": d.get("cpu_pct",0),
            "mem": round(d.get("mem_used",0)/mt*100),
            "mem_used": fmt_mem(d.get("mem_used",0)),
            "mem_total": fmt_mem(mt),
            "net_rx": fmt_speed(d.get("net_rx",0)), "net_tx": fmt_speed(d.get("net_tx",0)),
            "disk_r": fmt_speed(d.get("disk_read",0)), "disk_w": fmt_speed(d.get("disk_write",0)),
            "vols": vols,
            "show": {"cpu": all_on or "cpu" in sel, "mem": all_on or "mem" in sel,
                     "net": all_on or "net" in sel, "disk_io": all_on or "disk_io" in sel},
        }

    # 动态机器列表。数据 cache["devices_metrics"]={key: raw}(local/ssh 用 name 做 key,push 用 agent 上报的 id)。
    # 配置 cfg.devices.machines 提供显示名(可自定义)与字段勾选;未配置的 push 设备自动采纳(默认用 hostname)。
    devices_cfg = ((cfg or {}).get("devices", {}) or {}).get("machines", []) or []
    metrics_map = cache.get("devices_metrics") or {}
    # push 设备超时未上报视为离线(agent 关机/断网后最多 offline_after 秒从看板消失);
    # local/ssh 直采设备不带 updated_at,不参与时间过滤,服务端采集器在跑就常驻。
    offline_after = ((cfg or {}).get("devices", {}) or {}).get("offline_after", 180)
    machines = []
    seen = set()
    for mc in devices_cfg:
        key = (mc.get("id") or "").strip() or (mc.get("name") or "").strip()
        raw = metrics_map.get(key)
        if raw is None and mc.get("name"):
            key = (mc.get("name") or "").strip()
            raw = metrics_map.get(key)
        if raw is None:
            continue
        ua = raw.get("updated_at")
        if ua and (time.time() - ua) > offline_after:
            continue                     # push 设备离线(关机/断网):不上看板
        one = dev(raw, mc.get("fields"))
        if one:
            one["name"] = (mc.get("name") or key)
            machines.append(one)
            seen.add(key)
    # push 设备不再自动上看板——必须在设置页「发现设备」里点加进来才显示。
    # 未加进来的设备只出现在 /api/discovered-devices(设置页用)。
    device = {"machines": machines}

    # ---- Printer ----
    pr = cache.get("printer")
    printer = None
    if pr:
        status_map = PRINTER_STATUS["en"] if en else PRINTER_STATUS["zh"]
        st = pr.get("status") or ""
        state_text = status_map.get(st, st or ("Unknown" if en else "未知"))
        if not pr.get("online"):
            state_text = status_map["offline"]
        # 剩余时间：HA 的 remaining_time 单位是【小时】（如 7.25 = 7小时15分）
        rm = pr.get("remaining_min")  # 实为小时
        if rm is not None and rm > 0:
            h = int(rm)
            m = int(round((rm - h) * 60))
            if en:
                remaining_text = f"{h}h {m}m" if h else f"{m}m"
            else:
                remaining_text = f"{h}小时{m}分" if h else f"{m}分钟"
        else:
            remaining_text = "--"
        # 完成时刻：当前时间 + 剩余小时
        eta_clock = "--"
        if rm is not None and rm > 0:
            eta_clock = (now + timedelta(hours=rm)).strftime("%H:%M")
        # 文件名截断
        task = pr.get("task") or "--"
        printer = {
            "online": pr.get("online", False),
            "printing": st in ("running", "prepare", "slicing"),
            "paused": st == "pause",          # 仅 App 控制用(暂停态显示「恢复」);Kindle 模板可无视
            "state_text": state_text,
            "progress": pr.get("progress", 0),
            "task": task,
            "layer": pr.get("layer", "0"),
            "total_layer": pr.get("total_layer", "0"),
            "remaining_text": remaining_text,
            "eta_clock": eta_clock,
            "nozzle": pr.get("nozzle", "--"), "nozzle_t": pr.get("nozzle_t", "--"),
            "bed": pr.get("bed", "--"), "bed_t": pr.get("bed_t", "--"),
            "speed": (PRINTER_SPEED["en"] if en else PRINTER_SPEED["zh"]).get(pr.get("speed"), pr.get("speed","--")),
            "weight": pr.get("weight", "--"),
            "material": pr.get("material", "--"),
            "cooling_fan": pr.get("cooling_fan", "0"),
            "name": pr.get("printer_name", "A1"),
        }

    # ---- News(RSS 资讯,无状态时间分桶轮播) ----
    from server.sources.rss import pick_index
    news_cfg = (cfg or {}).get("news", {}) or {}
    raw_items = cache.get("news_items") or []
    nn = len(raw_items)
    news = {"entries": [], "index": 0, "total": nn,
            "title": news_cfg.get("title") or ("Headlines" if en else "AI 热点")}
    if nn:
        from server.config import schema
        mode = news_cfg.get("rotate", "random")
        # news 跟翻页节奏走:**一个完整轮播周期换一条新的,停留期间不变**(用户要求,Kindle/安卓一致)。
        # 周期 = page_interval × 启用页数 → news 每轮到一次才推进一条(不再用独立 rotate_interval 自走计时)。
        page_interval = max(5, int((cfg.get("server", {}) or {}).get("page_interval", 20) or 20))
        n_pages = max(1, len(schema.active_pages(cfg)))
        period = page_interval * n_pages
        start = pick_index(nn, mode, now.timestamp(), period)
        # 给一批候选(默认 12,wrapping);页面自适应引擎按容器高度取前缀显示:
        # 短讯多并几条、长文只第一条缩字铺满。够填满任何屏。
        batch = min(nn, 12)
        picked = [raw_items[(start + k) % nn] for k in range(batch)]
        news["entries"] = [{
            "title": it.get("title", ""),
            "summary": it.get("summary", ""),
            "source": it.get("source", ""),
            "category": it.get("category", ""),
            "when": _rel_time(it.get("ts", 0), now, lang),
            "link": it.get("link", ""),
        } for it in picked]
        news["index"] = start + 1     # 本批起始序号(显示"第 start+1 起 / nn 条")

    # ---- Download(qB + Transmission 合并;adapter 给原始值,这里格式化 + 状态本地化) ----
    draw = cache.get("download")
    dl_state = DL_STATE["en"] if en else DL_STATE["zh"]
    if not draw:
        download = {"ok": False, "dl_speed": "0", "up_speed": "0", "active": 0, "total": 0,
                    "ratio": "0.00", "uploaded": "--", "downloaded": "--", "free": "",
                    "torrents": [], "errors": []}
    else:
        ulb, dlb = draw.get("ul_bytes", 0), draw.get("dl_bytes", 0)
        free = draw.get("free", -1)
        download = {
            "ok": True,
            "dl_speed": fmt_speed(draw.get("dl_speed", 0)),
            "up_speed": fmt_speed(draw.get("up_speed", 0)),
            "active": draw.get("active", 0), "total": draw.get("total", 0),
            "ratio": f"{(ulb / dlb):.2f}" if dlb else "0.00",   # 聚合全局分享率
            "uploaded": fmt_bytes(ulb), "downloaded": fmt_bytes(dlb),
            "free": fmt_bytes(free) if isinstance(free, (int, float)) and free >= 0 else "",
            "errors": draw.get("errors", []) or [],
            "torrents": [{
                "name": t.get("name", ""),
                "progress": t.get("progress", 0),
                "dl": fmt_speed(t.get("dl", 0)), "up": fmt_speed(t.get("up", 0)),
                "ratio": f"{t.get('ratio', 0):.2f}",
                "size": fmt_bytes(t.get("size", 0)),
                "eta": _fmt_eta(t.get("eta"), lang),
                "state": t.get("state", "other"),
                "state_text": dl_state.get(t.get("state", "other"), t.get("state", "")),
                # 仅 App 控制用(点暂停/恢复→/api/action/torrent);Kindle 模板不读
                "id": t.get("id", ""),
                "client": t.get("client", ""),
            } for t in (draw.get("torrents_raw", []) or [])],
        }

    # ---- Music(Apple Music / Music.app,Mac agent 推送;无数据→空状态) ----
    # 采集器(后续独立任务)会把 agent 上报的 {player,track,artwork} 规整成扁平 cache["music"]。
    # 本层只做契约对齐 + 按 lang 派生文案/进度;缺数据则降级为空状态(has_track=False,诚实降级)。
    mraw = cache.get("music")
    music = contract.empty_music()
    mcfg = ((cfg or {}).get("music", {}) or {})
    try:
        pause_idle_after = int(mcfg.get("pause_idle_after", 300) or 0)
    except (TypeError, ValueError):
        pause_idle_after = 300
    if mraw and (mraw.get("name") or mraw.get("has_track")):
        st = mraw.get("state", "stopped")
        dur = mraw.get("duration", 0) or 0
        pos = mraw.get("position", 0) or 0
        pct = int(max(0, min(100, round(pos / dur * 100)))) if dur > 0 else 0
        rep = mraw.get("repeat", "off") or "off"
        shuf = bool(mraw.get("shuffle", False))
        state_since = mraw.get("state_since") or mraw.get("sampled_at") or 0
        paused_for = int(max(0, now.timestamp() - state_since)) if st == "paused" and state_since else 0
        idle_wall = bool(st == "paused" and pause_idle_after > 0 and paused_for >= pause_idle_after)
        artwork_wall, wall_cols, wall_rows, wall_mode = _music_wall_layout(mraw.get("artwork_wall", []) or [])
        lyrics = _sanitize_lyrics(mraw.get("lyrics"))
        music.update({
            "available": True, "has_track": True, "state": st,
            "has_lyrics": bool(lyrics), "lyrics": lyrics,
            "state_text": MUSIC_STATE[lang].get(st, MUSIC_STATE[lang]["stopped"]),
            "name": mraw.get("name") or "--",
            "artist": mraw.get("artist", "") or "",
            "album": mraw.get("album", "") or "",
            "album_artist": mraw.get("album_artist", "") or "",
            "composer": mraw.get("composer", "") or "",
            "genre": mraw.get("genre", "") or "",
            "year": str(mraw.get("year", "") or ""),
            "duration": dur, "duration_text": _fmt_mmss(dur),
            "position": pos, "position_text": _fmt_mmss(pos),
            "progress_pct": pct,
            "sampled_at": mraw.get("sampled_at", 0) or 0,
            "state_since": state_since,
            "paused_for": paused_for,
            "idle_wall": idle_wall,
            "shuffle": shuf, "shuffle_text": MUSIC_SHUFFLE[lang][shuf],
            "repeat": rep, "repeat_text": MUSIC_REPEAT[lang].get(rep, MUSIC_REPEAT[lang]["off"]),
            "track_number": mraw.get("track_number", 0) or 0,
            "track_count": mraw.get("track_count", 0) or 0,
            "loved": bool(mraw.get("loved", False)),
            "play_count": mraw.get("play_count", 0) or 0,
            "artwork_url": mraw.get("artwork_url", "") or "",
            "artwork_wall": artwork_wall,
            "artwork_wall_cols": wall_cols,
            "artwork_wall_rows": wall_rows,
            "artwork_wall_mode": wall_mode,
            "updated_at": mraw.get("updated_at", "") or "",
        })
    elif mraw is not None:
        # agent 在线但当前无播放:空状态页(模块仍 available,只是没歌)
        artwork_wall, wall_cols, wall_rows, wall_mode = _music_wall_layout(mraw.get("artwork_wall", []) or [])
        music["available"] = True
        music["idle_wall"] = True
        music["artwork_wall"] = artwork_wall
        music["artwork_wall_cols"] = wall_cols
        music["artwork_wall_rows"] = wall_rows
        music["artwork_wall_mode"] = wall_mode
        music["sampled_at"] = mraw.get("sampled_at", 0) or 0
        music["state_since"] = mraw.get("state_since", 0) or 0
        music["updated_at"] = mraw.get("updated_at", "") or ""

    batt = cache.get("kindle_battery")
    battery = {
        "level": batt if batt is not None else "--",
        "charging": cache.get("kindle_charging", False),
        "has": batt is not None,
    }

    return {
        "lang": lang,
        "now": now.strftime("%m/%d %H:%M"),
        "time_hm": now.strftime("%H:%M"),
        "clock": now.strftime("%H:%M:%S"),
        "battery": battery,
        # 页脚动态页码默认 1/1;真实渲染由各入口(render_all/app_page/preview)按启用页顺序覆盖。
        "page_no": 1, "page_total": 1,
        "home": home, "ai": ai, "device": device,
        "ha": cache.get("ha") or {"cards": []},   # 采集失败/未配 → 空墙(诚实降级,该页隐藏)
        "printer": printer,
        "news": news,
        "photo": _build_photo(cfg),
        "download": download,
        "music": music,
    }
