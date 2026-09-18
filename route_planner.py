# -*- coding: utf-8 -*-
"""
普速铁路中转方案网站 - 路径规划模块（同城多站合并版）

依赖：
    - stations.csv / lines.csv（均由 upgrade_data.py 从 main_railway_line.txt 生成）
    - networkx（需提前安装：pip install networkx）

对外接口：
    load_data(stations_file="stations.csv", lines_file="lines.csv")
        重新加载数据并构建"城市级"图（模块导入时会自动调用一次）

    find_route(start, end)
        计算从 start 到 end 的最短普速铁路中转方案。

设计说明：
    - 同一城市可能拥有多个站点（如北京有北京、北京西等）。
      本模块通过 SAME_CITY_MAP 将同一城市的多个站点视作一个"城市节点"
      接入铁路网络，规划的是"城市到城市"的中转方案。
    - start / end 既可以是具体站名，也可以是城市名（如"北京"、
      "北京西"、"上海虹桥" 均会被解析为对应的城市）。
    - 若 start/end 不是特等 / 一等站，自动就近连接到最近的可接入大站。
    - 路径中间节点仅限特等 / 一等 / 二等站。
    - 使用 Dijkstra 计算最短路径。

示例：
    find_route("北京", "上海")
    find_route("北京西", "虹桥")       # 站点名自动归并到城市
    find_route("凤凰", "广州")        # 三等站自动接入最近大站
"""

import json
import math

import networkx as nx


# 大站（可作为连接端点 / 主要枢纽）的等级集合
MAJOR_GRADES = {"特等站", "一等站"}
# 可作为路径中间节点的等级集合
MID_GRADES = {"特等站", "一等站", "二等站"}


# ============================================================
# 同城多站映射
# 将同一城市的多个站点归并到同一个城市节点。
# 键：具体站点名；值：该站所属城市（城市作为图中的一个节点）。
# ============================================================

SAME_CITY_MAP = {
    "北京": "北京", "北京西": "北京",
    "北京南": "北京", "北京北": "北京", "北京东": "北京",
    "上海": "上海", "上海虹桥": "上海", "上海南": "上海",
    "广州": "广州", "广州南": "广州", "广州东": "广州",
    "深圳": "深圳", "深圳北": "深圳", "深圳东": "深圳",
    "南京": "南京", "南京南": "南京",
    "杭州": "杭州", "杭州东": "杭州", "杭州南": "杭州",
    "成都": "成都", "成都东": "成都", "成都南": "成都",
    "重庆": "重庆", "重庆北": "重庆", "重庆西": "重庆",
    "武汉": "武汉", "汉口": "武汉", "武昌": "武汉",
    "郑州": "郑州", "郑州东": "郑州", "郑州西": "郑州",
    "西安": "西安", "西安北": "西安",
    "长沙": "长沙", "长沙南": "长沙",
    "哈尔滨": "哈尔滨", "哈尔滨西": "哈尔滨",
    "沈阳": "沈阳", "沈阳北": "沈阳", "沈阳南": "沈阳",
    "昆明": "昆明", "昆明南": "昆明",
    "遵义": "遵义", "遵义站": "遵义",
    "九江": "九江", "九江站": "九江",
    "攀枝花": "攀枝花", "攀枝花站": "攀枝花",
    "绍兴": "绍兴", "绍兴北": "绍兴",
    # ---- 补充：同城但此前漏归并的站点 ----
    # 漏归并的后果不是"少合并一个站"这么轻：同一城市的两站若不合并，
    # 分处两端的铁路线就**接不上**，整条走廊会断裂，算路被迫大绕行。
    # 典型例子：宁铜铁路止于「铜陵西」、铜九铁路起于「铜陵」，
    # 不合并则走廊断裂 —— 南京→九江 会被迫绕行 六安·信阳·武昌，
    # 算成 813km（实际约 380km）。
    # 另有若干条同理（合肥西 / 镇江东 / 石家庄北 / 邯郸南 / 济南南 /
    # 西安东 / 长春南 / 厦门高崎 / 北京丰台 / 龙川北）。
    "北京丰台": "北京",
    "石家庄北": "石家庄",
    "邯郸南": "邯郸",
    "济南南": "济南",
    "西安东": "西安",
    "合肥西": "合肥",
    "镇江东": "镇江",
    "铜陵西": "铜陵",
    "长春南": "长春",
    "厦门高崎": "厦门",
    "龙川北": "龙川",
}


# ============================================================
# 车站"省会化"展示映射
# 由于无法获取每个小站是否有实际列车停靠，前端展示时可将
# 一些"二等小站"替换为其所在省省会 / 枢纽大站，使方案走向
# 看起来更贴近"实际可乘车"的干线（仅影响展示，不影响算路）。
# 键：原站名；值：替换后的省会/枢纽站名（须存在于网络中）。
# 可按需要随时增删，按权重大小：命中的小站都会在展示时被替换。
# ============================================================

DISPLAY_UPGRADE_MAP = {
    # 用户点名：沪昆线 滇黔 / 赣闽 段小站 -> 省会
    "向塘": "南昌", "邵武": "福州", "来舟": "福州",
    "安顺": "贵阳", "都匀": "贵阳", "贵港": "南宁", "金城江": "南宁",
    # 黑龙江 -> 哈尔滨
    "一面坡": "哈尔滨", "勃利": "哈尔滨", "卧里屯": "哈尔滨",
    "安达": "哈尔滨", "肇东": "哈尔滨", "玉泉": "哈尔滨",
    "新香坊": "哈尔滨", "阿城": "哈尔滨", "嫩江": "哈尔滨",
    "富裕": "哈尔滨", "讷河": "哈尔滨", "漠河": "哈尔滨", "塔河": "哈尔滨",
    # 吉林 -> 长春
    "图们": "长春", "双辽": "长春", "太平川": "长春", "大安北": "长春",
    # 辽宁 -> 沈阳
    "大虎山": "沈阳", "铁岭": "沈阳", "葫芦岛": "沈阳",
    # 内蒙古 -> 呼和浩特 / 包头
    "伊图里河": "呼和浩特", "扎兰屯": "呼和浩特", "杜尔伯特": "呼和浩特",
    "牙克石": "呼和浩特", "扎赉诺尔西": "呼和浩特",
    # 河北 -> 石家庄
    "井陉": "石家庄", "任丘": "石家庄", "泊头": "石家庄",
    "唐山北": "石家庄", "北戴河": "石家庄", "昌黎": "石家庄",
    "沙河市": "石家庄", "马头": "石家庄", "邯郸南": "石家庄",
    # 天津 -> 天津西
    "蓟州": "天津西", "静海": "天津西", "广阳": "天津西",
    # 北京 -> 北京
    "南口": "北京", "星火": "北京", "双桥": "北京", "黄村": "北京",
    # 山东 -> 济南
    "泰山": "济南", "邹城": "济南", "滕州": "济南", "枣庄西": "济南",
    # 江苏 -> 南京
    "丹阳": "南京", "镇江东": "南京", "前亭": "南京", "连云港东": "南京",
    # 浙江 -> 杭州东
    "嘉兴": "杭州东", "衢州": "杭州东",
    # 安徽 -> 合肥
    "六安": "合肥", "合肥西": "合肥", "舒城": "合肥", "桐城": "合肥",
    "怀宁": "合肥", "宿松": "合肥", "太湖": "合肥", "黄梅": "九江",
    # 河南 / 山西
    "关林": "郑州", "焦作": "郑州", "长治": "太原", "晋城": "太原",
    "寿阳": "太原", "阳泉": "太原",
    # 陕西 -> 西安
    "咸阳": "西安", "西安东": "西安",
    # 湖北 -> 武汉
    "丹江": "汉口", "十堰": "武汉",
    # 四川 -> 成都
    "自贡北": "成都", "宜宾南": "成都", "燕岗": "成都", "西昌": "成都",
    "绵阳": "成都",
    # 云南 -> 昆明
    "威舍": "昆明",
    # 福建 -> 福州 / 厦门
    "来舟": "福州", "邵武": "福州", "漳平": "福州", "厦门高崎": "厦门",
    # 江西 -> 南昌
    "吉安": "南昌",
    # 广东 -> 广州
    "东莞东": "广州", "常平": "广州", "惠州": "广州", "龙川": "广州",
    "龙川北": "广州",
    # 青海 -> 西宁
    "格尔木": "西宁",
}


def haversine(lon1, lat1, lon2, lat2):
    """计算两个经纬度坐标之间的球面距离（公里）"""
    R = 6371.0
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + \
        math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
    return R * c


# ============================================================
# 全局数据（模块导入时加载）
# ============================================================

# 站点原始信息：{站名: {"grade": 等级, "lon": 经度, "lat": 纬度}}
STATIONS = {}
# 站名 -> 城市名
STATION_TO_CITY = {}
# 城市名 -> 该城市下最"核心"的一个站点名（用于展示 / 就近连接）
CITY_STATIONS = {}
# 城市节点属性：{城市名: {"grade": 城市最高等级, "lon": 中心经度, "lat": 中心纬度}}
CITY_INFO = {}
# 全量"城市级"无向加权图（节点为城市，权重为里程）
GRAPH = nx.Graph()
# 城市坐标：{城市名: (经度, 纬度)}，供就近连接计算使用
_CITY_COORDS = {}
# 城市级边 -> 实际站点对：{(城市A, 城市B): [(站点a, 站点b), ...]}
# 用于把"城市级路径"还原为"具体站点路径"，从而在呈现时展示真实采用的车站。
EDGE_STATION_PAIRS = {}
# 城市级边 -> 所属铁路线名：{(城市A, 城市B): set("线名", ...)}
# 用于前端按线路分段着色展示。
EDGE_LINES = {}
# 因坐标无效（缺省 0,0 / 越界）而**未能接入路网**的站点名集合。
# 这些站点不会出现在 STATIONS / GRAPH 中，因此既不参与算路，也不会
# 产生"上万公里的假边"。由 main.py 读取后向前端提示，引导补录坐标。
INVALID_COORD_STATIONS = set()


def is_valid_coord(lon, lat):
    """
    判断一组经纬度是否可用。

    排除三种情况：
        - 无法转成数字
        - 超出经纬度合法范围
        - (0,0) 或近似 (0,0) —— 这是数据生成阶段"缺坐标"的占位值，
          它在几内亚湾，若直接参与 haversine 计算会得到上万公里的假边
          （实测曾让「中华门 → 北京」算出 12593 公里）。

    注意：本函数是"坐标有效性"的**唯一判据**。不要改用"相邻站距离阈值"
    来判断异常 —— 兰新线（乌鲁木齐—西宁 1441km）、青藏线
    （拉萨—格尔木 828km）本身就是合法的长区间，用阈值会误伤它们。
    """
    try:
        lon = float(lon)
        lat = float(lat)
    except (TypeError, ValueError):
        return False
    if not (-180.0 <= lon <= 180.0) or not (-90.0 <= lat <= 90.0):
        return False
    if lon == 0.0 and lat == 0.0:
        return False
    if abs(lon) < 0.5 and abs(lat) < 0.5:
        return False
    return True


# ============================================================
# 同城异站自动归并（Same-City Auto Merge）
# ============================================================
# 背景：同一城市常有多个车站（芜湖 / 芜湖南、铜陵 / 铜陵西）。
# 若它们分处两条铁路的端点而没有被归并，**两条线就接不上**，
# 整条走廊断裂、算路被迫大绕行。手工维护 SAME_CITY_MAP 极易漏
# （实测曾漏 11 对），所以这里再做一层**自动归并**兜底。
#
# 规则（两条同时满足，缺一不可）：
#   1) 站名有前缀关系：A 是 B 的前缀（如 芜湖 / 芜湖南、铜陵 / 铜陵西）
#   2) 两者直线距离 <= AUTO_SAME_CITY_KM
#
# ⚠ 为什么必须带前缀条件：**只靠距离会误判**。实测本数据集里
#   西安—咸阳 21.1km、株洲—湘潭 21.6km、太原—榆次 23.6km、
#   丹阳—镇江 24.0km 都挨得很近，但分属**不同城市**；只按距离
#   自动合并会把它们错并成一个节点，反而把路网搞坏。
AUTO_SAME_CITY = True            # 是否启用自动归并
AUTO_SAME_CITY_KM = 25.0         # 距离阈值（公里）
AUTO_SAME_CITY_REQUIRE_PREFIX = True   # 是否要求站名有前缀关系
SAME_CITY_FILE = "same_city.json"      # 外部手工配置（可选）

# 运行时结果（load_data 时填充，供接口 / 前端展示）
AUTO_MERGED = {}            # {站名: 归并到的城市名} 本次自动归并的结果
SAME_CITY_CANDIDATES = []   # 距离近但未归并的站对（供人工确认，不自动合并）


def load_same_city_config():
    """
    读取可选的 same_city.json，返回 (manual_extra, auto_exclude, overrides)。

    文件格式（全部可选）：
        {
          "manual":       { "广安门": "北京", ... },   # 补充手工归并（覆盖内置表）
          "auto_exclude": [ "太原", "榆次" ],          # 这些站不参与自动归并
          "auto":         { "enabled": true, "max_km": 25, "require_prefix": true }
        }

    JSON 不支持注释，因此**以下划线开头的键一律忽略**（如 "_说明"、"_注释_北京"），
    可以借此在文件里写备注。

    文件不存在或格式有误时一律静默返回空配置，不影响主流程。
    """
    manual_extra, auto_exclude, overrides = {}, set(), {}
    try:
        with open(SAME_CITY_FILE, "r", encoding="utf-8") as f:
            cfg = json.load(f)
        if isinstance(cfg, dict):
            m = cfg.get("manual")
            if isinstance(m, dict):
                for k, v in m.items():
                    if not isinstance(k, str) or not isinstance(v, str):
                        continue
                    k, v = k.strip(), v.strip()
                    if not k or not v or k.startswith("_"):
                        continue          # 下划线开头 = 备注，不是站点
                    manual_extra[k] = v
            ex = cfg.get("auto_exclude")
            if isinstance(ex, list):
                auto_exclude = {str(x).strip() for x in ex if str(x).strip()}
            ao = cfg.get("auto")
            if isinstance(ao, dict):
                overrides = ao
    except FileNotFoundError:
        pass
    except Exception as e:
        print(f"  [警告] 读取 {SAME_CITY_FILE} 失败，已忽略：{e}")
    return manual_extra, auto_exclude, overrides


def detect_auto_same_city(stations, max_km=None, exclude=None, require_prefix=None):
    """
    自动识别同城异站，返回 {站名: 归并到的城市名}。

    参数：
        stations: {站名: {"lon","lat",...}}
        max_km / require_prefix: 覆盖模块级默认配置
        exclude:  不参与自动归并的站名集合

    规则见模块顶部注释。归并方向：**短名为主站**（芜湖 / 芜湖南 → 芜湖）。
    """
    if not AUTO_SAME_CITY and max_km is None:
        return {}
    max_km = AUTO_SAME_CITY_KM if max_km is None else float(max_km)
    require_prefix = AUTO_SAME_CITY_REQUIRE_PREFIX if require_prefix is None else bool(require_prefix)
    exclude = exclude or set()

    names = [n for n in stations if n not in exclude and len(n) >= 2]
    # 短名优先作为主站
    names.sort(key=lambda s: (len(s), s))

    merged = {}
    for short in names:
        if short in merged:
            continue
        lon1, lat1 = stations[short]["lon"], stations[short]["lat"]
        for other in names:
            if other == short or len(other) <= len(short):
                continue
            if require_prefix and not other.startswith(short):
                continue
            lon2, lat2 = stations[other]["lon"], stations[other]["lat"]
            if haversine(lon1, lat1, lon2, lat2) <= max_km:
                merged[other] = short
    return merged


def find_same_city_candidates(stations, merged_map, max_km=None, limit=40):
    """
    找出"距离很近、但未被归并"的站对，供人工确认。

    **只做提示、不自动合并** —— 因为这些站对里既有真同城
    （如 广安门—北京西 2.8km），也有不同城市
    （如 西安—咸阳 21.1km），机器无法可靠区分，交由使用者判断。

    返回 list[dict]，按距离升序：{"a","b","distance_km"}
    """
    max_km = AUTO_SAME_CITY_KM if max_km is None else float(max_km)
    names = sorted(stations)
    out = []
    for i, a in enumerate(names):
        ca = merged_map.get(a, a)
        for b in names[i + 1:]:
            cb = merged_map.get(b, b)
            if ca == cb:
                continue          # 已同城，跳过
            d = haversine(stations[a]["lon"], stations[a]["lat"],
                          stations[b]["lon"], stations[b]["lat"])
            if d <= max_km:
                out.append({"a": a, "b": b, "distance_km": round(d, 1)})
    out.sort(key=lambda x: x["distance_km"])
    return out[:limit]


def build_city_map(stations):
    """
    计算最终「站点 -> 城市」映射：内置手工表 + 外部配置 + 自动归并。

    优先级：外部配置 manual > 内置 SAME_CITY_MAP > 自动归并 > 站名本身。
    同时把结果写入 AUTO_MERGED / SAME_CITY_CANDIDATES 供展示。
    """
    global AUTO_MERGED, SAME_CITY_CANDIDATES

    manual_extra, auto_exclude, overrides = load_same_city_config()
    manual = dict(SAME_CITY_MAP)
    manual.update(manual_extra)

    auto_on = overrides.get("enabled", AUTO_SAME_CITY)
    auto_km = overrides.get("max_km", AUTO_SAME_CITY_KM)
    auto_prefix = overrides.get("require_prefix", AUTO_SAME_CITY_REQUIRE_PREFIX)

    merged = {}
    if auto_on:
        merged = detect_auto_same_city(
            stations, max_km=auto_km, exclude=auto_exclude,
            require_prefix=auto_prefix,
        )
        # 手工表优先级更高：手工已指定的站不再被自动归并覆盖
        for name in manual:
            merged.pop(name, None)

    AUTO_MERGED = dict(merged)
    city_map = {}
    for name in stations:
        city_map[name] = manual.get(name) or merged.get(name) or name

    SAME_CITY_CANDIDATES = find_same_city_candidates(stations, city_map, max_km=auto_km)
    return city_map


def _city_of(station):
    """返回站点所属城市；若不在同城映射中，默认就是它自身。"""
    return STATION_TO_CITY.get(station, station)


def load_data(stations_file="stations.csv", lines_file="lines.csv"):
    """
    读取 CSV 并构建"城市级"无向加权图。

    同一城市的多个站点（见 SAME_CITY_MAP）会被归并到同一个城市节点，
    视作一个站点接入铁路网络。
    """
    global STATIONS, STATION_TO_CITY, CITY_STATIONS, CITY_INFO, GRAPH
    global EDGE_STATION_PAIRS, EDGE_LINES, INVALID_COORD_STATIONS

    STATIONS = {}
    STATION_TO_CITY = {}
    CITY_STATIONS = {}
    CITY_INFO = {}
    GRAPH = nx.Graph()
    EDGE_STATION_PAIRS = {}
    EDGE_LINES = {}
    INVALID_COORD_STATIONS = set()

    # ---- 读取站点 ----
    raw = {}
    with open(stations_file, "r", encoding="utf-8-sig") as f:
        import csv as _csv
        reader = _csv.DictReader(f)
        for row in reader:
            name = row["站名"].strip()
            if not name:
                continue
            grade = row["等级"].strip()
            try:
                lon = float(row["经度"])
                lat = float(row["纬度"])
            except (TypeError, ValueError):
                lon = lat = 0.0

            # 坐标无效的站点不接入路网：既不进 STATIONS/GRAPH，也不会生成边，
            # 从而彻底避免 (0,0) 参与算路产生的假边。
            if not is_valid_coord(lon, lat):
                INVALID_COORD_STATIONS.add(name)
                continue

            raw[name] = {"grade": grade, "lon": lon, "lat": lat}

    # ---- 计算「站点 -> 城市」映射：手工表 + 自动同城归并 ----
    city_map = build_city_map(raw)

    # ---- 填充各类数据结构 ----
    _GRADE_ORDER = {"特等站": 0, "一等站": 1, "二等站": 2, "三等站": 3}
    for name, info in raw.items():
        grade, lon, lat = info["grade"], info["lon"], info["lat"]
        STATIONS[name] = {"grade": grade, "lon": lon, "lat": lat}

        city = city_map[name]
        STATION_TO_CITY[name] = city
        CITY_STATIONS.setdefault(city, []).append(name)

        # 城市代表信息：取同城内**等级最高**的站（同级则取站名最短者，
        # 通常就是主站）作为该城市的代表等级与坐标。
        cur = CITY_INFO.get(city)
        if cur is None:
            CITY_INFO[city] = {"grade": grade, "lon": lon, "lat": lat,
                               "_rep": name}
        else:
            better = _GRADE_ORDER.get(grade, 9) < _GRADE_ORDER.get(cur["grade"], 9)
            same_grade_shorter = (
                _GRADE_ORDER.get(grade, 9) == _GRADE_ORDER.get(cur["grade"], 9)
                and len(name) < len(cur["_rep"])
            )
            if better or same_grade_shorter:
                cur.update({"grade": grade, "lon": lon, "lat": lat, "_rep": name})

    # 清掉内部字段
    for info in CITY_INFO.values():
        info.pop("_rep", None)

    # 为每个城市节点加入图节点
    for city, info in CITY_INFO.items():
        GRAPH.add_node(city, grade=info["grade"])

    # 城市代表坐标（用于就近连接），如果该城市在图内，补充经纬度属性
    # （find_route 就近连接时需要用到，这里仅存一份便于计算）
    global _CITY_COORDS
    _CITY_COORDS = {
        city: (info["lon"], info["lat"]) for city, info in CITY_INFO.items()
    }

    # ---- 读取线路连接，构建城市级图 ----
    with open(lines_file, "r", encoding="utf-8-sig") as f:
        import csv as _csv
        reader = _csv.DictReader(f)
        for row in reader:
            a = row["起点"].strip()
            b = row["终点"].strip()
            if a not in STATIONS or b not in STATIONS:
                continue
            ca = _city_of(a)
            cb = _city_of(b)
            if ca == cb:
                # 同城自环（如 广州-广州南），合并后忽略
                continue
            try:
                dist = float(row["里程"])
            except (ValueError, KeyError):
                lon1, lat1 = STATIONS[a]["lon"], STATIONS[a]["lat"]
                lon2, lat2 = STATIONS[b]["lon"], STATIONS[b]["lat"]
                dist = haversine(lon1, lat1, lon2, lat2)

            # 记录该城市级边对应的实际站点对（用于还原具体站点）
            key = tuple(sorted([ca, cb]))
            EDGE_STATION_PAIRS.setdefault(key, []).append((a, b))

            # 记录该边所属铁路线名（用 | 分隔多线）
            lnames = row.get("线路", "").strip()
            if lnames:
                for ln in lnames.split("|"):
                    ln = ln.strip()
                    if ln:
                        EDGE_LINES.setdefault(key, set()).add(ln)

            # 保留两城市间里程最短的一条
            if GRAPH.has_edge(ca, cb):
                if dist < GRAPH[ca][cb]["weight"]:
                    GRAPH[ca][cb]["weight"] = dist
            else:
                GRAPH.add_edge(ca, cb, weight=dist)

    if INVALID_COORD_STATIONS:
        print(f"  [警告] {len(INVALID_COORD_STATIONS)} 个站点因缺少有效坐标未接入路网"
              f"（不影响其他线路，补全坐标后自动恢复）："
              + "、".join(sorted(INVALID_COORD_STATIONS)))


def get_city(station):
    """返回站点对应的城市名；站点不存在时返回 None。"""
    return STATION_TO_CITY.get(str(station).strip())


def get_grade(city):
    """返回城市节点的等级；不存在时返回 None。"""
    info = CITY_INFO.get(city)
    return info["grade"] if info else None


def is_major(city):
    """城市是否为特等站或一等站级别。"""
    return get_grade(city) in MAJOR_GRADES


def is_mid(city):
    """城市是否可作为路径中间节点（特等/一等/二等）。"""
    return get_grade(city) in MID_GRADES


def nearest_major(city, limit=5):
    """
    返回距离指定城市最近、且已接入铁路网络的若干大城（按里程升序）。

    参数：
        city:  城市名
        limit: 最多返回的候选大城数量

    返回：
        列表，元素为 (城市名, 里程)，按里程升序排列。
    """
    if city not in CITY_INFO:
        return []

    lon0, lat0 = _CITY_COORDS.get(city, (0, 0))
    candidates = []

    for name, info in CITY_INFO.items():
        if name == city:
            continue
        if info["grade"] not in MAJOR_GRADES:
            continue
        if GRAPH.degree(name) == 0:
            continue  # 该大城没有线路接入，跳过
        d = haversine(lon0, lat0, info["lon"], info["lat"])
        candidates.append((name, d))

    candidates.sort(key=lambda x: x[1])
    return candidates[:limit]


def add_station(name, lon, lat, grade="二等站"):
    """
    动态新增一个站点并自动接入铁路网络。

    新站点会作为独立"城市节点"加入图（无同城映射），并自动连接到
    距离最近的已接入大城（特等/一等站），从而可参与后续路径规划。
    （仅在内存中生效，重启后需重新添加。）

    参数：
        name:  站点名
        lon:   经度
        lat:   纬度
        grade: 等级（特等站 / 一等站 / 二等站 / 三等站），默认二等站

    返回：
        {"added": bool, "msg": str, "connected_to": 接入的大城名或 None}
    """
    name = str(name).strip()
    if name in STATIONS:
        return {"added": False,
                "msg": f"站点「{name}」已存在于数据中，无需重复添加。",
                "connected_to": None}

    # 1. 写入各类数据结构
    grade = grade.strip() if grade else "二等站"
    lon = float(lon)
    lat = float(lat)
    STATIONS[name] = {"grade": grade, "lon": lon, "lat": lat}
    STATION_TO_CITY[name] = name
    CITY_STATIONS.setdefault(name, []).append(name)
    CITY_INFO[name] = {"grade": grade, "lon": lon, "lat": lat}
    _CITY_COORDS[name] = (lon, lat)
    GRAPH.add_node(name, grade=grade)

    # 2. 找到最近的可接入大城并建立连接
    majors = nearest_major(name, limit=3)
    connected = None
    if majors:
        major, dist = majors[0]
        GRAPH.add_edge(name, major, weight=dist)
        key = tuple(sorted([name, major]))
        EDGE_STATION_PAIRS.setdefault(key, []).append((name, major))
        connected = major

    msg = f"站点「{name}」（{grade}）已添加"
    msg += f"，接入最近大城「{connected}」" if connected else "，但暂无可接入的大城"
    return {"added": True, "msg": msg, "connected_to": connected}


# ============================================================
# 路径规划核心
# ============================================================

class RoutePlannerError(Exception):
    """路径规划过程中发生的业务错误。"""
    pass


def _resolve(node):
    """
    将输入的站点名/城市名解析为城市名。
    若输入同时匹配多个站点，返回其中等级最高的站点所属城市。
    """
    node = str(node).strip()

    # 1. 如果直接是站点名，返回其所属城市
    if node in STATION_TO_CITY:
        return STATION_TO_CITY[node]

    # 2. 如果直接是城市名
    if node in CITY_INFO:
        return node

    # 3. 尝试通过站点名前缀映射（如"北京西"未在映射中时）
    matched = [c for s, c in STATION_TO_CITY.items() if node == s]
    if matched:
        return matched[0]

    return None


def city_default(city):
    """返回城市的默认代表性站点（该城市的第一个站点）。"""
    stations = CITY_STATIONS.get(city)
    return stations[0] if stations else city


def resolve_station_path(cities_path):
    """
    把"城市级路径"还原为"具体站点路径"。

    算路在城市层面进行（多站合一），但向用户呈现时应展示该线路
    实际采用的具体车站（例如从京广线走应展示"北京西"而非笼统"北京"）。

    还原策略：
        - 对每一条相邻城市边，依据构造该边时的真实站点对，选择与先后
          相邻站点衔接一致的站点，从而与真实行驶路线保持一致。
        - 若某城市无可匹配的真实站点（如三等站 feeder 端点或孤立城市），
          则回退为该城市的默认代表性站点。

    参数：
        cities_path: list[str] 城市名序列

    返回：
        list[str] 具体站点名序列，长度与 cities_path 一致。
    """
    n = len(cities_path)
    if n == 0:
        return []
    if n == 1:
        return [city_default(cities_path[0])]

    stations = [None] * n
    # 起点默认站：先取城市默认站，之后若存在衔接边会修正
    stations[0] = city_default(cities_path[0])

    for i in range(n - 1):
        u = cities_path[i]
        v = cities_path[i + 1]
        key = tuple(sorted([u, v]))
        pairs = EDGE_STATION_PAIRS.get(key, [])

        chosen = None
        # 优先选择与已确定站点（u 当前站点）衔接一致的真实站点对
        if stations[i] is not None and pairs:
            for (a, b) in pairs:
                # 确定 a/b 分别属于哪个城市
                ca = STATION_TO_CITY.get(a, a)
                cb = STATION_TO_CITY.get(b, b)
                if ca == u and cb == v:
                    sa, sv = a, b
                elif ca == v and cb == u:
                    sa, sv = b, a
                else:
                    continue
                if sa == stations[i]:
                    chosen = (sa, sv)
                    break
        # 若无匹配，且该边存在真实站点对，则采用第一对（顺带修正 u 的站点）
        if chosen is None and pairs:
            a, b = pairs[0]
            if STATION_TO_CITY.get(a, a) == u:
                chosen = (a, b)
            else:
                chosen = (b, a)

        if chosen:
            stations[i] = chosen[0]
            stations[i + 1] = chosen[1]
        else:
            # fallback：使用城市默认站
            if stations[i] is None:
                stations[i] = city_default(u)
            stations[i + 1] = city_default(v)

    # 兜底：确保最后一站非空
    if stations[-1] is None:
        stations[-1] = city_default(cities_path[-1])

    return stations


def compute_segments(stations_path):
    """
    根据具体站点路径，计算每相邻两站之间（城市级）所属的铁路线名。

    返回：
        list[{"from": 站A, "to": 站B, "lines": ["线名", ...], "via": "区域"}]
    """
    segs = []
    for a, b in zip(stations_path, stations_path[1:]):
        ca = STATION_TO_CITY.get(a, a)
        cb = STATION_TO_CITY.get(b, b)
        key = tuple(sorted([ca, cb]))
        lines = sorted(EDGE_LINES.get(key, [])) or ["未知线路"]
        segs.append({
            "from": a,
            "to": b,
            "lines": lines,
        })
    return segs


def apply_display_upgrade(stations_path):
    """
    对路径做"省会化"展示替换（仅影响展示，不影响算路）。

    规则：将路径中间的二等小站（命中 DISPLAY_UPGRADE_MAP）替换为其
    所在省省会 / 枢纽大站。起点与终点保留原样（用户直接输入的站名）。

    返回：与 stations_path 等长的新站名序列。
    """
    upgraded = list(stations_path)
    n = len(stations_path)
    for i, s in enumerate(stations_path):
        if i == 0 or i == n - 1:
            continue  # 保留起终点
        upgraded[i] = DISPLAY_UPGRADE_MAP.get(s, s)
    return upgraded


def find_route(start, end):
    """
    计算从 start 到 end 的最短普速铁路中转方案。

    参数：
        start (str): 起点站名或城市名
        end   (str): 终点站名或城市名

    返回：
        {"path": [...], "transfers": [...], "total_distance": int}

        path 为城市序列。若某城市有多个站点，路径节点为该城市名，
        "城市到城市"即为所求的中转方案。

    说明：
        - 同城多站视作一个城市节点。
        - 若 start/end 不是特等 / 一等站所在城市，自动就近连接到
          最近的可接入大城；若最近大城无法到达，会自动尝试更远的候选。
        - 中间节点仅允许为 特等 / 一等 / 二等。
        - 使用 Dijkstra 计算最短路径。

    抛出：
        RoutePlannerError: 站点/城市不存在、无法到达、无可用大城连接等。
    """
    # ---------- 1. 基础校验 ----------
    scity = _resolve(start)
    ecity = _resolve(end)

    if scity is None:
        raise RoutePlannerError(f"无法识别起点「{start}」（不存在于站点数据中）")
    if ecity is None:
        raise RoutePlannerError(f"无法识别终点「{end}」（不存在于站点数据中）")
    if scity not in GRAPH:
        raise RoutePlannerError(f"起点城市「{scity}」未接入铁路网络")
    if ecity not in GRAPH:
        raise RoutePlannerError(f"终点城市「{ecity}」未接入铁路网络")
    if scity == ecity:
        return {
            "path": [scity],
            "stations": [city_default(scity)],
            "stations_upgraded": [city_default(scity)],
            "transfers": [],
            "transfer_stations": [],
            "total_distance": 0,
        }

    # ---------- 2. 确定起 / 终点候选接入大城 ----------
    def _candidates(city):
        if is_major(city):
            return [(city, 0.0)]
        return nearest_major(city)

    scands = _candidates(scity)
    ecands = _candidates(ecity)

    if not scands:
        raise RoutePlannerError(f"无法为起点「{scity}」找到可接入的大城")
    if not ecands:
        raise RoutePlannerError(f"无法为终点「{ecity}」找到可接入的大城")

    # ---------- 3. 构建受限子图（中间节点仅限 特等/一等/二等） ----------
    mid_cities = {n for n in GRAPH.nodes if is_mid(n)}
    base_restricted = nx.Graph()
    for u, v, data in GRAPH.edges(data=True):
        if u in mid_cities and v in mid_cities:
            base_restricted.add_edge(u, v, weight=data["weight"])

    # ---------- 4. 遍历起终点候选大城组合，求最小可行总里程 ----------
    best = None

    for sm, sd in scands:
        for em, ed in ecands:
            G = base_restricted.copy()
            for feeder, major, feeder_dist in ((scity, sm, sd), (ecity, em, ed)):
                if feeder != major:
                    G.add_edge(feeder, major, weight=feeder_dist)
                for n in (feeder, major):
                    if n not in G:
                        G.add_node(n)

            try:
                dist = nx.dijkstra_path_length(G, scity, ecity, weight="weight")
            except (nx.NetworkXNoPath, nx.NodeNotFound):
                continue

            if best is None or dist < best["dist"]:
                path_nodes = nx.dijkstra_path(G, scity, ecity, weight="weight")
                best = {
                    "start_major": sm,
                    "end_major": em,
                    "path": path_nodes,
                    "dist": dist,
                }

    # ---------- 5. 结果处理 ----------
    if best is None:
        raise RoutePlannerError(
            f"无法从「{scity}」到达「{ecity}」（铁路网络不连通，或无可用换乘方案）"
        )

    path_nodes = best["path"]
    transfers = []
    intermediate = path_nodes[1:-1]
    for node in intermediate:
        grade = get_grade(node)
        deg = GRAPH.degree(node)
        # 大城 且 图内连接数 >= 3 视为中转枢纽
        if grade in MAJOR_GRADES and deg >= 3:
            transfers.append(node)

    # 将城市级路径还原为具体站点路径（呈现给用户时展示实际采用的车站）
    stations_path = resolve_station_path(list(path_nodes))
    transfers_set = set(transfers)
    transfer_stations = [
        s for c, s in zip(path_nodes, stations_path) if c in transfers_set
    ]

    return {
        "path": list(path_nodes),
        "stations": stations_path,
        "stations_upgraded": apply_display_upgrade(stations_path),
        "transfers": transfers,
        "transfer_stations": transfer_stations,
        "segments": compute_segments(stations_path),
        "total_distance": int(round(best["dist"])),
    }


# ============================================================
# 多方案路径规划
# ============================================================

def _build_base_restricted():
    """构建受限子图：仅含特等/一等/二等（可作为中间节点的城市）之间的边。"""
    mid_cities = {n for n in GRAPH.nodes if is_mid(n)}
    base = nx.Graph()
    for u, v, data in GRAPH.edges(data=True):
        if u in mid_cities and v in mid_cities:
            base.add_edge(u, v, weight=data["weight"])
    return base


def _solve_with(base_restricted, scity, ecity):
    """
    在给定受限子图及起终点候选接入大城下，求最小可行总里程路径。

    返回 best 字典（含 path / dist），无可行路径则返回 None。
    """
    def _candidates(city):
        if is_major(city):
            return [(city, 0.0)]
        return nearest_major(city)

    scands = _candidates(scity)
    ecands = _candidates(ecity)
    if not scands or not ecands:
        return None

    best = None
    for sm, sd in scands:
        for em, ed in ecands:
            G = base_restricted.copy()
            for feeder, major, feeder_dist in ((scity, sm, sd), (ecity, em, ed)):
                if feeder != major:
                    G.add_edge(feeder, major, weight=feeder_dist)
                for n in (feeder, major):
                    if n not in G:
                        G.add_node(n)
            try:
                dist = nx.dijkstra_path_length(G, scity, ecity, weight="weight")
            except (nx.NetworkXNoPath, nx.NodeNotFound):
                continue
            if best is None or dist < best["dist"]:
                path_nodes = nx.dijkstra_path(G, scity, ecity, weight="weight")
                best = {
                    "start_major": sm,
                    "end_major": em,
                    "path": list(path_nodes),
                    "dist": dist,
                }
    return best


def _format_route(best):
    """把 _solve_with / find_route 的最优结果格式化为对外结构。"""
    path_nodes = best["path"]
    transfers = []
    for node in path_nodes[1:-1]:
        grade = get_grade(node)
        deg = GRAPH.degree(node)
        if grade in MAJOR_GRADES and deg >= 3:
            transfers.append(node)

    stations_path = resolve_station_path(list(path_nodes))
    transfers_set = set(transfers)
    transfer_stations = [
        s for c, s in zip(path_nodes, stations_path) if c in transfers_set
    ]

    return {
        "path": list(path_nodes),
        "stations": stations_path,
        "stations_upgraded": apply_display_upgrade(stations_path),
        "transfers": transfers,
        "transfer_stations": transfer_stations,
        "segments": compute_segments(stations_path),
        "total_distance": int(round(best["dist"])),
        "path_key": tuple(path_nodes),  # 用于区分不同方案
    }


def find_routes(start, end, k=3, max_attempts=30):
    """
    计算从 start 到 end 的多个（至多 k 条）不同的普速铁路中转方案。

    第一条为全局最短路径，后续每条通过对已用边进行屏蔽、寻找绕行路线，
    使方案在途经站点/线路组合上有实质差异。

    参数：
        start (str): 起点站名或城市名
        end   (str): 终点站名或城市名
        k     (int): 期望返回的方案条数（默认 3）
        max_attempts: 屏蔽边重算的最大尝试次数上限

    返回：
        list[dict]，每个 dict 与 find_route 返回结构一致，并含
        额外字段 "recommended"(bool)。
    """
    scity = _resolve(start)
    ecity = _resolve(end)

    if scity is None:
        raise RoutePlannerError(f"无法识别起点「{start}」（不存在于站点数据中）")
    if ecity is None:
        raise RoutePlannerError(f"无法识别终点「{end}」（不存在于站点数据中）")
    if scity not in GRAPH:
        raise RoutePlannerError(f"起点城市「{scity}」未接入铁路网络")
    if ecity not in GRAPH:
        raise RoutePlannerError(f"终点城市「{ecity}」未接入铁路网络")

    if scity == ecity:
        single = find_route(start, end)
        single["recommended"] = True
        return [single]

    base = _build_base_restricted()

    # 第一条：完整图下的最优
    first = _solve_with(base, scity, ecity)
    if first is None:
        raise RoutePlannerError(
            f"无法从「{scity}」到达「{ecity}」（铁路网络不连通，或无可用换乘方案）"
        )

    # 方案仓库：path_key -> best
    found = {tuple(first["path"]): first}
    result = [_format_route(first)]

    # 依次对已找到方案的每条中间边做屏蔽，生成绕行候选
    attempts = 0
    queue = list(found.values())

    while len(result) < k and queue and attempts < max_attempts:
        attempts += 1
        cur = queue.pop(0)
        cur_path = cur["path"]

        # 对该路径的每一条相邻边做一次屏蔽
        for i in range(len(cur_path) - 1):
            u, v = cur_path[i], cur_path[i + 1]
            Gtry = base.copy()

            # 屏蔽主段边
            if Gtry.has_edge(u, v):
                Gtry.remove_edge(u, v)
            # 同时屏蔽该路径全线上所有 feeder 以外的边，强制大幅绕行，
            # 从而尽量产生"走得不一样"的方案。
            for j in range(len(cur_path) - 1):
                a, b = cur_path[j], cur_path[j + 1]
                if Gtry.has_edge(a, b):
                    Gtry.remove_edge(a, b)

            alt = _solve_with(Gtry, scity, ecity)
            if alt is None:
                continue
            key = tuple(alt["path"])
            if key in found:
                continue
            # 忽略与已有方案在"起点/终点大城接入"完全一致且路径也一样的
            found[key] = alt
            queue.append(alt)

            # 按距离插入结果（保持升序），并保留去重后的实际路径
            fmt = _format_route(alt)
            result.append(fmt)
            if len(result) >= k:
                break
        if len(result) >= k:
            break

    # 排序：按总里程升序；标记推荐方案
    result.sort(key=lambda r: (r["total_distance"], r["path"]))
    if result:
        result[0]["recommended"] = True

    # 移除内部调试字段
    for r_ in result:
        r_.pop("path_key", None)

    return result[:k]


# ============================================================
# 定制中转路径规划（用户指定必过站点）
# ============================================================

def _resolve_required(node, role):
    """解析用户输入的站点/城市为城市名；无法识别或未接入则抛错。"""
    c = _resolve(node)
    if c is None:
        raise RoutePlannerError(f"无法识别{role}「{node}」（不存在于站点数据中）")
    if c not in GRAPH:
        raise RoutePlannerError(f"{role}城市「{c}」未接入铁路网络")
    return c


def find_route_via(start, end, via_list=None, via_str=None):
    """
    计算一条"强制经过若干指定中转站"的中转方案。

    依次规划 起点 -> via1 -> via2 -> ... -> viaN -> 终点 的最短路径并拼接。
    若未指定中转站，则退化为普通的 find_route。

    参数：
        start:     起点（站名或城市名）
        end:       终点（站名或城市名）
        via_list:  中转站列表（list[str]），站名或城市名
        via_str:   兼容入参：逗号/顿号分隔的中转站字符串（如 "武汉,郑州"）

    返回：
        dict，与 find_route 结构一致，并额外含 "vias"（实际经停的中转站城市名）。

    抛出：
        RoutePlannerError: 站点不存在 / 某段无法到达等。
    """
    # 统一收集中转站
    via = []
    if via_list:
        for v in via_list:
            if isinstance(v, str) and v.strip():
                via.append(v.strip())
    if via_str:
        for v in (via_str.split(",") if "," in via_str else via_str.split("，")):
            v = v.strip()
            if v:
                via.append(v)

    if not via:
        # 无指定中转，退化为普通方案
        return find_route(start, end)

    # 解析中转站所在城市（校验存在性）
    via_cities = []
    for v in via:
        vc = _resolve_required(v, "中转站")
        via_cities.append(vc)

    # 解析起终点城市
    scity = _resolve_required(start, "起点")
    ecity = _resolve_required(end, "终点")

    # 逐段求最短路径并拼接
    parts = []          # 每段的 find_route 结果
    total = 0
    chain = [scity] + via_cities

    for i in range(len(chain)):
        u = chain[i]
        v = ecity if i == len(chain) - 1 else chain[i + 1]
        if u == v:
            continue  # 相邻点相同，跳过空段
        part = find_route(_city_default2(u), _city_default2(v))
        parts.append(part)

    if not parts:
        raise RoutePlannerError(f"无法从「{scity}」经指定中转到达「{ecity}」")

    # 拼接完整站点序列（每段末站与下一段首站相同，去重）
    full_stations = []
    for part in parts:
        for s in part["stations"]:
            if full_stations and full_stations[-1] == s:
                continue
            full_stations.append(s)

    # 换乘站：合并各段自识别的中转站，并加入用户指定必经站
    transfer_set = set()
    for part in parts:
        for t in part["transfer_stations"]:
            transfer_set.add(t)
    via_stations = set(city_default(vc) for vc in via_cities)
    transfer_stations = [
        s for s in full_stations if s in transfer_set or s in via_stations
    ]
    # 起终点不属于换乘
    if transfer_stations and transfer_stations[0] == full_stations[0]:
        transfer_stations = transfer_stations[1:]
    if transfer_stations and transfer_stations[-1] == full_stations[-1]:
        transfer_stations = transfer_stations[:-1]

    # 城市级 path（去重连续相同城市）
    city_path = []
    for s in full_stations:
        c = STATION_TO_CITY.get(s, s)
        if city_path and city_path[-1] == c:
            continue
        city_path.append(c)

    return {
        "path": city_path,
        "stations": full_stations,
        "stations_upgraded": apply_display_upgrade(full_stations),
        "vias": via_cities,
        "transfers": city_path[1:-1],
        "transfer_stations": transfer_stations,
        "segments": compute_segments(full_stations),
        "total_distance": sum(p["total_distance"] for p in parts),
        "recommended": True,  # 定制方案视为推荐
    }


def _city_default2(city):
    """按城市名返回对应的默认展示名（优先用站点名，否则用城市名）。"""
    sts = CITY_STATIONS.get(city)
    return sts[0] if sts else city


# ============================================================
# 便捷入口
# ============================================================

# 模块导入时自动加载数据，使 find_route 立即可用。
load_data()

if __name__ == "__main__":
    # 简单命令行交互演示
    print("已加载城市节点：{} 个，线路连接：{} 条".format(
        len(GRAPH.nodes), GRAPH.number_of_edges()
    ))
    for s, e in [("北京", "上海"), ("北京西", "广州"), ("凤凰", "深圳北")]:
        try:
            r = find_route(s, e)
            print(f"{s} -> {e} | d={r['total_distance']} | "
                  f"stations={r['stations']} | transfers={r['transfer_stations']}")
        except RoutePlannerError as ex:
            print(f"{s} -> {e} | ERROR: {ex}")
