"""天气采集(和风天气 QWeather)。服务端直采。

城市:用户在设置网页搜城市名 → GeoAPI 反查得 LocationID;采集只认 LocationID。
城市显示名优先用配置里的 location_name(用户选城市时写入);为空则用 GeoAPI 反查并缓存,
兼容"手填编码"路径(没搜过、没存名字)。
"""
import httpx

# (host, location) -> 城市名,避免每轮采集重复反查(LocationID 对应的城市名不会变)
_city_name_cache = {}


def search_city(host: str, key: str, q: str) -> list:
    """按城市名/拼音搜索,返回候选 [{id, name, adm2, adm1}]。供设置网页城市选择器用。
    重名(如"朝阳")会返回多条,带 adm2(市)/adm1(省)供消歧。"""
    host = (host or "").strip()
    key = (key or "").strip()
    q = (q or "").strip()
    if not (host and key and q):
        return []
    url = f"https://{host}/geo/v2/city/lookup"
    with httpx.Client(timeout=10) as c:
        r = c.get(url, params={"location": q, "key": key, "number": 10},
                  headers={"Accept-Encoding": "gzip"}).json()
    if r.get("code") != "200":
        return []
    return [{"id": x.get("id"), "name": x.get("name"),
             "adm2": x.get("adm2"), "adm1": x.get("adm1")}
            for x in (r.get("location") or [])]


def lookup_city_name(host: str, key: str, location: str) -> str:
    """反查 LocationID 对应的城市名,结果缓存。查不到返回空串。"""
    host = (host or "").strip()
    key = (key or "").strip()
    location = (location or "").strip()
    if not (host and key and location):
        return ""
    ck = (host, location)
    if ck in _city_name_cache:
        return _city_name_cache[ck]
    name = ""
    try:
        hits = search_city(host, key, location)   # GeoAPI lookup 也吃 LocationID
        if hits:
            name = hits[0].get("name") or ""
    except Exception as e:
        print(f"[weather] city lookup: {e}")
    if name:
        _city_name_cache[ck] = name
    return name


def collect(cfg: dict):
    w = (cfg or {}).get("weather", {})
    key = (w.get("key") or "").strip()
    host = (w.get("host") or "").strip()
    loc = (w.get("location") or "").strip()
    if not (key and host and loc):
        return None
    base = f"https://{host}/v7"
    params = {"location": loc, "key": key}
    headers = {"Accept-Encoding": "gzip"}
    try:
        import os as _os
        px = _os.environ.get("https_proxy") or _os.environ.get("HTTPS_PROXY") or ""
        with httpx.Client(timeout=10, proxy=px or None) as c:
            nd = c.get(f"{base}/weather/now", params=params, headers=headers).json()
            dd = c.get(f"{base}/weather/3d", params=params, headers=headers).json()
    except Exception as e:
        print(f"[weather] {e}")
        return None
    if nd.get("code") == "200" and dd.get("code") == "200":
        city = (w.get("location_name") or "").strip() or lookup_city_name(host, key, loc)
        return {"weather_now": nd["now"], "weather_daily": dd["daily"], "weather_city": city}
    print(f"[weather] api code now={nd.get('code')} 3d={dd.get('code')}")
    return None
