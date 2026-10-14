#!/bin/bash
# ============================================================
# L4D2 管理面板 - 交互式管理脚本
# 用法: bash scripts/manage.sh
# 功能:
#   1) 安装系统依赖（等同 sudo bash scripts/setup.sh）
#   2) 配置面板信息（逐项交互生成/修改 .env）
#   3) 启动面板（独立 screen 后台会话，不占用当前终端）
#   4/5/6) 停止 / 重启 / 查看面板状态
# ============================================================

# 本脚本位于 scripts/，BASE_DIR 统一定位到项目根（.env、panel.log 与 backend/ 都在根目录下）
BASE_DIR="$(cd "$(dirname "$0")/.." && pwd)"
ENV_FILE="$BASE_DIR/.env"
PANEL_LOG="$BASE_DIR/panel.log"
PANEL_SESSION="l4d2_panel"   # 面板进程所在的 screen 会话名（游戏实例为 l4d2_manager_*，互不影响）
PANEL_PORT=8080              # 默认面板端口，实际以 .env 中的 PANEL_PORT 为准（下方重新读取）

# 面板进程匹配模式：仅限定"本项目 .venv 下启动的 gunicorn (app:app)"，
# 避免用户服务器上存在其他 gunicorn 服务时被 stop_panel 误杀。
# 用户安装路径不可预知，用纯 bash 逐个转义正则特殊字符（不依赖外部工具，各平台行为一致）
_p=$BASE_DIR
_p=${_p//\\/\\\\}; _p=${_p//./\\.}; _p=${_p//+/\\+}; _p=${_p//\?/\\?}
_p=${_p//\*/\\*};  _p=${_p//\[/\\[}; _p=${_p//\]/\\]}; _p=${_p//\^/\\^}
_p=${_p//\$/\\$};  _p=${_p//(/\\(};  _p=${_p//)/\\)};  _p=${_p//\|/\\|}
_p=${_p//\{/\\{};  _p=${_p//\}/\\\}}   # 注意: } 的替换需三层转义，否则 bash 会把结尾 } 误认为扩展结束符
PANEL_PROC_PAT="$_p/.venv.*gunicorn.*app:app"
unset _p

# --- 终端颜色（仅交互终端着色；管道/重定向时自动退化为纯文本） ---
if [ -t 1 ]; then
    C_RESET=$'\033[0m'
    C_RED=$'\033[31m'
    C_GREEN=$'\033[32m'
    C_YELLOW=$'\033[33m'
    C_CYAN=$'\033[36m'
    C_BOLD=$'\033[1m'
else
    C_RESET=''; C_RED=''; C_GREEN=''; C_YELLOW=''; C_CYAN=''; C_BOLD=''
fi
ok()   { printf '%s\n' "${C_GREEN}$*${C_RESET}"; }
warn() { printf '%s\n' "${C_YELLOW}$*${C_RESET}"; }
err()  { printf '%s\n' "${C_RED}$*${C_RESET}" >&2; }
info() { printf '%s\n' "${C_CYAN}$*${C_RESET}"; }

# --- screen 会话探测（与 L4D2_Manager_API.sh 同一套判定逻辑） ---
session_exists() {
    screen -ls 2>/dev/null | grep -q "\.$PANEL_SESSION[[:space:]]"
}
session_alive() {
    local line
    line=$(screen -ls 2>/dev/null | grep "\.$PANEL_SESSION[[:space:]]") || return 1
    echo "$line" | grep -q "(Dead" && return 1
    return 0
}

# 读取 .env 中已有配置项作为默认值（保留值中的 = 号）
get_env() {
    grep -E "^$1=" "$ENV_FILE" 2>/dev/null | tail -n1 | cut -d= -f2-
}

# 面板端口：优先使用 .env 中的 PANEL_PORT（"配置面板信息"时设置），非法或未配置时回退 8080。
# "查看状态"/"启动面板"前会重新调用本函数，保证配置修改后打印与探测的端口同步
refresh_port() {
    local p
    p=$(get_env PANEL_PORT)
    if [[ "$p" =~ ^[0-9]+$ ]] && [ "$p" -ge 1024 ] && [ "$p" -le 65535 ]; then
        PANEL_PORT=$p
    fi
}
refresh_port

# ---------------- 1. 安装系统依赖 ----------------
install_deps() {
    if [ ! -f "$BASE_DIR/scripts/setup.sh" ]; then
        err "错误: 未找到 $BASE_DIR/scripts/setup.sh"; return 1
    fi
    bash "$BASE_DIR/scripts/setup.sh"
}

# ---------------- 2. 配置面板信息 ----------------
configure_env() {
    info "== 配置面板信息（写入 $ENV_FILE，已有项直接回车保留）=="

    # 登录账号
    local def_user cur
    cur=$(get_env PANEL_USER); def_user=${cur:-admin}
    local user=""
    while true; do
        read -rp "面板登录账号 [回车默认 $def_user]: " user
        [ -z "$user" ] && user="$def_user"
        if [[ "$user" =~ ^[A-Za-z0-9_-]{1,32}$ ]]; then break; fi
        err "  账号只能包含字母/数字/下划线/短横线（1-32 位），请重新输入"
    done

    # 登录密码（隐藏输入；已有密码回车保留）
    local password=""
    cur=$(get_env PANEL_PASSWORD)
    if [ -n "$cur" ]; then
        read -rs -p "面板登录密码 [回车保留现有密码]: " password; echo
        [ -z "$password" ] && password="$cur"
    fi
    while [ -z "$password" ]; do
        read -rs -p "请设置面板登录密码（输入不回显）: " password; echo
        [ -n "$password" ] || { err "  密码不能为空"; continue; }
        [[ "$password" != *#* ]] || { err "  密码不能包含 # 字符"; password=""; continue; }
        local confirm=""
        read -rs -p "请再次输入密码确认: " confirm; echo
        [ "$password" = "$confirm" ] || { err "  两次输入不一致，请重新设置"; password=""; }
    done

    # 会话签名密钥（默认自动生成，保证重启后登录状态不失效）
    local def_secret secret_key
    def_secret=$(LC_ALL=C tr -dc 'a-f0-9' < /dev/urandom | head -c 64)
    cur=$(get_env SECRET_KEY)
    if [ -n "$cur" ]; then
        read -rp "会话签名密钥 SECRET_KEY [回车保留现有]: " secret_key
        [ -z "$secret_key" ] && secret_key="$cur"
    else
        read -rp "会话签名密钥 SECRET_KEY [回车自动生成]: " secret_key
        [ -z "$secret_key" ] && secret_key="$def_secret"
    fi
    while true; do
        [[ "$secret_key" != *#* && "$secret_key" != *" "* ]] && break
        read -rp "  密钥不能包含 # 或空格，请重新输入 [回车自动生成]: " secret_key
        [ -z "$secret_key" ] && secret_key="$def_secret"
    done

    # 可选：Steam 账号（服务端下载登录用，不填走匿名）
    local steam_user steam_password
    cur=$(get_env STEAM_USER)
    read -rp "Steam 账号（可选，服务端下载用）[回车${cur:+保留现值、}跳过使用匿名下载]: " steam_user
    [ -z "$steam_user" ] && steam_user="$cur"
    if [ -n "$steam_user" ]; then
        cur=$(get_env STEAM_PASSWORD)
        read -rp "Steam 密码 [回车${cur:+保留现值、}跳过]: " steam_password
        [ -z "$steam_password" ] && steam_password="$cur"
        steam_user_line="STEAM_USER=$steam_user"
        steam_password_line="STEAM_PASSWORD=$steam_password"
    else
        steam_user_line="#STEAM_USER="
        steam_password_line="#STEAM_PASSWORD="
    fi

    # 可选：目录配置（回车使用默认路径）
    local server_root steamcmd_dir
    read -rp "服务端安装目录 SERVER_ROOT [回车默认 /home/steam/l4d2server]: " server_root
    [ -z "$server_root" ] && server_root="/home/steam/l4d2server"
    read -rp "SteamCMD 安装目录 STEAMCMD_DIR [回车默认 /home/steam/steamcmd]: " steamcmd_dir
    [ -z "$steamcmd_dir" ] && steamcmd_dir="/home/steam/steamcmd"

    # 面板端口（默认 8080；与服务器其他服务冲突时可自行修改）
    local def_port panel_port
    local cur_port
    cur_port=$(get_env PANEL_PORT)
    def_port=${cur_port:-8080}
    while true; do
        read -rp "面板端口 [回车默认 $def_port]: " panel_port
        [ -z "$panel_port" ] && panel_port="$def_port"
        if [[ "$panel_port" =~ ^[0-9]+$ ]] && [ "$panel_port" -ge 1024 ] && [ "$panel_port" -le 65535 ]; then break; fi
        err "  端口须为 1024-65535 的数字，请重新输入"
    done

    # 备份并写入
    if [ -f "$ENV_FILE" ]; then
        cp "$ENV_FILE" "$ENV_FILE.bak"
        info "（原配置已备份为 .env.bak）"
    fi
    cat > "$ENV_FILE" <<EOF
# ============================================================
# L4D2 管理面板配置文件（由 manage.sh 交互式生成）
# 手动修改保存后，需重启面板生效
# ============================================================

# 面板登录账号
PANEL_USER=$user
PANEL_PASSWORD=$password

# 会话签名密钥（固定不变可使面板重启后登录状态仍有效）
SECRET_KEY=$secret_key

# 面板监听端口（1024-65535；与服务器其他服务冲突时可修改，重启面板后生效）
PANEL_PORT=$panel_port

# 可选：Steam 账号（用于服务端下载登录；不填则使用匿名下载）
$steam_user_line
$steam_password_line

# 服务端安装目录（同时是"文件管理"页根目录）
SERVER_ROOT=$server_root

# SteamCMD 安装目录（面板"部署与更新"功能使用）
STEAMCMD_DIR=$steamcmd_dir
EOF

    echo ""
    ok "== 配置已写入 $ENV_FILE =="
    echo "   账号: $user"
    echo "   密码: ${password:0:2}******（已隐藏）"
    echo "   SECRET_KEY: ${secret_key:0:8}...（已隐藏）"
    echo "   面板端口: $panel_port"
    echo "   SERVER_ROOT: $server_root"
    echo "   STEAMCMD_DIR: $steamcmd_dir"
    warn "   提示: 若面板正在运行，需执行\"重启面板\"后新配置才生效"
}

# ---------------- 3. 启动面板 ----------------
start_panel() {
    refresh_port
    if ! command -v screen >/dev/null 2>&1; then
        err "错误: 未安装 screen，请先在菜单执行 1) 安装系统依赖"; return 1
    fi
    if session_alive; then
        warn "面板已在运行（screen 会话 $PANEL_SESSION），无需重复启动。"
        info "查看状态请选 6，或访问 http://127.0.0.1:$PANEL_PORT"
        return 0
    fi
    if session_exists; then
        warn "检测到残留的僵尸会话，正在清理..."
        screen -wipe >/dev/null 2>&1
    fi
    if [ ! -f "$BASE_DIR/scripts/start.sh" ]; then
        err "错误: 未找到 $BASE_DIR/scripts/start.sh"; return 1
    fi
    if [ ! -f "$ENV_FILE" ]; then
        warn "提示: 尚未生成 .env，面板将使用默认账号 admin / password，建议先执行 2) 配置面板信息。"
    fi

    info "== 正在后台启动面板（screen 会话: $PANEL_SESSION）=="
    screen -dmS "$PANEL_SESSION" bash -c "exec bash '$BASE_DIR/scripts/start.sh' > '$PANEL_LOG' 2>&1"
    sleep 3
    if session_alive; then
        ok "启动成功! 访问 http://<服务器IP>:$PANEL_PORT"
        info "  查看实时输出: ${C_GREEN}screen -r $PANEL_SESSION${C_RESET}${C_CYAN}（返回终端按 ${C_GREEN}Ctrl+A+D${C_RESET}${C_CYAN}）"
        info "  查看日志文件: ${C_GREEN}tail -f $PANEL_LOG${C_RESET}"
        info "  首次启动需创建虚拟环境并安装依赖，可能需要等待片刻。"
    else
        err "启动失败，panel.log 最后 20 行如下:"
        tail -n 20 "$PANEL_LOG" 2>/dev/null
        return 1
    fi
}

# ---------------- 4. 停止面板 ----------------
stop_panel() {
    if session_alive; then
        screen -S "$PANEL_SESSION" -X quit
        sleep 1
    fi
    # 关键: screen 退出只会让 gunicorn 收到 SIGHUP（其语义是重载而非退出），
    # 进程会变成孤儿继续占用面板端口，必须显式补发 SIGTERM 才能真正停止
    if command -v pkill >/dev/null 2>&1; then
        pkill -TERM -f "$PANEL_PROC_PAT" 2>/dev/null
        sleep 2
        if pgrep -f "$PANEL_PROC_PAT" >/dev/null 2>&1; then
            pkill -KILL -f "$PANEL_PROC_PAT" 2>/dev/null
            sleep 1
        fi
    fi
    if session_exists; then
        warn "检测到残留的僵尸会话，正在清理..."
        screen -wipe >/dev/null 2>&1
    fi
    if command -v pgrep >/dev/null 2>&1 && pgrep -f "$PANEL_PROC_PAT" >/dev/null 2>&1; then
        err "错误: 面板 gunicorn 进程未能退出，请手动执行: pkill -9 -f '${BASE_DIR}/.venv'"
        return 1
    fi
    ok "面板已停止。"
    info "提示: 正在运行的游戏实例（l4d2_manager_* 会话）不受影响。"
}

# ---------------- 6. 查看状态 ----------------
status_panel() {
    refresh_port
    info "== 面板进程（screen 会话 $PANEL_SESSION）=="
    if session_alive; then
        screen -ls 2>/dev/null | grep "\.$PANEL_SESSION[[:space:]]"
    else
        err "未在运行"
    fi

    echo ""
    info "== HTTP 探测（127.0.0.1:$PANEL_PORT）=="
    if command -v curl >/dev/null 2>&1; then
        local code
        code=$(curl -s -o /dev/null -w '%{http_code}' -m 2 "http://127.0.0.1:$PANEL_PORT/login" 2>/dev/null)
        if [ "$code" = "200" ]; then
            ok "服务正常（HTTP $code）"
        else
            warn "无响应或异常（HTTP ${code:-000}）"
            if session_alive; then
                warn "提示: 面板进程在运行但该端口无响应——若刚修改过面板端口，旧进程仍监听原端口，执行 5) 重启面板 后生效。"
            fi
        fi
    else
        echo "（未安装 curl，跳过探测）"
    fi

    echo ""
    info "== 游戏实例会话 =="
    local inst
    inst=$(screen -ls 2>/dev/null | grep "l4d2_manager_" || true)
    if [ -n "$inst" ]; then echo "$inst"; else echo "无正在运行的实例"; fi

    echo ""
    info "== panel.log 末尾 5 行 =="
    tail -n 5 "$PANEL_LOG" 2>/dev/null || echo "（暂无日志文件）"
    echo ""
    info "提示: ${C_GREEN}screen -r $PANEL_SESSION${C_RESET}${C_CYAN} 查看实时输出（返回终端按 ${C_GREEN}Ctrl+A+D${C_RESET}${C_CYAN}）"
}

# ---------------- 主菜单 ----------------
show_menu() {
    # 交互终端下每次显示菜单前清屏，让菜单始终从第一行开始；管道/重定向时跳过
    if [ -t 1 ]; then
        clear 2>/dev/null || printf '\033[2J\033[3J\033[H'
    fi
    echo ""
    printf '%s\n' "${C_CYAN}==============================================${C_RESET}"
    printf '%s\n' "${C_YELLOW}${C_BOLD}          L4D2 管理面板 - 管理脚本${C_RESET}"
    printf '%s\n' "${C_CYAN}==============================================${C_RESET}"
    printf '%s\n' "  ${C_GREEN}1)${C_RESET} 安装系统依赖"
    printf '%s\n' "  ${C_GREEN}2)${C_RESET} 配置面板信息"
    printf '%s\n' "  ${C_GREEN}3)${C_RESET} 启动面板"
    printf '%s\n' "  ${C_GREEN}4)${C_RESET} 停止面板"
    printf '%s\n' "  ${C_GREEN}5)${C_RESET} 重启面板"
    printf '%s\n' "  ${C_GREEN}6)${C_RESET} 查看面板状态"
    printf '%s\n' "  ${C_GREEN}0)${C_RESET} 退出"
    printf '%s\n' "${C_CYAN}----------------------------------------------${C_RESET}"
}

while true; do
    show_menu
    printf '%s' "${C_YELLOW}请输入选项: ${C_RESET}"
    # stdin 关闭（如 Ctrl+D）时直接退出，避免空输入死循环
    read -r choice || { echo ""; ok "GoodBye"; exit 0; }
    case "$choice" in
        1) install_deps; read -rp "按回车返回菜单..." _ ;;
        2) configure_env; read -rp "按回车返回菜单..." _ ;;
        3) start_panel;   read -rp "按回车返回菜单..." _ ;;
        4) stop_panel;    read -rp "按回车返回菜单..." _ ;;
        5) stop_panel; start_panel; read -rp "按回车返回菜单..." _ ;;
        6) status_panel;  read -rp "按回车返回菜单..." _ ;;
        0|q|Q) ok "GoodBye"; exit 0 ;;
        *) err "无效选项，请重新输入" ;;
    esac
done
