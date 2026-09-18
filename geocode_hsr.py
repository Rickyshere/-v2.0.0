# -*- coding: utf-8 -*-
"""
为 hsr_network.txt 里缺坐标的车站补全经纬度

用途：
    编辑过 hsr_network.txt（八纵八横通道数据）后，新加的车站没有坐标，
    用这个脚本一次性补全。结果写入 extra_coords.json，与普速页面共用同一份缓存。

用法：
    # 只看缺哪些站，不实际请求
    python geocode_hsr.py --dry-run

    # 实际补全（需要 Key）
    set BAIDU_AK=你的百度服务端AK
    python geocode_hsr.py

并发与额度：
    百度对同一 AK 的并发上限是 3，本脚本复用 fetch_coords 的硬闸门
    （MAX_CONCURRENT_GEOCODE ≤ 3 + 最小间隔 GEOCODE_MIN_INTERVAL）。
    **每个车站消耗一次额度**，脚本会先打印待补数量，便于判断是否要分批。

补完之后建议跑一次数据体检（相邻站间距是否合理），因为在线地理编码
对**异地同名**的小地名容易解析错（实测「马鞍山」曾被解析到广东、
「雄安站」也会匹配到广东的同名地点）。体检见 README。
"""

import csv
import json
import os
import sys
import time

import fetch_coords as fc

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
NETWORK_FILE = os.path.join(BASE_DIR, "hsr_network.txt")
STATIONS_FILE = os.path.join(BASE_DIR, "stations.csv")


def load_network_stations(path=None):
    """按出现顺序返回 hsr_network.txt 里的全部车站（去重）。"""
    path = path or NETWORK_FILE
    out, seen = [], set()
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "：" not in line:
                continue
            for c in line.split("：", 1)[1].split("、"):
                c = c.strip()
                if c and c not in seen:
                    seen.add(c)
                    out.append(c)
    return out


def existing_coords():
    """已有坐标的站名集合（stations.csv + extra_coords.json）。"""
    have = set()
    if os.path.exists(STATIONS_FILE):
        with open(STATIONS_FILE, "r", encoding="utf-8-sig") as f:
            for row in csv.DictReader(f):
                name = (row.get("站名") or "").strip()
                if name:
                    have.add(name)
    have.update(fc.load_extra_coords().keys())
    return have


def main():
    dry = "--dry-run" in sys.argv

    stations = load_network_stations()
    have = existing_coords()
    missing = [c for c in stations if c not in have]

    print(f"通道车站 {len(stations)} 个，已有坐标 {len(stations) - len(missing)} 个，"
          f"待补 {len(missing)} 个")
    if not missing:
        print("无需补全。")
        return
    print("待补车站：" + "、".join(missing[:40]) + ("…" if len(missing) > 40 else ""))

    if dry:
        print("\n（--dry-run：只列出，不发起请求）")
        return

    fc.refresh_keys()
    if not fc.online_key_available():
        print("\n未检测到 Key，请先设置系统环境变量 BAIDU_AK 后重试。")
        sys.exit(1)

    print(f"\n并发上限 = {fc.MAX_CONCURRENT_GEOCODE}，最小间隔 = {fc.GEOCODE_MIN_INTERVAL}s")
    print(f"预计消耗 {len(missing)} 次地理编码额度\n")

    t0 = time.time()
    ok, fail = fc.autofill_online(missing)
    print(f"\n完成：成功 {len(ok)}，失败 {len(fail)}，耗时 {time.time() - t0:.0f}s")
    if fail:
        print("失败车站：" + "、".join(fail))
        print("（可稍后重跑；在线地理编码偶发超时属正常）")


if __name__ == "__main__":
    main()
