# -*- coding: utf-8 -*-
"""
八纵八横高速铁路通道规划器（城市级）

与普速的 route_planner.py 的关系：
    - **完全独立**：普速用车站级的 main_railway_line.txt，高铁用城市级的 hsr_network.txt，
      两套数据、两套图，互不影响（因此可以并存，不会互相污染内存中的图）。
    - **响应结构刻意与普速保持一致**（path / stations / segments / total_distance …），
      这样前端可以复用同一套渲染代码，做到"功能与普速页面一致"。

数据来源：
    - hsr_network.txt      八纵八横 16 条通道（2016《中长期铁路网规划》）
    - stations.csv         复用普速已有的城市坐标
    - extra_coords.json    为通道新增城市补全的坐标（在线地理编码结果）

依赖：networkx
"""

import csv
import json
import math
import os

import networkx as nx

_BASE_DIR = os.path.dirname(os.path.abspath(__file__))

NETWORK_FILE = "hsr_network.txt"
STATIONS_FILE = "stations.csv"
EXTRA_COORDS_FILE = "extra_coords.json"

# ---- 图与索引 ----
STATIONS = {}      # 车站 -> {"grade","lon","lat"}
CITY_INFO = {}     # 车站 -> {"grade","lon","lat"}
GRAPH = nx.Graph()
EDGE_LINES = {}    # (车站A, 车站B) -> set(通道名)
CORRIDORS = {}     # 通道名 -> [车站, ...]（含在建段）
PLANNED_CORRIDORS = set()   # 标记为 [在建] 的通道名
COORD_SOURCE = {}  # 车站 -> 坐标来源说明（便于排查）

# 行首标记：带此前缀的通道视为「规划 / 在建」，其边**默认不参与算路**。
# 理由：八纵八横里有若干段尚未通车（如京沪二线的津潍段），
# 若参与算路会给出"查不到车次"的方案。
PLANNED_TAG = "[在建]"


class RoutePlannerError(Exception):
    """路径规划相关错误（与普速模块同名，便于统一捕获）。"""


def haversine(lon1, lat1, lon2, lat2):
    """两经纬度点之间的球面距离（公里）。"""
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def _load_coords():
    """
    汇总坐标：stations.csv（普速已有城市）+ extra_coords.json（通道新增城市）。

    extra_coords.json 里的值优先，因为它是针对缺坐标城市专门补全/修正过的。
    """
    coords = {}
    src = {}
    path = os.path.join(_BASE_DIR, STATIONS_FILE)
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8-sig") as f:
            for row in csv.DictReader(f):
                name = (row.get("站名") or "").strip()
                try:
                    lon, lat = float(row["经度"]), float(row["纬度"])
                except (TypeError, ValueError, KeyError):
                    continue
                if lon == 0.0 and lat == 0.0:
                    continue
                coords.setdefault(name, (lon, lat))
                src.setdefault(name, "stations.csv")

    path = os.path.join(_BASE_DIR, EXTRA_COORDS_FILE)
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                extra = json.load(f)
            for name, v in extra.items():
                if isinstance(v, (list, tuple)) and len(v) == 2:
                    coords[name] = (float(v[0]), float(v[1]))   # 覆盖
                    src[name] = "extra_coords.json"
        except Exception:
            pass
    return coords, src


def parse_network(path=None):
    """
    解析 hsr_network.txt，返回 (corridors, planned)。

    - corridors: {通道名: [车站, ...]}（保持文件顺序，含在建段）
    - planned:   标记为 [在建] 的通道名集合

    格式：每行「通道名：车站1、车站2、…」；`#` 开头与空行忽略。
    通道名前加 `[在建]` 表示该段尚未通车。
    """
    path = path or os.path.join(_BASE_DIR, NETWORK_FILE)
    out, planned = {}, set()
    with open(path, "r", encoding="utf-8") as f:
        for raw in f:
            line = raw.strip()
            if not line or line.startswith("#") or "：" not in line:
                continue
            name, rest = line.split("：", 1)
            name = name.strip()
            is_planned = name.startswith(PLANNED_TAG)
            if is_planned:
                name = name[len(PLANNED_TAG):].strip()
            cities = [c.strip() for c in rest.split("、") if c.strip()]
            if name and cities:
                out[name] = cities
                if is_planned:
                    planned.add(name)
    return out, planned


def load_data(network_file=None, quiet=False, include_planned=False):
    """
    加载通道数据并建图。可重复调用（热重载）。

    参数：
        include_planned: 是否把 [在建] 段也接入算路图。
            默认 **False** —— 在建段没有实际车次，接进来只会给出
            "方案查不到车次"的困惑。注意：无论该值为真与否，
            **车站本身都会保留在 STATIONS 里**（便于地图绘制），
            只有「边」受此开关影响。
    """
    global STATIONS, CITY_INFO, GRAPH, EDGE_LINES, CORRIDORS, COORD_SOURCE
    global PLANNED_CORRIDORS

    STATIONS = {}
    CITY_INFO = {}
    GRAPH = nx.Graph()
    EDGE_LINES = {}
    CORRIDORS, PLANNED_CORRIDORS = parse_network(network_file)
    coords, src = _load_coords()
    COORD_SOURCE = src

    missing = []
    for name, cities in CORRIDORS.items():
        for c in cities:
            if c in STATIONS:
                continue
            hit = coords.get(c)
            if not hit:
                missing.append(c)
                continue
            STATIONS[c] = {"grade": "特等站", "lon": hit[0], "lat": hit[1]}
            CITY_INFO[c] = {"grade": "特等站", "lon": hit[0], "lat": hit[1]}

    # 建边：同一条通道里相邻的车站相连，权重 = 直线距离
    # [在建] 段默认跳过（见 include_planned 说明）
    for name, cities in CORRIDORS.items():
        if name in PLANNED_CORRIDORS and not include_planned:
            continue
        for a, b in zip(cities, cities[1:]):
            if a not in STATIONS or b not in STATIONS:
                continue
            d = haversine(STATIONS[a]["lon"], STATIONS[a]["lat"],
                          STATIONS[b]["lon"], STATIONS[b]["lat"])
            if GRAPH.has_edge(a, b):
                if d < GRAPH[a][b]["weight"]:
                    GRAPH[a][b]["weight"] = d
            else:
                GRAPH.add_edge(a, b, weight=d)
            EDGE_LINES.setdefault((a, b), set()).add(name)
            EDGE_LINES.setdefault((b, a), set()).add(name)

    if missing and not quiet:
        print(f"  [警告] {len(set(missing))} 个通道车站缺坐标，未接入高铁网络："
              + "、".join(sorted(set(missing))))
    return {"city_count": len(STATIONS), "edge_count": GRAPH.number_of_edges(),
            "corridor_count": len(CORRIDORS), "missing": sorted(set(missing)),
            "planned_corridors": sorted(PLANNED_CORRIDORS),
            "include_planned": bool(include_planned)}


# ============================================================
# 名称解析与算路
# ============================================================
def _resolve(name):
    """把用户输入解析成网络里的城市名（支持"××站"、去空格）。"""
    if not name:
        return None
    s = str(name).strip()
    if s in STATIONS:
        return s
    if s.endswith("站") and s[:-1] in STATIONS:
        return s[:-1]
    # 包含匹配（如"北京南" -> "北京"）
    cands = [c for c in STATIONS if c and (c in s or s in c)]
    if len(cands) == 1:
        return cands[0]
    if cands:
        return min(cands, key=len)
    return None


def _segments(path):
    """把城市序列转成 segments（与普速结构一致）。"""
    segs = []
    for a, b in zip(path, path[1:]):
        lines = sorted(EDGE_LINES.get((a, b), []))
        segs.append({"from": a, "to": b, "lines": lines})
    return segs


def _pack(path):
    """把一条城市路径打包成与普速一致的返回结构。"""
    total = 0.0
    for a, b in zip(path, path[1:]):
        total += GRAPH[a][b]["weight"]
    return {
        "path": list(path),
        "stations": list(path),
        "stations_upgraded": list(path),   # 城市级网络无需"小站省会化"
        "transfers": list(path[1:-1]),
        "transfer_stations": list(path[1:-1]),
        "segments": _segments(path),
        "total_distance": round(total),
    }


def _planned_hint(name):
    """
    若某个站在图上是孤立的、且只出现在 [在建] 段里，返回一句提示，否则返回空串。

    这样用户查「东营南」这类只在建段才有的站时，能知道原因，
    而不是看到一句笼统的「不连通」。
    """
    if name not in STATIONS:
        return ""
    if name in GRAPH and GRAPH.degree(name) > 0:
        return ""
    hit = [c for c in PLANNED_CORRIDORS if name in CORRIDORS.get(c, [])]
    if hit:
        return f"（「{name}」目前只出现在在建通道「{'、'.join(hit)}」上，暂无车次）"
    return ""


def _unreachable(s, e):
    return RoutePlannerError(
        f"「{s}」与「{e}」之间在高铁通道网中不连通"
        + _planned_hint(s) + _planned_hint(e)
    )


def find_route(start, end):
    """最短路径（单个方案）。"""
    s, e = _resolve(start), _resolve(end)
    if not s:
        raise RoutePlannerError(f"无法识别起点「{start}」（不在八纵八横通道车站中）")
    if not e:
        raise RoutePlannerError(f"无法识别终点「{end}」（不在八纵八横通道车站中）")
    if s == e:
        raise RoutePlannerError("起点与终点相同")
    try:
        path = nx.shortest_path(GRAPH, s, e, weight="weight")
    except (nx.NetworkXNoPath, nx.NodeNotFound):
        # 只在 [在建] 段出现的站没有任何边，networkx 会抛 NodeNotFound
        # （而不是 NoPath）—— 两种情况都归为"不连通"并给出原因提示。
        raise _unreachable(s, e)
    return _pack(path)


def find_routes(start, end, k=3):
    """
    返回最多 k 条走向不同的方案。

    做法：先求最短路，然后**依次禁用已得方案里的每条边**再求最短路，
    收集去重后的结果（Yen 算法的简化版，对城市级小图足够）。
    """
    s, e = _resolve(start), _resolve(end)
    if not s:
        raise RoutePlannerError(f"无法识别起点「{start}」")
    if not e:
        raise RoutePlannerError(f"无法识别终点「{end}」")
    if s == e:
        raise RoutePlannerError("起点与终点相同")

    found = []
    seen = set()

    def add(path):
        key = tuple(path)
        if key not in seen:
            seen.add(key)
            found.append(path)

    try:
        base = nx.shortest_path(GRAPH, s, e, weight="weight")
    except (nx.NetworkXNoPath, nx.NodeNotFound):
        raise _unreachable(s, e)
    add(base)

    # 依次禁用已发现方案中的边，寻找替代走向
    # 注意：必须**真的把边从图里去掉**再求最短路 —— 只记录在 banned 里
    # 而不改图，shortest_path 每次都会返回同一条路，替代方案永远找不到。
    banned = set()

    def shortest_without_banned():
        g = GRAPH.copy()
        g.remove_edges_from([e for e in banned if g.has_edge(*e)])
        return nx.shortest_path(g, s, e, weight="weight")

    for _ in range(k * 6):
        if len(found) >= k:
            break
        progressed = False
        for path in list(found):
            if len(found) >= k:
                break
            for a, b in zip(path, path[1:]):
                edge = tuple(sorted((a, b)))
                if edge in banned:
                    continue
                banned.add(edge)
                try:
                    alt = shortest_without_banned()
                except (nx.NetworkXNoPath, nx.NodeNotFound):
                    continue
                if tuple(alt) not in seen:
                    add(alt)
                    progressed = True
                    break
        if not progressed:
            break

    out = [_pack(p) for p in found[:k]]
    out.sort(key=lambda r: r["total_distance"])
    return out


def find_route_via(start, end, via_list=None, via_str=None):
    """强制经过若干中间城市的方案（与普速同签名）。"""
    if via_list is None:
        via_list = []
        if via_str:
            via_list = [v for v in str(via_str).replace("，", ",").split(",") if v.strip()]
    s, e = _resolve(start), _resolve(end)
    if not s:
        raise RoutePlannerError(f"无法识别起点「{start}」")
    if not e:
        raise RoutePlannerError(f"无法识别终点「{end}」")

    stops = [s]
    for v in via_list:
        r = _resolve(v)
        if not r:
            raise RoutePlannerError(f"无法识别定制中转站「{v}」")
        stops.append(r)
    stops.append(e)

    full = []
    for a, b in zip(stops, stops[1:]):
        try:
            seg = nx.shortest_path(GRAPH, a, b, weight="weight")
        except (nx.NetworkXNoPath, nx.NodeNotFound):
            raise _unreachable(a, b)
        full.extend(seg if not full else seg[1:])
    return _pack(full)


def list_cities():
    """返回全部通道城市（供前端下拉/提示）。"""
    return sorted(STATIONS.keys())


def list_corridors():
    """返回通道概览：名称 + 车站数 + 首末站 + 是否在建。"""
    out = []
    for name, cities in CORRIDORS.items():
        present = [c for c in cities if c in STATIONS]
        if not present:
            continue
        out.append({"name": name, "city_count": len(present),
                    "from": present[0], "to": present[-1],
                    "cities": present,
                    "planned": name in PLANNED_CORRIDORS})
    return out


# ============================================================
# 数据体检：相邻站间距异常检测
# ============================================================
# 高铁的相邻站间距一般在 20~80km，超过阈值基本可以断定是**坐标错了**
# （在线地理编码对异地同名的小地名极易解析错：实测「马鞍山」被解析到广东、
#  「雄安站」也匹配到广东的同名地点、「汉中」被解析到佛坪县城）。
#
# ⚠ 阈值取 150km 而非更宽：原先用 250km，结果「汉中→宁强南 173.6km」
#   这种明显错误（实际约 100km）被漏掉了。150km 只会让
#   「柳园南—哈密 253km」（兰新高铁真实的沙漠长区间）一处误报，可接受。
# 本函数**只报告、不修改数据** —— 判断权交给使用者。
MAX_ADJACENT_KM = 150.0


def find_suspect_edges(threshold_km=None, limit=50):
    """
    扫描各通道的相邻站，返回间距超过阈值的区段（疑似坐标错误）。

    [在建] 段默认跳过 —— 它们的站序是规划方案，站距偏大属正常。

    返回 list[dict]，按距离降序：{"from","to","distance_km","corridor"}
    """
    thr = MAX_ADJACENT_KM if threshold_km is None else float(threshold_km)
    out = []
    for name, cities in CORRIDORS.items():
        if name in PLANNED_CORRIDORS:
            continue
        for a, b in zip(cities, cities[1:]):
            if a not in STATIONS or b not in STATIONS:
                continue
            d = haversine(STATIONS[a]["lon"], STATIONS[a]["lat"],
                          STATIONS[b]["lon"], STATIONS[b]["lat"])
            if d > thr:
                out.append({"from": a, "to": b,
                            "distance_km": round(d, 1), "corridor": name})
    out.sort(key=lambda x: -x["distance_km"])
    return out[:limit]


def _common_prefix_len(a, b):
    """两个站名的公共前缀长度。"""
    n = 0
    for x, y in zip(a, b):
        if x != y:
            break
        n += 1
    return n


def find_suspect_duplicates(threshold_km=3.0, limit=50, min_prefix=2):
    """
    找出**几乎重合但名字毫无关系**的车站 —— 这是地理编码抓错地点的强信号。

    为什么需要它（距离阈值不够用）：
        实测「汉中」曾被解析到「佛坪」县城，两站相距仅 1.26km，而
        汉中→宁强南 因此变成 173.6km。但这个数值**落在合法长区间范围内**
        （兰新高铁柳园南—哈密就有 253km），所以 find_suspect_edges()
        抓不到它。近重合则是非常干净的信号。

    为什么还要排除"同城多站"：
        重庆北 / 重庆西 相距 3.4km、齐齐哈尔南 / 齐齐哈尔 2.3km 都是
        合法的同城车站。用**站名公共前缀**区分：前缀 ≥2 字
        （重庆…、桂林…）视为同城；毫无共同前缀（汉中 / 佛坪）才报可疑。

    实测：本数据集用 3km + 前缀规则，**零误报**地抓出汉中那一处错误。

    返回 list[dict]，按距离升序：{"a","b","distance_km"}
    """
    names = sorted(STATIONS)
    out = []
    for i, a in enumerate(names):
        for b in names[i + 1:]:
            d = haversine(STATIONS[a]["lon"], STATIONS[a]["lat"],
                          STATIONS[b]["lon"], STATIONS[b]["lat"])
            if d > threshold_km:
                continue
            if _common_prefix_len(a, b) >= min_prefix:
                continue          # 同城多站，正常
            out.append({"a": a, "b": b, "distance_km": round(d, 2)})
    out.sort(key=lambda x: x["distance_km"])
    return out[:limit]


def coverage():
    """返回通道车站的坐标覆盖情况：{total, with_coord, missing:[...]}"""
    total = 0
    missing = []
    for name, cities in CORRIDORS.items():
        for c in cities:
            total += 1
            if c not in STATIONS and c not in missing:
                missing.append(c)
    return {"total": total, "with_coord": total - len(missing), "missing": missing}


load_data(quiet=True)


if __name__ == "__main__":
    info = load_data()
    print(f"八纵八横通道 {info['corridor_count']} 条，车站 {info['city_count']} 个，"
          f"连接 {info['edge_count']} 条")
    if info.get("planned_corridors"):
        print(f"（其中 {len(info['planned_corridors'])} 段标记为 [在建]，默认不参与算路）")
    for r in find_routes("北京", "广州", k=3):
        print(f"  {r['total_distance']:>5} km  " + " → ".join(r["path"]))
