# -*- coding: utf-8 -*-
"""
从 main_railway_line.txt 生成 stations.csv / lines.csv，
供 route_planner.py 直接使用。

数据来源：
    - main_railway_line.txt：铁路线路清单（线路名 + 站点顺序 + 等级）
    - stations.csv（旧数据，可选）：作为坐标来源之一
    - 内置城市坐标表（CITY_FALLBACK）：为缺失坐标的站点兜底

运行方式：
    python upgrade_data.py

产出：
    stations.csv： 站名,经度,纬度,等级
    lines.csv：    起点,终点,里程
"""

import csv
import re
import math
import sys

# 坐标自动补全工具（可选导入，缺失时不影响原有流程）
try:
    import fetch_coords
except Exception:
    fetch_coords = None

# ============================================================
# 配置
# ============================================================

LINE_FILE = "main_railway_line.txt"
OUT_STATIONS = "stations.csv"
OUT_LINES = "lines.csv"
# 旧站点坐标文件（用于优先取坐标）。生成时会先读取旧 stations.csv，
# 再把新生成的 stations.csv 覆盖上去，因此直接复用文件名即可。
LEGACY_STATIONS = "stations.csv"

# 相邻站间距的"可疑阈值"（公里）。
# 超过它的区间会被列为"超长区间"供人工复核坐标 —— 只报告，不自动处理。
# 取值说明：中国普速铁路真实存在的长区间有 兰新线 乌鲁木齐—西宁 1441km、
# 青藏线 拉萨—格尔木 828km、格尔木—西宁 615km，因此阈值定在 900km
# 既能揪出"地名被解析到异地同名"的错误（实测中华门—马鞍山 1144km），
# 又只会顺带提示少数几条真实长区间（这些确认一下即可）。
MAX_ADJACENT_KM = 900.0

# 缺失坐标站点的城市级坐标兜底表。
# key 为站名（规整：去"站"后缀），value 为 (经度, 纬度)。
# 优先级：现有数据 > 此表。
CITY_FALLBACK = {
    # 华北/京津冀
    "北京南": (116.38, 39.87), "北京丰台": (116.30, 39.85),
    "黄村": (116.33, 39.73), "广阳": (116.41, 39.56),
    "南仓": (117.21, 39.22), "天津西": (117.16, 39.16),
    "静海": (116.93, 38.95),
    # 京沪沿线
    "德州": (116.31, 37.45), "济南南": (116.99, 36.65),
    "泰山": (117.09, 36.20), "邹城": (116.97, 35.40),
    "滕州": (117.16, 35.08), "枣庄西": (117.21, 34.81),
    "前亭": (117.28, 34.30), "镇江东": (119.43, 32.19),
    "丹阳": (119.58, 32.01),
    # 京九沿线
    "广安门": (116.35, 39.88), "任丘": (116.09, 38.68),
    "亳州": (115.78, 33.87), "龙川北": (115.26, 24.10),
    "龙川": (115.26, 24.10), "惠州": (114.42, 23.11),
    "常平": (114.10, 22.97), "东莞东": (113.75, 23.02),
    # 京广沿线
    "邢台": (114.50, 37.07), "安阳": (114.35, 36.10),
    "新乡": (113.90, 35.30), "许昌": (113.85, 34.02),
    "漯河": (114.02, 33.57), "驻马店": (114.02, 33.01),
    "信阳": (114.07, 32.13), "武昌": (114.31, 30.56),
    "韶关东": (113.59, 24.81),
    # 焦柳沿线
    "宝丰": (113.05, 33.85), "荆门": (112.20, 31.04),
    "邵阳": (111.47, 27.24),
    # 陇海/兰新/青藏沿线
    "格尔木": (94.90, 36.40),
    # 沪昆沿线
    "鹰潭": (117.07, 28.27), "向塘": (116.01, 28.42),
    "新余": (114.91, 27.81), "宜春": (114.39, 27.80),
    "萍乡": (113.85, 27.62),
    # 通让
    "大安北": (124.30, 45.52), "新肇": (124.85, 45.55),
    # 平齐
    "双辽": (123.50, 43.52), "白城": (122.84, 45.62),
    # 图佳
    "图们": (129.85, 42.97), "佳木斯": (130.37, 46.81),
    # 滨洲
    "肇东": (125.96, 46.05), "安达": (125.34, 46.41),
    "卧里屯": (125.20, 46.42), "杜尔伯特": (124.45, 46.86),
    "扎兰屯": (122.74, 48.00), "牙克石": (120.73, 49.29),
    "海满": (119.76, 49.21), "扎赉诺尔": (117.68, 49.51),
    "扎赉诺尔西": (117.57, 49.50), "满洲里": (117.38, 49.60),
    # 滨绥
    "新香坊": (126.76, 45.72), "阿城": (126.98, 45.53),
    "玉泉": (127.20, 45.40), "一面坡": (128.10, 45.15),
    "绥芬河": (131.15, 44.41),
    # 富西
    "富裕": (124.47, 47.80), "讷河": (124.88, 48.48),
    "嫩江": (125.22, 49.18), "塔河": (124.70, 52.33),
    # 内六
    "自贡北": (104.78, 29.34), "翠屏": (104.63, 28.77),
    "宜宾南": (104.64, 28.73),
    # 京哈线
    "北京东": (116.48, 39.89), "双桥": (116.59, 39.86),
    "蓟州": (117.40, 40.04), "唐山北": (118.14, 39.65),
    "昌黎": (119.16, 39.71), "北戴河": (119.49, 39.83),
    "秦皇岛": (119.60, 39.94), "山海关": (119.78, 39.98),
    "葫芦岛": (120.84, 40.71), "锦州": (121.13, 41.10),
    "大虎山": (122.06, 41.62), "沈阳北": (123.43, 41.83),
    "沈阳": (123.43, 41.81), "铁岭": (123.84, 42.29),
    "长春南": (125.30, 43.84), "长春": (125.32, 43.89),
    # 京哈沿线补充
    "唐山北": (118.14, 39.65), "沈阳南": (123.39, 41.66),
    "盘锦": (122.07, 41.12), "四平": (124.35, 43.17),
    # 主坐标 - 华北
    "北京": (116.40, 39.90), "北京西": (116.32, 39.89),
    "星火": (116.50, 39.96), "南口": (116.13, 40.24),
    "大同": (113.30, 40.09), "呼和浩特": (111.75, 40.84),
    "包头": (109.84, 40.66), "银川": (106.23, 38.49),
    "保定": (115.46, 38.87), "石家庄": (114.51, 38.04),
    "石家庄北": (114.46, 38.06), "邯郸": (114.49, 36.63),
    "邯郸南": (114.49, 36.55), "沙河市": (114.50, 36.86),
    "马头": (114.39, 36.44), "沧州": (116.84, 38.30),
    "泊头": (116.58, 38.08), "衡水": (115.67, 37.74),
    "井陉": (114.15, 38.03), "阳泉": (113.58, 37.86),
    "寿阳": (113.14, 37.90), "榆次": (112.71, 37.70),
    "太原": (112.55, 37.87), "长治": (113.12, 36.20),
    "晋城": (112.85, 35.49), "月山": (113.02, 35.23),
    "焦作": (113.24, 35.22), "关林": (112.47, 34.63),
    "南阳": (112.53, 32.98), "襄阳": (112.12, 32.01),
    # 主坐标 - 东北
    "哈尔滨": (126.53, 45.80), "香坊": (126.70, 45.72),
    "佳木斯": (130.37, 46.81), "齐齐哈尔": (123.92, 47.34),
    "牡丹江": (129.63, 44.58), "通辽": (122.24, 43.65),
    "大庆西": (124.87, 46.62), "太平川": (123.18, 44.35),
    "吉林省吉林": (126.55, 43.84), "盘锦": (122.07, 41.12),
    "勃利": (130.57, 45.75), "加格达奇": (124.12, 50.42),
    "漠河": (122.54, 52.98), "伊图里河": (121.37, 50.53),
    # 主坐标 - 华东
    "上海": (121.47, 31.23), "苏州": (120.58, 31.30),
    "无锡": (120.31, 31.49), "常州": (119.97, 31.81),
    "镇江": (119.44, 32.19), "南京": (118.80, 32.06),
    "徐州": (117.28, 34.26), "连云港东": (119.22, 34.74),
    "嘉兴": (120.76, 30.75), "杭州东": (120.21, 30.29),
    "金华": (119.65, 29.08), "衢州": (118.87, 28.94),
    "六安": (116.51, 31.75), "合肥": (117.23, 31.82),
    "合肥西": (117.20, 31.84), "舒城": (116.94, 31.47),
    "桐城": (116.97, 31.06), "怀宁": (116.83, 30.73),
    "黄梅": (115.94, 30.07), "安庆": (117.06, 30.52),
    "南昌": (115.86, 28.68), "九江": (116.00, 29.71),
    "吉安": (114.99, 27.11), "赣州": (114.94, 25.83),
    "潢川": (115.05, 32.13), "麻城": (115.03, 31.18),
    "阜阳": (115.81, 32.89), "商丘": (115.66, 34.41),
    "聊城": (115.99, 36.45),
    "济南": (117.00, 36.67), "淄博": (118.05, 36.81),
    "潍坊": (119.16, 36.71), "青岛": (120.38, 36.07),
    "六安": (116.51, 31.75),
    # 主坐标 - 中南
    "郑州": (113.63, 34.75), "洛阳": (112.45, 34.62),
    "武汉": (114.30, 30.59), "汉口": (114.26, 30.58),
    "武昌": (114.31, 30.53), "岳阳": (113.13, 29.37),
    "长沙": (112.94, 28.23), "株洲": (113.13, 27.83),
    "衡阳": (112.58, 26.89), "郴州": (113.01, 25.77),
    "邵武": (117.49, 27.34), "来舟": (118.02, 26.62),
    "福州": (119.30, 26.07), "漳平": (117.42, 25.29),
    "厦门": (118.09, 24.48), "厦门高崎": (118.12, 24.56),
    "月山": (113.02, 35.23), "柳州": (109.42, 24.33),
    "桂林": (110.29, 25.27), "南宁": (108.32, 22.82),
    "黎塘": (109.11, 23.20), "贵港": (109.60, 23.11),
    "玉林": (110.18, 22.63), "湛江": (110.36, 21.27),
    "广州": (113.27, 23.13), "广州东": (113.32, 23.15),
    "深圳": (114.06, 22.54), "江村": (113.20, 23.35),
    # 主坐标 - 西南
    "南宁": (108.32, 22.82), "贵阳": (106.63, 26.65),
    "安顺": (105.95, 26.25), "六盘水": (104.83, 26.59),
    "曲靖": (103.80, 25.49), "昆明": (102.83, 24.88),
    "都匀": (107.52, 26.26), "金城江": (108.06, 24.69),
    "桂林": (110.29, 25.27), "内江": (105.06, 29.58),
    "成都": (104.07, 30.67), "广元": (105.85, 32.44),
    "绵阳": (104.68, 31.47), "燕岗": (103.90, 29.55),
    "西昌": (102.26, 27.89), "攀枝花": (101.72, 26.58),
    "重庆": (106.55, 29.56), "重庆北": (106.55, 29.60),
    "遵义": (106.93, 27.73), "十堰": (110.80, 32.63),
    "安康": (109.03, 32.69), "西安东": (109.19, 34.28),
    "威舍": (104.84, 25.68), "星火": (116.50, 39.96),
    # 主坐标 - 西北
    "兰州": (103.83, 36.06), "西宁": (101.78, 36.62),
    "西安": (108.94, 34.34), "咸阳": (108.71, 34.33),
    "宝鸡": (107.14, 34.36), "乌鲁木齐": (87.62, 43.82),
    "拉萨": (91.14, 29.65),
    "马头": (114.39, 36.44),
    # 补充缺失站
    "常德": (111.70, 29.03), "益阳": (112.36, 28.55),
    "娄底": (112.00, 27.70), "怀化": (110.00, 27.55),
    "上饶": (117.97, 28.45), "湘潭": (112.91, 27.83),
    "太湖": (116.31, 30.45), "宿松": (116.13, 30.16),
    "吉林": (126.55, 43.84), "丹江": (111.51, 32.54),
}


def norm_name(name):
    """规整站名：去掉尾部"站"字。"""
    s = str(name).strip()
    if s.endswith("站"):
        s = s[:-1]
    return s


# 合法等级。用来判断「（…）」里到底是等级，还是别的注释。
_GRADE_RE = re.compile(r"^(特等|一等|二等|三等)站?$")


def _split_line_head(raw):
    """把单行式「线名：站点、站点…」拆成 (线名, 站点串)。

    关键点：线名本身可能带括号注释，例如

        浙赣铁路（连接：南昌、鹰潭）：南昌站（一等）、鹰潭站（特等）

    这里**括号内的「：」不是分隔符**，只有括号外的才算。若直接用
    `raw.split("：", 1)`，会从括号里的冒号切开，把线名注释的残余
    当成站点，进而生成「浙赣铁路」「鹰潭）：南昌站」这类**幽灵站**
    ——它们随后会被当成缺坐标站点，污染路网。

    找不到括号外的冒号时返回 (None, raw)。
    """
    depth = 0
    for i, ch in enumerate(raw):
        if ch == "（":
            depth += 1
        elif ch == "）":
            if depth > 0:
                depth -= 1
        elif ch == "：" and depth == 0:
            return raw[:i].strip(), raw[i + 1:]
    return None, raw


def haversine(lon1, lat1, lon2, lat2):
    """球面距离（公里）。"""
    R = 6371.0
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = (math.sin(dphi / 2) ** 2
         + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2)
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def load_legacy_coords():
    """读取旧 stations.csv 的坐标作为来源（若存在）。返回 {规整名: (lon,lat)}"""
    coords = {}
    try:
        with open(LEGACY_STATIONS, "r", encoding="utf-8-sig") as f:
            for row in csv.DictReader(f):
                n = row["站名"].strip()
                try:
                    coords[norm_name(n)] = (float(row["经度"]), float(row["纬度"]))
                except (ValueError, KeyError):
                    continue
    except FileNotFoundError:
        pass
    return coords


def load_railway_lines():
    """解析 main_railway_line.txt，返回 (lines, stations_meta)。
    lines: {线名: [(站名, 等级), ...]}
    stations_meta: {规整名: {"name": 原始名, "grade": 等级}}

    支持多种排版：
      1. 两行式：
            【线名（连接：...）】
            站1（等级）、站2（等级）、...
         （标题行与站点行分行，空行分隔）
      2. 单行式：
            线名：站1（等级）、站2（等级）、...
    """
    lines = {}
    stations_meta = {}
    pending_line = None  # 尚未配对站点列表的线名
    skipped_parts = []   # 「（…）」里不是等级、被跳过的片段（多半是格式写错）

    with open(LINE_FILE, "r", encoding="utf-8") as f:
        for raw in f.read().splitlines():
            raw = raw.strip()
            if not raw:
                pending_line = None  # 空行：结束上一条线路
                continue

            # 标题行：【线名（连接：...）】
            if raw.startswith("【"):
                end = raw.find("】")
                if end != -1:
                    pending_line = raw[1:end]
                continue

            # 站点行：绑定到 pending 线名
            lname = pending_line if pending_line else None
            # 兼容单行格式"线名：站1（等级）、站2..."（线名可带括号注释）
            sts_part = raw
            if lname is None:
                maybe_head, maybe_sts = _split_line_head(raw)
                if maybe_head:
                    lname = maybe_head
                    sts_part = maybe_sts

            sts = []
            for part in sts_part.split("、"):
                m = re.match(r"(.+?)（(.+?)）", part.strip())
                if not m:
                    continue
                name = m.group(1).strip()
                grade = m.group(2).strip()
                if not name:
                    continue
                # 「（…）」里必须是等级，否则视为格式写错，不当作站点
                # （例如「线名（连接：…）」被误当成一个站）
                if not _GRADE_RE.match(grade):
                    skipped_parts.append(f"{name}（{grade}）")
                    continue
                sts.append((name, grade))
                key = norm_name(name)
                stations_meta.setdefault(key, {"name": name, "grade": grade})

            if sts:
                lines[lname or f"线路{len(lines)+1}"] = sts
                pending_line = None  # 已配对

    if skipped_parts:
        print(f"  [提示] 有 {len(skipped_parts)} 个片段因括号内不是合法等级被忽略"
              f"（等级应为 特等/一等/二等/三等）：")
        for s in skipped_parts[:8]:
            print(f"        {s}")
        if len(skipped_parts) > 8:
            print(f"        …另有 {len(skipped_parts) - 8} 个")

    return lines, stations_meta


_GRADE_FULL = {"特等": "特等站", "一等": "一等站", "二等": "二等站",
               "三等": "三等站"}


def _usable_coord(v):
    """把 (lon, lat) 规整成可用的元组；无效（0,0 / 越界 / 非数字）返回 None。"""
    try:
        a, b = float(v[0]), float(v[1])
    except (TypeError, ValueError, IndexError, KeyError):
        return None
    if a == 0.0 and b == 0.0:
        return None
    if abs(a) < 0.5:
        return None
    if not (-180.0 <= a <= 180.0) or not (-90.0 <= b <= 90.0):
        return None
    return (a, b)


def build_stations(lines, stations_meta, legacy_coords, extra_coords=None):
    """构建最终站点表。返回 {规整名: {"name","grade","lon","lat"}}

    坐标优先级（高 -> 低）：
        1) 内置城市坐标表 CITY_FALLBACK —— 人工维护，最可信
        2) extra_coords.json 缓存 —— 本机补全 / 修正的结果
        3) 旧的 stations.csv —— **生成产物**，优先级最低

    第 3 项的排序很关键：stations.csv 是每次生成时被覆盖重写的产物。
    如果让它排在 extra_coords 之前（历史实现如此），一旦某个坐标被写错，
    之后在 extra_coords.json 里做的修正就会被它**永久遮蔽、永远不生效**
    —— 实测「马鞍山」曾被写成广东境内坐标（113.81, 22.72），
    修正 extra_coords.json 后重生成仍然无效，就是因为这个顺序问题。
    """
    extra = extra_coords or {}
    result = {}
    for key, meta in stations_meta.items():
        name = meta["name"]
        raw_grade = meta["grade"]
        # 统一等级命名（如 "特等" -> "特等站"），与 route_planner 兼容
        grade = _GRADE_FULL.get(raw_grade, "三等站")
        lon = lat = None

        # 1) 内置城市坐标表（人工维护，最可信）
        if key in CITY_FALLBACK:
            lon, lat = CITY_FALLBACK[key]
        # 2) extra_coords.json 缓存（本机补全 / 修正）
        elif key in extra:
            t = _usable_coord(extra[key])
            if t:
                lon, lat = t
        # 3) 旧的 stations.csv（生成产物，优先级最低，跳过无效值）
        if lon is None and key in legacy_coords:
            t = _usable_coord(legacy_coords[key])
            if t:
                lon, lat = t

        if lon is None:
            print(f"  [警告] 站点「{name}」缺少坐标，标记为 0,0"
                  f"（可在网页勾选在线补全，或手工补录）")
            lon, lat = 0.0, 0.0
        result[key] = {
            "name": key,
            "grade": grade,
            "lon": lon,
            "lat": lat,
        }
    return result


def build_lines(lines, stations):
    """根据线路顺序生成相邻站点连接。返回 list of (A, B, dist, lines_set)。

    每条边会记录其所属的铁路线名（可能属于多条线），供前端按线路分段着色。
    """
    edges = {}  # key=(A,B) 逆序归一 -> {"dist": 最短里程, "lines": set()}
    for lname, sts in lines.items():
        names = [norm_name(s) for s, _ in sts]
        for a, b in zip(names, names[1:]):
            if a == b:
                continue
            ca = stations[a]
            cb = stations[b]
            dist = haversine(ca["lon"], ca["lat"], cb["lon"], cb["lat"])
            key = tuple(sorted([a, b]))
            if key not in edges or dist < edges[key]["dist"]:
                edges[key] = {"dist": dist, "lines": {lname}}
            else:
                edges[key]["lines"].add(lname)
    # 统一输出：按 (A,B) 排序，线路名用 "|" 分隔
    out = []
    for (a, b), info in sorted(edges.items()):
        out.append((a, b, info["dist"], "|".join(sorted(info["lines"]))))
    return out


def find_transfer_stations(lines, stations):
    """找出出现在 >=2 条线路中的站点（交汇枢纽）。返回 [规整名, ...]"""
    counts = {}
    line_of = {}
    for lname, sts in lines.items():
        for s, _ in sts:
            k = norm_name(s)
            counts[k] = counts.get(k, 0) + 1
            line_of.setdefault(k, set()).add(lname)
    transfers = []
    for k, c in counts.items():
        if c >= 2:
            transfers.append((stations[k]["name"], sorted(line_of[k])))
    transfers.sort()
    return transfers


def write_stations(stations):
    with open(OUT_STATIONS, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["站名", "经度", "纬度", "等级"])
        for k, v in sorted(stations.items()):
            w.writerow([v["name"], f"{v['lon']:.4f}", f"{v['lat']:.4f}",
                        v["grade"]])
    print(f"  已写出 {OUT_STATIONS}，共 {len(stations)} 个站点")


def write_lines(edges):
    with open(OUT_LINES, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["起点", "终点", "里程", "线路"])
        for a, b, dist, lnames in edges:
            w.writerow([a, b, round(dist, 1), lnames])
    print(f"  已写出 {OUT_LINES}，共 {len(edges)} 条线路连接")


def find_suspect_edges(threshold_km=None):
    """
    扫描 lines.csv，找出"相邻站间距异常长"的区间，供人工复核坐标。

    为什么需要它：在线地理编码对**异地同名**的小地名极易解析错
    （实测「马鞍山」被解析到广东境内 113.81,22.72）。这类错误坐标
    **看上去完全合法** —— 不是 0,0、也不越界，所以坐标有效性校验拦不住它。
    但症状很明显：该站与相邻站的间距会异常大（中华门—马鞍山 1144km，
    而宁铜铁路真实区间只有几十公里）。

    本函数**只报告、不修改任何数据**。因为兰新线（乌鲁木齐—西宁 1441km）、
    青藏线（拉萨—格尔木 828km）本身就是合法的长区间，用阈值自动删除
    会误伤真实数据，所以把判断权交给使用者。

    参数：
        threshold_km: 阈值（公里），默认取 MAX_ADJACENT_KM

    返回 list[dict]，按里程降序：
        {"from", "to", "distance_km", "line"}
    """
    thr = MAX_ADJACENT_KM if threshold_km is None else float(threshold_km)
    out = []
    try:
        with open(OUT_LINES, "r", encoding="utf-8-sig") as f:
            for row in csv.DictReader(f):
                try:
                    d = float(row["里程"])
                except (TypeError, ValueError, KeyError):
                    continue
                if d > thr:
                    out.append({
                        "from": (row.get("起点") or "").strip(),
                        "to": (row.get("终点") or "").strip(),
                        "distance_km": round(d, 1),
                        "line": (row.get("线路") or "").strip(),
                    })
    except FileNotFoundError:
        pass
    out.sort(key=lambda x: -x["distance_km"])
    return out


def main():
    want_fetch = "--fetch" in sys.argv

    print("=" * 60)
    print("升级数据：由 main_railway_line.txt 生成路网")
    print("  缺失坐标自动补全：" + ("开(--fetch)" if want_fetch
          else "关（如需自动爬取坐标请加参数 --fetch）"))
    print("=" * 60)

    extra = fetch_coords.load_extra_coords() if fetch_coords else {}

    legacy = load_legacy_coords()
    print(f"  旧坐标来源：{len(legacy)} 个可用")

    lines, stations_meta = load_railway_lines()
    print(f"  线路数：{len(lines)}")
    print(f"  站点数：{len(stations_meta)}")

    stations = build_stations(lines, stations_meta, legacy, extra)

    # 若存在缺坐标站 且 开启自动补全，则调用 fetch_coords 补全后重新生成
    zero = [k for k, v in stations.items()
            if v["lon"] == 0 and v["lat"] == 0]
    if want_fetch and zero and fetch_coords:
        print(f"\n  检测到 {len(zero)} 个站点缺坐标，自动补全中…")
        try:
            fixed = fetch_coords.batch_fix(zero)
            extra = fetch_coords.load_extra_coords()
            print(f"  本次补全 {len(fixed)} 个站点坐标。")
            stations = build_stations(lines, stations_meta, legacy, extra)
        except Exception as e:
            print(f"  自动补全中断：{e}")

    edges = build_lines(lines, stations)
    transfers = find_transfer_stations(lines, stations)

    # 统计等级分布与坐标齐全情况
    from collections import Counter
    grades = Counter(v["grade"] for v in stations.values())
    zero_coord = [k for k, v in stations.items() if v["lon"] == 0 and v["lat"] == 0]
    print(f"  等级分布：{dict(grades)}")
    print(f"  缺少坐标的站：{len(zero_coord)} 个")
    if zero_coord:
        print("    ", zero_coord)

    write_stations(stations)
    write_lines(edges)

    print(f"\n完成！共 {len(lines)} 条线路、{len(stations)} 个站点、"
          f"{len(edges)} 条连接。")
    print(f"交汇枢纽（换乘站）{len(transfers)} 个：")
    for name, ls in transfers:
        print(f"    {name}  <-  {'、'.join(ls)}")


if __name__ == "__main__":
    main()
