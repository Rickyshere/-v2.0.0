# -*- coding: utf-8 -*-
"""
修正 hsr_network.txt 中被在线地理编码定位到错误省份的车站

背景：
    在线地理编码对**异地同名**的小地名极易解析错。批量补全 488 个车站后，
    用「相邻站间距」体检发现 17 个站被定位到了错误的地方，例如：

        绥阳   → 贵州绥阳（应在黑龙江牡丹江）
        惠安堡 → 福建惠安（应在宁夏盐池）
        五台山南 → 广西柳州（应在山西忻州）
        华山北 → 云南昆明（应在陕西华阴）
        雁荡山 → 天津（应在浙江乐清）
        桃源 / 新化南 → 台湾（应在湖南）
        清河 / 沙河 → 辽宁铁岭 / 河北邢台（应在北京）

    这类错误**看上去完全合法**（不是 0,0、不越界），只能靠"与邻居的距离"
    这种上下文校验发现 —— 这正是 hsr_planner.find_suspect_edges() 的用途。

修正方式：
    用**省份限定的地址**重新地理编码（如「黑龙江绥阳站」而不是「绥阳站」），
    百度就能返回正确位置。修完写回 extra_coords.json。

用法：
    set BAIDU_AK=你的百度服务端AK
    python fix_hsr_coords.py --dry-run   # 只看会怎么改
    python fix_hsr_coords.py             # 实际修正
"""

import json
import os
import sys

import fetch_coords as fc

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# 站名 -> 用于重新定位的「省份限定查询词」
# 左列是数据里的站名，右列是给地理编码的查询串（脚本会自动补「站」字）
FIXES = {
    "绥阳": "黑龙江牡丹江绥阳",
    "民乐": "甘肃张掖民乐",
    "惠安堡": "宁夏吴忠盐池惠安堡",
    "五台山南": "山西忻州五台山南",
    "华山北": "陕西渭南华阴华山北",
    "雁荡山": "浙江温州乐清雁荡山",
    "海东": "青海海东",
    "平安驿": "青海海东平安驿",
    "桃源": "湖南常德桃源",
    "新化南": "湖南娄底新化南",
    "清河": "北京海淀清河",
    "金山北": "上海金山北",
    "广南": "云南文山广南",
    "普者黑": "云南文山丘北普者黑",
    "滨海西": "天津滨海西",
    "沙河": "北京昌平沙河",
    "古田会址": "福建龙岩上杭古田会址",
}


def main():
    dry = "--dry-run" in sys.argv
    fc.refresh_keys()
    if not dry and not fc.online_key_available():
        print("未检测到 Key，请先设置系统环境变量 BAIDU_AK。")
        sys.exit(1)

    extra = fc.load_extra_coords()
    print(f"待修正 {len(FIXES)} 个车站\n")

    changed = {}
    for station, query in FIXES.items():
        old = extra.get(station)
        if dry:
            print(f"  {station:<8} 当前 {old}  ← 将用「{query}」重新定位")
            continue
        hit = fc.geocode_baidu(query)
        if not hit:
            print(f"  ✗ {station:<8} 重新定位失败，保留原值")
            continue
        extra[station] = [hit[0], hit[1]]
        changed[station] = (old, [hit[0], hit[1]])
        old_s = f"{old[0]:.3f},{old[1]:.3f}" if isinstance(old, (list, tuple)) else str(old)
        print(f"  ✓ {station:<8} {old_s}  ->  {hit[0]:.3f},{hit[1]:.3f}")

    if dry:
        print("\n（--dry-run：未实际修改）")
        return

    if changed:
        fc.save_extra_coords(extra)
        print(f"\n已写回 extra_coords.json（修正 {len(changed)} 个）")
    print("\n请重新运行体检确认：python -c \"import hsr_planner as hp; "
          "hp.load_data(); print(hp.find_suspect_edges())\"")


if __name__ == "__main__":
    main()
