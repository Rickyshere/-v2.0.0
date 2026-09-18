# -*- coding: utf-8 -*-
"""输出最终的车站电报码对照表（含别名映射与货运站标注）"""
import re, csv

import upgrade_data as ud

# ---- 读取 12306 站名库 ----
js = open("station_name.js", encoding="utf-8-sig").read()
code_map = {}
for m in re.finditer(r'\|([\u4e00-\u9fff]+)\|([A-Z]{2,4})\|', js):
    code_map.setdefault(m.group(1), m.group(2))

# ---- 站点列表：复用 upgrade_data 的解析器（唯一真源）----
# 不要再自己写一套「站名（等级）」正则：那样会把
# 「金温铁路（金华—温州）：金华南站（二等）、…」里的线名当成站点，
# 产生「金温铁路」这种幽灵站。upgrade_data.load_railway_lines()
# 已经做了括号深度感知的线名切分 + 等级白名单校验，直接复用即可。
_, stations_meta = ud.load_railway_lines()
stations = set(stations_meta.keys())

# ---- 追加「八纵八横」高速通道的城市 ----
# 高铁页面用的是城市级网络（hsr_network.txt），城市名同样需要电报码，
# 否则查不到 G/D/C 车次。这里一并纳入同一张对照表。
HSR_NETWORK_FILE = "hsr_network.txt"
try:
    with open(HSR_NETWORK_FILE, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "：" not in line:
                continue
            for c in line.split("：", 1)[1].split("、"):
                c = c.strip()
                if c:
                    stations.add(c)
except FileNotFoundError:
    pass

# ---- 别名映射：线路用名 -> 12306 实际客运站名 ----
ALIAS = {
    "济南南": "大明湖",
    "镇江东": "镇江",
    "宜宾南": "宜宾",
    "龙川北": "龙川",
    "邯郸南": "邯郸",
    "呼和浩特": "呼和浩特",
    # 八纵八横通道里的城市名 -> 12306 可售站名
    "防城港": "防城港北",
    "香港": "香港西九龙",
}
# 纯货运/技术站（12306 客运库中无客运代码）
FREIGHT = ["南仓", "双桥", "星火", "广安门", "江村", "马头",
           "卧里屯", "新香坊", "前亭", "丹江"]
# 在八纵八横规划里、但 12306 暂不发售的节点（规划中 / 非国铁售票范围）
PLANNED = ["台北", "澳门"]

rows = []
for s in sorted(stations):
    code = code_map.get(s)
    matched_name = s
    if code is None:
        alias = ALIAS.get(s)
        if alias:
            code = code_map.get(alias)
            matched_name = f"{alias}({s})" if code else None
    if code:
        kind = "货/技" if s in FREIGHT else "客"
        rows.append((s, matched_name.split("(")[0], code, kind))
    elif s in PLANNED:
        rows.append((s, "", "", "规划中"))
    else:
        rows.append((s, "", "", "货/技/失联"))

# CSV 输出
with open("station_codes.csv", "w", encoding="utf-8-sig", newline="") as f:
    w = csv.writer(f)
    w.writerow(["站名", "12306站名", "电报码", "类型"])
    for r in rows:
        w.writerow(r)

print(f"总站点：{len(rows)}，成功获取电报码：{sum(1 for r in rows if r[2])}")
print("已写入 station_codes.csv")
print()
print("成功获取电报码的车站：")
for r in rows:
    if r[2]:
        print(f"  {r[0]} <- {r[2]}")
print()
print("获取不到客运电报码的车站（多为货运/技术站）：")
for r in rows:
    if not r[2]:
        print(f"  {r[0]}")
