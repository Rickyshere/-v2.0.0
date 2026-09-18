# -*- coding: utf-8 -*-
"""
中转方案推荐（普速 / 高速通用）

思路：
    1. 先查 A→B 直达车次；有直达就直接推荐直达。
    2. 无论有没有直达，都用**路网图**算出若干候选枢纽城市
       （取多方案最短路径上的中间城市，按出现顺序去重）。
    3. 对每个候选枢纽 H，查 A→H 与 H→B 两段车次，
       只保留**接续时间 >= MIN_TRANSFER_MINUTES**（默认 60 分钟）的配对。
    4. 按「总耗时」排序，输出前若干条推荐。

接续时间的算法：
    到达时刻 A1 = 第一程发车时刻 + 第一程历时（分钟，可超过 1440 表示跨天）
    第二程发车时刻 D2 = 第二程发车时刻（分钟）
    接续时间 = D2 - A1；若为负，再试「次日出发」D2 + 1440 - A1。
    只有 >= 60 分钟才算有效接续。

为什么必须支持次日接续：
    普速车常跨天（如 北京→南京 的 1461 次 11:59 发、次日 02:56 到），
    若只允许当日接续，几乎所有普速中转都会被判为"不可行"。
    因此对第二程会**同时查当日与次日**两天的车次。
"""

import datetime

import railway_query as rq

MIN_TRANSFER_MINUTES = 60     # 接续时间下限（分钟）
MAX_HUBS = 3                  # 最多考察几个候选枢纽


def _hhmm_to_min(s):
    """'18:46' -> 1126；解析失败返回 None。"""
    try:
        parts = str(s).strip().split(":")
        if len(parts) < 2:
            return None
        return int(parts[0]) * 60 + int(parts[1])
    except (ValueError, TypeError):
        return None


def _duration_to_min(s):
    """历时 '18:46' -> 1126 分钟；支持 '1天05:56' 这种写法。"""
    s = str(s or "").strip()
    days = 0
    if "天" in s:
        head, s = s.split("天", 1)
        try:
            days = int(head)
        except ValueError:
            days = 0
    m = _hhmm_to_min(s)
    if m is None:
        return None
    return days * 1440 + m


def _next_date(date):
    """'2026-09-25' -> '2026-09-26'；解析失败返回 None。"""
    try:
        return (datetime.date.fromisoformat(str(date).strip())
                + datetime.timedelta(days=1)).isoformat()
    except (ValueError, TypeError):
        return None


def _fmt_min(m):
    """分钟 -> 'HH:MM'（超过 24 小时用 +1天 表示）。"""
    if m is None:
        return ""
    d, rem = divmod(int(m), 1440)
    tag = f"+{d}天" if d else ""
    return f"{rem // 60:02d}:{rem % 60:02d}{tag}"


def _candidate_hubs(planner, start, end, max_hubs):
    """
    用路网图找候选枢纽城市。

    取每条方案的 **transfers（换乘大站）**，而不是 path 里所有中间站 ——
    路径上的中间站常常是「广阳」「静海」这类小站，在那里换乘没有意义；
    transfers 是规划器已经筛过的特等/一等枢纽。
    """
    hubs = []
    try:
        routes = planner.find_routes(start, end, k=3)
    except Exception:
        return hubs
    for r in routes:
        for c in (r.get("transfers") or r.get("transfer_stations") or []):
            if c not in hubs:
                hubs.append(c)
    # 兜底：没有 transfers 信息时退回路径中间站
    if not hubs:
        for r in routes:
            for c in (r.get("path") or [])[1:-1]:
                if c not in hubs:
                    hubs.append(c)
    return hubs[:max_hubs]


def recommend(planner, start, end, date, kind="slow",
              max_hubs=MAX_HUBS, min_transfer_min=MIN_TRANSFER_MINUTES,
              with_price=False, max_results=5):
    """
    给出推荐方案。

    参数：
        planner: 路网规划器模块（route_planner 或 hsr_planner）
        start/end: 起点 / 终点城市名
        date: 出发日期 YYYY-MM-DD
        kind: "slow" / "hsr"
        min_transfer_min: 接续时间下限（分钟），默认 60

    返回：
        {"ok":bool, "from","to","date","kind",
         "direct":[{车次..., "advice"}],
         "transfers":[{hub, leg1, leg2, transfer_minutes, total_minutes, advice}],
         "reason":str}
    """
    out = {
        "ok": True, "from": start, "to": end, "date": date, "kind": kind,
        "direct": [], "transfers": [], "reason": "",
        "min_transfer_minutes": min_transfer_min,
    }

    if not rq.get_station_code(start) or not rq.get_station_code(end):
        out["ok"] = False
        out["reason"] = f"出发站「{start}」或到达站「{end}」无有效电报码"
        return out

    # ---- 1) 直达 ----
    d = rq.query_trains(start, end, date, with_price=with_price, kind=kind)
    if d.get("ok") and d.get("trains"):
        for t in d["trains"][:3]:
            dur = _duration_to_min(t.get("duration"))
            item = dict(t)
            item["total_minutes"] = dur
            item["advice"] = "直达，无需换乘"
            out["direct"].append(item)

    # ---- 2) 中转 ----
    hubs = _candidate_hubs(planner, start, end, max_hubs)
    if not hubs:
        if not out["direct"]:
            out["ok"] = False
            out["reason"] = out["reason"] or "未找到可用路网路径，无法推荐中转枢纽"
        return out

    for h in hubs:
        if not rq.get_station_code(h):
            continue
        r1 = rq.query_trains(start, h, date, with_price=with_price, kind=kind)
        if not (r1.get("ok") and r1.get("trains")):
            continue
        # 第二程同时查当日与次日（普速常跨天）
        r2_list = []
        for d_off, d_str in ((0, date), (1440, _next_date(date))):
            if d_str is None:
                continue
            rr = rq.query_trains(h, end, d_str, with_price=with_price, kind=kind)
            if rr.get("ok") and rr.get("trains"):
                r2_list.append((d_off, d_str, rr["trains"]))
        if not r2_list:
            continue

        for t1 in r1.get("trains", []):
            a1 = _hhmm_to_min(t1.get("depart"))
            l1 = _duration_to_min(t1.get("duration"))
            if a1 is None or l1 is None:
                continue
            arr1 = a1 + l1                      # 到达时刻（分钟，可 >1440）
            for day_off, d_str, t2s in r2_list:
                for t2 in t2s:
                    d2 = _hhmm_to_min(t2.get("depart"))
                    l2 = _duration_to_min(t2.get("duration"))
                    if d2 is None or l2 is None:
                        continue
                    d2_abs = d2 + day_off
                    gap = d2_abs - arr1
                    if gap < min_transfer_min:
                        continue
                    item = {
                        "hub": h,
                        "leg1": t1,
                        "leg2": t2,
                        "leg2_date": d_str,
                        "next_day": day_off > 0,
                        "transfer_minutes": gap,
                        "total_minutes": (d2_abs + l2) - a1,
                        "advice": (f"在「{h}」换乘，接续 {gap // 60} 小时 {gap % 60:02d} 分"
                                   + ("（第二程次日出发）" if day_off > 0 else "")),
                    }
                    out["transfers"].append(item)

    out["transfers"].sort(key=lambda x: x["total_minutes"])
    out["transfers"] = out["transfers"][:max_results]

    if not out["direct"] and not out["transfers"]:
        out["ok"] = False
        out["reason"] = (f"当日无直达车次，且在候选枢纽"
                         f"（{'、'.join(hubs)}）也未找到接续时间"
                         f"≥{min_transfer_min}分钟的中转组合")
    return out
