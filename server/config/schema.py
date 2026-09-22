"""配置 schema —— 配置层的唯一真相源。

一处定义,四处复用:
1. 校验   —— validate(config) 检查类型/必填。
2. 默认值 —— default_config() 生成全默认配置。
3. 设置网页 —— to_json() 把 schema 给前端,自动生成分模块表单(配置即页面)。
4. 模块启用 —— enabled_modules(config) 按关键字段是否填写,判断哪些数据源/页面开。

铁律对应:
- 零硬编码:所有用户数据(IP/密钥/城市/设备)都是这里的字段,代码别处不写死。
- 配置即页面:模块的 `enable_when` 决定"填了才出对应页",没填自动隐藏。
- 诚实降级:校验失败给出明确错误,不静默;缺非必填项用默认值兜底。

凭据安全:secret=True 的字段在生成 example / 给前端回显时**绝不带真实值**。
"""
from dataclasses import dataclass, field, asdict
from typing import Any, Optional


# ---- 字段类型 ----
# str / int / bool / float / enum(配 options) / str_list / module_list(配 item_fields)

@dataclass
class Field:
    key: str
    label: str                      # 设置网页显示的中文标签
    type: str = "str"
    default: Any = ""
    required: bool = False          # 模块启用时是否必填
    secret: bool = False            # 凭据,不回显真实值、不写进 example
    help: str = ""                  # 表单下方说明
    options: Optional[list] = None  # type=enum 时的可选值 [(value, label), ...]
    item_fields: Optional[list] = None  # type=module_list 时,每项的子字段
    hidden: bool = False            # True=不在设置网页单独渲染输入框(由配套控件维护,如城市名由城市选择器写入)
    label_en: str = ""              # i18n:英文标签;空=回退中文 label(见 to_json(lang))
    help_en: str = ""               # i18n:英文说明;空=回退中文 help


@dataclass
class Section:
    key: str
    label: str                      # 模块标题
    help: str = ""
    fields: list = field(default_factory=list)
    label_en: str = ""              # i18n:英文模块标题;空=回退中文
    help_en: str = ""               # i18n:英文模块说明;空=回退中文
    # 该模块"算启用(用户有意图)"的条件:列出的字段全部非空才算启用。
    # 设计原则:enable_when 放"足以表达启用意图"的字段,**不要放需要单独报错提示的凭据**。
    #   - 模块有非凭据的意图字段(如 HA 的 url 地址)→ 用它;凭据(token)靠 required 校验缺失。
    #   - 模块只有凭据能表达意图(如天气的 key)→ 才把凭据放进来,此时缺凭据=静默隐藏(诚实降级)。
    # 用途区分:enable_when 驱动 validate(有意图才查必填)与"配置即页面"的页面显示。
    enable_when: list = field(default_factory=list)
    # 该模块对应的页面 key(contract.PAGES 里的);None=非页面模块(如 server)。
    page: Optional[str] = None


# ============================================================
# Kindle 机型 → 横屏渲染分辨率(唯一数据源)
# ============================================================
# 竖屏物理分辨率转横屏(宽高对调)填入。基准画布是横屏 800×600(4:3),
# 主流机型几乎都是 3:4,等比放大即可(见 docs/multi-resolution-spec.md)。
# 元组:(value, label, render_width 横, render_height 横)
KINDLE_MODELS = [
    ("base",   "Kindle 基础版 6\"(竖 600×800)",                          800,  600),
    ("pw34",   "Paperwhite 3/4 · Voyage(竖 1072×1448)",                 1448, 1072),
    ("pw5",    "Paperwhite 5 · 6.8\"(竖 1236×1648)",                    1648, 1236),
    ("pw12",   "Paperwhite 12代/7\" · Oasis · Colorsoft(竖 1264×1680)",  1680, 1264),
    ("scribe", "Scribe 10.2\"(竖 1860×2480)",                           2480, 1860),
    ("custom", "自定义(下方手填横屏分辨率)",                               0,    0),
]
_MODEL_RES = {m[0]: (m[2], m[3]) for m in KINDLE_MODELS if m[0] != "custom"}


def resolve_render_size(server_cfg: dict) -> tuple:
    """按机型预设解析最终横屏输出分辨率。
    选了具体机型 → 用预设;选「自定义」或未知 → 用手填 render_width/height;
    缺省一律回落基准 800×600(诚实降级,绝不崩)。"""
    s = server_cfg or {}
    model = s.get("kindle_model", "base")
    if model in _MODEL_RES:
        return _MODEL_RES[model]
    try:
        w = int(s.get("render_width", 800) or 800)
        h = int(s.get("render_height", 600) or 600)
    except (TypeError, ValueError):
        w, h = 800, 600
    return (w if w > 0 else 800, h if h > 0 else 600)


# ============================================================
# Schema 定义
# ============================================================
SCHEMA: list = [
    Section(
        key="server", label="服务",
        help="服务基础设置,一般用默认值即可。",
        label_en="Server", help_en="Basic service settings; defaults are usually fine.",
        fields=[
            Field("language", "界面语言 / Language", "enum", "zh",
                  options=[("zh", "中文"), ("en", "English")],
                  help="设置页、菜单栏、看板的语言。英文版看板会隐藏农历、节气、生肖、天干地支等中国元素。",
                  label_en="Language", help_en="Language for the settings page, menu bar and dashboard. "
                  "The English dashboard hides Chinese-calendar elements (lunar date, solar terms, zodiac)."),
            Field("port", "端口", "int", 8585, label_en="Port"),
            Field("timezone", "时区", "str", "Asia/Shanghai", label_en="Timezone"),
            Field("page_interval", "轮播间隔(秒)", "int", 20,
                  help="Kindle 每隔多少秒切换到下一页。"),
            Field("kindle_model", "Kindle 机型", "enum", "base",
                  options=[(m[0], m[1], m[2], m[3]) for m in KINDLE_MODELS],
                  help="选你的 Kindle 型号,服务端按原生分辨率出清晰图(不糊)。"
                       "6 寸基础版选第一个即可;不确定型号查机器背面或「设置→设备信息」。"
                       "选「自定义」才需手填下方横屏分辨率。"),
            Field("render_width", "渲染宽(横屏)", "int", 800,
                  help="仅「自定义」机型需填:你的 Kindle 横屏分辨率(= 竖屏宽高对调,"
                       "如竖 1236×1648 填 1648)。选了具体机型则此值由机型自动决定。"),
            Field("render_height", "渲染高(横屏)", "int", 600,
                  help="仅「自定义」机型需填:横屏高(= 竖屏宽,如竖 1236×1648 填 1236)。"),
            Field("render_rotate", "旋转角度", "int", 270,
                  help="横屏渲染后旋转成竖屏写墨水屏。270=顺时针(横屏顶边朝屏幕右侧,默认);90=逆时针;0=不旋转。"),
            Field("render_grayscale", "灰度输出", "bool", True),
            Field("render_interval", "出图刷新间隔(秒)", "int", 30,
                  help="多久重渲染一次看板图(刷新时钟与已变数据)。与采集解耦,慢数据源不影响它。"),
            Field("app_poll_interval", "App 刷新间隔(秒)", "int", 3,
                  label_en="App refresh interval (s)",
                  help="安卓 App 版活 HTML 每隔几秒拉一次最新页面(触控版,与 Kindle 出图无关)。建议 3~10。"
                       "音乐页在歌曲结束时还会额外主动拉一次以尽快换到下一首。",
                  help_en="How often the Android app re-fetches the live page (touch version; unrelated to Kindle frames). 3~10 recommended. "
                          "The music page also pulls once extra when a track ends, to switch to the next song sooner."),
            Field("access_token", "设置页访问令牌", "str", "", secret=True,
                  help="保护设置页/配置接口不被同 WiFi 其他人访问。安装/首次启动自动生成,"
                       "用带令牌的链接打开设置页(install 会打印 :端口/setup?token=...)。"
                       "Kindle 拉图、设备上报不受影响。留空=不鉴权(不推荐)。"),
        ],
    ),

    Section(
        key="weather", label="天气", page="home",
        help="和风天气 QWeather 免费 API。填了 Key 才显示天气。",
        enable_when=["key", "location"],
        fields=[
            Field("provider", "服务商", "enum", "qweather",
                  options=[("qweather", "和风天气 QWeather")]),
            Field("host", "API Host", "str", "",
                  help="QWeather 控制台分配的专属域名,如 xxx.re.qweatherapi.com。"),
            Field("key", "API Key", "str", "", required=True, secret=True),
            Field("location", "城市", "city", "101010100", required=True,
                  help="搜城市名选择即可(自动匹配编码);也可在「高级」手填 LocationID。"),
            Field("location_name", "城市名", "str", "", hidden=True,
                  help="城市显示名,由城市选择器写入(GeoAPI 反查),用于看板天气标题。"),
            Field("interval", "采集间隔(秒)", "int", 600,
                  help="多久拉一次天气。变化慢,建议 ≥600(也省 API 限额)。"),
        ],
    ),

    Section(
        key="reminders", label="提醒事项", page="home",
        help="把本机「提醒事项」App(含 iPhone 经 iCloud 同步过来的)显示到看板。仅 macOS。"
             "启用/停用通过下方命令一键完成(会请求一次系统授权),无需重装。",
        enable_when=["enabled"],
        fields=[
            # enabled 不在网页直接开关:由 enable_reminders.sh / disable_reminders.sh 命令管理,
            # 避免"网页点了开、但本机 agent 没装"的假启用。设置页改为显示可复制命令(见 setup.html 特例渲染)。
            Field("enabled", "启用", "bool", False, hidden=True),
            Field("interval", "上报间隔(秒)", "int", 300,
                  help="Mac 提醒同步多久推一次。它是 launchd 定时器(非热重载):改后需重跑 enable_reminders.sh 才生效。"),
        ],
    ),

    Section(
        key="mstodo", label="Microsoft To Do", page="home",
        help="可选。把微软 To Do 待办显示到看板,与苹果提醒事项合并。点下方【连接】用你的微软账号登录一次即可,"
             "无需申请任何 API/密钥。仅显示未完成任务。",
        enable_when=["enabled"],
        fields=[
            # enabled 由登录端点写入(成功置 true、断开置 false),不在网页手填。
            Field("enabled", "启用", "bool", False, hidden=True),
            # client_id 有内置默认(公开客户端,免注册);进阶用户改 config.yaml 换自有应用。隐藏避免干扰普通用户。
            Field("client_id", "应用 ID", "str", "14d82eec-204b-4c2f-b7e8-296a70dab67e", hidden=True),
            Field("include_flagged_emails", "包含『标记的邮件』列表", "bool", False,
                  help="Outlook 标记邮件会生成一个任务列表,默认不混入提醒事项。"),
            Field("interval", "采集间隔(秒)", "int", 600,
                  help="多久拉一次微软待办。待办变化慢,建议 ≥600。"),
        ],
    ),

    Section(
        key="ai_usage", label="AI 用量", page="ai",
        help="Claude / Codex 的用量与花费。本机直接读 ccusage(npm 包,读本地日志),"
             "安装时选「启用 AI 用量」会自动装好;无需任何中间服务。",
        enable_when=["enabled"],
        fields=[
            Field("enabled", "启用", "bool", False),
            Field("quota_show", "额度面板显示", "enum", "both",
                  options=[("both", "Claude + Codex"), ("claude", "只显示 Claude"),
                           ("codex", "只显示 Codex"), ("none", "都不显示(仅 Token/花费)")],
                  help="AI 页『额度用量』(5h/周百分比+刷新倒计时)显示哪一个。"
                       "只用 Claude/Codex 时隐藏另一个;用中转站、无官方额度选「都不显示」——"
                       "页面自动收成趋势图为主+花费。均不影响 token 统计与趋势图采集。",
                  label_en="Quota panel", help_en="Which quota block to show on the AI page "
                       "(5h/week % + reset countdown). Hide the unused one, or pick 'none' (relay users with "
                       "no official quota) to collapse the page to a chart-first + cost layout. "
                       "Never affects token stats / trend chart collection."),
            Field("claude_rate", "Claude 价格倍率", "float", 1.0,
                  help="Claude 官方价 × 此倍率 = 自定义花费(中转站对账用)。默认 1.0=按官方价。"),
            Field("codex_rate", "Codex 价格倍率", "float", 1.0,
                  help="Codex 官方价 × 此倍率 = 自定义花费。默认 1.0=按官方价。两档都为 1 时不显示自定义价。"),
            Field("interval", "采集间隔(秒)", "int", 300,
                  help="本机 ccusage 多久采一次。每次解析本机日志约 10 秒,建议 ≥300。"),
            Field("codex_quota_interval", "Codex 额度上报间隔(秒)", "int", 600,
                  help="Codex 5h/周额度上报间隔。launchd 定时器(非热重载):改后重跑 enable_quota.sh。"),
            Field("claude_quota_interval", "Claude 额度上报节流(秒)", "int", 300,
                  help="Claude 额度走 Claude Code statusLine 推送(push),此值为节流间隔(非热重载)。"),
            Field("codex_proxy", "Codex 代理(可选)", "str", "",
                  help="国内访问 chatgpt.com 调 Codex 额度多半需代理,如 http://127.0.0.1:7897;留空=直连。"
                       "改后重跑 enable_quota.sh 生效。"),
        ],
    ),

    Section(
        key="home_assistant", label="Home Assistant", page=None,
        help="智能家居中枢。填了地址+令牌,打印机等设备页才能拉到数据。",
        enable_when=["url"],  # 填了地址=想启用;token 缺失由 required 校验报错(见 enable_when 设计原则)
        fields=[
            Field("url", "地址", "str", "",
                  help="如 http://192.168.x.x:8123"),
            Field("token", "长期访问令牌", "str", "", required=True, secret=True,
                  help="HA 用户资料页生成的 Long-Lived Access Token。"),
            Field("interval", "采集间隔(秒)", "int", 60,
                  help="多久拉一次 HA 实体状态(打印机/设备页共用这一次拉取)。"),
        ],
    ),

    Section(
        key="printer", label="3D 打印机", page="printer",
        help="经 Home Assistant 拉取(需先配好上面的 HA)。当前适配拓竹,后续抽象为通用实体。",
        enable_when=["enabled", "entity_prefix"],
        fields=[
            Field("enabled", "启用", "bool", False),
            Field("entity_prefix", "选择打印机", "printer", "",
                  help="从 Home Assistant 自动扫描可用打印机。高级用户仍可手填实体前缀。"),
        ],
    ),

    Section(
        key="news", label="资讯(RSS)", page="news",
        help="把任意 RSS 订阅源显示成一屏资讯,自动轮播。默认已预置 AIHOT(AI 行业每日精选),"
             "无需任何账号/密钥。想换源/加源,改下面的列表即可;清空则关闭此页。",
        label_en="News (RSS)",
        help_en="Show any RSS feed as a rotating headline page. Comes preloaded with AIHOT "
                "(curated daily AI news); no account or key needed. Add or change feeds below; "
                "clear the list to hide this page.",
        enable_when=["feeds"],          # 列表非空即启用(list 特判见 enabled_modules,同 ha_page/devices)
        fields=[
            Field("feeds", "订阅源", "module_list",
                  default=[{"url": "https://aihot.virxact.com/feed.xml", "name": "AIHOT"}],
                  label_en="Feeds",
                  item_fields=[
                      Field("url", "RSS 地址", "str", "", required=True,
                            help="任意 RSS 2.0 订阅地址。", label_en="RSS URL"),
                      Field("name", "来源名(可选)", "str", "",
                            help="留空=用 feed 自带的来源。", label_en="Name"),
                  ]),
            Field("rotate", "轮播方式", "enum", "random",
                  options=[("random", "随机"), ("time", "按时间(新→旧)")],
                  label_en="Rotation", help_en="random | by time (newest first)",
                  help="资讯页每轮到一次换一批新的(跟看板翻页节奏走、停留期间不变):随机=每次随机挑、按时间=从新到旧循环。"),
            Field("interval", "拉取间隔(秒)", "int", 1800,
                  label_en="Fetch interval (s)",
                  help="多久从 RSS 拉一次。资讯变化慢,建议 ≥1800。"),
        ],
    ),


    Section(
        key="photo", label="电子相册", page="photo",
        help="从本地/NAS 目录读取照片作为一个页面轮播。填了路径才显示此页。",
        label_en="Photo Gallery",
        help_en="Show photos from a local/NAS directory as a page.",
        enable_when=["path"],
        fields=[
            Field("path", "照片目录", "str", "/Volumes/摄影图片",
                  help="照片根目录，递归读取。", label_en="Photo path"),
            Field("grayscale", "灰度", "bool", True,
                  help="墨水屏推荐灰度。", label_en="Grayscale"),
            Field("interval", "轮播间隔(秒)", "int", 60,
                  help="每张照片停留多久。", label_en="Interval (s)"),
        ],
    ),
    Section(
        key="downloaders", label="下载器", page="download",
        help="接入 qBittorrent / Transmission(可多台),把所有种子合并显示成一屏下载看板。"
             "填了至少一个才出此页;清空则隐藏。账号密码只存本地。",
        label_en="Downloaders",
        help_en="Connect qBittorrent / Transmission (multiple OK); shows all torrents merged on one page. "
                "Add at least one to show the page; clear to hide. Credentials stored locally only.",
        enable_when=["clients"],        # 列表非空即启用(list 特判见 enabled_modules,同 ha_page/devices)
        fields=[
            Field("clients", "下载器", "module_list", default=[],
                  label_en="Clients",
                  item_fields=[
                      # name 必须第一位:loader 的 secret 回填按 item_fields[0] 匹配(密码不串台)
                      Field("name", "名称", "str", "", required=True,
                            help="自己起个名,如「群晖 qB」。", label_en="Name"),
                      Field("type", "类型", "enum", "qbittorrent",
                            options=[("qbittorrent", "qBittorrent"), ("transmission", "Transmission")],
                            label_en="Type"),
                      Field("host", "地址", "str", "", required=True,
                            help="如 192.168.x.x", label_en="Host"),
                      Field("port", "端口", "int", 8080,
                            help="qB 默认 8080;Transmission 默认 9091。", label_en="Port"),
                      Field("username", "用户名", "str", "", label_en="Username"),
                      Field("password", "密码", "str", "", secret=True, label_en="Password"),
                  ]),
            Field("rows", "显示条数", "int", 8,
                  help="种子列表最多显示几条(活跃优先);全局统计仍含全部。", label_en="Rows"),
            Field("interval", "采集间隔(秒)", "int", 15,
                  help="多久拉一次下载状态。下载变化快,建议 10~30。", label_en="Interval (s)"),
        ],
    ),

    Section(
        key="music", label="音乐(Apple Music)", page="music",
        help="显示 Mac 上 Apple Music / Music.app 的当前播放。由 Mac 上的 Music agent 定时上报;"
             "启用后即使没在放歌也会显示空状态页。采集 agent 用设置页给的一行命令装(install_music.sh),此处控制是否启用页面。",
        label_en="Music (Apple Music)",
        help_en="Show the current track from Apple Music / Music.app on your Mac, pushed by a Mac agent. "
                "Once enabled the page stays in rotation and shows an empty state when nothing is playing. "
                "The Mac agent is a separate upcoming task; this toggle only controls the page.",
        enable_when=["enabled"],
        fields=[
            Field("enabled", "启用", "bool", False, label_en="Enabled"),
            Field("interval", "上报间隔(秒)", "int", 5,
                  help="Mac Music agent 多久推一次当前播放。建议 2~5 秒;改后需重跑安装命令。",
                  label_en="Push interval (s)",
                  help_en="How often the Mac Music agent pushes the current track. 2-5 seconds recommended; rerun the install command after changing."),
            Field("pause_idle_after", "暂停转封面墙(秒)", "int", 300,
                  help="暂停超过多久后不再显示暂停歌曲页,改为专辑封面墙。0=关闭。",
                  label_en="Paused idle wall after (s)",
                  help_en="After this many paused seconds, show the album-wall screensaver instead of the paused track. 0 disables it."),
            Field("lyrics_enabled", "显示歌词", "bool", True,
                  help="换歌时自动联网查带时间轴的歌词(网易云优先、LRCLIB 兜底,只在手机/网页动态版滚动显示;"
                       "Kindle 静态版不显歌词)。查不到则回退普通播放器。需联网。",
                  label_en="Show lyrics",
                  help_en="Auto-fetch time-synced lyrics on track change (NetEase first, LRCLIB fallback; scrolls only "
                          "in the live phone/web view, never on the static Kindle view). Falls back to the plain player "
                          "when none found. Requires internet."),
            Field("artwork_cache_limit", "封面缓存上限(张)", "int", 120,
                  help="服务端最多缓存多少张历史专辑封面,用于无播放/暂停超时封面墙。",
                  label_en="Artwork cache limit",
                  help_en="Maximum number of historical album covers kept for the idle album wall."),
            Field("artwork_wall_count", "封面墙数量(张)", "int", 20,
                  help="每次封面墙最多抽取多少张缓存封面。实际数量受缓存中已有封面数量限制。",
                  label_en="Album wall covers",
                  help_en="Maximum cached covers sampled for each album wall."),
            Field("artwork_pool_size", "渐换封面池(张)", "int", 40,
                  help="手机/网页动态版封面墙渐换时,服务端在多大的封面池里挑替换(Apple TV 式)。"
                       "池子要大于封面墙数量才会漂移;Kindle 静态版不受影响(每次整批重抽)。",
                  label_en="Live wall pool size",
                  help_en="On the live phone/web wall, the server cycles covers from a pool of this size (Apple-TV style). "
                          "Must exceed the wall count to drift. The static Kindle wall is unaffected (it re-rolls the whole batch each render)."),
            Field("artwork_swap_interval", "渐换间隔(秒)", "int", 4,
                  help="手机/网页动态版封面墙每隔多少秒悄悄替换几张封面。Kindle 静态版不受影响。",
                  label_en="Live wall swap interval (s)",
                  help_en="How often (seconds) the live phone/web wall quietly swaps a few covers. The static Kindle wall is unaffected."),
            Field("artwork_swap_max", "渐换每次最多换(张)", "int", 3,
                  help="手机/网页动态版封面墙每次漂移最多替换几张(实际随机 1~该值)。Kindle 静态版不受影响。",
                  label_en="Live wall covers per swap",
                  help_en="Maximum covers swapped each drift on the live phone/web wall (actual is random 1..this). The static Kindle wall is unaffected."),
        ],
    ),

    Section(
        key="ha_page", label="智能家居", page="ha",
        help="把 Home Assistant 里你关心的实体显示成一面卡片墙(需先配好上面的 Home Assistant 地址+令牌)。",
        enable_when=["entities"],   # 选了至少一个实体才启用;list 特判见 enabled_modules
        fields=[
            Field("entities", "实体卡片", "module_list", default=[],
                  item_fields=[
                      Field("entity_id", "实体", "ha_entity", "", required=True,
                            help="从 HA 搜索选择,自动带出名称和图标。"),
                      Field("name", "显示名", "str", "",
                            help="留空=用 HA 的友好名(friendly_name)。"),
                      Field("icon", "图标", "str", "", hidden=True,
                            help="mdi:xxx;留空=用 HA 实体自带图标。由实体选择器写入,高级可手填。"),
                  ]),
        ],
    ),

    Section(
        key="devices", label="设备监控", page="device",
        help="要监控的机器(Windows/Linux/Mac 均可)。本机直读,或远程机器装 agent 推送上报(不交密码)。",
        enable_when=[],  # 有任意一台设备即启用,见 enabled_modules 特判
        fields=[
            Field("machines", "机器列表", "module_list", default=[],
                  item_fields=[
                      Field("name", "名称", "str", "", required=True,
                            help="显示名,可自定义(如 客厅NAS)。push 设备默认用 hostname,可改。"),
                      # 关联键:push=agent 上报的 id/hostname;local 留空。
                      Field("id", "标识", "str", "",
                            help="数据关联用。push 由 agent 上报;local 一般留空。"),
                      # 要显示的指标条:cpu/mem/net/disk_io/vol:<挂载点>。留空=全显示。
                      # 可勾选项由设置网页按设备实际上报内容动态生成。
                      Field("fields", "显示项", "str_list", default=[],
                            help="勾选要显示的指标,留空=全显示。"),
                      Field("mode", "采集方式", "enum", "local",
                            options=[("local", "本机直读"),
                                     ("push", "推(目标机装 agent)")]),
                      # 被监控机的系统 —— 采集脚本按它分(linux/macos/windows)。
                      # local 自检;auto 则服务端先探测,建议明确选。
                      Field("platform", "系统", "enum", "auto",
                            options=[("auto", "自动识别"), ("linux", "Linux"),
                                     ("macos", "macOS"), ("windows", "Windows")]),
                  ]),
            Field("interval", "采集间隔(秒)", "int", 30,
                  help="服务端多久采一次本机直读(local)设备指标。想更实时就调小。"
                       "注:push 设备的上报间隔由目标机 agent 自己定,不受此值控制。"),
            Field("offline_after", "离线判定(秒)", "int", 180,
                  help="push 设备超过这么久没上报就判离线、从看板撤下(目标机关机/断网后自动消失)。"
                       "建议 ≥ agent 上报间隔的 3 倍,避免偶发丢一两次就误判。本机直读的设备不受影响。",
                  label_en="Offline after (s)",
                  help_en="A push device with no report for this many seconds is treated as offline and "
                          "dropped from the dashboard (so it disappears after the machine powers off / "
                          "drops network). Use >= 3x the agent's report interval to avoid false positives "
                          "from an occasional missed report. Local devices are unaffected."),
        ],
    ),

    Section(
        key="display", label="页面与风格", page=None,
        help="启用哪些页、轮播顺序、用哪套风格。留空=按数据源自动决定。",
        fields=[
            Field("pages", "页面顺序", "str_list", default=[],
                  label_en="Page order",
                  help="用 ↑/↓ 调翻页顺序;用每行的开关关掉不想要的页——关掉的页即使配了数据源也不显示、不刷新。",
                  help_en="Reorder pages with ↑/↓; use each row's toggle to switch off pages you don't want — "
                          "a disabled page is hidden and not refreshed even if its data source is configured."),
            Field("hidden_pages", "隐藏的页", "str_list", default=[], hidden=True,
                  label_en="Hidden pages",
                  help="被用户手动关闭的页(由页面顺序编辑器维护,不单独渲染)。",
                  help_en="Pages turned off by the user (maintained by the page-order editor)."),
            Field("style_mode", "风格模式", "enum", "fixed",
                  options=[("fixed", "固定一套"), ("daily_random", "每日随机")]),
            Field("style", "风格", "str", "style_a"),
            Field("style_rotation", "随机池", "str_list", default=["style_a"],
                  help="每日随机时,从这些风格里按日期选。可填:经典仪表盘、卡片分区、蓝图线框、仪表盘、极简、报纸、终端。"),
        ],
    ),
]


# ============================================================
# 派生工具
# ============================================================
_SECTIONS = {s.key: s for s in SCHEMA}


def default_config() -> dict:
    """从 schema 生成全默认配置(secret 字段为空)。"""
    cfg = {}
    for sec in SCHEMA:
        d = {}
        for f in sec.fields:
            if f.type == "module_list":
                # 套用字段自带的默认列表(如 news.feeds 预置 AIHOT,无需凭据即默认开);
                # 未设默认的(设备/下载器/HA 实体)为空。复制每项,避免多配置共享同一可变默认对象。
                d[f.key] = [dict(x) for x in f.default] if isinstance(f.default, list) else []
            elif f.secret:
                d[f.key] = ""           # 凭据默认空
            else:
                d[f.key] = f.default
        cfg[sec.key] = d
    return cfg


def _is_filled(v) -> bool:
    if v is None:
        return False
    if isinstance(v, str):
        return v.strip() != ""
    if isinstance(v, (list, dict)):
        return len(v) > 0
    if isinstance(v, bool):
        return v
    return True  # 数字等


def enabled_modules(config: dict) -> dict:
    """判断每个模块是否启用(配置即页面的核心)。
    返回 {section_key: bool}。"""
    out = {}
    for sec in SCHEMA:
        secd = config.get(sec.key, {}) or {}
        if sec.key == "devices":
            out[sec.key] = len(secd.get("machines", []) or []) > 0
            continue
        if sec.key == "ha_page":            # 同 devices:列表非空即启用(选了实体才出 ha 页)
            out[sec.key] = len(secd.get("entities", []) or []) > 0
            continue
        if sec.key == "news":               # 同 ha_page:订阅源列表非空即启用
            out[sec.key] = len(secd.get("feeds", []) or []) > 0
            continue
        if sec.key == "downloaders":        # 同 ha_page:下载器列表非空即启用
            out[sec.key] = len(secd.get("clients", []) or []) > 0
            continue
        if not sec.enable_when:
            out[sec.key] = True     # 无启用条件的模块(server/display)恒启用
            continue
        out[sec.key] = all(_is_filled(secd.get(k)) for k in sec.enable_when)
    return out


def active_pages(config: dict) -> list:
    """最终要渲染/轮播的页面列表(顺序敏感)。
    display.pages 非空 → 用它(但仍过滤掉数据源没配的页);
    为空 → 按已启用的数据源自动推导。"""
    enabled = enabled_modules(config)
    ha_cfg = config.get("home_assistant", {}) or {}
    ha_ready = _is_filled(ha_cfg.get("url")) and _is_filled(ha_cfg.get("token"))
    # 数据源模块 → 页面 的映射(server/home_assistant/display 不直接对应页)
    page_ready = {
        "home": enabled.get("weather") or enabled.get("reminders") or enabled.get("mstodo"),
        "ai": enabled.get("ai_usage"),
        "device": enabled.get("devices"),
        "ha": enabled.get("ha_page") and ha_ready,      # 选了实体 + HA 地址/令牌都配好,才出 ha 页
        "printer": enabled.get("printer") and ha_ready, # 打印机也经 HA,同理依赖 HA 有效
        "news": enabled.get("news"),
        "photo": bool((config or {}).get("photo", {}).get("path")),                    # 订阅源非空即出页(无凭据依赖)
        "download": enabled.get("downloaders"),         # 下载器列表非空即出页
        "music": enabled.get("music"),                  # 启用即出页(空状态也显示,无外部凭据依赖)
    }
    default_order = ["home", "ai", "news", "photo", "music", "download", "device", "ha", "printer"]
    chosen = config.get("display", {}).get("pages") or []
    hidden = config.get("display", {}).get("hidden_pages") or []
    base = chosen if chosen else default_order
    # 补上 base 没列、但已就绪的页(自定义顺序后新配的数据源页不致凭空消失);
    # display.pages 是"顺序偏好"而非"白名单",配了数据源的页默认显示(诚实降级);
    # 但 hidden_pages 里的页是用户在设置页主动关闭的,即使数据源就绪也撤下、不渲染。
    order = list(base) + [p for p in default_order if p not in base]
    return [p for p in order if page_ready.get(p) and p not in hidden]


def validate(config: dict) -> list:
    """校验配置,返回错误信息列表(空=通过)。
    只对【已启用】的模块查必填项,未启用模块不强求填写(诚实降级)。"""
    errors = []
    enabled = enabled_modules(config)
    for sec in SCHEMA:
        secd = config.get(sec.key, {}) or {}
        for f in sec.fields:
            if f.type == "module_list":
                for i, item in enumerate(secd.get(f.key, []) or []):
                    for sub in (f.item_fields or []):
                        if sub.required and not _is_filled(item.get(sub.key)):
                            errors.append(f"{sec.label}[{i}].{sub.label} 必填")
                continue
            v = secd.get(f.key)
            if v is not None and not _check_type(v, f.type):
                errors.append(f"{sec.label}.{f.label} 类型应为 {f.type}")
            if f.required and enabled.get(sec.key) and not _is_filled(v):
                errors.append(f"{sec.label}.{f.label} 必填")
    # 天气 API Host 软校验:填了但明显不是纯域名(带协议/空格/非 ASCII)→ 阻止保存并提示
    host = ((config.get("weather", {}) or {}).get("host") or "").strip()
    if host and (("://" in host) or (" " in host) or any(ord(c) > 127 for c in host)):
        errors.append("天气.API Host 格式有误:应是纯域名(如 xxx.re.qweatherapi.com),不要带 http:// 或空格")
    return errors


def _check_type(v, t) -> bool:
    if t in ("str", "enum", "city", "ha_entity", "printer"):
        return isinstance(v, str)
    if t == "int":
        return isinstance(v, int) and not isinstance(v, bool)
    if t == "float":
        return isinstance(v, (int, float)) and not isinstance(v, bool)
    if t == "bool":
        return isinstance(v, bool)
    if t == "str_list":
        return isinstance(v, list) and all(isinstance(x, str) for x in v)
    return True


# enum 选项标签的英文(按 value 翻;to_json(lang='en') 时替换 options[i][1])。
# 不在此表的 value 回退中文标签。机型名(KINDLE_MODELS)保留原样(含分辨率,语言无关)。
OPTION_LABELS_EN = {
    "zh": "中文", "en": "English",
    "qweather": "QWeather",
    "local": "Local (read on host)", "push": "Push (agent on target)",
    "auto": "Auto-detect", "linux": "Linux", "macos": "macOS", "windows": "Windows",
    "fixed": "Fixed one", "daily_random": "Daily random",
    "both": "Claude + Codex", "claude": "Claude only", "codex": "Codex only",
    "none": "None (tokens/cost only)",
}


def _opt_en(o):
    """单个 enum option(list/tuple [value,label,...])→ 英文 label,其余元素保留。"""
    o = list(o)
    if o and o[0] in OPTION_LABELS_EN:
        o[1] = OPTION_LABELS_EN[o[0]]
    return o


def to_json(lang: str = "zh") -> list:
    """给设置网页:schema 的可序列化表达(不含任何用户数据)。
    lang='en' 时把 label/help/options 换成英文(label_en/help_en 为空则回退中文)。
    lang='zh'(默认)= 原样输出,保证中文行为像素级不变。"""
    out = []
    for s in SCHEMA:
        sd = asdict(s)
        if lang == "en":
            sd["label"] = s.label_en or s.label
            sd["help"] = s.help_en or s.help
            for fd, f in zip(sd["fields"], s.fields):
                fd["label"] = f.label_en or f.label
                fd["help"] = f.help_en or f.help
                if fd.get("options"):
                    fd["options"] = [_opt_en(o) for o in fd["options"]]
                for ifd, iff in zip(fd.get("item_fields") or [], f.item_fields or []):
                    ifd["label"] = iff.label_en or iff.label
                    ifd["help"] = iff.help_en or iff.help
                    if ifd.get("options"):
                        ifd["options"] = [_opt_en(o) for o in ifd["options"]]
        out.append(sd)
    return out
