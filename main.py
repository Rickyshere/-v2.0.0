# -*- coding: utf-8 -*-
"""
普速铁路中转方案网站 - FastAPI 后端服务

运行方式：
    uvicorn main:app --reload
    或
    python main.py

接口：
    GET /api/route?start=北京&end=上海

依赖：
    - route_planner（本项目路径规划模块）
    - fastapi / uvicorn（需提前安装：pip install fastapi uvicorn）
"""

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

import os
import io
import threading
import contextlib

import route_planner
from route_planner import RoutePlannerError
import railway_query
import ctrip_query  # 携程：查询车次真实经停站（供 /api/train/stops 使用）
import upgrade_data  # 由 main_railway_line.txt 重新生成路网数据
import fetch_coords  # 站点坐标在线补全（百度/高德，Key 走系统环境变量）
import hsr_planner   # 八纵八横高速铁路通道（城市级）规划器
import transfer_advisor  # 中转方案推荐（接续时间 >= 1 小时）

# 本文件所在目录（用于定位 index.html / 线路清单 / 数据文件）
_BASE_DIR = os.path.dirname(os.path.abspath(__file__))
LINE_FILE = os.path.join(_BASE_DIR, "main_railway_line.txt")
STATIONS_FILE = os.path.join(_BASE_DIR, "stations.csv")
LINES_FILE = os.path.join(_BASE_DIR, "lines.csv")

# 防止并发触发"重新生成"时互相覆盖，用一把锁串行化
_rebuild_lock = threading.Lock()


def _missing_coord_stations():
    """
    返回当前 stations.csv 中**坐标无效**的站点名列表。

    与 route_planner.is_valid_coord 使用同一判据，保证"报出来的缺坐标站点"
    与"实际未接入路网的站点"完全一致。
    """
    out = []
    if not os.path.exists(STATIONS_FILE):
        return out
    try:
        import csv as _csv
        with open(STATIONS_FILE, "r", encoding="utf-8-sig") as f:
            for row in _csv.DictReader(f):
                name = (row.get("站名") or "").strip()
                if not name:
                    continue
                try:
                    lon = float(row.get("经度"))
                    lat = float(row.get("纬度"))
                except (TypeError, ValueError):
                    out.append(name)
                    continue
                if not route_planner.is_valid_coord(lon, lat):
                    out.append(name)
    except OSError:
        pass
    return out


def _generate_and_reload():
    """运行数据生成 + 热重载。调用方须已持有 _rebuild_lock。"""
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        upgrade_data.main()  # 会重新写 stations.csv / lines.csv
    route_planner.load_data(STATIONS_FILE, LINES_FILE)


def _rebuild_and_reload(auto_coord=False):
    """
    从 main_railway_line.txt 重新生成 stations.csv / lines.csv，
    并热重载到 route_planner 内存中以立即生效（无需重启服务）。

    参数：
        auto_coord: 是否启用在线地理坐标补全（百度/高德，Key 取系统环境变量）。
                    **仅当确实存在坐标无效的站点时才会发起在线请求**
                    —— 在线地理编码每天有额度限制。
                    补全成功后会再生成一次路网，使新坐标立即生效。

    返回统计字典（含缺坐标站点与本次补全结果）。
    """
    with _rebuild_lock:
        _generate_and_reload()

        missing_before = _missing_coord_stations()
        autofill = {
            "enabled": bool(auto_coord),
            "key_configured": fetch_coords.online_key_available(),
            "provider": fetch_coords.online_provider_name(),
            "requested": [],   # 本次实际尝试补全的站点
            "filled": [],      # 补全成功
            "failed": [],      # 补全失败
            "skipped": "",     # 未发起请求的原因
        }

        if not auto_coord:
            autofill["skipped"] = "未勾选「启用在线地理坐标补全」"
        elif not missing_before:
            autofill["skipped"] = "所有站点坐标齐全，无需在线补全（未消耗额度）"
        elif not autofill["key_configured"]:
            autofill["skipped"] = "未检测到 Key，请设置系统环境变量 BAIDU_AK"
        else:
            autofill["requested"] = list(missing_before)
            try:
                ok, failed = fetch_coords.autofill_online(missing_before)
                autofill["filled"] = sorted(ok.keys())
                autofill["failed"] = sorted(failed)
                if ok:
                    # 有新坐标 → 重新生成并重载，让补全结果立即生效
                    _generate_and_reload()
            except Exception as e:
                autofill["failed"] = list(missing_before)
                autofill["error"] = str(e)

        missing_after = _missing_coord_stations()

    return {
        "station_count": len(route_planner.STATIONS),
        "edge_count": len(route_planner.GRAPH.edges()),
        "missing_coords": missing_after,
        "skipped_stations": sorted(route_planner.INVALID_COORD_STATIONS),
        "suspect_edges": upgrade_data.find_suspect_edges(),
        "same_city": {
            "auto_merged": len(route_planner.AUTO_MERGED),
            "auto_pairs": [f"{k}→{v}" for k, v in sorted(route_planner.AUTO_MERGED.items())],
            "candidates": len(route_planner.SAME_CITY_CANDIDATES),
        },
        "autofill": autofill,
    }

app = FastAPI(
    title="普速铁路中转方案 API",
    description="根据中国主要普速铁路干线，计算两座城市之间的最短铁路中转方案。",
    version="1.0.0",
)

# ============================================================
# CORS 中间件：允许前端跨域访问
# ============================================================
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # 允许所有来源（生产环境可改为具体域名）
    allow_credentials=True,
    allow_methods=["*"],  # 允许所有方法
    allow_headers=["*"],  # 允许所有请求头
)

# 模块导入时 route_planner 会自动加载数据并构建城市级图。


def _build_error(status_code: int, message: str):
    """构造统一的错误响应。"""
    raise HTTPException(status_code=status_code, detail=message)


@app.get("/api/route")
def get_route(
    start: str = Query(..., description="起点（站名或城市名），如：北京"),
    end: str = Query(..., description="终点（站名或城市名），如：上海"),
):
    """
    计算从 start 到 end 的最短普速铁路中转方案。

    成功时返回：
        {"status": "success", "data": {"path": [...], "transfers": [...], "total_distance": 1234}}

    出错时返回非 2xx 状态码，并给出说明。
    """
    # 参数基础校验
    if not start or not start.strip():
        _build_error(400, "参数 'start' 不能为空")
    if not end or not end.strip():
        _build_error(400, "参数 'end' 不能为空")

    try:
        result = route_planner.find_route(start, end)
    except RoutePlannerError as e:
        # 站点不存在 / 无法接入网络等 → 400 参数错误
        if "无法识别" in str(e) or "未接入" in str(e) or "找不到可接入" in str(e):
            _build_error(400, str(e))
        # 无法到达 / 不连通等 → 404 无可用路径
        else:
            _build_error(404, str(e))
    except Exception as e:  # 兜底：未知服务器错误
        _build_error(500, f"服务器内部错误：{e}")

    return {
        "status": "success",
        "data": result,
    }


@app.get("/api/routes")
def get_routes(
    start: str = Query(..., description="起点（站名或城市名），如：北京"),
    end: str = Query(..., description="终点（站名或城市名），如：上海"),
    k: int = Query(3, description="期望返回的方案条数（1-5）"),
):
    """
    计算从 start 到 end 的多个（至多 k 条）不同的普速铁路中转方案。

    第一条为最短方案（recommended=True），其余为绕行/替代线路方案。

    成功时返回：
        {"status": "success", "data": [ {方案1}, {方案2}, ... ]}
    """
    if not start or not start.strip():
        _build_error(400, "参数 'start' 不能为空")
    if not end or not end.strip():
        _build_error(400, "参数 'end' 不能为空")
    k = max(1, min(int(k), 5))

    try:
        results = route_planner.find_routes(start, end, k=k)
    except RoutePlannerError as e:
        if "无法识别" in str(e) or "未接入" in str(e) or "找不到可接入" in str(e):
            _build_error(400, str(e))
        else:
            _build_error(404, str(e))
    except Exception as e:
        _build_error(500, f"服务器内部错误：{e}")

    return {"status": "success", "data": results}


@app.get("/api/routes/via")
def get_routes_via(
    start: str = Query(..., description="起点（站名或城市名），如：北京"),
    end: str = Query(..., description="终点（站名或城市名），如：上海"),
    via: str = Query("", description="强制经过的中转站，多个用逗号/顿号分隔，如：武汉,郑州"),
    k: int = Query(1, description="期望返回的方案条数（1-5）"),
):
    """
    定制中转方案：强制经过一个或多个用户指定的中转站。

    依次规划 起点 -> 中转1 -> 中转2 -> ... -> 终点 的最短路径并拼接。

    via 传多个中转站，用逗号或顿号分隔，例如：?via=武汉,郑州
    若 via 为空，则退化为普通的 find_routes 多方案查询。

    成功时返回：
        {"status": "success", "data": [ {方案}, ... ]}
    """
    if not start or not start.strip():
        _build_error(400, "参数 'start' 不能为空")
    if not end or not end.strip():
        _build_error(400, "参数 'end' 不能为空")
    k = max(1, min(int(k), 5))

    try:
        if via and via.strip():
            # 强制经过指定中转站：仅生成一条拼接方案
            result = route_planner.find_route_via(start, end, via_str=via.strip())
            results = [result]
        else:
            # 未指定中转：退化为普通多方案
            results = route_planner.find_routes(start, end, k=k)
    except RoutePlannerError as e:
        if "无法识别" in str(e) or "未接入" in str(e) or "找不到可接入" in str(e):
            _build_error(400, str(e))
        else:
            _build_error(404, str(e))
    except Exception as e:
        _build_error(500, f"服务器内部错误：{e}")

    return {"status": "success", "data": results}


@app.get("/health")
def health():
    """健康检查接口。"""
    return {"status": "ok"}


@app.get("/api/cities")
def get_cities():
    """
    返回所有城市的坐标，用于前端在地图上绘制站点/折线。

    返回格式：
        {"北京": [116.407, 39.904], "上海": [121.473, 31.230], ...}
    """
    coords = {}
    for city, info in route_planner.CITY_INFO.items():
        coords[city] = [info["lon"], info["lat"]]
    return coords


# ============================================================
# 八纵八横高速铁路通道（城市级）—— 独立于普速页面
# ============================================================
# 与普速页面的区别：数据源是 hsr_network.txt（16 条通道的城市级骨架），
# 车次查询用 kind=hsr（G/D/C 字头）。响应结构刻意与普速保持一致，
# 前端复用同一套渲染逻辑。

@app.get("/api/recommend")
def recommend_routes(
    start: str = Query(..., description="起点城市，如：北京"),
    end: str = Query(..., description="终点城市，如：广州"),
    date: str = Query(..., description="出发日期 YYYY-MM-DD"),
    kind: str = Query("slow", description="车次类别：slow 普速 / hsr 高速（G/D/C）"),
    price: bool = Query(False, description="是否附带票价"),
    min_transfer: int = Query(60, description="中转接续时间下限（分钟），默认 60"),
):
    """
    **推荐方案**：先看直达，再用路网图找候选枢纽拼中转。

    - 直达：直接列出当日直达车次（最多 3 趟）
    - 中转：对每个候选枢纽分别查「A→枢纽」「枢纽→B」，
      只保留**接续时间 >= min_transfer 分钟**（默认 60）的组合，
      按总耗时排序取前 5 条。

    ⚠ 只考虑当日接续；请求数 = 1（直达）+ 2×候选枢纽数，
      受 12306 串行限速影响，可能耗时 10 秒左右。
    """
    if not start or not start.strip():
        _build_error(400, "参数 'start' 不能为空")
    if not end or not end.strip():
        _build_error(400, "参数 'end' 不能为空")
    if not date or not date.strip():
        _build_error(400, "参数 'date' 不能为空")
    kind = (kind or "slow").strip().lower()
    if kind not in ("slow", "hsr"):
        _build_error(400, "参数 'kind' 必须为 slow / hsr")
    min_transfer = max(0, min(int(min_transfer), 720))

    planner = hsr_planner if kind == "hsr" else route_planner
    try:
        data = transfer_advisor.recommend(
            planner, start.strip(), end.strip(), date.strip(),
            kind=kind, min_transfer_min=min_transfer, with_price=bool(price),
        )
    except Exception as e:
        _build_error(500, f"推荐失败：{e}")
    return {"status": "success", "data": data}


@app.get("/api/hsr/cities")
def hsr_cities():
    """
    返回八纵八横通道内全部城市的坐标，供前端绘制。

    返回格式与 /api/cities 一致：{"北京": [116.4, 39.9], ...}
    """
    coords = {}
    for city, info in hsr_planner.CITY_INFO.items():
        coords[city] = [info["lon"], info["lat"]]
    return coords


@app.get("/api/hsr/corridors")
def hsr_corridors():
    """
    返回 16 条通道（含支线）的概览，供前端「通道总览」绘制。

    **返回结构刻意与 /api/line/list 完全一致**（name / stations / coords /
    station_count / major_count / grades），这样前端的线路总览绘制代码
    不用改就能同时服务普速与高铁两个页面。
    """
    out = []
    for c in hsr_planner.list_corridors():
        coords = []
        for city in c["cities"]:
            info = hsr_planner.CITY_INFO.get(city)
            coords.append([info["lon"], info["lat"]] if info else None)
        out.append({
            "name": c["name"] + ("（在建）" if c.get("planned") else ""),
            "stations": c["cities"],
            "coords": coords,
            "station_count": c["city_count"],
            "major_count": c["city_count"],
            "grades": {"特等站": c["city_count"]},
            "planned": bool(c.get("planned")),
        })
    planned = sum(1 for c in hsr_planner.list_corridors() if c.get("planned"))
    return {"status": "success", "data": out, "line_count": len(out),
            "planned_count": planned}


@app.get("/api/hsr/routes")
def hsr_routes(
    start: str = Query(..., description="起点城市，如：北京"),
    end: str = Query(..., description="终点城市，如：上海"),
    k: int = Query(3, description="期望返回的方案条数（1-5）"),
):
    """
    计算 start -> end 的多个高铁通道方案（响应结构与普速 /api/routes 一致）。
    """
    if not start or not start.strip():
        _build_error(400, "参数 'start' 不能为空")
    if not end or not end.strip():
        _build_error(400, "参数 'end' 不能为空")
    k = max(1, min(int(k), 5))
    try:
        results = hsr_planner.find_routes(start, end, k=k)
    except hsr_planner.RoutePlannerError as e:
        if "无法识别" in str(e):
            _build_error(400, str(e))
        else:
            _build_error(404, str(e))
    except Exception as e:
        _build_error(500, f"服务器内部错误：{e}")
    return {"status": "success", "data": results}


@app.get("/api/hsr/routes/via")
def hsr_routes_via(
    start: str = Query(..., description="起点城市"),
    end: str = Query(..., description="终点城市"),
    via: str = Query("", description="强制经过的中转城市，多个用逗号分隔"),
    k: int = Query(1, description="期望返回的方案条数（1-5）"),
):
    """定制中转：强制经过指定城市。via 为空时退化为普通多方案。"""
    if not start or not start.strip():
        _build_error(400, "参数 'start' 不能为空")
    if not end or not end.strip():
        _build_error(400, "参数 'end' 不能为空")
    k = max(1, min(int(k), 5))
    try:
        if via and via.strip():
            results = [hsr_planner.find_route_via(start, end, via_str=via.strip())]
        else:
            results = hsr_planner.find_routes(start, end, k=k)
    except hsr_planner.RoutePlannerError as e:
        if "无法识别" in str(e):
            _build_error(400, str(e))
        else:
            _build_error(404, str(e))
    except Exception as e:
        _build_error(500, f"服务器内部错误：{e}")
    return {"status": "success", "data": results}


@app.post("/api/hsr/reload")
def hsr_reload(
    include_planned: bool = Query(
        False,
        description="是否把 [在建] 段也接入算路（默认 False —— 在建段没有实际车次）",
    ),
):
    """
    重新加载 hsr_network.txt 并热重载（编辑通道文件后调用，无需重启）。

    `include_planned=True` 时，标记为 [在建] 的通道也会参与算路。
    默认 False：在建段（如京沪二线津潍段）尚未通车，接进来只会给出
    "方案查不到车次"的困惑。车站本身无论开关都会保留（便于地图绘制）。
    """
    try:
        info = hsr_planner.load_data(include_planned=bool(include_planned))
    except Exception as e:
        _build_error(500, f"重新加载高铁通道失败：{e}")
    info["suspect_edges"] = hsr_planner.find_suspect_edges()
    info["suspect_duplicates"] = hsr_planner.find_suspect_duplicates()
    return {"status": "ok", "data": info}


@app.get("/api/stations")
def get_stations():
    """
    返回所有具体站点的坐标，用于前端精确绘制路径上实际采用的车站。

    返回格式：
        {"北京": [116.407, 39.904], "北京西": [116.321, 39.894], ...}
    """
    coords = {}
    for name, info in route_planner.STATIONS.items():
        coords[name] = [info["lon"], info["lat"]]
    return coords


@app.get("/api/left-ticket")
def get_left_ticket(
    from_station: str = Query(..., description="出发站中文名，如：广州"),
    to_station: str = Query(..., description="到达站中文名，如：衡阳"),
    date: str = Query(..., description="出发日期 YYYY-MM-DD，如：2026-08-18"),
    price: bool = Query(False, description="是否附带票价（来自 yp_info，不产生额外请求）"),
    kind: str = Query("slow", description="车次类别：slow 普速（K/Z/T/Y/纯数字）、hsr 高速（G/D/C）、all 全部"),
):
    """
    手动查询 from_station -> to_station 当天开行的车次。

    - kind=slow（默认）：K / Z / T / Y 字头或无字母的纯数字车次
    - kind=hsr：G / D / C 字头（高铁 / 动车 / 城际）
    - price=true：额外解出票价（硬座 / 硬卧 / 软卧 / 二等座 等）。
      票价直接来自 12306 返回的 yp_info 字段，**不产生额外请求**。

    该接口串行限速 + 60 秒缓存，手动触发，失败优雅降级（不阻塞页面）。

    返回：
        {"status":"success","data":{"ok":bool,"reason":str,"trains":[...],"from":..,"to":..}}
    """
    if not from_station or not from_station.strip():
        _build_error(400, "参数 'from_station' 不能为空")
    if not to_station or not to_station.strip():
        _build_error(400, "参数 'to_station' 不能为空")
    if not date or not date.strip():
        _build_error(400, "参数 'date' 不能为空")
    kind = (kind or "slow").strip().lower()
    if kind not in ("slow", "hsr", "all"):
        _build_error(400, "参数 'kind' 必须为 slow / hsr / all")

    result = railway_query.query_trains(
        from_station.strip(), to_station.strip(), date.strip(),
        with_price=bool(price), kind=kind,
    )
    return {"status": "success", "data": result}


@app.get("/api/train/stops")
def get_train_stops(
    train_no: str = Query(..., description="车次号，如：1461 / Z281"),
    date: str = Query("", description="出发日期 YYYY-MM-DD（可空，携程返回全程站序）"),
):
    """
    查询指定车次的真实经停站（含坐标），用于把"直达方案"的途经站点
    真实地标注在地图上。

    数据来自携程「车次时刻」页（用普通车次号即可），经停站为**全程站序**，
    每个站附经度/纬度。缺坐标的站会就地通过百度地理编码补全并缓存
    （extra_coords.json），一次补全永久复用。

    返回：
        {"status":"success","data":{
            "ok":bool,"reason":str,"train":str,"from":str,"to":str,
            "source":"携程",
            "stops":[{"name","seq","lon","lat","arrive","depart"}, ...]
        }}
    """
    if not train_no or not train_no.strip():
        _build_error(400, "参数 'train_no' 不能为空")
    result = ctrip_query.get_train_stops(train_no.strip())
    return {"status": "success", "data": result}


class AddStationRequest(BaseModel):
    """新增站点的请求体。"""
    name: str = Field(..., description="站点名，如：××站")
    lon: float = Field(..., description="经度，东经为正，如 116.41")
    lat: float = Field(..., description="纬度，如 39.90")
    grade: str = Field("二等站", description="等级：特等站/一等站/二等站/三等站")


@app.post("/api/station/add", status_code=201)
def add_station(req: AddStationRequest):
    """
    动态新增一个本地库中没有的站点，并自动接入最近的大城，
    使其可参与后续路径规划。返回实时反馈信息。
    """
    # 基础校验
    if not req.name or not req.name.strip():
        _build_error(400, "参数 'name' 不能为空")
    if not (-180 <= req.lon <= 180):
        _build_error(400, "参数 'lon' 超出有效范围（-180 ~ 180）")
    if not (-90 <= req.lat <= 90):
        _build_error(400, "参数 'lat' 超出有效范围（-90 ~ 90）")
    if req.grade not in ("特等站", "一等站", "二等站", "三等站"):
        _build_error(400, "参数 'grade' 必须为：特等站/一等站/二等站/三等站")

    try:
        result = route_planner.add_station(
            req.name, req.lon, req.lat, req.grade
        )
    except (ValueError, TypeError) as e:
        _build_error(400, f"新增站点参数有误：{e}")
    except Exception as e:
        _build_error(500, f"新增站点失败：{e}")

    return {"status": "ok", "data": result}


class LineUpdateRequest(BaseModel):
    """用整份线路清单文本更新数据的请求体。"""
    content: str = Field(..., description="完整线路清单文本（与 main_railway_line.txt 格式一致）")
    auto_coord: bool = Field(
        False,
        description="是否启用在线地理坐标补全（需百度服务端 AK；仅当存在缺坐标站点时才会调用）",
    )


@app.get("/api/line/current")
def get_line_current():
    """
    返回当前 main_railway_line.txt 的完整内容与行数，供前端回显编辑。
    """
    if not os.path.exists(LINE_FILE):
        _build_error(404, f"未找到线路清单文件：{LINE_FILE}")
    try:
        with open(LINE_FILE, "r", encoding="utf-8") as f:
            content = f.read()
    except Exception as e:
        _build_error(500, f"读取线路清单失败：{e}")
    lines, _meta = upgrade_data.load_railway_lines()
    return {
        "status": "ok",
        "content": content,
        "line_count": len(lines),
        "file": "main_railway_line.txt",
    }


@app.post("/api/line/rebuild", status_code=200)
def rebuild_data(
    auto_coord: bool = Query(
        False,
        description="是否启用在线地理坐标补全（需百度服务端 AK；仅当存在缺坐标站点时才会调用）",
    ),
):
    """
    触发"重新生成 + 热重载"：从已有的 main_railway_line.txt 重算
    stations.csv / lines.csv，并立即生效到运行中的服务（无需重启）。

    适合用户在本机直接编辑了 txt 文件后调用。
    auto_coord=True 时，若检测到缺坐标站点，会尝试在线补全（每天有额度）。
    """
    if not os.path.exists(LINE_FILE):
        _build_error(404, f"未找到线路清单文件：{LINE_FILE}，请先创建")
    try:
        stats = _rebuild_and_reload(auto_coord=auto_coord)
    except Exception as e:
        _build_error(500, f"重新生成数据失败：{e}")
    return {"status": "ok", "data": stats}


@app.post("/api/line/update", status_code=200)
def update_lines(req: LineUpdateRequest):
    """
    前端把编辑好的完整线路清单文本 POST 过来：
      1) 写入 main_railway_line.txt
      2) 重新生成 CSV
      3) 热重载到内存
    立即生效，前端无需重启服务。

    若 auto_coord=True 且检测到缺坐标站点，会先尝试在线地理坐标补全
    （百度/高德，Key 取系统环境变量），补全成功后再重生成一次。
    """
    if not req.content or not req.content.strip():
        _build_error(400, "参数 'content' 不能为空")
    try:
        with open(LINE_FILE, "w", encoding="utf-8") as f:
            f.write(req.content)
    except Exception as e:
        _build_error(500, f"写入线路清单失败：{e}")

    try:
        stats = _rebuild_and_reload(auto_coord=req.auto_coord)
        stats["message"] = "线路清单已保存，数据已重新生成并生效。"
    except Exception as e:
        _build_error(500, f"线路清单已写入，但重新生成数据失败：{e}")

    return {"status": "ok", "data": stats}


@app.get("/api/station-codes")
def station_codes_info():
    """
    返回铁路电报码（telecode）的覆盖情况。

    12306 余票接口只认**电报码**、不认中文站名，所以没有电报码的站
    查不到车次（`railway_query.query_trains` 会返回 ok=false 并说明原因）。
    本接口把"没有电报码的站"列出来，便于排查"为什么这个站查不到车次"。

    - total / with_code: 站网内站点数与其中有电报码的数量
    - missing:           没有电报码的站名（多为货运/技术站，本就没有客运码）
    - file:              对照表文件
    """
    stations = sorted(route_planner.STATIONS.keys())
    missing = [s for s in stations if not railway_query.get_station_code(s)]
    return {
        "status": "ok",
        "data": {
            "total": len(stations),
            "with_code": len(stations) - len(missing),
            "missing": missing,
            "file": "station_codes.csv",
        },
    }


@app.get("/api/same-city")
def same_city_info():
    """
    返回「同城异站」的归并情况，供前端展示与人工确认。

    - auto_merged:  本次**自动归并**的站对（站名有前缀关系 + 距离足够近）
    - candidates:   距离很近但**未归并**的站对，供人工判断。
                    只提示、不自动合并 —— 因为其中既有真同城
                    （中华门—南京），也有不同城市（西安—咸阳），机器区分不了。
    - manual:       same_city.json 里手工补充的条目
    - config:       当前自动归并参数
    """
    manual_extra, auto_exclude, overrides = route_planner.load_same_city_config()
    return {
        "status": "ok",
        "data": {
            "auto_merged": [
                {"station": k, "city": v}
                for k, v in sorted(route_planner.AUTO_MERGED.items())
            ],
            "candidates": route_planner.SAME_CITY_CANDIDATES,
            "manual": manual_extra,
            "config": {
                "auto_enabled": overrides.get("enabled", route_planner.AUTO_SAME_CITY),
                "max_km": overrides.get("max_km", route_planner.AUTO_SAME_CITY_KM),
                "require_prefix": overrides.get(
                    "require_prefix", route_planner.AUTO_SAME_CITY_REQUIRE_PREFIX),
                "config_file": route_planner.SAME_CITY_FILE,
            },
        },
    }


@app.get("/api/coords/status")
def coords_status():
    """
    返回在线地理坐标补全的可用状态，供前端提示。

    - key_configured: 是否已配置 Key（系统环境变量 BAIDU_AK / AMAP_KEY）
    - provider:       实际会使用的在线来源名（百度 / 高德 / null）
    - missing:        当前坐标无效、未接入路网的站点
    - suspect_edges:  相邻站间距异常大的区间（疑似坐标解析到异地同名），
                      只作提示，需人工确认
    """
    return {
        "status": "ok",
        "data": {
            "key_configured": fetch_coords.online_key_available(),
            "provider": fetch_coords.online_provider_name(),
            "missing": _missing_coord_stations(),
            "skipped_stations": sorted(route_planner.INVALID_COORD_STATIONS),
            "suspect_edges": upgrade_data.find_suspect_edges(),
        },
    }


@app.get("/api/line/list")
def get_line_list():
    """
    返回所有铁路线路的完整概览，供前端"线路总览"界面展示。

    每条线路包含：
      name           线名（去掉括号注释）
      stations       途经站点（规整名，按顺序）
      station_count  站点数
      major_count    特等 / 一等大站数
      grades         各等级站点数量
      coords         [[经度, 纬度], ...]，与 stations 一一对应（缺坐标的为 None）
    """
    lines_raw, _meta = upgrade_data.load_railway_lines()

    def clean(name):
        n = str(name).strip()
        i = n.find("（")
        if i > 0:
            n = n[:i]
        return n.strip()

    def norm(name):
        s = str(name).strip()
        return s[:-1] if s.endswith("站") else s

    out = []
    for raw_name, sts in lines_raw.items():
        lname = clean(raw_name)
        stations = []
        coords = []
        grades = {}
        major = 0
        for sname, sgrade in sts:
            key = norm(sname)
            stations.append(key)
            info = route_planner.STATIONS.get(key)
            coords.append([info["lon"], info["lat"]] if info else None)
            g = str(sgrade).strip()
            grades[g] = grades.get(g, 0) + 1
            if g in ("特等", "一等", "特等站", "一等站"):
                major += 1
        out.append({
            "name": lname,
            "stations": stations,
            "station_count": len(stations),
            "major_count": major,
            "grades": grades,
            "coords": coords,
        })

    return {"status": "ok", "data": out, "line_count": len(out)}


@app.get("/")
def serve_index():
    """返回前端页面 index.html。"""
    index_path = os.path.join(_BASE_DIR, "index.html")
    if os.path.exists(index_path):
        return FileResponse(index_path)
    return {"message": "index.html 不存在", "hint": "请将前端页面命名为 index.html"}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000)

