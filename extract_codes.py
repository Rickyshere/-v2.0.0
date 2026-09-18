# -*- coding: utf-8 -*-
"""从 12306 station_name.js 提取 main_railway_line.txt 中车站的电报码"""
import re

# ---------- 1. 解析站名->电报码 ----------
with open("station_name.js", "r", encoding="utf-8-sig") as f:
    js = f.read()

# 提取所有 |站名|电报码| 段（站名不含英文，电报码为大写字母）
code_map = {}
for m in re.finditer(r'\|([\u4e00-\u9fffA-Za-z0-9]+)\|([A-Z]{2,4})\|', js):
    name = m.group(1)
    code = m.group(2)
    if '\u4e00' <= name[0] <= '\u9fff':  # 仅取中文站名
        code_map.setdefault(name, code)  # 同名取第一个（站码）

print(f"12306 站名库共 {len(code_map)} 个中文车站")

# ---------- 2. 提取 main_railway_line.txt 中的站名 ----------
# 复用 upgrade_data 的解析器（唯一真源），避免重复实现导致行为不一致：
# 自己写「站名（等级）」正则会把「金温铁路（金华—温州）：…」里的线名
# 当成站点，产生幽灵站。load_railway_lines() 已做括号深度感知的线名切分
# 与等级白名单校验。
import upgrade_data as ud

_, _meta = ud.load_railway_lines()
stations_in_lines = set(_meta.keys())

print(f"main_railway_line.txt 中共 {len(stations_in_lines)} 个站点")

# ---------- 3. 匹配并输出 ----------
print("\n===== 车站电报码对照表 =====")
print(f"{'站名':<10}{'电报码':<8}{'是否在12306库'}")
print("-" * 28)
matched = []
missing = []
for s in sorted(stations_in_lines):
    code = code_map.get(s)
    if code:
        matched.append((s, code))
    else:
        missing.append(s)

for s, code in sorted(matched):
    print(f"{s:<10}{code:<8}✓")
if missing:
    print(f"\n----- 未在 12306 库中查到的站 ({len(missing)}) -----")
    for s in sorted(missing):
        print(f"  {s}")

# ---------- 4. 输出为一行式对照（便于复制） ----------
print("\n===== 一行式：站名=电报码，逗号分隔 =====")
print(",".join(f"{s}={c}" for s, c in sorted(matched)))

