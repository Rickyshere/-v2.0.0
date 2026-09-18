#!/usr/bin/env bash
# 普速铁路中转方案 - 一键安装并启动 (macOS / Linux)
set -e
cd "$(dirname "$0")"

echo "============================================"
echo "   普速铁路中转方案 - 一键安装并启动"
echo "============================================"
echo

# 1. 检测 Python
PY=""
if command -v python3 >/dev/null 2>&1; then
    PY="python3"
elif command -v python >/dev/null 2>&1; then
    PY="python"
else
    echo "[错误] 未检测到 Python，请先安装 Python 3.9+"
    exit 1
fi
echo "[1/4] 使用: $($PY --version)"
echo

# 2. 安装依赖（缺则装）
echo "[2/4] 检查并安装依赖..."
if $PY -c "import fastapi, uvicorn, networkx, pandas, requests" >/dev/null 2>&1; then
    echo "      依赖已就绪。"
else
    echo "      首次安装依赖（可能较慢）..."
    $PY -m pip install -r requirements.txt
    echo "      依赖安装完成。"
fi
echo

# 3. 生成路网数据（若缺失）
echo "[3/4] 检查路网数据文件..."
if [ ! -f stations.csv ] || [ ! -f lines.csv ]; then
    echo "      未发现完整数据，正在生成..."
    $PY upgrade_data.py
    echo "      数据生成完成。"
else
    echo "      数据文件已存在。"
fi
echo

# 4. 启动服务（后台），延时后开浏览器
echo "[4/4] 正在启动服务..."
if command -v xdg-open >/dev/null 2>&1; then
    OPEN="xdg-open"
elif command -v open >/dev/null 2>&1; then
    OPEN="open"
else
    OPEN=""
fi

$PY main.py > /dev/null 2>&1 &
SERVER_PID=$!
echo "      服务进程 PID: $SERVER_PID"
echo "      访问地址: http://127.0.0.1:8000"
sleep 3

if [ -n "$OPEN" ]; then
    "$OPEN" "http://127.0.0.1:8000" >/dev/null 2>&1 &
fi

echo
echo "============================================"
echo "  服务已启动，浏览器应已打开。"
echo "  停止服务: kill $SERVER_PID"
echo "  局域网访问: 放行 8000 端口，访问 http://本机IP:8000"
echo "============================================"
wait $SERVER_PID
