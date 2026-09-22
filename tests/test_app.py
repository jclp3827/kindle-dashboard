"""主服务 API 验证(FastAPI TestClient)。用临时 config,不污染仓库;不触发采集/渲染线程。"""
import os
import sys
import json
import base64
import tempfile
import shutil

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
# 必须在 import app 前指定临时配置路径(app 模块级初始化 ConfigManager)
TEST_DATA_DIR = tempfile.mkdtemp()
os.environ["KINDLE_CONFIG"] = os.path.join(TEST_DATA_DIR, "config.yaml")
os.environ["KINDLE_DATA_DIR"] = TEST_DATA_DIR

from fastapi.testclient import TestClient  # noqa: E402
from server.app import app, cm              # noqa: E402
import server.app as _appmod                 # noqa: E402

_appmod.lyrics.fetch_lyrics = lambda *a, **k: []   # 测试绝不触网查歌词(music 推送会触发后台查询)

client = TestClient(app)                     # 不用 with → 不触发 startup/data_loop


def test_health():
    j = client.get("/health").json()
    assert j["status"] == "ok" and "active_pages" in j


def test_auth_token_protects_management_apis():
    """设了访问令牌:配置/管理接口需令牌;Kindle 拉图、设备上报、health 豁免。"""
    cm.get()["server"]["access_token"] = "T0KEN"   # get() 返回 _config 引用,直接设/清,不走 secret 保留逻辑
    try:
        assert client.get("/api/config").status_code == 401          # 无令牌 → 挡住
        assert client.get("/api/styles").status_code == 401
        assert client.get("/api/config", headers={"X-Access-Token": "T0KEN"}).status_code == 200   # header 令牌
        assert client.get("/api/config?token=T0KEN").status_code == 200                            # query 令牌
        assert client.get("/kindle/frame.png").status_code == 200    # Kindle 拉图豁免
        assert client.get("/health").status_code == 200              # health 豁免
        assert client.post("/api/rate-limits",
                           json={"source": "claude", "rate_limits": {}}).status_code == 200  # 设备上报豁免
        assert client.post("/api/music",
                           json={"has_track": False, "state": "stopped"}).status_code == 200
    finally:
        cm.get()["server"]["access_token"] = ""    # 清掉,不影响其他测试(空=放行)


def test_kindle_clear_cmd_flow():
    """一键清屏:设置页下发(需令牌)→ Kindle 轮询读到 1(豁免令牌)且读后复位 → 再读为 0。"""
    from server.app import CLEAR_CMD
    CLEAR_CMD["on"] = False
    cm.get()["server"]["access_token"] = "T0KEN"
    try:
        assert client.get("/kindle/clear-cmd").text == "0"                        # 无指令
        assert client.post("/api/kindle/clear").status_code == 401                # 下发需令牌
        r = client.post("/api/kindle/clear", headers={"X-Access-Token": "T0KEN"})
        assert r.status_code == 200 and r.json().get("ok") is True
        assert client.get("/kindle/clear-cmd").text == "1"                        # Kindle 读到指令(豁免)
        assert client.get("/kindle/clear-cmd").text == "0"                        # 读后复位,只执行一次
    finally:
        CLEAR_CMD["on"] = False
        cm.get()["server"]["access_token"] = ""


def test_music_artwork_cache_and_pause_idle_wall():
    """Music 推送:封面进缓存;paused 的 state_since 不被心跳刷新;暂停超时/停播出封面墙。"""
    from datetime import datetime, timezone
    from server.app import cache, MUSIC_CACHE
    from server.render.build_context import prep_context

    cache.pop("music", None)
    raw = base64.b64encode(b"fake-jpeg-for-cache").decode()
    r = client.post("/api/music", json={
        "has_track": True,
        "state": "playing",
        "sampled_at": 1000,
        "position": 12,
        "duration": 240,
        "name": "Song",
        "artist": "Artist",
        "album": "Album",
        "track_id": "track-1",
        "has_artwork": True,
        "artwork_hash": "hash-1",
        "artwork_mime": "image/jpeg",
        "artwork_data": raw,
    })
    assert r.status_code == 200
    assert r.json()["artwork_wall"] == 1
    assert len(cache["music"]["artwork_wall"]) == 1
    persisted = json.load(open(MUSIC_CACHE, encoding="utf-8"))
    assert "artwork_wall" not in persisted

    client.post("/api/music", json={
        "has_track": True, "state": "paused", "sampled_at": 2000,
        "position": 20, "duration": 240, "name": "Song", "artist": "Artist",
        "album": "Album", "track_id": "track-1", "has_artwork": True,
        "artwork_hash": "hash-1",
    })
    first_paused_since = cache["music"]["state_since"]
    assert first_paused_since == 2000

    client.post("/api/music", json={
        "has_track": True, "state": "paused", "sampled_at": 2400,
        "position": 20, "duration": 240, "name": "Song", "artist": "Artist",
        "album": "Album", "track_id": "track-1", "has_artwork": True,
        "artwork_hash": "hash-1",
    })
    assert cache["music"]["state_since"] == first_paused_since

    ctx = prep_context(datetime.fromtimestamp(2400, timezone.utc), {"music": cache["music"]},
                       {"server": {"language": "zh"}, "music": {"pause_idle_after": 300}})
    assert ctx["music"]["idle_wall"] is True
    assert ctx["music"]["artwork_wall_mode"] == "one"

    client.post("/api/music", json={"has_track": False, "state": "stopped", "sampled_at": 2500})
    assert cache["music"]["has_track"] is False
    assert len(cache["music"]["artwork_wall"]) == 1


def test_music_artwork_wall_layout_tiers():
    """Music 封面墙按视觉档位退档,避免中间数量硬塞导致留白/裁切。"""
    from datetime import datetime, timezone
    from server.render.build_context import prep_context

    now = datetime.fromtimestamp(1000, timezone.utc)
    wall = [{"url": "data:image/png;base64,x", "hash": f"h{i}"} for i in range(25)]
    expected = {
        0: ("empty", 0),
        1: ("one", 1),
        3: ("two", 2),
        5: ("four", 4),
        10: ("nine", 9),
        16: ("sixteen", 16),
        19: ("sixteen", 16),
        20: ("twenty", 20),
        25: ("twenty", 20),
    }
    for count, (mode, size) in expected.items():
        ctx = prep_context(now, {"music": {"artwork_wall": wall[:count]}},
                           {"server": {"language": "zh"}, "music": {"enabled": True}})
        assert ctx["music"]["artwork_wall_mode"] == mode
        assert len(ctx["music"]["artwork_wall"]) == size


def test_music_artwork_http_endpoint():
    """动态版封面墙用的 /music/artwork/<hash>:命中返回图 + 长缓存头;未知 hash → 404;令牌保护。"""
    from server.app import cache
    cache.pop("music", None)
    raw = base64.b64encode(b"fake-jpeg-bytes-xyz").decode()
    client.post("/api/music", json={
        "has_track": True, "state": "playing", "sampled_at": 1000,
        "position": 1, "duration": 200, "name": "Song", "artist": "Artist", "album": "Album",
        "track_id": "track-art", "has_artwork": True,
        "artwork_hash": "hash-http", "artwork_mime": "image/jpeg", "artwork_data": raw,
    })
    r = client.get("/music/artwork/hash-http")
    assert r.status_code == 200
    assert r.content == b"fake-jpeg-bytes-xyz"
    assert r.headers.get("content-type") == "image/jpeg"
    assert "immutable" in (r.headers.get("cache-control") or "")
    assert client.get("/music/artwork/nope-not-a-hash").status_code == 404
    # 路径穿越尝试:hash 精确匹配 + abspath 限定 → 拿不到任意文件
    assert client.get("/music/artwork/..%2F..%2Fetc%2Fpasswd").status_code == 404
    # 令牌保护:设了令牌而不带 → 挡住(封面接口不在豁免名单)
    cm.get()["server"]["access_token"] = "T0KEN"
    try:
        assert client.get("/music/artwork/hash-http").status_code == 401
        assert client.get("/music/artwork/hash-http?token=T0KEN").status_code == 200
    finally:
        cm.get()["server"]["access_token"] = ""


def _seed_artwork(n):
    """往封面索引塞 n 张假封面,返回它们的 hash 集合。"""
    import os
    from server.app import MUSIC_ARTWORK_DIR, _music_write_artwork_index
    os.makedirs(MUSIC_ARTWORK_DIR, exist_ok=True)
    items = []
    for i in range(n):
        fn = f"drift{i}.jpg"
        with open(os.path.join(MUSIC_ARTWORK_DIR, fn), "wb") as f:
            f.write(b"x" * 8)
        items.append({"hash": f"d{i}", "path": fn, "mime": "image/jpeg",
                      "last_seen": 100 + i, "album": "", "artist": ""})
    _music_write_artwork_index(items)
    return {f"d{i}" for i in range(n)}


def test_dyn_wall_show_count_matches_layout_tiers():
    """漂移层的显示数必须和布局档位(1/2/4/9/16/20 向下取)一致 —— 否则 5 张会被当 5 张漂移、实际只显示 4。"""
    from server.app import _wall_show_count
    for n, expect in [(0, 0), (1, 1), (2, 2), (3, 2), (4, 4), (5, 4), (8, 4),
                      (9, 9), (15, 9), (16, 16), (19, 16), (20, 20), (40, 20)]:
        assert _wall_show_count(n) == expect, n


def test_dyn_wall_drift():
    """动态版漂移墙:显示数按档位退档;有备用图→稳定换新图(含 5 张显示 4、第 5 张轮进来);
    正好满档(无备用)→ 格子间对调、不重复显示。"""
    from server.app import _dyn_wall_tiles, _DYN_WALL
    cm.get().setdefault("music", {}).update(
        {"artwork_wall_count": 20, "artwork_pool_size": 40,
         "artwork_swap_interval": 4, "artwork_swap_max": 3})

    # 5 张封面:档位退到显示 4,留 1 张备用 → 每次漂移必把那张备用换进来(用户问的场景)
    pool5 = _seed_artwork(5)
    _DYN_WALL["tiles"] = []
    _DYN_WALL["ts"] = 0.0
    t1 = [x["hash"] for x in _dyn_wall_tiles()]
    assert len(t1) == 4 and len(set(t1)) == 4 and set(t1) <= pool5   # 显示 4(不是 5)
    spare1 = (pool5 - set(t1)).pop()                                 # 当前没显示的那张
    _DYN_WALL["ts"] = 0.0                                            # 强制到点
    t2 = [x["hash"] for x in _dyn_wall_tiles()]
    assert len(t2) == 4 and len(set(t2)) == 4
    assert spare1 in set(t2)                                         # 备用那张被换进了显示区

    # 正好 4 张:显示 4、无备用 → 对调位置,仍是这 4 张、无重复
    pool4 = _seed_artwork(4)
    _DYN_WALL["tiles"] = []
    _DYN_WALL["ts"] = 0.0
    t3 = [x["hash"] for x in _dyn_wall_tiles()]
    assert set(t3) == pool4 and len(set(t3)) == 4
    _DYN_WALL["ts"] = 0.0
    t4 = [x["hash"] for x in _dyn_wall_tiles()]
    assert set(t4) == pool4 and len(set(t4)) == 4                    # 不引入重复、不报错


def test_music_artwork_cache_limit_is_bounded():
    """Music 封面缓存按配置保留最近 N 张,不会无限增长。"""
    from server.app import MUSIC_ARTWORK_DIR, _music_load_artwork_index, cache

    shutil.rmtree(MUSIC_ARTWORK_DIR, ignore_errors=True)
    cache.pop("music", None)
    old_music_cfg = dict(cm.get().get("music", {}) or {})
    cm.get().setdefault("music", {})["artwork_cache_limit"] = 2
    try:
        for i in range(4):
            raw = base64.b64encode(f"fake-art-{i}".encode()).decode()
            r = client.post("/api/music", json={
                "has_track": True,
                "state": "playing",
                "sampled_at": 3000 + i,
                "position": 1,
                "duration": 60,
                "name": f"Song {i}",
                "artist": "Artist",
                "album": f"Album {i}",
                "track_id": f"track-{i}",
                "has_artwork": True,
                "artwork_hash": f"limit-{i}",
                "artwork_mime": "image/jpeg",
                "artwork_data": raw,
            })
            assert r.status_code == 200
        items = _music_load_artwork_index()
        assert [it["hash"] for it in items] == ["limit-3", "limit-2"]
    finally:
        cm.get()["music"] = old_music_cfg


def test_schema_served():
    j = client.get("/api/schema").json()
    assert isinstance(j, list) and any(s["key"] == "weather" for s in j)


def test_get_config_has_status():
    j = client.get("/api/config").json()
    assert "config" in j and "status" in j


def test_save_config_and_redact():
    r = client.post("/api/config", json={"config": {"weather": {"key": "sk-real", "location": "101010100"}}})
    assert r.json()["ok"] is True
    j = client.get("/api/config").json()
    assert j["config"]["weather"]["key"] == "••••••"     # 脱敏不吐真实值
    assert "home" in j["status"]["active_pages"]          # 配置即页面


def test_save_invalid_rejected():
    r = client.post("/api/config", json={"config": {"home_assistant": {"url": "http://x:8123"}}})
    assert r.status_code == 400 and r.json()["ok"] is False
    assert any("令牌" in e for e in r.json()["errors"])


def test_device_push_and_discover():
    r = client.post("/api/device-metrics", json={
        "id": "pc-1", "hostname": "my-pc",
        "metrics": {"cpu_pct": 50, "mem_used": 1, "mem_total": 2,
                    "disks": [{"name": "C:", "pct": 40, "used": 1, "total": 2}]}})
    assert r.json()["status"] == "ok"
    devs = client.get("/api/discovered-devices").json()["devices"]
    pc = next((x for x in devs if x["key"] == "pc-1"), None)
    assert pc is not None and pc["hostname"] == "my-pc"
    assert "cpu" in pc["fields"] and "vol:C:" in pc["fields"]  # 动态字段含分区


def test_pull_device_merge_keeps_stale_prune_cleans():
    """⑤:采集(_merge)只更新本轮成功项、**不删旧的**(单台临时失败保留上一帧、不凭空消失);
    改名/删除的清理交给保存时的 _prune_pull_device_cache。push 发现设备始终保留。"""
    from server.app import cache, _merge, _prune_pull_device_cache
    cache["devices_metrics"] = {
        "old-mac": {"hostname": "old-mac", "cpu_pct": 1},
        "push-1": {"hostname": "push-1", "updated_at": 123, "cpu_pct": 2},
    }
    # 这轮只采到 new-mac(old-mac 临时失败、不在本轮结果)→ _merge 不该删 old-mac
    _merge({"devices_metrics": {"new-mac": {"hostname": "new-mac", "cpu_pct": 3}}})
    assert "old-mac" in cache["devices_metrics"]      # 单台失败保留上一帧,不凭空消失
    assert "new-mac" in cache["devices_metrics"]
    assert "push-1" in cache["devices_metrics"]
    # 保存配置(machines 只剩 renamed)→ _prune 剪掉不在配置里的本机/SSH 旧指标,push 保留
    _prune_pull_device_cache({"devices": {"machines": [{"name": "renamed", "mode": "local"}]}})
    assert "old-mac" not in cache["devices_metrics"]
    assert "new-mac" not in cache["devices_metrics"]
    assert "push-1" in cache["devices_metrics"]


def test_apple_sync_buckets_reminders():
    """提醒事项自采自推:POST read_reminders.js 的格式 → build_context 正确分桶。
    覆盖搬运缺口(installers/macos/reminders),防止接收端回归。"""
    from datetime import datetime
    from zoneinfo import ZoneInfo
    from server.app import cache, APPLE_REMINDERS_CACHE, _load_apple_reminders_cache
    from server.render.build_context import prep_context
    # read_reminders.js 产出的字段:title/completed/list/dueDate/priority
    r = client.post("/api/apple-sync", json={
        "updated_at": "2026-06-07T22:00:00Z",
        "reminders": [
            {"title": "过期事", "completed": False, "list": "工作", "dueDate": "2026-06-05T18:00:00Z", "priority": 5},
            {"title": "今天事", "completed": False, "list": "生活", "dueDate": "2026-06-07T10:00:00Z", "priority": 0},
            {"title": "明天事", "completed": False, "list": "待办", "dueDate": "2026-06-08T09:00:00Z", "priority": 1},
            {"title": "已完成", "completed": True,  "list": "待办", "dueDate": None, "priority": 0},
        ],
    })
    assert r.json()["status"] == "ok"
    assert cache.get("reminders")  # 接收端已存
    persisted = json.load(open(APPLE_REMINDERS_CACHE, encoding="utf-8"))
    assert persisted["reminders"][0]["title"] == "过期事"
    cache.pop("reminders", None)
    cache.pop("apple_updated", None)
    _load_apple_reminders_cache()
    assert cache.get("reminders") and cache.get("apple_updated") == "2026-06-07T22:00:00Z"
    now = datetime(2026, 6, 7, 22, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
    rem = prep_context(now, dict(cache), {})["home"]["reminders"]
    assert rem["total"] == 3                                   # 已完成被过滤
    assert [x["title"] for x in rem["overdue"]] == ["过期事"]
    assert [x["title"] for x in rem["today"]] == ["今天事"]
    assert any(x["title"] == "明天事" and x["dt"] == "明天" for x in rem["upcoming"])


def test_city_search_requires_saved_key():
    """城市搜索需先配天气 host/key;测试用空配置 → 400 + 明确提示(不打网络)。"""
    r = client.get("/api/city-search?q=上海")
    assert r.status_code == 400
    j = r.json()
    assert j["ok"] is False and "key" in j["error"].lower() or "Key" in j["error"]


def test_schema_location_is_city_with_hidden_name():
    """location 改为 city 类型(城市选择器);location_name 隐藏(由选择器写入)。"""
    schema_json = client.get("/api/schema").json()
    w = next(s for s in schema_json if s["key"] == "weather")
    loc = next(f for f in w["fields"] if f["key"] == "location")
    name = next(f for f in w["fields"] if f["key"] == "location_name")
    assert loc["type"] == "city"
    assert name["hidden"] is True


def test_ha_entities_requires_saved_ha():
    """实体搜索需先配 HA 地址+令牌;空配置 → 400 + 明确提示(不打网络)。"""
    r = client.get("/api/ha-entities?q=客厅")
    assert r.status_code == 400
    j = r.json()
    assert j["ok"] is False and "Home Assistant" in j["error"]


def test_printers_endpoint_requires_saved_ha():
    """打印机扫描需先配 HA 地址+令牌;空配置 → 400 + 明确提示(不打网络)。"""
    r = client.get("/api/printers")
    assert r.status_code == 400
    j = r.json()
    assert j["ok"] is False and "Home Assistant" in j["error"]


def test_interval_resolution():
    """间隔解析(新签名:段+字段+默认):自定义生效、缺失/非法/0 回落默认、最低 5 秒。"""
    from server.app import _interval
    assert _interval({"weather": {"interval": 900}}, "weather", "interval", 600) == 900
    assert _interval({}, "weather", "interval", 600) == 600                          # 默认
    assert _interval({"weather": {"interval": "x"}}, "weather", "interval", 600) == 600   # 非法
    assert _interval({"weather": {"interval": 0}}, "weather", "interval", 600) == 600     # 0=默认
    assert _interval({"server": {"render_interval": 2}}, "server", "render_interval", 30) == 5  # 下限


def test_interval_fields_per_card():
    """间隔分散到各源卡:独立 intervals 段已删;各源段含 interval 字段。"""
    schema_json = client.get("/api/schema").json()
    assert "intervals" not in {s["key"] for s in schema_json}     # 独立段已删
    def has(sec, field):
        s = next(x for x in schema_json if x["key"] == sec)
        return any(f["key"] == field for f in s["fields"])
    assert has("weather", "interval") and has("ai_usage", "interval")
    assert has("home_assistant", "interval") and has("devices", "interval")
    assert has("mstodo", "interval") and has("reminders", "interval")
    assert has("server", "render_interval")
    assert has("ai_usage", "codex_quota_interval") and has("ai_usage", "claude_quota_interval")


def test_agent_files_served():
    """推送 agent 脚本下发:白名单内 200 且是脚本内容,白名单外 404(防任意文件读取)。"""
    r = client.get("/agent/install.sh")
    assert r.status_code == 200 and "kindle-dash-agent" in r.text
    assert client.get("/agent/push_agent.sh").status_code == 200
    assert client.get("/agent/collect_linux.sh").status_code == 200
    assert client.get("/agent/collect_macos.sh").status_code == 200
    assert client.get("/agent/install.ps1").status_code == 200          # Windows
    assert client.get("/agent/push_agent.ps1").status_code == 200
    assert client.get("/agent/collect_windows.ps1").status_code == 200
    assert client.get("/agent/evil.sh").status_code == 404
    assert client.get("/agent/../config.yaml").status_code == 404   # 不许穿越白名单


def test_server_url_replaces_loopback_for_agent_commands():
    """设置页从 127.0.0.1 打开时,远程 agent 命令应使用 LAN 地址。"""
    import server.app as appmod
    old = appmod._lan_ips
    appmod._lan_ips = lambda: ["192.168.1.20", "10.0.0.8"]
    try:
        j = client.get("/api/server-url", headers={"host": "127.0.0.1:8585"}).json()
    finally:
        appmod._lan_ips = old
    assert j["is_loopback"] is True
    assert j["recommended"] == "http://192.168.1.20:8585"
    assert "http://192.168.1.20:8585" in j["candidates"]


def test_lan_priority_demotes_proxy_tun():
    """开着代理(Clash 的 198.18.0.1 TUN)时,真实 LAN 段应排前,虚拟网卡垫底。
    防回归:_lan_ips 的 recommended=ips[0] 不能选中 198.18.x。"""
    import server.app as appmod
    # socket 探测常把代理 TUN 放第一位,排序后必须被挤到最后
    got = sorted(["198.18.0.1", "192.168.1.19", "10.0.0.8", "172.20.1.2"],
                 key=appmod._lan_priority)
    assert got[0] == "192.168.1.19"        # 192.168 段最优先
    assert got[-1] == "198.18.0.1"         # 代理 TUN 垫底
    assert appmod._lan_priority("198.18.0.1") > appmod._lan_priority("10.0.0.8")


def test_config_path_external_with_auto_migration(tmp_path):
    """配置外置:KINDLE_CONFIG 覆盖优先;新位置缺、旧仓库内有 → 自动迁移搬出来。"""
    import server.app as appmod
    # 1) 环境变量覆盖,直接用
    assert appmod._resolve_config_path(env="/custom/c.yaml") == "/custom/c.yaml"
    old = tmp_path / "repo" / "config.yaml"
    old.parent.mkdir()
    old.write_text("server: {port: 9}\n", encoding="utf-8")
    # 2) 新位置不存在 + 旧存在 → 迁移(连同建目录),返回新路径且内容搬过去
    new = tmp_path / "ext" / "kindle-dashboard" / "config.yaml"
    got = appmod._resolve_config_path(env="", new_default=str(new), old_default=str(old))
    assert got == str(new)
    assert new.read_text(encoding="utf-8") == "server: {port: 9}\n"
    # 3) 新位置已存在 → 不迁移、不覆盖
    new2 = tmp_path / "ext2.yaml"
    new2.write_text("keep: me\n", encoding="utf-8")
    assert appmod._resolve_config_path(env="", new_default=str(new2), old_default=str(old)) == str(new2)
    assert new2.read_text(encoding="utf-8") == "keep: me\n"
    # 4) 新旧都没有 → 返回新路径(不报错,服务后续全默认)
    none_old = tmp_path / "nope.yaml"
    new3 = tmp_path / "ext3.yaml"
    assert appmod._resolve_config_path(env="", new_default=str(new3), old_default=str(none_old)) == str(new3)


def test_server_url_includes_local_mdns_candidate():
    """.local mDNS 候选:支持 mDNS 的设备(Mac/Linux/手机)可选它当看板地址,绕开 IP 漂移;
    出现在 candidates 供设置页 agent 命令下拉选,但**不抢 recommended**(默认仍用 IP,兼容不支持 mDNS 的设备)。"""
    import server.app as appmod
    old = appmod._local_hostname_url
    appmod._local_hostname_url = lambda scheme, port: f"{scheme}://mymac.local:{port}"
    try:
        j = client.get("/api/server-url", headers={"host": "127.0.0.1:8585"}).json()
    finally:
        appmod._local_hostname_url = old
    assert "http://mymac.local:8585" in j["candidates"]
    assert j["recommended"] != "http://mymac.local:8585"


def test_styles_endpoint():
    j = client.get("/api/styles").json()
    assert "style_a" in j["styles"] and "home" in j["pages"]


def test_setup_page_served():
    r = client.get("/setup")
    assert r.status_code == 200 and "实时预览" in r.text and "/api/server-url" in r.text


# ---- 安卓 App v1 真机迭代回归(2026-06-18,这些此前只手动/离线验过,补上防回退)----

def test_app_page_canvas_fixed_no_cw_inject():
    """看板画布固定 800×600(与 Kindle 一致):整块画布由前端等比缩放居中铺满(web/app.html 的 fit()),
    服务端**不再**按 cw 注入 body 宽度覆盖——避免任意宽度下的内容溢出适配,任意屏都用同一套验证过的布局。"""
    assert "html,body{width:1333px}" not in client.get("/app/page/news?cw=1333").text   # cw 注入已移除
    assert "html,body{width:9999px}" not in client.get("/app/page/news?cw=9999").text
    assert client.get("/app/page/news?cw=1333").status_code == 200                       # 传了 cw 也照常渲染(忽略)
    assert client.get("/app/page/news").status_code == 200


def test_app_page_phone_battery_override():
    """安卓 App 传本机电量 kbatt/kchg → 页脚显示手机电量;**传不到就不显示**,绝不退回显示
    Kindle 经 /api/kindle-status 上报的电量(/app/page、/app-legacy 永远不是 Kindle 在请求,显 Kindle 电量是误导)。"""
    from server.app import cache, cache_lock
    with cache_lock:
        old = cache.get("kindle_battery")
        cache["kindle_battery"] = 88
    try:
        assert "88%" not in client.get("/app/page/news").text              # 不传 → 不泄漏 Kindle 的 88(隐藏)
        assert "37%" in client.get("/app/page/news?kbatt=37&kchg=1").text   # 传 → 显示手机的 37
        assert "88%" not in client.get("/app/page/news?kbatt=37").text      # 手机电量覆盖,绝不显 Kindle 的
        assert "88%" not in client.get("/app-legacy/page/news").text        # legacy 老安卓/浏览器同理:无 kbatt 不显 Kindle 电量
    finally:
        with cache_lock:
            if old is None:
                cache.pop("kindle_battery", None)
            else:
                cache["kindle_battery"] = old


def test_collect_source_return_signals_for_cold_retry():
    """collect_source:真拿到数据→真值;失败返回 None / 抛异常→None。冷启动快速重试据此判断本源是否已就绪。"""
    from server.app import collect_source

    class OK:
        __name__ = "x.ok"
        @staticmethod
        def collect(cfg):
            return {"news_items": [{"a": 1}]}

    class EmptyNone:
        __name__ = "x.none"
        @staticmethod
        def collect(cfg):
            return None

    class Boom:
        __name__ = "x.boom"
        @staticmethod
        def collect(cfg):
            raise RuntimeError("net down")

    assert collect_source(OK, {})                  # 有数据 → 真值(got=True)
    assert collect_source(EmptyNone, {}) is None   # 拉空 → None(继续快速重试)
    assert collect_source(Boom, {}) is None        # 抛异常被吞 → None(不挂线程)


def test_action_test_page_and_state_require_token():
    """动作测试台与状态接口能改/暴露设备 → 必须经令牌,绝不豁免。"""
    cm.get()["server"]["access_token"] = "T0KEN"
    try:
        assert client.get("/action-test").status_code == 401          # 无令牌 → 挡住
        assert client.get("/api/action-state").status_code == 401
        assert client.get("/action-test?token=T0KEN").status_code == 200
        assert client.get("/api/action-state", headers={"X-Access-Token": "T0KEN"}).status_code == 200
    finally:
        cm.get()["server"]["access_token"] = ""


def test_action_state_empty_config_all_unconfigured():
    """空配置:三类目标都 configured=False、不报错、不联网。"""
    from server.app import _action_state
    st = _action_state({})
    assert st["ha"] == {"configured": False}
    assert st["printer"] == {"configured": False}
    assert st["torrents"] == {"configured": False}


def test_action_state_degrades_without_ha_creds():
    """缺 HA 地址令牌:HA 段不 configured(全量清单靠实时拉取,无凭据无从拉);
    但配了打印机仍标 configured 且报「未配置」错误,不抛、不联网。"""
    from server.app import _action_state
    cfg = {"printer": {"enabled": True, "entity_prefix": "a1"}}
    st = _action_state(cfg)
    assert st["ha"] == {"configured": False}
    assert st["printer"]["configured"] is True and "未配置" in st["printer"]["error"]


def test_resolve_action_whitelist_and_injection_safety():
    """_resolve_action:service 永远来自固定白名单,value 只进 option/value/temperature 等
    类型化字段(防注入);越界/非法 action 一律 ValueError。"""
    from server.actions import _resolve_action
    assert _resolve_action("switch", "toggle", None) == ("homeassistant", "toggle", {})
    assert _resolve_action("light", "on", None) == ("homeassistant", "turn_on", {})
    assert _resolve_action("lock", "on", None) == ("lock", "lock", {})
    assert _resolve_action("lock", "off", None) == ("lock", "unlock", {})
    assert _resolve_action("cover", "stop", None) == ("cover", "stop_cover", {})
    assert _resolve_action("climate", "set_temp", 26) == ("climate", "set_temperature", {"temperature": 26})
    assert _resolve_action("climate", "set_mode", "cool") == ("climate", "set_hvac_mode", {"hvac_mode": "cool"})
    assert _resolve_action("select", "select_option", "sport") == ("select", "select_option", {"option": "sport"})
    assert _resolve_action("number", "set_value", "5") == ("number", "set_value", {"value": 5})
    assert _resolve_action("button", "press", None) == ("button", "press", {})
    assert _resolve_action("alarm_control_panel", "disarm", "1234") == ("alarm_control_panel", "alarm_disarm", {"code": "1234"})
    # 越界 / 张冠李戴 / 注入尝试 一律拒
    bad = [("switch", "rm -rf", None), ("sensor", "on", None), ("climate", "set_mode", "evil"),
           ("number", "press", None), ("button", "on", None), ("select", "press", None),
           ("light", "set_value", 1)]
    for dom, act, val in bad:
        try:
            _resolve_action(dom, act, val)
            assert False, f"{dom}.{act} 应被拒"
        except ValueError:
            pass


def test_num_and_kind_classification():
    """_num 拒非数值;_kind_of 与看板同口径(传感器→不可控)。"""
    from server.actions import _num, _kind_of
    assert _num("26") == 26 and _num(21.5) == 21.5
    try:
        _num("abc"); assert False
    except ValueError:
        pass
    assert _kind_of("switch") == "toggle" and _kind_of("cover") == "cover"
    assert _kind_of("alarm_control_panel") == "alarm" and _kind_of("button") == "button"
    assert _kind_of("sensor") is None and _kind_of("binary_sensor") is None


def test_action_state_endpoint_ok_empty():
    """接口在空配置下返回 ok=True + 三段结构(诚实降级,不 500)。"""
    j = client.get("/api/action-state").json()
    assert j["ok"] is True
    assert {"ha", "printer", "torrents"}.issubset(j.keys())


def test_action_invalid_action_rejected():
    """白名单外的 action 被拒(400),不透传任意 service(防注入)。"""
    cm.get()["server"]["access_token"] = ""
    r = client.post("/api/action/ha", json={"entity_id": "switch.x", "action": "rm -rf"})
    assert r.status_code == 400 and r.json()["ok"] is False
    r = client.post("/api/action/torrent", json={"client": "x", "id": "1", "action": "delete"})
    assert r.status_code == 400 and r.json()["ok"] is False


def test_web_simple_fallback_page_served():
    """古董浏览器降级页:静态 HTML,嵌当前看板整图 + meta refresh 整页定时重载,无 JS。"""
    r = client.get("/web-simple")
    assert r.status_code == 200
    assert 'http-equiv="refresh"' in r.text                 # 整页定时重载
    assert "/kindle/frame.png" in r.text                    # 复用 Kindle 拉图(本就豁免),不另渲染
    assert "<script" not in r.text                          # 古董浏览器:纯静态,无 JS
    # meta refresh 显式带上当前令牌(无令牌时 url 不带 query)
    assert "url=/web-simple" in r.text


def test_web_simple_requires_token_and_preserves_it():
    """降级页外壳能看看板数据 → 必须经令牌,绝不豁免;带令牌时 refresh url 显式回带令牌。"""
    cm.get()["server"]["access_token"] = "T0KEN"
    try:
        assert client.get("/web-simple").status_code == 401             # 无令牌 → 挡住(不在豁免名单)
        r = client.get("/web-simple?token=T0KEN")
        assert r.status_code == 200
        assert "url=/web-simple?token=T0KEN" in r.text                  # refresh 回带同令牌,古董浏览器免重输
        assert client.get("/kindle/frame.png").status_code == 200       # 内嵌整图本就豁免,无需令牌
    finally:
        cm.get()["server"]["access_token"] = ""


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print(f"  ✓ {fn.__name__}")
    print(f"\n{len(fns)} passed")


def test_lyrics_wiring():
    """歌词接线:worker 写缓存并补进 cache['music'];_music_payload 挂歌词;开关关则不查。"""
    from server import app as A

    A.cache["music"] = {"has_track": True, "track_id": "tid-x", "name": "S"}
    fake = [{"t": 1.0, "text": "一句歌词"}]
    orig = A.lyrics.fetch_lyrics
    A.lyrics.fetch_lyrics = lambda *a, **k: fake
    try:
        with A._LYRICS_LOCK:
            A._LYRICS_CACHE.clear()
        A._lyrics_worker("tid-x", "S", "A", 200, "Alb")      # 同步跑,不起线程
    finally:
        A.lyrics.fetch_lyrics = orig
    assert A._LYRICS_CACHE["tid-x"] == fake
    assert A.cache["music"]["lyrics"] == fake                 # 补进当前曲

    # _music_payload 从缓存挂歌词
    payload = A._music_payload({"has_track": True, "state": "playing", "name": "S",
                               "track_id": "tid-x", "duration": 200, "sampled_at": 1})
    assert payload["lyrics"] == fake

    # 开关关闭 → 不查(不进缓存/不在飞行)
    with A._LYRICS_LOCK:
        A._LYRICS_CACHE.clear(); A._LYRICS_INFLIGHT.clear()
    A.cm.get().setdefault("music", {})["lyrics_enabled"] = False
    try:
        A._maybe_fetch_lyrics({"has_track": True, "track_id": "tid-y", "name": "S2"})
        assert "tid-y" not in A._LYRICS_CACHE and "tid-y" not in A._LYRICS_INFLIGHT
    finally:
        A.cm.get()["music"]["lyrics_enabled"] = True
