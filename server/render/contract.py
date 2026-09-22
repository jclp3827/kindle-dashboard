"""数据契约(Data Contract)—— 风格系统的地基。

风格(模板皮肤)引用的就是这里定义的字段。契约一旦冻结,所有风格共享同一套字段;
改契约会牵动所有风格,所以非必要不动。

本文件是契约的**权威定义**(docs/data-contract.md 是它的人类可读摘要,二者须同步)。

两个职责:
1. 描述每页 render context 有哪些字段(见 PAGES 与下方各 empty_* 函数的结构)。
2. 提供"降级空上下文" empty_context():缺数据 / 预览 / 数据源未配置时,
   渲染仍能出图而非报错(对应铁律「诚实降级」)。

注意:字段命名与结构 1:1 沿用现状(老 data_prep.py 的输出),保证已有 style_a 模板
零摩擦搬运。打印机字段当前贴合「单台 3D 打印机」,P2 抽象成通用 HA 实体卡片时再扩展契约。
"""

# 页面 key → 该页消费的契约段。
# 顶层时间/电池字段(now/time_hm/clock/battery)所有页可用。
# enabled 由配置决定(配置即页面);契约只负责"有这个页时,它能用哪些字段"。
PAGES = {
    "home":    {"title": "首页",   "section": "home",    "needs": ["weather", "reminders"]},
    "ai":      {"title": "AI 用量", "section": "ai",      "needs": ["ai_usage"]},
    "device":  {"title": "设备",   "section": "device",  "needs": ["devices"]},
    "ha":      {"title": "智能家居", "section": "ha",      "needs": ["ha_page"]},
    "printer": {"title": "打印机", "section": "printer", "needs": ["printer"]},
    "news":    {"title": "资讯",   "section": "news",    "needs": ["news"]},
    "download":{"title": "下载",   "section": "download","needs": ["downloaders"]},
    "photo":   {"title": "相册",   "section": "photo",    "needs": ["photo"]},
    "music":   {"title": "音乐",   "section": "music",   "needs": ["music"]},
}

# 降级占位符:数字类用 0,文本类用 "--",列表类用 []。
DASH = "--"


def empty_top():
    """顶层字段(所有页共享)。"""
    return {
        "lang": "zh",       # 界面/看板语言 zh|en;模板用它隐藏中国元素(英文版)
        "now": DASH,        # "05/27 14:30"  当前日期+时间
        "time_hm": DASH,    # "14:30"
        "clock": DASH,      # "14:30:05"
        "battery": {
            "level": DASH,      # int 0-100 或 "--"
            "charging": False,  # bool
            "has": False,       # bool,有无电池数据(无则模板不渲染电池块)
        },
        # 页脚动态页码:由渲染入口按「实际启用页顺序」(active_pages)注入,反映用户手动排序。
        # 默认 1/1(空上下文/测试),真实渲染时被覆盖。模板用 {{ page_no }} / {{ page_total }}。
        "page_no": 1,
        "page_total": 1,
    }


def empty_home():
    """首页:日期/农历/天气/日历/提醒。"""
    return {
        "date_md": DASH,    # "05/27"
        "date_dot": DASH,   # "05.27"
        "weekday": DASH,    # "周三"
        "lunar": DASH,      # "四月初一"
        "ganzhi": DASH,     # "丙午马年"
        "term": "",         # "今日芒种" / "夏至还有3天" / ""
        "year": 0, "month": 0,
        "weather": {        # 数据源 weather;未配置则全 "--"
            "city": "",              # 城市名(GeoAPI 反查 location);未配置/查不到则空,模板不显城市
            "temp": DASH, "cond": DASH, "feels": DASH, "humidity": DASH,
            "wind": DASH,            # "西北风3级"
            "today_range": DASH,     # "18–26°"
            "tmr_range": DASH,       # "19–27°"
            "tmr_cond": "",          # 明日天气文字
        },
        # 月历:周行数组,每格为 None(空)或 {d,l,today,holiday,weekend}
        "calendar": [],
        "reminders": {      # 数据源 reminders(Mac 推送);未配置则各列表为空
            "overdue": [],   # [{title, dt}]  dt 如 "05.20"
            "today": [],     # [{title, dt}]
            "upcoming": [],  # [{title, dt}]  dt 如 "明天"/"+3天"/"05.30"
            "total": 0,
        },
    }


def empty_ai():
    """AI 用量页:额度百分比 + 花费 + 7 天柱状图。"""
    return {
        "five_pct": 0, "five_reset": DASH,       # Claude 5h 额度已用% / 重置倒计时
        "week_pct": 0, "week_reset": DASH,       # Claude 周额度
        "cx_five_pct": 0, "cx_five_reset": DASH, # Codex 5h 额度
        "cx_week_pct": 0, "cx_week_reset": DASH, # Codex 周额度
        "today_cost": "$0",                      # 今日总花费
        "cc_cost": "$0", "cc_tok": "0",          # Claude 今日花费/token
        "cx_cost": "$0", "cx_tok": "0",          # Codex 今日花费/token
        "tok_7d": "0", "tok_30d": "0", "tok_all": "0",
        "chart": [],        # [{day:"27", cc_h:0-100, cx_h:0-100, val:"1.2M"}] 近 7 天
        "custom_total": "",  # 自定义倍率折算的今日实际花费,如 "¥12.34"
        "custom_name": "",   # 中转站/供应商名
    }


def empty_device_one():
    """单台设备指标。name=显示名(可自定义);show=各指标条是否显示(按用户勾选)。"""
    return {
        "name": "--",        # 显示名(local/ssh 来自配置;push 默认 hostname,可改)
        "cpu": 0,            # CPU 使用率 %
        "mem": 0,            # 内存使用率 %
        "mem_used": "0", "mem_total": "0",
        "net_rx": "0", "net_tx": "0",         # 网络收发速率
        "disk_r": "0", "disk_w": "0",         # 磁盘读写速率
        "vols": [],          # [{name, pct, used, total}] 各分区(按勾选过滤)
        # 字段勾选结果:留空配置=全 True;否则按用户勾选
        "show": {"cpu": True, "mem": True, "net": True, "disk_io": True},
    }


def empty_device():
    """设备监控页:动态机器列表(Windows/Linux/Mac 任意台)。
    machines = [empty_device_one(), ...];无机器时为空,该页隐藏。"""
    return {"machines": []}


def empty_ha():
    """智能家居实体墙;未配置/拉不到则卡片列表为空,该页隐藏。
    单张卡片对象(契约,冻结字段名):
        name        str   显示名(用户覆盖 or HA 友好名)
        kind        str   toggle/lock/cover/binary/sensor/climate/media/presence/text
                          /select/number/button/scene/alarm
        icon        str   MDI 图标名(mdi:xxx);空串=不显图标
        on          bool  激活态强调(开/有人/已锁/播放中…);sensor/select/number/
                          button/scene/alarm 恒 false(不改 Kindle 主次)
        state_text  str   主显文本(toggle/lock/cover/binary/climate/media/presence/text
                          /select/number/button/scene/alarm)
        value       str   主显数值(sensor;非 sensor 为空)
        unit        str   数值单位(sensor;如 °C / % / W)
        sub         str   次要行(climate 目标温度、media 标题…),可空
        entity_id   str   HA 实体 id(**仅 App 交互用**:点卡→/api/action/ha;Kindle 模板无视)
        meta        dict  **仅 App 控制面板用**(B 类可控卡才有;Kindle 模板无视,不影响出图):
                          cover  {on, position}
                          climate{on, mode, modes[], current, target}
                          media  {on, title}
                          select {options[], current}
                          number {min, max, step, unit, value}
                          alarm  {state}
                          (toggle/lock/scene/button=A 类单击直发,sensor 等=只读详情,均无 meta)
    模板主显:value 非空 → value 大字 + unit 小字;否则 state_text 大字。sub 非空加一行。
    交互模型见 docs/ha-page-interaction-spec.md。**meta 是 App 出口追加的字段名**(非 dict 方法名,安全)。
    """
    return {"cards": []}


def empty_printer():
    """3D 打印机页(依赖 Home Assistant)。printer 为 None 时整页降级/隐藏。"""
    return {
        "online": False,
        "printing": False,
        "paused": False,         # 仅 App 控制用(暂停态→显示「恢复」按钮);Kindle 模板无视
        "state_text": DASH,      # "打印中"/"空闲"/"离线"...
        "progress": 0,           # 0-100
        "task": DASH,            # 文件名
        "layer": "0", "total_layer": "0",
        "remaining_text": DASH,  # "2小时15分"
        "eta_clock": DASH,       # 预计完成时刻 "16:45"
        "nozzle": DASH, "nozzle_t": DASH,   # 喷嘴温度 / 目标
        "bed": DASH, "bed_t": DASH,         # 热床温度 / 目标
        "speed": DASH,           # 速度档位
        "weight": DASH,          # 耗材重量
        "material": DASH,        # 耗材
        "cooling_fan": "0",      # 风扇转速
        "name": DASH,            # 打印机名
    }


def empty_news():
    """RSS 资讯页:轮播选中的 1 条(或多条)。未配 feed/拉不到 → items 空,该页隐藏。
    单条对象(契约,冻结字段名):
        title    str  标题
        summary  str  正文整段(feed 的 description,已去 HTML)
        source   str  来源名(author 括号内 / feed 名)
        category str  话题(feed 带 <category> 才有,AIHOT 无 → 空串,模板有则显示)
        when     str  相对时间,如 "2小时前" / "2h ago" / 日期(已按 lang 本地化)
        link     str  原文链接(墨水屏不可点,默认不显示)
    """
    return {
        # 字段名用 entries 不用 items:Jinja `news.items` 会撞 dict.items() 方法(坑),entries 不撞。
        "entries": [],   # [单条, ...] 轮播选中的条目(默认 1 条)
        "index": 0,      # 当前在全部条目中的序号(从 1 起,给模板显示"第 x 条")
        "total": 0,      # 全部条目数(显示"/N")
        "title": DASH,   # 页面主标题(通常来自 strings.json 的 t,channel 标题兜底)
    }


def empty_download():
    """下载看板页(qBittorrent + Transmission 合并)。未配/全连不上 → torrents 空,该页隐藏。
    顶层全局字段已格式化;单条种子(torrents[*],冻结字段名):
        name        str   种子名
        progress    int   0~100
        dl / up     str   速度,已格式化 "29 MB/s" / "0"
        ratio       str   分享率 "0.43"
        size        str   "12.3G"
        eta         str   "12分" / "1时5分" / "—"(已本地化)
        state       str   统一枚举 downloading/seeding/paused/checking/queued/stalled/error/other(模板选图标)
        state_text  str   已本地化状态文字(下载中/做种/暂停…)(模板显示)
        id          str   种子 id(**仅 App 控制用**:qB=hash / Transmission=数字 id;Kindle 模板无视)
        client      str   所属下载器名(**仅 App 控制用**:按它路由到对应 adapter;Kindle 模板无视)
    """
    return {
        "ok": False,
        "dl_speed": "0", "up_speed": "0",   # 已格式化总速度
        "active": 0, "total": 0,
        "ratio": "0.00",                    # 聚合全局分享率
        "uploaded": DASH, "downloaded": DASH,  # 累计(已格式化)
        "free": "",                         # 剩余空间;未知=空串(模板不显)
        "torrents": [],                     # 已排序+截断;字段见上
        "errors": [],                       # 连不上的下载器名(部分离线提示)
    }


def empty_music():
    """音乐播放页(Apple Music / Music.app,由 Mac 上的 Music agent 推送)。
    has_track=False → 空状态「当前无播放」:模板显占位框、**不显假进度条**(对标打印机页「无任务」)。

    渲染目标分层(模板用 target 区分):
      静态目标(target=kindle/legacy、/web-simple):**不显** progress/position/递增时间,只显「当前是哪首歌」。
      动态目标(target=android):前端据 position+duration+sampled_at 自走进度条与递增时间。
    *_text 字段一律由 build_context 按 lang 产出(state_text/shuffle_text/repeat_text/位置时长),
    **别在模板里写死中文**(i18n 红线)。字段名不用 dict 方法名(items/keys/get)。
    """
    return {
        "available": False,    # 模块已启用且 agent 有效(长期不可用可考虑撤页;空状态仍 True)
        "has_track": False,    # 有无当前曲目;False=空状态
        "state": "stopped",    # playing / paused / stopped
        "state_text": DASH,    # 已本地化:播放中 / 已暂停 / 已停止
        "name": DASH,          # 歌名
        "artist": "",          # 艺人(可空)
        "album": "",           # 专辑(可空)
        "album_artist": "",    # 专辑艺人(可选)
        "composer": "",        # 作曲(可选)
        "genre": "",           # 流派(可选)
        "year": "",            # 年份(可空,字符串)
        "duration": 0,         # 总时长(秒,动态目标用)
        "duration_text": DASH, # "5:14"
        "position": 0,         # 当前进度(秒,动态目标用)
        "position_text": DASH, # "1:43"
        "progress_pct": 0,     # 0-100(动态目标用)
        "sampled_at": 0,       # 采集时刻 epoch 秒(动态目标:前端据此推算实时进度)
        "state_since": 0,      # 当前播放状态从何时开始(epoch 秒);暂停超时屏保用
        "paused_for": 0,       # 已暂停秒数(非 paused 为 0)
        "idle_wall": False,    # True=显示封面墙/屏保主体(无播放或暂停超时)
        "shuffle": False,      # 随机播放
        "shuffle_text": DASH,  # 已本地化:随机开 / 随机关
        "repeat": "off",       # off / all / one
        "repeat_text": DASH,   # 已本地化:循环关 / 列表循环 / 单曲循环
        "track_number": 0, "track_count": 0,   # 曲目序号 / 总数(可选)
        "loved": False,        # 是否喜欢(可选)
        "play_count": 0,       # 播放次数(可选)
        "has_lyrics": False,   # 是否查到带时间轴歌词(仅 android 动态目标用;Kindle 永不显歌词)
        "lyrics": [],          # [{t: 秒(float), text: 该行}] 按时间升序;空=无歌词→回退现状播放器
        "artwork_url": "",     # 封面 URL;空=无封面,模板显占位框
        "artwork_wall": [],    # [{url, album, artist, hash}] 无播放/暂停超时封面墙
        "artwork_wall_cols": 0, # 封面墙建议列数:0=无缓存,1/2/3/4/5 自适应
        "artwork_wall_rows": 0, # 封面墙建议行数:0=无缓存,1/2/3/4 自适应
        "artwork_wall_mode": "empty", # empty/one/two/four/nine/sixteen/twenty
        "updated_at": "",      # 更新时间 "17:22"(可选)
    }


def empty_context():
    """完整的降级上下文:所有页字段齐备、全为占位值。
    预览无数据、数据源未配置、采集失败时用它兜底,保证渲染出图不报错。"""
    ctx = empty_top()
    ctx["home"] = empty_home()
    ctx["ai"] = empty_ai()
    ctx["device"] = empty_device()
    ctx["ha"] = empty_ha()
    ctx["printer"] = None   # 默认无打印机
    ctx["news"] = empty_news()
    ctx["download"] = empty_download()
    ctx["music"] = empty_music()
    return ctx
