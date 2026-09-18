# -*- coding: utf-8 -*-
"""
携程列车时刻查询模块（真实经停站）
==================================

背景：
    本项目原有的 12306「直达优先」功能只能拿到直达车次的号与时间，
    无法拿到该车次**真实途经哪些站点**（123 06 的站序接口需要内部
    车次编码 train_no，且需破解 secretStr 加密，成本高且极易随官方
    版本升级失效）。

    本模块改为模拟浏览器访问携程火车票在售页面，用**普通车次号**
    即可抓取该车次**完整真实的运行停靠站序列**，从而在地图上把
    直达方案的途径站点真实地标注出来。

数据来源：
    https://trains.ctrip.com/trainSchedule/{车次号}
    （页面为 Next.js SSR，经停站在 __NEXT_DATA__ 内嵌 JSON 的
      props.pageProps.initialState.trainStopList）

返回结构：
    {
      "ok": True,
      "train": "1461",
      "from": "北京",
      "to": "上海",
      "stops": [ {"name": "北京", "lon": 116.41, "lat": 39.90, "seq": 1}, ... ],
      "source": "携程"
    }

说明：
    - 该页面返回的是**全程停靠站序**（不限查询区间），正是我们标注
      "直达方案途经站点"所需。
    - 坐标补全优先级：
        1) 本项目 stations.csv（route_planner.STATIONS）
        2) 内置城市坐标兜底表（upgrade_data.CITY_FALLBACK）
        3) extra_coords.json 缓存
        4) 在线地理编码（高德/百度需 Key，Nominatim 免费，国内可能超时）
        全部缺失时该站 lon/lat 为 None，前端可跳过仅画其余站点。
    - 串行限速 + 600 秒缓存 + 失败降级，绝不抛异常。
"""

import re
import json
import time
import threading
import requests

# 请求间隔（秒）：尊重对方站点访问频率，避免被封
_REQ_INTERVAL = 2.0
_CACHE_TTL = 600          # 结果缓存秒数（车次站序相对稳定，缓存久一点）
_TIMEOUT = 15

_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")

_SESSION = None
_SESSION_LOCK = threading.Lock()
_CACHE = {}
_CACHE_LOCK = threading.Lock()


# ============================================================
# 会话与串行限速
# ============================================================
class _RateLimiter:
    def __init__(self, interval):
        self.interval = interval
        self.lock = threading.Lock()
        self.last = 0.0

    def acquire(self):
        with self.lock:
            wait = self.last + self.interval - time.time()
            if wait > 0:
                time.sleep(wait)
            self.last = time.time()


_rate = _RateLimiter(_REQ_INTERVAL)


def _get_session():
    """获取共享会话（先访问一次首页以初始化 cookie，降低被拦截概率）。"""
    global _SESSION
    with _SESSION_LOCK:
        if _SESSION is None:
            s = requests.Session()
            s.headers.update({
                "User-Agent": _UA,
                "Accept-Language": "zh-CN,zh;q=0.9",
            })
            try:
                s.get("https://trains.ctrip.com/", timeout=_TIMEOUT, verify=False)
            except Exception:
                pass
            _SESSION = s
        return _SESSION


def _cache_get(key):
    with _CACHE_LOCK:
        item = _CACHE.get(key)
        if item and time.time() - item[0] < _CACHE_TTL:
            return item[1]
        if item:
            _CACHE.pop(key, None)
        return None


def _cache_put(key, value):
    with _CACHE_LOCK:
        now = time.time()
        for k in list(_CACHE.keys()):
            if now - _CACHE[k][0] > _CACHE_TTL:
                _CACHE.pop(k, None)
        _CACHE[key] = (now, value)


# ============================================================
# 坐标补全来源
# ============================================================
def _load_station_coords():
    """站点库坐标：{站名: [lon, lat]}（来自 route_planner.STATIONS）。"""
    coords = {}
    try:
        import route_planner
        for name, info in route_planner.STATIONS.items():
            lon, lat = info.get("lon"), info.get("lat")
            if lon is not None and lat is not None:
                coords[name] = [float(lon), float(lat)]
    except Exception:
        pass
    return coords


def _load_city_fallback():
    """内置城市坐标兜底表：{站名: [lon, lat]}。"""
    try:
        import upgrade_data
        fb = upgrade_data.CITY_FALLBACK
        return {k: list(v) for k, v in fb.items()}
    except Exception:
        return {}


def _load_extra():
    """extra_coords.json 缓存坐标：{站名: [lon, lat]}。"""
    try:
        with open("extra_coords.json", "r", encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def _load_station_grades():
    """车站等级表：{站名: 等级}（来自 route_planner.STATIONS 的 grade 字段）。

    注意：stations.csv 只收录干线上的站点（特等~三等），故部分经停小站
    （如县级站、支线站）查不到等级，返回 None。
    """
    grades = {}
    try:
        import route_planner
        for name, info in route_planner.STATIONS.items():
            g = info.get("grade")
            if g:
                grades[name] = g
    except Exception:
        pass
    return grades


_COORD_SOURCES_CACHE = None

# ============================================================
# 已知会"撞名/误解析"的铁路站纠偏表
# ============================================================
# 问题：百度/高德等地理编码对县级同名字极易解析到异地同名（如"新化"
# 被解析到台湾新化区、"沙城"被解析到浙江），且 city 参数也约束不了。
# 这些站的真实铁路坐标是确定的，故手工维护纠偏表，`_resolve_coord`
# 在查本地来源之前**最高优先**命中此表。
RAIL_AMBIGUOUS = {
    "沙城": [115.6152, 40.4052],   # 京包线 · 河北怀来（勿解析到浙江同名）
    "新化": [111.307, 27.728],     # 沪昆线 · 湖南新化（勿解析到台湾新化区）
}


def _haversine_km(lon1, lat1, lon2, lat2):
    """两地理坐标间的球面距离（公里）。"""
    import math
    R = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * R * math.asin(math.sqrt(a))


def _build_coord_sources():
    global _COORD_SOURCES_CACHE
    if _COORD_SOURCES_CACHE is None:
        merged = {}
        merged.update(_load_extra())
        merged.update(_load_city_fallback())
        merged.update(_load_station_coords())
        merged.update(RAIL_AMBIGUOUS)  # 纠偏表最高优先
        _COORD_SOURCES_CACHE = merged
    return _COORD_SOURCES_CACHE


def _resolve_coord(name):
    """
    为站名解析坐标：
      1) 先查 已知撞名纠偏表 RAIL_AMBIGUOUS
      2) 再查本地来源（stations.csv + 内置兜底 + extra_coords.json 缓存）
      3) 缺失时尝试百度在线地理编码，并把成功结果写回 extra_coords.json，
         永久复用，避免每次查询都重复请求。
    返回 [lon, lat] 或 None。
    """
    sources = _build_coord_sources()
    hit = sources.get(name)
    if hit and isinstance(hit, (list, tuple)) and len(hit) == 2:
        lon, lat = float(hit[0]), float(hit[1])
        if abs(lon) > 0.5 and abs(lat) > 0:
            return [lon, lat]
    # 在线地理编码（百度优先），成功后写入 extra_coords.json
    try:
        import fetch_coords
        r = fetch_coords.geocode_online(name)
        if r:
            coord = [r[0], r[1]]
            _save_extra(name, coord)
            with _CACHE_LOCK:
                sources[name] = coord
            return coord
    except Exception:
        pass
    return None


def _save_extra(name, coord):
    """把补全坐标写入 extra_coords.json（与 fetch_coords 共享缓存）。"""
    try:
        try:
            with open("extra_coords.json", "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            data = {}
        data[name] = coord
        with open("extra_coords.json", "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


# ============================================================
# 携程页面抓取与解析
# ============================================================
def _fetch_raw(train_no):
    """请求携程车次页面，返回经停站原始列表。"""
    s = _get_session()
    url = f"https://trains.ctrip.com/trainSchedule/{train_no.strip()}"
    with _rate_lock():
        resp = s.get(url, timeout=_TIMEOUT, verify=False,
                     headers={"Referer": "https://trains.ctrip.com/"})
    resp.raise_for_status()
    text = resp.text
    if "__NEXT_DATA__" not in text:
        raise RuntimeError("页面未包含 __NEXT_DATA__（可能被拦截或车次不存在）")
    m = re.search(
        r'<script id="__NEXT_DATA__" type="application/json"[^>]*>(.*?)</script>',
        text, re.S,
    )
    if not m:
        raise RuntimeError("未找到 __NEXT_DATA__ 数据块")
    data = json.loads(m.group(1))
    return data["props"]["pageProps"]["initialState"]["trainStopList"]


def _rate_lock():
    class _Ctx:
        def __enter__(self):
            _rate.acquire()

        def __exit__(self, *a):
            return False
    return _Ctx()


def _sanity_filter_coords(stops):
    """
    对经停站坐标做"相邻距离合理性"校验，剔除明显误解析的异常点。

    背景：在线地理编码（百度等）对县级同名字极易解析到异地同名（如
    "新化"→台湾、~1000km；"沙城"→浙江），会在地图上把整条线路拉飞。
    这类错误点与前后相邻站的直线距离都异常偏大（同时远距两侧邻居），
    据此可通用拦截，不必逐个人工维护。

    规则：对同时拥有前后邻站的中部站，若它到"前序站">_MAX_STEP_KM 且到
    "后序站">_MAX_STEP_KM，判定为异常点，将其坐标置 None（前端跳过该点）。

    返回处理后的 stops（原列表就地修改并经内存缓存抹除）。
    """
    _MAX_STEP_KM = 600.0
    n = len(stops)
    for i in range(1, n - 1):
        a, b, c = stops[i - 1], stops[i], stops[i + 1]
        if b.get("lon") is None or b.get("lat") is None:
            continue
        if a.get("lon") is None or c.get("lon") is None:
            continue
        d_prev = _haversine_km(a["lon"], a["lat"], b["lon"], b["lat"])
        d_next = _haversine_km(b["lon"], b["lat"], c["lon"], c["lat"])
        if d_prev > _MAX_STEP_KM and d_next > _MAX_STEP_KM:
            # 该站同时远离前后邻站（疑似异地同名误解析）→ 丢弃坐标
            b["lon"] = None
            b["lat"] = None
            b["_outlier"] = True
    return stops


def get_train_stops(train_no):
    """
    查询指定车次的真实经停站（含坐标）。

    参数:
        train_no: 车次号（如 "1461"、"Z281"）

    返回:
        {
          "ok": bool, "reason": str, "train": str,
          "from": str, "to": str,
          "stops": [{"name","seq","lon","lat","arrive","depart"}, ...],
          "source": "携程"
        }
    """
    train_no = (train_no or "").strip().upper()
    if not train_no:
        return {"ok": False, "reason": "车次号为空", "train": train_no,
                "from": "", "to": "", "stops": [], "source": "携程"}

    cached = _cache_get(train_no)
    if cached is not None:
        return cached

    result = None
    try:
        raw = _fetch_raw(train_no)
        grade_map = _load_station_grades()
        stops = []
        seen = set()
        for x in raw:
            name = (x.get("stationName") or "").strip()
            if not name:
                continue
            if name in seen:
                continue
            seen.add(name)
            seq = x.get("stationSequence")
            coord = _resolve_coord(name)
            stops.append({
                "name": name,
                "seq": int(seq) if isinstance(seq, int) else len(stops) + 1,
                "lon": coord[0] if coord else None,
                "lat": coord[1] if coord else None,
                "arrive": x.get("arrivalTime"),
                "depart": x.get("departureTime"),
                "grade": grade_map.get(name),  # 无等级(查不到)则为 None
            })
        if not stops:
            raise RuntimeError("未解析到任何经停站")
        # 相邻距离合理性校验：拦截在线地理编码的异地同名误解析
        _sanity_filter_coords(stops)
        result = {
            "ok": True,
            "reason": "",
            "train": train_no,
            "from": stops[0]["name"],
            "to": stops[-1]["name"],
            "stops": stops,
            "source": "携程",
        }
    except Exception as e:
        result = {
            "ok": False,
            "reason": f"携程时刻查询失败：{e}",
            "train": train_no,
            "from": "",
            "to": "",
            "stops": [],
            "source": "携程",
        }

    _cache_put(train_no, result)
    return result


if __name__ == "__main__":
    import sys
    for no in (sys.argv[1:] or ["1461", "Z281", "T109"]):
        r = get_train_stops(no)
        print(f"===== {no} ok={r['ok']} {r['from']} -> {r['to']} 共{len(r['stops'])}站 =====")
        if not r["ok"]:
            print("  ", r["reason"])
            continue
        for st in r["stops"]:
            c = (f"({st['lon']:.3f},{st['lat']:.3f})" if st["lon"] else "(无坐标)")
            print(f"  {st['seq']:>2} {st['name']:<6} {c} 到:{st['arrive']} 开:{st['depart']}")
