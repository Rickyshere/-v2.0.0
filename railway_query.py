# -*- coding: utf-8 -*-
"""
12306 实时车次查询模块（普速列车型）

功能：
    - 手动触发、串行限速、TTL 缓存
    - 仅查询"本网站关心的普速车次"：K / Z / T / Y 字头，或无字母字头的纯数字车次
    - 会话预热 + 复用，指数退避重试
    - 失败优雅降级（被拦截/无票/异常均返回空列表，绝不让调用方抛错）

依赖：
    - requests（已安装）
    - station_codes.csv（站名 -> 电报码对照，沿用已有数据）
"""

import threading
import time
import re
import csv
import os

import requests
from requests.packages.urllib3.exceptions import InsecureRequestWarning

# 项目内测试场景，放宽证书校验并抑制告警
requests.packages.urllib3.disable_warnings(InsecureRequestWarning)

_BASE_DIR = os.path.dirname(os.path.abspath(__file__))
_QUERY_URL = "https://kyfw.12306.cn/otn/leftTicket/queryG"
_INIT_URL = "https://kyfw.12306.cn/otn/leftTicket/init"
_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
_CACHE_TTL = 60          # 结果缓存秒数
_MIN_INTERVAL = 1.6      # 两次真实请求的最小间隔（秒）
_MAX_RETRIES = 2         # 指数退避最大重试次数


# ============================================================
# 站名 -> 电报码
# ============================================================
_STATION_CODE = {}


def _load_station_codes():
    """从 station_codes.csv 加载 站名 -> 电报码 映射。"""
    path = os.path.join(_BASE_DIR, "station_codes.csv")
    if not os.path.exists(path):
        return
    with open(path, "r", encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            name = (row.get("站名") or "").strip()
            code = (row.get("电报码") or "").strip()
            if name and code:
                _STATION_CODE.setdefault(name, code)


_load_station_codes()


def get_station_code(name):
    """返回站点名对应的 12306 电报码；无则返回 None。"""
    name = str(name).strip()
    return _STATION_CODE.get(name)


# ============================================================
# 全局串行限速器
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
            return self.last


_rate = _RateLimiter(_MIN_INTERVAL)

# ============================================================
# 会话管理与缓存
# ============================================================
_session = None
_session_lock = threading.Lock()
_cache = {}
_cache_lock = threading.Lock()

# 放宽证书校验（本地测试环境）
_SSL = {"verify": False}


def _get_session():
    """获取（或初始化）一个带预热 Cookie 的会话。"""
    global _session
    with _session_lock:
        if _session is None:
            s = requests.Session()
            s.headers.update({"User-Agent": _UA})
            # 预热：先访问 init 拿 JSESSIONID / BIGip 等 Cookie
            try:
                with _rate_locked():
                    s.get(_INIT_URL, timeout=10, **_SSL)
            except Exception:
                pass
            _session = s
        return _session


def _rate_locked():
    """在限速锁内执行用户代码。"""
    class _Ctx:
        def __enter__(self):
            _rate.acquire()
            return self
        def __exit__(self, *a):
            return False
    return _Ctx()


def _cache_get(key):
    with _cache_lock:
        item = _cache.get(key)
        if item and time.time() - item[0] < _CACHE_TTL:
            return item[1]
        if item:
            _cache.pop(key, None)
        return None


def _cache_put(key, value):
    with _cache_lock:
        _cache[key] = (time.time(), value)
        # 简单清理过期项，避免无限增长
        now = time.time()
        for k in list(_cache.keys()):
            if now - _cache[k][0] > _CACHE_TTL:
                _cache.pop(k, None)


# ============================================================
# 票价解析（直接来自 yp_info 字段，**不需要额外请求**）
# ============================================================
# yp_info 形如 "3028350000404555000010156500001015653175"，
# 每 10 个字符一组：席别(1) + 价格(5 位，单位「角」) + 尾数(4)。
# 实测 1461 次（北京→上海）：3→283.5(硬卧) 4→455.5(软卧) 1→156.5(硬座)，
# 与 12306 官方 queryTicketPrice 接口返回的价格完全一致。
_INDEX_YP_INFO = 39

# 席别代码 -> 中文名
_PRICE_SEAT_NAMES = {
    "1": "硬座",
    "2": "软座",
    "3": "硬卧",
    "4": "软卧",
    "6": "高级软卧",
    "9": "商务座",
    "P": "特等座",
    "M": "一等座",
    "O": "二等座",
    "W": "无座",
}


def parse_prices(yp_info):
    """
    从 yp_info 解出 {席别: 价格(元)}。解析失败一律返回空 dict，绝不抛异常。

    同一席别可能出现多组（如硬卧上/中/下铺），这里取**最低价**。
    """
    out = {}
    s = (yp_info or "").strip()
    if not s:
        return out
    for i in range(0, len(s) - 9, 10):
        group = s[i:i + 10]
        seat, num = group[0], group[1:6]
        if not num.isdigit():
            continue
        name = _PRICE_SEAT_NAMES.get(seat)
        if not name:
            continue
        price = int(num) / 10.0
        if price <= 0:
            continue
        if name not in out or price < out[name]:
            out[name] = round(price, 1)
    return out


# 前端关心的席别顺序
_PRICE_ORDER = ["无座", "硬座", "硬卧", "软卧", "二等座", "一等座", "软座", "商务座", "特等座", "高级软卧"]


def pick_prices(prices):
    """
    从完整票价里挑出展示用的席别并排序。

    补充规则：**无座票价与硬座（普速）/二等座（动车）相同**，是铁路的固定规则；
    而 yp_info 里不含无座，所以这里按该规则推导出来，并标注 derived=True，
    前端会显示成「无座(同硬座价)」以免误解。
    """
    out = []
    derived_wz = None
    if "无座" not in prices:
        base = prices.get("硬座") or prices.get("二等座")
        if base:
            derived_wz = base
    for name in _PRICE_ORDER:
        if name == "无座":
            if name in prices:
                out.append({"seat": name, "price": prices[name], "derived": False})
            elif derived_wz:
                out.append({"seat": name, "price": derived_wz, "derived": True})
        elif name in prices:
            out.append({"seat": name, "price": prices[name], "derived": False})
    return out


# ============================================================
# 车次筛选：K / Z / T / Y 字头 或 纯数字
# ============================================================
_TRAIN_PATTERN = re.compile(r"^[KZTYGDC]?\d{1,4}$")

_SLOW_PREFIX = "KZTY"   # 普速
_HSR_PREFIX = "GDC"     # 高速：G 高铁 / D 动车 / C 城际


def _keep_train(no, kind="slow"):
    """
    判断车次是否属于目标类别。

    kind:
        "slow" —— 普速：K / Z / T / Y 字头，或无字母的纯数字车次
        "hsr"  —— 高速：G / D / C 字头
        "all"  —— 全部
    """
    no = (no or "").strip()
    if not _TRAIN_PATTERN.match(no):
        return False
    head = no[0] if no[0].isalpha() else ""
    if kind == "all":
        return True
    if kind == "hsr":
        return head in _HSR_PREFIX
    return head in _SLOW_PREFIX or head == ""


# ============================================================
# 解析 queryG 返回的 result（每行按 | 分隔）
# ============================================================
_INDEX_SECRET = 0              # 密钥
_INDEX_TRAIN_NO = 3            # 车次号
_INDEX_FROM_CODE = 4           # 出发站代码
_INDEX_TO_CODE = 5             # 到达站代码
_INDEX_DEPA = 8                # 出发时间 HH:MM
_INDEX_ARRI = 9                # 到达时间 HH:MM
_INDEX_DURA = 10               # 历时
_INDEX_CANBOOK = 11            # Y/N 是否可预订


def _parse_result(result_rows, code_map, with_price=False, kind="slow"):
    """
    把 queryG 的 data.result（list[str]）解析为结构化车次列表。

    with_price=True 时额外解出票价（来自 yp_info，不产生额外请求）。
    """
    out = []
    for row in result_rows:
        fields = row.split("|")
        if len(fields) < 12:
            continue
        no = fields[_INDEX_TRAIN_NO].strip()
        if not _keep_train(no, kind):
            continue
        depa = fields[_INDEX_DEPA]
        arri = fields[_INDEX_ARRI]
        dura = fields[_INDEX_DURA]
        # 12306 对「当日不开行」的车次会返回 24:00 / 99:59 这种占位值，
        # 直接列出来会让人误以为有车，这里过滤掉。
        if depa == "24:00" or arri == "24:00" or dura == "99:59":
            continue
        can = fields[_INDEX_CANBOOK].upper() == "Y"
        from_code = fields[_INDEX_FROM_CODE]
        to_code = fields[_INDEX_TO_CODE]
        item = {
            "train_no": no,
            "from": code_map.get(from_code) or from_code,
            "to": code_map.get(to_code) or to_code,
            "depart": depa,
            "arrive": arri,
            "duration": dura,
            "can_book": can,
        }
        if with_price:
            yp = fields[_INDEX_YP_INFO] if len(fields) > _INDEX_YP_INFO else ""
            item["prices"] = pick_prices(parse_prices(yp))
        out.append(item)
    # 按出发时间排序
    out.sort(key=lambda x: x["depart"])
    return out


# ============================================================
# 对外主入口：查询普速车次（带缓存、串行限速、指数退避、降级）
# ============================================================

def query_trains(from_name, to_name, date, timeout=12, with_price=False, kind="slow"):
    """
    查询 from -> to 在 date 当天开行的车次。

    参数：
        from_name: 出发站中文名（须能在 station_codes 中找到电报码）
        to_name:   到达站中文名
        date:      出发日期，格式 YYYY-MM-DD
        with_price: 是否解析票价（来自 yp_info，**不产生额外请求**）
        kind:      "slow" 普速（K/Z/T/Y/纯数字，默认）
                   "hsr"  高速（G/D/C）
                   "all"  全部

    返回：
        {"ok": bool, "reason": str, "trains": [ {车次...}, ... ], "from": 站名, "to": 站名}

    任何失败都会降级返回 ok=False 的空列表，绝不抛异常。
    """
    from_code = get_station_code(from_name)
    to_code = get_station_code(to_name)

    if not from_code or not to_code:
        return {
            "ok": False,
            "reason": f"出发站「{from_name}」或到达站「{to_name}」无有效电报码",
            "trains": [],
        }

    cache_key = f"{from_code}|{to_code}|{date}|{kind}|{'p' if with_price else ''}"
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached

    result = None
    last_err = "未知错误"

    for attempt in range(_MAX_RETRIES):
        try:
            s = _get_session()
            params = {
                "leftTicketDTO.train_date": date,
                "leftTicketDTO.from_station": from_code,
                "leftTicketDTO.to_station": to_code,
                "purpose_codes": "ADULT",
            }
            with _rate_locked():
                resp = s.get(
                    _QUERY_URL, params=params, timeout=timeout,
                    headers={"Referer": _INIT_URL}, **_SSL,
                )
            data = resp.json()

            if data.get("status") is True and data.get("data"):
                code_map = data["data"].get("map", {})
                rows = data["data"].get("result", [])
                trains = _parse_result(rows, code_map, with_price=with_price, kind=kind)
                result = {
                    "ok": True,
                    "reason": "",
                    "trains": trains,
                    "from": from_name,
                    "to": to_name,
                }
                break
            else:
                # status:false 或 data 为空 —— 可能是会话失效，缓冲后重试
                last_err = "返回空或未成功（可能需要重新建立会话）"
                _reset_session()
        except requests.exceptions.Timeout:
            last_err = "请求超时"
        except Exception as e:
            last_err = f"请求异常：{e}"

        if attempt < _MAX_RETRIES - 1:
            time.sleep(2 ** attempt)  # 1s、2s 退避

    if result is None:
        result = {
            "ok": False,
            "reason": last_err,
            "trains": [],
            "from": from_name,
            "to": to_name,
        }

    _cache_put(cache_key, result)
    return result


def _reset_session():
    """会话失效时清空，下次自动重建并预热。"""
    global _session
    with _session_lock:
        _session = None


# 供外部判断是否有该站电报码
def has_code(name):
    return get_station_code(name) is not None


if __name__ == "__main__":
    import datetime
    d = (datetime.date.today() + datetime.timedelta(days=3)).isoformat()
    for f, t in [("北京", "上海"), ("广州", "衡阳")]:
        r = query_trains(f, t, d)
        print(f"===== {f} -> {t} ({d}) =====")
        print(f"ok={r['ok']} reason={r['reason']} 共{len(r['trains'])}趟")
        for tr in r["trains"][:8]:
            print(f"  {tr['train_no']:8} {tr['depart']}-{tr['arrive']} {tr['duration']} "
                  f"can_book={tr['can_book']}")
