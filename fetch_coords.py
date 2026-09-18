# -*- coding: utf-8 -*-
"""
站点坐标自动获取 / 补全工具
==========================

用途：
    当 main_railway_line.txt 里新增的铁路线路中，存在没有坐标数据的站点时
    （比如新加的「漳龙铁路」里的龙岩站），可运行本脚本自动为这些缺失坐标
    的站点补全经纬度，并把结果写入 extra_coords.json 缓存。

    之后 upgrade_data.py 生成数据时会自动读取 extra_coords.json，
    从而画出正确的线路走向，无需再手工维护坐标。

坐标来源（按优先级）：
    1. 缓存：extra_coords.json 中已有的坐标，直接复用。
    2. 在线地理编码：若配置了 API Key 则调用 高德 / 百度 地图接口自动爬取；
       配置了国内 Key 时**不再兜底** OpenStreetMap Nominatim（国内常连不通，
       白等约 10 秒）；未配置任何 Key 时才尝试它。
    3. 交互输入：以上均不可用时，逐个列出缺坐标的站点，由你手动输入
       经度、纬度，并写入缓存。

Key 配置（本项目不硬编码任何 Key）：
    推荐设为**系统环境变量**（Windows 用户级即可，无需管理员）：
        BAIDU_AK  百度「服务端 AK」（浏览器端 AK 调地理编码会返回 status=240）
        AMAP_KEY  高德 Web 服务 Key（可选，作为百度的补充来源）
        BAIDU_SK  百度 SK（可选，配了就走 sn 签名，规避 IP 白名单）
    设置后**新开的进程**才会读到；服务已运行时可用 refresh_keys() 重新读取。

并发限制：
    百度对同一 AK 的并发上限为 **3**，超出会被限流。本模块已做硬约束：
      - MAX_CONCURRENT_GEOCODE 上限固定为 3（可用环境变量 BAIDU_MAX_CONCURRENCY
        调低，但无法调高）
      - 所有在线请求都经过 _geocode_slot() 的并发/频率闸门
      - autofill_online() 的线程池大小同样不超过该上限
    两次请求之间的最小间隔由 BAIDU_MIN_INTERVAL 控制（默认 0.34 秒）。

网页端：
    main.py 的「线路管理 → 保存并重新生成」在勾选「启用在线地理坐标补全」后，
    会调用本模块的 autofill_online()（纯在线、无 input()，不会阻塞服务），
    且**仅在确实存在缺坐标站点时才发起请求**（在线额度有限）。

用法：
    python fetch_coords.py                         # 只列出当前所有缺坐标的站点
    python fetch_coords.py --fix                   # 对缺坐标站自动在线爬取 + 交互补全
    python fetch_coords.py --fix --amap-key=你的高德Key   # 用高德自动爬（推荐，国内可用）
    python fetch_coords.py --file=main_railway_line.txt  # 指定线路文件

说明：
    - 结果会写入该项目目录下的 extra_coords.json 永久保存。
    - 补全完成后请再运行一次  python upgrade_data.py  重新生成路网。
"""

import csv
import contextlib
import json
import os
import re
import sys
import threading
import time
import urllib.parse
import urllib.request

# ============================================================
# 配置
# ============================================================
LINE_FILE = "main_railway_line.txt"
EXTRA_COORDS_FILE = "extra_coords.json"

# ---- 在线地理编码的并发与频率限制 ----
# 百度地图开放平台对同一 AK 的**并发上限为 3**，超过会直接报错/限流。
# 这里做成硬上限：无论外部怎么配置，都不会超过 3。
MAX_CONCURRENT_GEOCODE = max(
    1, min(int(os.environ.get("BAIDU_MAX_CONCURRENCY", "3") or 3), 3)
)
# 两次在线请求之间的最小间隔（秒）。并发 3 时约合 3 QPS。
GEOCODE_MIN_INTERVAL = float(os.environ.get("BAIDU_MIN_INTERVAL", "0.34") or 0.34)

# 并发闸门：保证同时发往在线地理编码的请求数 <= MAX_CONCURRENT_GEOCODE
_GEOCODE_SEM = threading.Semaphore(MAX_CONCURRENT_GEOCODE)
# 频率闸门：保证两次请求之间有最小间隔
_GEOCODE_RATE_LOCK = threading.Lock()
_GEOCODE_LAST_TS = [0.0]


@contextlib.contextmanager
def _geocode_slot():
    """在线地理编码的并发 / 频率闸门。

    进入前需获取一个并发名额（最多 MAX_CONCURRENT_GEOCODE 个），
    并保证距上次请求已过 GEOCODE_MIN_INTERVAL。退出时释放名额。
    """
    _GEOCODE_SEM.acquire()
    try:
        with _GEOCODE_RATE_LOCK:
            wait = _GEOCODE_LAST_TS[0] + GEOCODE_MIN_INTERVAL - time.time()
            if wait > 0:
                time.sleep(wait)
            _GEOCODE_LAST_TS[0] = time.time()
        yield
    finally:
        _GEOCODE_SEM.release()

# 在线地理编码 API Key（选填）：
#   - 高德（推荐，国内可用）：到 https://lbs.amap.com/ 注册，创建"Web服务" key
#   - 百度：到 https://lbsyun.baidu.com/ 申请「服务端 AK」以调用 Web 服务
#        地理编码接口（注意：若只申请「浏览器端 AK」调此接口会返回 status=240）
#
# **安全说明**：为安全的开源发布，本项目默认**不硬编码任何真实 Key**，
# 请勿把你自己的 Key 写死在代码里并提交到仓库！推荐用下面任一方式配：
#   1) 环境变量：设置 AMAP_KEY / BAIDU_AK / BAIDU_SK
#   2) 命令行：  python fetch_coords.py --fix --amap-key=你的key --baidu-ak=你的key
#   3) 删除下面的 os.environ 行，直接改成字面量（仅本地用，勿提交）
AMAP_KEY = os.environ.get("AMAP_KEY", "")
BAIDU_AK = os.environ.get("BAIDU_AK", "")
# 百度「服务端 AK」地理编码接口。若后台配了 IP 白名单导致本机请求失败
# （返回 240/ip 校验失败），可改用 AK+SK 的 sn 签名方式规避。
BAIDU_SK = os.environ.get("BAIDU_SK", "")  # 可选：配置后可启用 sn 签名
# 百度地理编码请求时携带的 Referer（服务端 AK 通常用不到，留空即可）
BAIDU_REFERER = ""
# 是否启用按需 city 兜底（先不带 city 试，失败再补充 city）
BAIDU_TRY_CITY_FALLBACK = True

# 请求间隔（秒），尊重各服务商访问频率限制
REQUEST_DELAY = 1.0


def norm_name(name):
    """规整站名：去掉尾部"站"字。"""
    s = str(name).strip()
    return s[:-1] if s.endswith("站") else s


def parse_railway_stations():
    """解析线路文件，返回所有出现的站点集合 {规整名}。"""
    stations = set()
    pending = None
    with open(LINE_FILE, "r", encoding="utf-8") as f:
        for raw in f.read().splitlines():
            raw = raw.strip()
            if not raw:
                pending = None
                continue
            if raw.startswith("【"):
                end = raw.find("】")
                if end != -1:
                    pending = raw[1:end]
                continue
            for part in raw.split("、"):
                m = re.match(r"(.+?)（(.+?)）", part.strip())
                if not m:
                    continue
                name = m.group(1).strip()
                if not name:
                    continue
                stations.add(norm_name(name))
    return stations


def load_stations_coords():
    """读取当前 stations.csv（若存在）的坐标，返回 {规整名: (lon,lat)}。"""
    coords = {}
    try:
        with open("stations.csv", "r", encoding="utf-8-sig") as f:
            for row in csv.DictReader(f):
                n = norm_name(row["站名"])
                try:
                    coords[n] = (float(row["经度"]), float(row["纬度"]))
                except (ValueError, KeyError):
                    continue
    except FileNotFoundError:
        pass
    return coords


def load_fallback():
    """导入 upgrade_data 的内置城市坐标兜底表。"""
    try:
        import upgrade_data
        return upgrade_data.CITY_FALLBACK
    except Exception:
        return {}


def load_extra_coords():
    """读取 extra_coords.json 缓存，返回 {规整名: [lon, lat]}。"""
    try:
        with open(EXTRA_COORDS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_extra_coords(coords):
    """将坐标补全结果写入 extra_coords.json。"""
    with open(EXTRA_COORDS_FILE, "w", encoding="utf-8") as f:
        json.dump(coords, f, ensure_ascii=False, indent=2)
    print(f"  ✓ 已保存 {len(coords)} 条坐标补全结果 -> {EXTRA_COORDS_FILE}")


# ============================================================
# 在线地理编码：多来源回退
# ============================================================
def _http_get_json(url, timeout=15, headers=None):
    req = urllib.request.Request(url, headers={
        "User-Agent": "RailwayPlanner/1.0 (coordinates autofill)"
    })
    if headers:
        req.headers.update(headers)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def geocode_nominatim(name):
    """OpenStreetMap Nominatim（免费，境外可访问；国内可能超时）。"""
    q = urllib.parse.quote(f"{name}站")
    url = ("https://nominatim.openstreetmap.org/search?"
           f"q={q}&format=json&countrycodes=cn&limit=1&accept-language=zh")
    d = _http_get_json(url)
    if d:
        return float(d[0]["lon"]), float(d[0]["lat"])
    return None


def geocode_amap(name, key=None):
    """高德地理编码（国内可用，需 key）。返回 (lon, lat) 或 None。

    注意：key 在**调用时**才从模块变量读取（而不是写成默认参数），
    否则默认值会在 import 时被绑定成空串，导致之后设置的 Key 永远不生效。
    """
    key = key or AMAP_KEY
    if not key:
        return None
    addr = urllib.parse.quote(f"{name}站")
    url = (f"https://restapi.amap.com/v3/geocode/geo?"
           f"address={addr}&key={key}&output=json")
    with _geocode_slot():
        d = _http_get_json(url)
    if d.get("status") == "1" and d.get("geocodes"):
        loc = d["geocodes"][0].get("location")
        if loc:
            lng, lat = loc.split(",")
            return float(lng), float(lat)
    return None


def _baidu_request(query_params, ak=None, sk=None):
    """
    构造百度 Web 服务 API 请求：
      - 若配置了 sk，则用 AK+SK 做 sn 签名（最稳，规避 IP/Referer 校验）
      - 否则用纯 AK 请求
    参数会按百度官方要求做 urlencode 并排序。
    返回解析后的 dict（含 status / result 等）。

    ak / sk 在**调用时**解析（默认参数会在 import 时被绑定成空串）。
    """
    ak = ak or BAIDU_AK
    sk = sk or BAIDU_SK
    # 排序 + urlencode（百度 sn 要求：参数按字母排序，各值做 URL 编码）
    keys = sorted(query_params.keys())
    query_string = "&".join(
        f"{urllib.parse.quote(k, safe='')}={urllib.parse.quote(str(query_params[k]), safe='')}"
        for k in keys
    )
    headers = {}
    if BAIDU_REFERER:
        headers["Referer"] = BAIDU_REFERER
    if sk:
        import hashlib
        # 百度 sn 算法：对 "路径 + ?查询串 + sk" 整体做 md5
        raw = "/geocoding/v3/?" + query_string + sk
        sn = hashlib.md5(raw.encode("utf-8")).hexdigest()
        full = (f"https://api.map.baidu.com/geocoding/v3/?{query_string}"
                f"&sn={sn}&ak={ak}")
    else:
        full = (f"https://api.map.baidu.com/geocoding/v3/?{query_string}"
                f"&ak={ak}")
    # 并发 / 频率闸门：同一 AK 并发不超过 MAX_CONCURRENT_GEOCODE（3）
    with _geocode_slot():
        return _http_get_json(full, headers=headers or None)


def geocode_baidu(name, ak=None, city=None):
    """百度地理编码（服务端 AK）。返回 (lon, lat) 或 None。

    说明：
        - 若传 city（站可达、站所在的县/市），会显著提高县级小站的命中率。
        - 策略：先不带 city 请求；若返回无可解析结果，再带 city 重试一次。
        - 若配置了 BAIDU_SK，将改用 AK+SK 的 sn 签名方式（规避 IP 校验）。
        - ak 在**调用时**解析（默认参数会在 import 时被绑定成空串）。
    """
    ak = ak or BAIDU_AK
    if not ak:
        return None

    def _try(c):
        params = {"address": f"{name}站", "output": "json"}
        if c:
            params["city"] = c
        d = _baidu_request(params, ak=ak)
        if d is not None and d.get("status") == 0 and d.get("result"):
            loc = d["result"].get("location")
            if loc:
                return float(loc["lng"]), float(loc["lat"])
        return None

    r = _try(None)
    if r:
        return r
    if city and BAIDU_TRY_CITY_FALLBACK:
        r = _try(city)
        if r:
            return r
    return None


def refresh_keys():
    """重新从环境变量读取 Key。

    便于「服务已在运行 → 用户新设置了系统变量 → 再触发补全」的场景生效。
    （各 geocode_* 函数都在调用时才解析 Key，因此刷新后立即有效。）
    """
    global AMAP_KEY, BAIDU_AK, BAIDU_SK
    AMAP_KEY = os.environ.get("AMAP_KEY", "")
    BAIDU_AK = os.environ.get("BAIDU_AK", "")
    BAIDU_SK = os.environ.get("BAIDU_SK", "")


def online_key_available():
    """是否配置了任一国内在线地理编码 Key（高德 / 百度）。"""
    refresh_keys()
    return bool(AMAP_KEY or BAIDU_AK)


def online_provider_name():
    """返回当前可用的在线地理编码来源名（百度 / 高德 / None）。"""
    refresh_keys()
    if BAIDU_AK:
        return "百度"
    if AMAP_KEY:
        return "高德"
    return None


def geocode_online(name, allow_nominatim=None):
    """依次尝试各在线来源，返回 (lon, lat) 或 None。

    allow_nominatim:
        None（默认）→ 自动判断：一旦配置了国内 Key（高德/百度），就不再兜底
        Nominatim。因为 Nominatim 在国内常连不通（实测约 10 秒后失败），
        白白拖慢每一次补全。
    """
    if allow_nominatim is None:
        allow_nominatim = not (AMAP_KEY or BAIDU_AK)

    chain = [(geocode_amap, "高德"), (geocode_baidu, "百度")]
    if allow_nominatim:
        chain.append((geocode_nominatim, "Nominatim"))

    for fn, tag in chain:
        try:
            r = fn(name)
        except Exception:
            r = None
        if r:
            print(f"  [在线:{tag}] 命中 {name} -> {r[0]:.4f}, {r[1]:.4f}")
            return r
        time.sleep(REQUEST_DELAY)
    return None


# ============================================================
# 主流程
# ============================================================
def collect_missing(use_extra=False):
    """统计所有缺坐标的站点（排除已在缓存/已在线补全的）。返回列表。"""
    stations = parse_railway_stations()
    known = load_stations_coords()
    known.update(load_fallback())
    if use_extra:
        extra = load_extra_coords()
        known.update({k: tuple(v) for k, v in extra.items()})

    missing = []
    for s in sorted(stations):
        has = known.get(s)
        if not has or has == (0.0, 0.0) or abs(has[0]) < 0.5:
            missing.append(s)
    return missing


def auto_fetch(missing):
    """对缺失站点在线爬取，返回 (成功dict, 未成功list)。"""
    result = {}
    remaining = []
    for s in missing:
        r = geocode_online(s)
        if r:
            result[s] = r
        else:
            remaining.append(s)
    return result, remaining


def interactive_fill(names, extra):
    """交互式让用户为仍缺坐标的站点输入经纬度。"""
    filled = {}
    for s in names:
        print(f"\n  >>> 站点「{s}」暂无可用坐标。")
        print("      请输入 经度,纬度 （例如 117.42,25.08）；直接回车跳过。")
        ans = input("      经度,纬度: ").strip()
        if not ans:
            print("      已跳过", s)
            continue
        parts = ans.replace("，", ",").split(",")
        try:
            lon, lat = float(parts[0].strip()), float(parts[1].strip())
        except (ValueError, IndexError):
            print("      格式有误，已跳过", s)
            continue
        filled[s] = (lon, lat)
        extra[s] = [lon, lat]
    return filled, extra


def batch_fix(names):
    """对一批缺坐标站名就地补全：在线爬取 + 交互输入，并保存 extra_coords.json。

    由 upgrade_data.py 在生成数据前调用。返回补全成功的 {站: (lon,lat)}。
    """
    extra = load_extra_coords()
    ok, fail = auto_fetch(names)
    for s, c in ok.items():
        extra[s] = [c[0], c[1]]
    if fail:
        filled, extra = interactive_fill(fail, extra)
        ok.update(filled)
    if extra:
        save_extra_coords(extra)
    return ok


def autofill_online(names):
    """纯在线补全（**不含交互输入**），供 Web 接口调用。

    与 batch_fix 的关键区别：绝不调用 input()，因此不会阻塞服务进程
    （batch_fix 内部会走到 interactive_fill 的 input()，只能用于命令行）。

    并发：使用不超过 MAX_CONCURRENT_GEOCODE（默认 3）的线程池。
    百度对同一 AK 的**并发上限就是 3**，超出会被限流，故此处做了硬约束；
    真正发请求时还会再经过 _geocode_slot() 的并发/频率闸门。

    调用方应遵循「只在确有缺坐标站点时才调用」的原则（在线额度有限）。

    返回 (ok_dict, failed_list)：
        ok_dict     {站名: (lon, lat)}，已写入 extra_coords.json
        failed_list 仍未能补全的站名列表
    """
    refresh_keys()
    names = list(names or [])
    if not names:
        return {}, []

    extra = load_extra_coords()

    # 并发度：不超过 AK 的并发上限，也不超过待补站数
    workers = max(1, min(MAX_CONCURRENT_GEOCODE, len(names)))
    if workers == 1:
        results = [(s, geocode_online(s)) for s in names]
    else:
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=workers) as pool:
            results = list(pool.map(lambda s: (s, geocode_online(s)), names))

    ok, fail = {}, []
    for s, r in results:
        if r:
            ok[s] = r
            extra[s] = [r[0], r[1]]
        else:
            fail.append(s)

    if ok:
        save_extra_coords(extra)
    return ok, fail


def main():
    args = [a for a in sys.argv[1:]]
    do_fix = "--fix" in args

    global AMAP_KEY, BAIDU_AK
    for arg in args:
        if arg.startswith("--amap-key="):
            AMAP_KEY = arg.split("=", 1)[1]
        elif arg.startswith("--baidu-ak="):
            BAIDU_AK = arg.split("=", 1)[1]

    print("=" * 60)
    print("站点坐标自动获取 / 补全")
    print("=" * 60)

    extra = load_extra_coords()
    missing = collect_missing(use_extra=True)
    print(f"\n缺坐标的站点（{len(missing)} 个）：")
    if not missing:
        print("  （无）所有站点坐标齐全，无需处理。")
        return
    print("  ", "、".join(missing))

    if not do_fix:
        print("\n提示：运行  python fetch_coords.py --fix  即可自动补全以上坐标。")
        print("      推荐国内网络使用  --fix --amap-key=你的高德Key 自动爬取。")
        return

    # 1) 在线爬取
    print("\n[1/3] 尝试在线地理编码自动爬取…")
    online_ok, online_fail = auto_fetch(missing)
    print(f"  在线命中 {len(online_ok)} 个，仍需处理 {len(online_fail)} 个。")
    if online_ok:
        for s, c in online_ok.items():
            extra[s] = [c[0], c[1]]

    # 2) 交互补全剩余
    all_done = dict(online_ok)
    if online_fail:
        print("\n[2/3] 以下站点需手动输入坐标：")
        filled, extra = interactive_fill(online_fail, extra)
        all_done.update(filled)

    # 3) 保存
    if extra:
        save_extra_coords(extra)

    if not all_done:
        print("\n⚠ 没有补全任何坐标。请配置在线 Key 或手动输入。")
        return

    print("\n完成！本次已补全坐标：")
    for s, c in all_done.items():
        print(f"    {s}: {c[0]:.4f}, {c[1]:.4f}")
    print(f"\n现在请运行：  python upgrade_data.py   重新生成路网并得到正确图示。")


if __name__ == "__main__":
    main()
