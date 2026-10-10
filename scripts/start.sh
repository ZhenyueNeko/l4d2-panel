#!/bin/bash
# ============================================================
# L4D2 管理面板 - 启动脚本（gunicorn 生产模式，端口由 .env 的 PANEL_PORT 决定，默认 8080）
# 用法: bash scripts/start.sh（manage.sh 菜单内部亦调用）
# ============================================================

# 本脚本位于 scripts/，BASE_DIR 统一定位到项目根（.env 与 .venv 都在根目录）
BASE_DIR="$(cd "$(dirname "$0")/.." && pwd)"

# 虚拟环境
if [ ! -d "${BASE_DIR}/.venv" ]; then
    echo "== 首次运行，创建 Python 虚拟环境 =="
    python3 -m venv "${BASE_DIR}/.venv" || { echo "错误: python3-venv 未安装（sudo bash scripts/setup.sh）"; exit 1; }
fi

PIP="${BASE_DIR}/.venv/bin/pip"
PYTHON="${BASE_DIR}/.venv/bin/python"

echo "== 安装/更新 Python 依赖 =="
"${PIP}" install -q -r "${BASE_DIR}/backend/requirements.txt" || { echo "错误: 依赖安装失败"; exit 1; }

# 首次部署提醒：.env
if [ ! -f "${BASE_DIR}/.env" ]; then
    echo ""
    echo "警告: 未找到 .env 配置文件，面板将使用默认账号 admin/password"
    echo "       请复制 .env.example 为 .env 并修改后重启面板"
    echo ""
fi

# 面板端口：读取 .env 中的 PANEL_PORT（manage.sh "配置面板信息" 可设置），未配置或非法时回退 8080
PANEL_PORT=""
if [ -f "${BASE_DIR}/.env" ]; then
    PANEL_PORT=$(grep -E "^PANEL_PORT=" "${BASE_DIR}/.env" | tail -n1 | cut -d= -f2- | tr -d '[:space:]')
fi
if ! [[ "$PANEL_PORT" =~ ^[0-9]+$ ]] || [ "$PANEL_PORT" -lt 1024 ] || [ "$PANEL_PORT" -gt 65535 ]; then
    PANEL_PORT=8080
fi

echo "== 启动面板: http://0.0.0.0:${PANEL_PORT} =="
cd "${BASE_DIR}/backend"
# threads 需容纳 SSE 长连接（部署日志流、每个打开的实例控制台各占 1 线程）
exec "${PYTHON}" -m gunicorn --bind "0.0.0.0:${PANEL_PORT}" --workers 1 --threads 16 --timeout 120 app:app
