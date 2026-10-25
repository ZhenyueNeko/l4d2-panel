#!/bin/bash

# =================================================================
# L4D2 服务器与插件管理器 API 版本 (非交互式)
# 改编自: L4D2_Manager.sh 2627 - Polaris
# 功能: 为Web面板提供后端执行接口
# =================================================================

# --- Bash 版本检查 ---
if ((BASH_VERSINFO[0] < 4)); then
    echo "错误: 此脚本需要 Bash 4.0 或更高版本才能运行。" >&2
    exit 1
fi

# ########################## 用户配置区 ##########################
# 实例配置已迁移到面板"实例管理"页面在线管理（存储于 data/instances.json），
# 本脚本通过参数接收实例配置，不再在此处定义。
# 目录支持通过环境变量覆盖（面板会自动传递 .env 中的 SERVER_ROOT / STEAMCMD_DIR）
ServerRoot="${SERVER_ROOT:-/home/steam/l4d2server}"
SteamCMDDir="${STEAMCMD_DIR:-/home/steam/steamcmd}"

# SteamCMD 下载源（依次回退：SteamChina 国内 CDN → 官方 → GitHub 代理加速）
STEAMCMD_URL_CN="https://cdn.steamchina.eccdnx.com/client/installer/steamcmd_linux.tar.gz"
STEAMCMD_URL_OFFICIAL="https://steamcdn-a.akamaihd.net/client/installer/steamcmd_linux.tar.gz"
STEAMCMD_URL_PROXY="https://gh-proxy.com/github.com/apples1949/SteamCmdLinuxFile/releases/download/steamcmd-latest/steamcmd_linux.tar.gz"

# #################################################################


# --- 脚本变量定义 ---
L4d2Dir="$ServerRoot/left4dead2"
ScriptDir="$( cd -- "$( dirname -- "${BASH_SOURCE[0]}" )" &> /dev/null && pwd )"
# 运行数据与代码分离：数据统一在项目根 data/（本脚本位于 backend/shell/，上溯两级即项目根），
# 与面板 app.py 的 DATA_DIR 指向同一处
DataDir="$( cd -- "$ScriptDir/../.." && pwd )/data"
PluginSourceDir="$DataDir/Available_Plugins"
ReceiptsDir="$DataDir/Installed_Receipts"
# 各实例的 screen 会话控制台日志（面板"控制台"功能的数据来源）
ConsoleLogDir="$DataDir/console_logs"
# 各实例的启动包装脚本（screen 会话实际执行的入口，每次启动重新生成）
LaunchScriptDir="$DataDir/launch_scripts"
# 第三方地图：AddonsDir 是游戏自动扫描 addon 的目录（文件管理页"三方上传目录"入口）；
# 地图库 MapLibraryDir 实际存放 vpk，"应用"时才以软链接接入 AddonsDir——
# 删除/清空永远只操作地图库，从机制上杜绝误伤 addons 内的插件等文件
AddonsDir="$L4d2Dir/addons"
MapLibraryDir="$DataDir/Maps"
MapUploadDir="$DataDir/map_uploads"

# 确保目录存在
mkdir -p "$PluginSourceDir" "$ReceiptsDir" "$ServerRoot" "$ConsoleLogDir" "$LaunchScriptDir" "$MapUploadDir" "$MapLibraryDir"

# --- Screen 会话辅助函数 ---

# 判断 screen 会话是否存活（会话存在且状态不是 Dead 僵尸态）
# 用法: Test-SessionAlive <session_name>，返回 0=存活，1=不存在或为 Dead 僵尸会话
function Test-SessionAlive() {
    local session_name="$1"
    local session_line
    session_line=$(screen -ls 2>/dev/null | grep "\.$session_name[[:space:]]") || return 1
    echo "$session_line" | grep -q "(Dead" && return 1
    return 0
}

# 清理实例残留的 screen 僵尸会话：仅删除 Dead 态会话；会话仍存活说明实例还在运行，拒绝清理
# 用法: Remove-SessionLeftover <instance_name>，返回 0=已无该会话，1=仍在运行或清理失败
function Remove-SessionLeftover() {
    local instanceName="$1"
    [[ "$instanceName" =~ ^[A-Za-z0-9_-]{1,32}$ ]] || { echo "错误: 实例名称不合法" >&2; return 1; }
    local session_name="l4d2_manager_${instanceName}"

    if ! screen -ls | grep -q "\.$session_name[[:space:]]"; then
        return 0
    fi
    if Test-SessionAlive "$session_name"; then
        echo "错误: 会话 '$session_name' 仍在运行，已跳过清理" >&2
        return 1
    fi
    # Dead 会话无法响应指令，用 wipe 移除（只影响 Dead 会话，不影响存活的实例）
    screen -wipe >/dev/null 2>&1
    if screen -ls | grep -q "\.$session_name[[:space:]]"; then
        echo "错误: 残留会话 '$session_name' 清理失败" >&2
        return 1
    fi
    return 0
}

# 判断端口是否仍被监听（UDP 或 TCP 任一存在即视为占用）
# 用法: Test-PortOccupied <port>，返回 0=占用中，1=空闲
function Test-PortOccupied() {
    local port="$1"
    ss -uln 2>/dev/null | grep -q ":${port}[[:space:]]" && return 0
    ss -tln 2>/dev/null | grep -q ":${port}[[:space:]]" && return 0
    return 1
}

# 向运行中实例的 screen 会话发送一行控制台命令（等效于在该会话键盘输入并回车）
# 用法: Send-InstanceCommand <instance_name> <command>，返回 0=已发送，1=未运行或命令不合法
function Send-InstanceCommand() {
    local instanceName="$1" command="$2"
    [[ "$instanceName" =~ ^[A-Za-z0-9_-]{1,32}$ ]] || { echo "错误: 实例名称不合法" >&2; return 1; }
    # 剔除控制字符保证单行（面板已有白名单校验，这里是绕过面板直调脚本时的兜底）
    command=$(printf '%s' "$command" | tr -d '\000-\037\177')
    [ -n "$command" ] || { echo "错误: 命令不能为空" >&2; return 1; }
    ((${#command} <= 128)) || { echo "错误: 命令过长（上限 128 字符）" >&2; return 1; }

    local session_name="l4d2_manager_${instanceName}"
    if ! Test-SessionAlive "$session_name"; then
        echo "错误: 实例 '$instanceName' 未在运行" >&2
        return 1
    fi
    # screen -X stuff 等同把命令逐键输入会话；发送前仅此一处补上回车
    screen -S "$session_name" -X stuff "${command}"$'\n' \
        && echo "命令已发送至实例 '$instanceName'"
}

# --- 核心功能函数 ---

# 通过 unbuffer 运行 steamcmd 并清洗 ANSI 转义码，保证日志可流式输出到前端
function Invoke-SteamCMD() {
    unbuffer "$SteamCMDDir/steamcmd.sh" "$@" | sed -u 's/\x1b\[[0-9;]*[a-zA-Z]//g'
    return ${PIPESTATUS[0]}
}

# 获取 SteamCMD：已存在则跳过，否则按 多源依次回退 下载
function Get-SteamCMD() {
    local steamcmd_executable="$SteamCMDDir/steamcmd.sh"
    if [ -f "$steamcmd_executable" ]; then
        return 0
    fi
    echo "未找到 SteamCMD，尝试自动下载..."
    mkdir -p "$SteamCMDDir"
    local src
    for src in "$STEAMCMD_URL_CN" "$STEAMCMD_URL_OFFICIAL" "$STEAMCMD_URL_PROXY"; do
        echo "下载源: $src"
        rm -f "$SteamCMDDir/steamcmd_linux.tar.gz"
        if wget -T 60 -t 1 -O "$SteamCMDDir/steamcmd_linux.tar.gz" "$src" \
           && tar -xzf "$SteamCMDDir/steamcmd_linux.tar.gz" -C "$SteamCMDDir" \
           && [ -f "$steamcmd_executable" ]; then
            rm -f "$SteamCMDDir/steamcmd_linux.tar.gz"
            echo "SteamCMD 下载成功。"
            return 0
        fi
        echo "该下载源失败或内容无效，尝试下一个..."
    done
    rm -f "$SteamCMDDir/steamcmd_linux.tar.gz"
    echo "所有下载源均失败，无法获取 SteamCMD。" >&2
    return 1
}

function Deploy-L4D2Server_NonInteractive() {
    echo "--- 开始部署/更新 L4D2 服务器 ---"
    if ! Get-SteamCMD; then
        return 1
    fi

    local rc=0
    if [ -n "$STEAM_USER" ] && [ -n "$STEAM_PASSWORD" ]; then
        echo "使用账户: $STEAM_USER"
        Invoke-SteamCMD +force_install_dir "$ServerRoot" +login "$STEAM_USER" "$STEAM_PASSWORD" +app_update 222860 validate +quit
        rc=$?
    else
        # 单步匿名拉取已被 Steam 官方限制，改用四步混合平台下载法（改编自 init.sh）：
        # 先按 Windows 清单拉取双平台共用主体文件，再切 Linux 清单补齐平台专属文件
        echo "[1/4] 刷新 Linux 平台应用信息..."
        Invoke-SteamCMD +force_install_dir "$ServerRoot" +login anonymous +@sSteamCmdForcePlatformType linux +app_info_update 1 +quit
        rc=$?
        if [ $rc -eq 0 ]; then
            echo "[2/4] 刷新 Windows 平台应用信息..."
            Invoke-SteamCMD +force_install_dir "$ServerRoot" +login anonymous +@sSteamCmdForcePlatformType windows +app_info_update 1 +quit
            rc=$?
        fi
        if [ $rc -eq 0 ]; then
            echo "[3/4] 下载 Windows 平台主体文件..."
            Invoke-SteamCMD +force_install_dir "$ServerRoot" +login anonymous +@sSteamCmdForcePlatformType windows +app_update 222860 validate +quit
            rc=$?
        fi
        if [ $rc -eq 0 ]; then
            echo "[4/4] 下载 Linux 平台专属文件并校验..."
            Invoke-SteamCMD +force_install_dir "$ServerRoot" +login anonymous +@sSteamCmdForcePlatformType linux +app_update 222860 validate +quit
            rc=$?
        fi
    fi

    if [ $rc -eq 0 ]; then
        echo "--- L4D2 服务器文件部署/更新成功! ---"
    else
        echo "--- SteamCMD 执行失败（退出码: $rc），请检查以上日志。 ---" >&2
        if [ -z "$STEAM_USER" ]; then
            echo "--- 若匿名下载反复失败，可在 docker-compose.yml 配置 STEAM_USER/STEAM_PASSWORD 后重试。 ---" >&2
        fi
        return 1
    fi
}

function Start-L4D2ServerInstance_NonInteractive() {
    # 参数式启动：配置由面板传入（实例数据存于 data/instances.json）
    # 用法: start_instance <name> <hostname> <port> <map> <mode> <cfg> <tick> <extra_params>
    local instanceName="$1" default_hostname="$2" default_port="$3" default_map="$4" default_mode="$5" default_cfg="$6" default_tick="$7" default_para="$8"
    local default_IP="0.0.0.0"

    # 轻量白名单再校验：即使绕过面板直接调用脚本也无法注入命令
    local re_name='^[A-Za-z0-9_-]{1,32}$' re_num='^[0-9]{1,5}$'
    local re_token='^[A-Za-z0-9_.-]{1,64}$' re_mode='^[A-Za-z0-9_]{1,32}$' re_params='^[A-Za-z0-9 +_=.:/-]{0,200}$'
    [[ "$instanceName" =~ $re_name ]] || { echo "错误: 实例名称不合法" >&2; exit 1; }
    [[ "$default_port" =~ $re_num && "$default_port" -ge 1024 && "$default_port" -le 65535 ]] || { echo "错误: 端口不合法（1024-65535）" >&2; exit 1; }
    [[ "$default_map" =~ $re_token ]] || { echo "错误: 地图名不合法" >&2; exit 1; }
    [[ -z "$default_mode" || "$default_mode" =~ $re_mode ]] || { echo "错误: 模式不合法" >&2; exit 1; }
    [[ "$default_cfg" =~ $re_token ]] || { echo "错误: 配置文件名不合法" >&2; exit 1; }
    [[ "$default_tick" =~ $re_num ]] || { echo "错误: tickrate 不合法" >&2; exit 1; }
    [[ "$default_para" =~ $re_params ]] || { echo "错误: 额外参数包含被禁止的字符" >&2; exit 1; }
    # 服务器名称会以双引号形式嵌入启动命令，禁止可逃逸引号的字符（;|&$`<>\ 和引号本身）
    case "$default_hostname" in
        *\"*|*\`*|*\$*|*\\*|*[\;\&\<\>]*) { echo "错误: 服务器名称包含被禁止的字符" >&2; exit 1; } ;;
    esac
    [ -n "$default_hostname" ] || { echo "错误: 服务器名称不能为空" >&2; exit 1; }

    local session_name="l4d2_manager_${instanceName}"

    # 存在同名 Dead 僵尸会话时先自动清理，避免误判"已在运行"导致实例无法启动
    if screen -ls | grep -q "\.$session_name[[:space:]]" && ! Test-SessionAlive "$session_name"; then
        echo "检测到残留的 Dead 僵尸会话 '$session_name'，正在自动清理..."
        screen -wipe >/dev/null 2>&1
    fi

    if screen -ls | grep -q "\.$session_name[[:space:]]"; then
        echo "错误: 名为 '$session_name' 的 screen 会话已在运行。" >&2
        exit 1
    fi

    # 启动前端口占用检查：端口被占时 srcds 会静默把监听端口顺延 +1
    # （实测 -strictportbind 在 L4D2 引擎上不生效），导致面板显示的端口与实际
    # 监听端口不一致。被占时先清理一轮本面板的残留进程链（命令行都含
    # "-port <port>"，只命中 srcds，不会误伤占用端口的其他服务），
    # 清理后仍被占则说明是无关服务，拒绝启动交由用户处理
    if Test-PortOccupied "$default_port"; then
        echo "端口 $default_port 已被占用，正在清理本面板的残留进程..."
        pkill -f -- "-port ${default_port}([[:space:]]|\$)" 2>/dev/null
        sleep 2
        if Test-PortOccupied "$default_port"; then
            echo "错误: 端口 $default_port 仍被非本面板的进程占用，无法启动。" >&2
            echo "      请在服务器上执行 ss -ulnp | grep $default_port 查看占用者后手动处理。" >&2
            exit 1
        fi
        echo "残留进程已清理，继续启动。"
    fi

    # 稳定性参数由后端统一追加（前端"额外参数"不再展示）：
    #   -strictportbind  端口被占用时直接启动失败（注：L4D2 引擎实测不生效，
    #                    仍会顺延端口——已由启动前的端口占用检查兜底）
    #   -nobreakpad      禁用崩溃转储，避免产生大量 dump 文件
    #   -noassert        禁用断言，减少调试开销
    # 用户额外参数中已包含的项不会重复添加
    local final_params="$default_para"
    local f
    for f in -strictportbind -nobreakpad -noassert; do
        case " $final_params " in
            *" $f "*) ;;
            *) final_params="$final_params $f" ;;
        esac
    done

    local srcds_path="./srcds_run"
    local srcdsArgs="-console -game left4dead2 -ip $default_IP -port $default_port +hostname \"$default_hostname\" +map $default_map +servercfgfile $default_cfg -tickrate $default_tick"
    # 游戏模式留空 = 不传 +mp_gamemode，引擎使用默认模式（coop）；传空值会破坏参数解析
    case "$default_mode" in
        ?*) srcdsArgs+=" +mp_gamemode $default_mode" ;;
    esac
    srcdsArgs+=" $final_params"

    # 控制台日志采集：不用 screen -L（实测其落盘有 1-2 秒以上滞后），也不能把 stdout 重定向给
    # tee（stdout 离开终端后程序切换为 4KB 块缓冲，日志成块滞后，且会破坏交互控制台）。
    # 这里用 script -f 在会话内再套一层终端：服务器 stdout 仍是 TTY（行为与直连终端完全一致，
    # attach 进 screen 可正常查看/操作），script 把终端流逐块无缓冲地同步写入日志文件，实时可见。
    # 每次启动重建日志，让每局从零开始，避免上下文无限累积。
    local console_log="$ConsoleLogDir/console_${instanceName}.log"
    rm -f "$console_log"

    # 命令经一层启动脚本包装：screen 会把参数拼接后交给 sh 重解析，嵌套引号难以内联表达。
    # 脚本统一生成到 data/launch_scripts/，每次启动覆盖重建，不散落在代码目录
    local launch_wrapper="$LaunchScriptDir/launch_${instanceName}.sh"
    cat > "$launch_wrapper" <<LAUNCH_EOF
#!/bin/bash
cd "$ServerRoot" || exit 1
# srcdsArgs 中不含单引号（面板白名单保证），可安全用单引号整体包裹
exec script -f -q -c './srcds_run $srcdsArgs' "$console_log"
LAUNCH_EOF
    chmod +x "$launch_wrapper"

    echo "准备在 Screen 会话 '$session_name' 中启动服务器..."
    echo "执行命令: $srcds_path $srcdsArgs"
    (cd "$ScriptDir" && screen -dmS "$session_name" bash "$launch_wrapper")
    sleep 1
    if screen -ls | grep -q "\.$session_name"; then
        echo "成功! 实例 '$instanceName' 已在 screen 会话 '$session_name' 中启动。"
    else
        echo "错误: 启动服务器失败。请检查 screen 和服务器文件是否正常。" >&2
        exit 1
    fi
}

function Stop-L4D2ServerInstance_NonInteractive() {
    # 参数: <实例名> <端口>。本函数保持轻量快速（停止按钮同步调用，典型 3-8 秒）：
    # 优雅退出 + 清理会话 + 一轮快速补杀。判据仍是端口释放，未清理干净的残留
    # 由实例管理页的轮询自愈兜底（前端检测到"已停止但端口有应答"会再次调用本函数，
    # 会话不存在 + 端口占用时走孤儿清理分支，按命令行归因整链 pkill）
    local instanceName="$1" port="$2"
    [[ "$instanceName" =~ ^[A-Za-z0-9_-]{1,32}$ ]] || { echo "错误: 实例名称不合法" >&2; exit 1; }
    [[ "$port" =~ ^[0-9]{1,5}$ ]] || { echo "错误: 端口参数不合法" >&2; exit 1; }
    local session_name="l4d2_manager_${instanceName}"

    local had_session=false port_was_occupied=false
    if Test-PortOccupied "$port"; then port_was_occupied=true; fi

    if screen -ls | grep -q "\.$session_name[[:space:]]"; then
        had_session=true
        if Test-SessionAlive "$session_name"; then
            # 第一阶段: 优雅停止 —— 向服务器控制台发送 quit 指令，短暂等待会话退出
            # （超时不再久等，残留交给轮询自愈，保证按钮响应速度）
            echo "正在向会话 '$session_name' 发送 quit 指令..."
            screen -S "$session_name" -X stuff $'quit\n'
            local waited=0
            while [ "$waited" -lt 5 ]; do
                screen -ls | grep -q "\.$session_name[[:space:]]" || break
                sleep 1
                waited=$((waited + 1))
            done
        fi
        # 第二阶段: 会话残留（含 Dead 僵尸）时强制终止整个会话
        if screen -ls | grep -q "\.$session_name[[:space:]]"; then
            echo "会话未能及时退出，正在终止会话（残留进程由轮询自愈兜底清理）..."
            screen -S "$session_name" -X quit 2>/dev/null
            sleep 1
        fi
        screen -wipe >/dev/null 2>&1
    fi

    # 第三阶段: 按端口归因整链补杀——无论引擎是否仍在监听。引擎退出释放端口后，
    # srcds_run 监工仍可能存活并在数秒后重启引擎重新占用端口，所以即使端口暂时
    # 空闲也要杀。进程链的命令行都含 "-port <port>"（录制壳经 -c 参数、监工与引擎
    # 经启动参数），按此一次同杀——必须连同 srcds_run 一起杀，否则它的崩溃重启
    # 循环几秒后会把引擎重新拉起（轮询自愈同样复用本机制）
    if pgrep -f -- "-port ${port}([[:space:]]|\$)" >/dev/null 2>&1 || Test-PortOccupied "$port"; then
        if [ "$had_session" = true ]; then
            echo "正在清理实例 '$instanceName' 的残留进程链..."
        else
            echo "检测到端口 $port 被残留进程占用（实例会话已不存在），正在清理..."
        fi
        pkill -f -- "-port ${port}([[:space:]]|\$)" 2>/dev/null
        sleep 1
        if Test-PortOccupied "$port"; then
            # 补刀: 对占用端口的进程直接 SIGKILL（UDP/TCP 监听者一并处理）
            local pids
            pids=$({ ss -ulpn 2>/dev/null; ss -tlnp 2>/dev/null; } | grep ":${port}[[:space:]]" | grep -oE 'pid=[0-9]+' | cut -d= -f2 | sort -u)
            if [ -n "$pids" ]; then kill -9 $pids 2>/dev/null; fi
            pkill -9 -f -- "-port ${port}([[:space:]]|\$)" 2>/dev/null
            sleep 1
        fi
    fi

    if Test-PortOccupied "$port"; then
        # 不再反复重试拖慢响应：清理未竟交由轮询自愈，向面板如实报告当前状态
        echo "实例 '$instanceName' 已停止。注意: 端口 $port 暂仍被占用，实例管理页将在轮询中自动完成清理。"
        return 0
    fi

    if [ "$had_session" = true ]; then
        echo "成功! 实例 '$instanceName' 已停止（端口 $port 已释放）。"
    elif [ "$port_was_occupied" = true ]; then
        echo "实例 '$instanceName' 未在运行，已清理占用端口 $port 的残留进程。"
    else
        echo "实例 '$instanceName' 当前未在运行，无需停止。"
    fi
}

function Invoke-PluginInstallation() {
    local pluginName="$1"
    local pluginPath="$PluginSourceDir/$pluginName"
    local receiptPath="$ReceiptsDir/$pluginName.receipt"
    if [ ! -d "$pluginPath" ]; then
      echo "错误: 找不到插件源目录 '$pluginName'。" >&2
      exit 1
    fi
    echo "--- 开始安装插件 '$pluginName' ---"
    echo "创建文件清单（含内容哈希）..."
    # v2 回执：每行"相对路径|SHA-256"。哈希用于卸载时判定文件是否被改过
    # （被改过=归属已转移，卸载不删）；库内主副本自始保留，安装只是复制落位
    : > "$receiptPath"
    local rel hash
    while IFS= read -r rel; do
        [ -z "$rel" ] && continue
        hash=$(sha256sum "$pluginPath/$rel" 2>/dev/null | awk '{print $1}')
        printf '%s|%s\n' "$rel" "$hash" >> "$receiptPath"
    done < <(cd "$pluginPath" && find . -type f | sed 's|^\./||')

    echo "正在将文件复制到服务器目录..."
    rsync -a "$pluginPath/" "$ServerRoot/"

    if [ $? -eq 0 ]; then
        # v2：库内主副本保留（源数据不可变），卸载/重装均以它为准
        echo "--- 插件 '$pluginName' 安装成功! ---"
    else
        echo "--- 错误! 安装插件 '$pluginName' 时文件操作失败。 ---" >&2
        rm -f "$receiptPath"
        exit 1
    fi
}

function Invoke-PluginUninstallation() {
    local pluginName="$1"
    local force="${2:-}"   # force = 跳过改动保护（恢复切换专用：状态切回备份时刻）
    local receiptPath="$ReceiptsDir/$pluginName.receipt"
    if [ ! -f "$receiptPath" ]; then
        echo "错误: 找不到插件清单 '$receiptPath'。无法卸载。" >&2
        exit 1
    fi

    echo "--- 开始卸载插件 '$pluginName' ---"

    # 卸载按回执逐文件判定（v2 回执带哈希，v1 旧回执无哈希自动降级）：
    #   ① 引用保护：其他"已安装"插件的回执也含此路径 → 保留（有别人在用）
    #   ② 改动保护：服务器文件哈希 ≠ 回执记录哈希 → 保留（内容被改过，删除即丢用户数据）
    #   ①② 均通过 → 删除该文件。库内主副本始终保留，卸载完全可逆
    local others_file paths_file shared_file
    others_file=$(mktemp) || { echo '{"success": false, "error": "创建临时文件失败"}'; return 1; }
    paths_file=$(mktemp)  || { rm -f "$others_file"; echo '{"success": false, "error": "创建临时文件失败"}'; return 1; }
    shared_file=$(mktemp) || { rm -f "$others_file" "$paths_file"; echo '{"success": false, "error": "创建临时文件失败"}'; return 1; }

    local f
    for f in "$ReceiptsDir"/*.receipt; do
        [ -f "$f" ] || continue
        [ "$(basename "$f")" = "$pluginName.receipt" ] && continue
        # 其他回执也取路径段参与比对：v2 行是"路径|哈希"，若原样整行参与
        # grep -Fx 全行比对则永远匹配不上，引用保护会静默失效（v1 无 | 不受影响）
        cut -d'|' -f1 "$f" >> "$others_file"
        printf '\n' >> "$others_file"
    done
    cut -d'|' -f1 "$receiptPath" > "$paths_file"
    if [ -s "$others_file" ]; then
        grep -Fx -f "$paths_file" "$others_file" > "$shared_file" 2>/dev/null || true
    fi

    local line rel hash
    while IFS= read -r line || [[ -n "$line" ]]; do
        [ -z "$line" ] && continue
        # v2 行 = "路径|哈希"；无 "|" 或哈希段不是 64 位十六进制 → 视为 v1 纯路径
        rel="$line"
        hash=""
        if [[ "$line" == *\|* ]]; then
            rel="${line%|*}"
            hash="${line##*|}"
            [[ "$hash" =~ ^[0-9a-fA-F]{64}$ ]] || { rel="$line"; hash=""; }
        fi
        [ -z "$rel" ] && continue

        local serverFile="$ServerRoot/$rel"
        [ -f "$serverFile" ] || continue   # 服务器上已不存在，跳过

        if [ -s "$shared_file" ] && grep -Fxq "$rel" "$shared_file"; then
            echo "  保留（其他插件仍引用）: $rel"
            continue
        fi
        # force 模式越过改动保护直接删；引用保护不受 force 影响，仍按引用计数放行
        if [ -n "$force" ]; then
            rm -f "$serverFile"
            continue
        fi
        if [ -n "$hash" ]; then
            local cur
            cur=$(sha256sum "$serverFile" 2>/dev/null | awk '{print $1}')
            if [ "$cur" != "$hash" ]; then
                echo "  保留（内容已被修改）: $rel"
                continue
            fi
        fi
        rm -f "$serverFile"
    done < "$receiptPath"

    # 清理服务器上可能残留的空目录
    while IFS= read -r relativePath; do
        [ -z "$relativePath" ] && continue
        local dirOnServer="$ServerRoot/$(dirname "$relativePath")"
        if [ -d "$dirOnServer" ] && [ -z "$(ls -A "$dirOnServer")" ]; then
            rmdir "$dirOnServer" 2>/dev/null
        fi
    done < <(cut -d'|' -f1 "$receiptPath" | sort -r)

    rm -f "$receiptPath" "$others_file" "$paths_file" "$shared_file"
    echo "--- 插件 '$pluginName' 卸载成功! ---"
}

# --- 第三方地图管理（文件管理页"地图管理"弹窗的数据来源） ---
# 模型：vpk 永远只存于地图库 MapLibraryDir；"应用"= 在 AddonsDir 创建指向库文件的软链接。
# 对 addons 的写操作仅限"创建本库链接/摘除本库链接"，普通文件与外来链接一律不碰。

# 校验地图文件名：仅拒绝路径分隔符、隐藏名与首字符为"-"的名字（防选项注入），
# 中文名/空格/括号等正常字符一律放行；面板侧另有更严格的白名单，这里是直调脚本的兜底
# 用法: Test-MapFileName <name>，返回 0=合法
function Test-MapFileName() {
    local name="$1"
    [ -n "$name" ] || return 1
    case "$name" in
        */*|*\\*|.*|-*) return 1 ;;
    esac
    return 0
}

# 对 addons 中链接类条目的统一安全原则：只有"目标精确指向本地图库文件"的软链接才可摘除，
# 普通文件与指向别处的链接一律不碰（悬空的本库链接同样放行，readlink -f 仍能解析目标路径）
# 判断 addons 中是否存在指向本库指定文件的软链接（不论链接叫什么名字）。
# 文件浏览器里重命名软链接只改链接自身名字、内部目标不变，按名字查找会漏检，
# 因此应用状态判定一律以"链接目标是否为本库文件"为准
# 用法: Test-MapLinkTarget <lib_file>，返回 0=存在指向该库文件的链接
function Test-MapLinkTarget() {
    local lib_file="$1"
    local lib_real target link
    lib_real=$(realpath -m "$lib_file")
    [ -d "$AddonsDir" ] || return 1
    while IFS= read -r -d '' link; do
        target=$(readlink -f "$link") || true
        if [ "$target" = "$lib_real" ]; then
            return 0
        fi
    done < <(find "$AddonsDir" -maxdepth 1 -type l -print0)
    return 1
}

# 摘除 addons 中所有指向本库指定文件的软链接（不论链接叫什么名字），返回摘除数量。
# 只删目标精确等于该库文件的链接：普通文件与指向别处的链接一律不碰（原则见上）
# 用法: Remove-MapLinksTo <lib_file>，stdout 输出摘除数量
function Remove-MapLinksTo() {
    local lib_file="$1"
    local lib_real target removed=0 link
    lib_real=$(realpath -m "$lib_file")
    if [ -d "$AddonsDir" ]; then
        while IFS= read -r -d '' link; do
            target=$(readlink -f "$link") || true
            if [ "$target" = "$lib_real" ]; then
                if rm -f "$link"; then removed=$((removed + 1)); fi
            fi
        done < <(find "$AddonsDir" -maxdepth 1 -type l -print0)
    fi
    echo "$removed"
}

# 安装上传的地图包：一律入地图库（vpk 直接入库；zip/rar/7z 解压提取其中全部 vpk 后入库）；
# 不自动应用，由用户在面板中手动"应用"。库内同名一律覆盖
# 用法: Install-MapPackage <暂存文件名> <原始文件名>，成功输出摘要，失败输出 stderr 并返回非 0
function Install-MapPackage() {
    local staged_name="$1" original_name="$2"
    Test-MapFileName "$staged_name" || { echo "错误: 暂存文件名不合法" >&2; return 1; }
    Test-MapFileName "$original_name" || { echo "错误: 文件名不合法" >&2; return 1; }

    local staged="$MapUploadDir/$staged_name"
    if [ ! -f "$staged" ]; then
        echo "错误: 暂存文件不存在: $staged_name" >&2
        return 1
    fi

    local ext="${original_name##*.}"
    ext=$(printf '%s' "$ext" | tr '[:upper:]' '[:lower:]')

    local installed=() overwritten=()
    mkdir -p "$MapLibraryDir"

    if [ "$ext" = "vpk" ]; then
        [ -e "$MapLibraryDir/$original_name" ] && overwritten+=("$original_name")
        if mv -f "$staged" "$MapLibraryDir/$original_name"; then
            installed+=("$original_name")
        else
            echo "错误: 入库 vpk 文件失败" >&2
            return 1
        fi
    else
        local tmp_dir
        tmp_dir=$(mktemp -d) || { echo "错误: 创建临时目录失败" >&2; return 1; }
        local extract_ok=false
        case "$ext" in
            zip)
                if Unzip-Smart "$staged" "$tmp_dir"; then
                    extract_ok=true
                else
                    echo "错误: 解压 zip 失败（文件可能已损坏）" >&2
                fi ;;
            rar)
                # RAR 解码依赖 p7zip-rar（经 7z）或 unrar，两者都没有时报错提示安装
                if command -v 7z >/dev/null 2>&1 && 7z x -y -o"$tmp_dir" "$staged" >/dev/null 2>&1; then
                    extract_ok=true
                elif command -v unrar >/dev/null 2>&1 && unrar x -o+ -y "$staged" "$tmp_dir/" >/dev/null 2>&1; then
                    extract_ok=true
                else
                    echo "错误: 服务器缺少 RAR 解压工具（请安装 p7zip-rar 或 unrar）" >&2
                fi ;;
            7z)
                if 7z x -y -o"$tmp_dir" "$staged" >/dev/null 2>&1; then
                    extract_ok=true
                else
                    echo "错误: 解压 7z 失败（文件可能已损坏）" >&2
                fi ;;
            *)
                rm -rf "$tmp_dir"
                echo "错误: 不支持的地图包格式: .$ext" >&2
                return 1 ;;
        esac

        if [ "$extract_ok" = true ]; then
            local vpk_file vname
            while IFS= read -r -d '' vpk_file; do
                vname=$(basename "$vpk_file")
                Test-MapFileName "$vname" || continue
                [ -e "$MapLibraryDir/$vname" ] && overwritten+=("$vname")
                if mv -f "$vpk_file" "$MapLibraryDir/$vname"; then
                    installed+=("$vname")
                fi
            done < <(find "$tmp_dir" -type f -iname '*.vpk' -print0)
        fi
        rm -rf "$tmp_dir"

        if [ "$extract_ok" = false ]; then
            return 1
        fi
        if [ ${#installed[@]} -eq 0 ]; then
            echo "错误: 压缩包中未找到 .vpk 地图文件" >&2
            return 1
        fi
    fi

    # 逐行输出：前端结果弹窗按行展示（文件多时列表内滚动查看）
    printf '成功入库 %d 个地图文件:\n' "${#installed[@]}"
    printf '%s\n' "${installed[@]}"
    if [ ${#overwritten[@]} -gt 0 ]; then
        printf '覆盖同名: %s\n' "${overwritten[*]}"
    fi
}

function List-MapFiles() {
    local lib_real
    lib_real=$(realpath -m "$MapLibraryDir")
    mkdir -p "$MapLibraryDir"
    # 顺手清扫 addons 里的悬空链接（指向本库但库文件已不存在，如用户在文件管理里删了库文件）
    local cleaned=0 link target
    if [ -d "$AddonsDir" ]; then
        while IFS= read -r -d '' link; do
            target=$(readlink -f "$link") || true
            if [ -n "$target" ] && [ ! -e "$target" ]; then
                case "$target" in
                    "$lib_real"/*)
                        if rm -f "$link"; then cleaned=$((cleaned + 1)); fi ;;
                esac
            fi
        done < <(find "$AddonsDir" -maxdepth 1 -type l -print0)
    fi

    local json_parts=""
    local f name size mtime applied file_json
    while IFS= read -r -d '' f; do
        name=$(basename "$f")
        size=$(stat -c %s "$f")
        mtime=$(date -r "$f" +"%Y-%m-%d %H:%M:%S")
        applied="false"
        if Test-MapLinkTarget "$f"; then
            applied="true"
        fi
        file_json="{\"name\": $(json_escape "$name"), \"size\": $size, \"mtime\": $(json_escape "$mtime"), \"applied\": $applied}"
        if [ -z "$json_parts" ]; then
            json_parts="$file_json"
        else
            json_parts="$json_parts,$file_json"
        fi
    done < <(find "$MapLibraryDir" -maxdepth 1 -type f -iname '*.vpk' -print0 | sort -z)
    echo "{\"maps\": [$json_parts], \"cleaned_dangling\": $cleaned, \"success\": true}"
}

# 应用地图：在 addons 中创建指向库文件的软链接（游戏按本目录文件正常加载）
# 用法: Apply-MapFile <name>，幂等；addons 中同名位置被普通文件/外来链接占用时拒绝覆盖
function Apply-MapFile() {
    local name="$1"
    Test-MapFileName "$name" || { echo "错误: 文件名不合法" >&2; return 1; }
    local lib_file="$MapLibraryDir/$name"
    if [ ! -f "$lib_file" ]; then
        echo "错误: 地图库中不存在: $name" >&2
        return 1
    fi
    local link="$AddonsDir/$name"
    if Test-MapLinkTarget "$lib_file"; then
        echo "地图 '$name' 已处于应用状态。"
        return 0
    fi
    if [ -e "$link" ] || [ -L "$link" ]; then
        echo "错误: addons 中已存在同名内容 '$name'，为避免覆盖已跳过，请手动检查" >&2
        return 1
    fi
    mkdir -p "$AddonsDir"
    if ln -s "$lib_file" "$link"; then
        echo "地图 '$name' 已应用到 addons。"
    else
        echo "错误: 创建软链接失败" >&2
        return 1
    fi
}

# 取消应用：摘除 addons 中所有指向该库文件的软链接（库文件保留），普通文件绝不触碰。
# 按链接目标而非名字查找——链接被改名（如用户在文件浏览器里重命名了软链接）也能正确摘除
# 用法: Unapply-MapFile <name>
function Unapply-MapFile() {
    local name="$1"
    Test-MapFileName "$name" || { echo "错误: 文件名不合法" >&2; return 1; }
    local lib_file="$MapLibraryDir/$name"
    local link="$AddonsDir/$name"
    local removed
    removed=$(Remove-MapLinksTo "$lib_file")
    if [ "$removed" -gt 0 ]; then
        echo "地图 '$name' 已取消应用（移除 $removed 个应用链接，库文件保留）。"
        return 0
    fi
    if [ -e "$link" ] || [ -L "$link" ]; then
        echo "错误: addons 中的 '$name' 不是本面板创建的地图链接，已跳过" >&2
        return 1
    fi
    echo "地图 '$name' 未在应用状态。"
    return 0
}

function Rename-MapFile() {
    local old_name="$1" new_name="$2"
    Test-MapFileName "$old_name" || { echo "错误: 原文件名不合法" >&2; return 1; }
    Test-MapFileName "$new_name" || { echo "错误: 新文件名不合法" >&2; return 1; }
    local new_ext="${new_name##*.}"
    [ "$(printf '%s' "$new_ext" | tr '[:upper:]' '[:lower:]')" = "vpk" ] || {
        echo "错误: 新名称必须以 .vpk 结尾" >&2; return 1; }

    local old_lib="$MapLibraryDir/$old_name" new_lib="$MapLibraryDir/$new_name"
    if [ ! -f "$old_lib" ]; then
        echo "错误: 地图库中不存在: $old_name" >&2
        return 1
    fi
    if [ "$old_lib" = "$new_lib" ]; then
        echo "新名称与原名称相同。"
        return 0
    fi
    if [ -e "$new_lib" ]; then
        echo "错误: 地图库已存在同名文件 '$new_name'" >&2
        return 1
    fi
    if Test-MapLinkTarget "$old_lib" && { [ -e "$AddonsDir/$new_name" ] || [ -L "$AddonsDir/$new_name" ]; }; then
        echo "错误: addons 中已存在 '$new_name'，无法同步更新应用链接" >&2
        return 1
    fi
    if ! mv "$old_lib" "$new_lib"; then
        echo "错误: 重命名 '$old_name' 失败" >&2
        return 1
    fi
    # 已应用时重建软链接：mv 只改链接名、保留内部目标路径，库文件名已变会让链接悬空。
    # 按目标摘除所有指向旧库文件的链接（含被用户改过名的），再统一以新库名重建，
    # 应用状态不丢失且收敛回规范命名
    local notice="" had_links
    had_links=$(Remove-MapLinksTo "$old_lib")
    if [ "$had_links" -gt 0 ]; then
        if ln -s "$new_lib" "$AddonsDir/$new_name"; then
            notice="，应用链接已同步更新"
        else
            echo "警告: 更新应用链接失败，请在面板中手动重新应用该地图" >&2
        fi
    fi
    echo "地图 '$old_name' 已重命名为 '$new_name'$notice。"
}

function Delete-MapFile() {
    local name="$1"
    Test-MapFileName "$name" || { echo "错误: 文件名不合法" >&2; return 1; }
    local lib_file="$MapLibraryDir/$name"
    local removed
    if [ ! -f "$lib_file" ]; then
        # 库文件已不在（如被外部手动删除）：顺手清掉 addons 里残留的悬空链接
        removed=$(Remove-MapLinksTo "$lib_file")
        if [ "$removed" -gt 0 ]; then
            echo "库文件 '$name' 已不存在，已同步清理 $removed 个残留的应用链接。"
            return 0
        fi
        echo "错误: 地图库中不存在: $name" >&2
        return 1
    fi
    if rm -f "$lib_file"; then
        local notice=""
        removed=$(Remove-MapLinksTo "$lib_file")
        if [ "$removed" -gt 0 ]; then
            notice="，已同步移除 $removed 个应用链接"
        fi
        echo "地图 '$name' 已删除$notice。"
    else
        echo "错误: 删除地图 '$name' 失败" >&2
        return 1
    fi
}

function Clear-MapFiles() {
    local lib_real
    lib_real=$(realpath -m "$MapLibraryDir")
    # 先摘除 addons 中指向本库的全部软链接（其它文件/链接不碰）
    local links=0 f link target
    if [ -d "$AddonsDir" ]; then
        while IFS= read -r -d '' link; do
            target=$(readlink -f "$link") || continue
            case "$target" in
                "$lib_real"/*)
                    if rm -f "$link"; then links=$((links + 1)); fi ;;
            esac
        done < <(find "$AddonsDir" -maxdepth 1 -type l -print0)
    fi
    # 再清空地图库本体
    local removed=0
    if [ -d "$MapLibraryDir" ]; then
        while IFS= read -r -d '' f; do
            if rm -f "$f"; then
                removed=$((removed + 1))
            fi
        done < <(find "$MapLibraryDir" -maxdepth 1 -type f -iname '*.vpk' -print0)
    fi
    echo "已清空 $removed 个地图文件（并移除 $links 个应用链接），addons 内其他文件不受影响。"
}

function Resolve-Path() {
    local path="$1"
    local base_dir
    local relative_path

    if [[ "$path" == "plugin_dir" || "$path" == "plugin_dir/"* ]]; then
        base_dir="$PluginSourceDir"
        if [[ "$path" == "plugin_dir" ]]; then
            relative_path=""
        else
            relative_path=${path#plugin_dir/}
        fi
    elif [[ "$path" == "addons_dir" || "$path" == "addons_dir/"* ]]; then
        # 快捷入口：服务器目录下的 left4dead2/addons（MetaMod/SourceMod 与第三方插件所在）
        base_dir="$L4d2Dir/addons"
        if [[ "$path" == "addons_dir" ]]; then
            relative_path=""
        else
            relative_path=${path#addons_dir/}
        fi
    else
        base_dir="$ServerRoot"
        relative_path="$path"
    fi

    local target_path="$base_dir/$relative_path"
    
    # 解析为绝对、规范的路径
    REAL_TARGET_PATH=$(realpath -m "$target_path")
    REAL_BASE_DIR=$(realpath -m "$base_dir")

    # 安全性检查
    if [[ "$REAL_TARGET_PATH" != "$REAL_BASE_DIR"* ]]; then
        echo "错误: 无效或危险的路径访问 '$path'" >&2
        return 1
    fi
    return 0
}

# --- JSON生成辅助函数 ---
# 字节安全：文件名可能含非 UTF-8 字节（如 GBK 压缩包解压产物）。
# 按字节读入后依次尝试 UTF-8 / GB18030 解码（GBK 乱码名可被还原成正确中文显示），
# 均失败再降级为替换符，保证任何输入都产出合法 JSON，列表接口不再因个别文件名崩溃
json_escape() {
    printf '%s' "$1" | python3 -c '
import json, sys
raw = sys.stdin.buffer.read()
text = None
for enc in ("utf-8", "gb18030"):
    try:
        text = raw.decode(enc)
        break
    except UnicodeDecodeError:
        continue
if text is None:
    text = raw.decode("utf-8", "replace")
print(json.dumps(text))
'
}

# 编码感知解压 zip：Windows 中文环境压出的 zip 条目名为 GBK 字节且无 UTF-8 标志，
# unzip 会把原始字节直接落盘产生非法 UTF-8 文件名（进而打崩 list_files/list_maps 的 JSON 拼接）。
# 改用 python zipfile：条目带 UTF-8 标志直接用原名；否则还原原始字节后依次按 UTF-8/GB18030 解码；
# 含 ".." 的条目一律跳过（防 zip 路径穿越）。语义等同 unzip -o（同名覆盖），文件按默认 umask 落盘。
# 用法: Unzip-Smart <zip文件> <目标目录>，成功返回 0；任一条目失败返回 1（错误信息走 stderr）
function Unzip-Smart() {
    python3 - "$1" "$2" <<'PYEOF'
import os, shutil, sys, zipfile

zip_path, dest = sys.argv[1], sys.argv[2]
dest = os.path.realpath(dest)
os.makedirs(dest, exist_ok=True)

def entry_name(info):
    if info.flag_bits & 0x800:                     # 条目自带 UTF-8 标志，zipfile 已正确解码
        return info.filename
    try:
        raw = info.orig_filename.encode("cp437")   # 无标志条目按 CP437 还原出 zip 内原始字节
    except UnicodeEncodeError:
        return info.filename
    for enc in ("utf-8", "gb18030"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", "replace")

try:
    zf = zipfile.ZipFile(zip_path)
except (zipfile.BadZipFile, OSError) as e:
    print("错误: 无法读取 zip 文件: %s" % e, file=sys.stderr)
    sys.exit(1)

failed = 0
with zf:
    for info in zf.infolist():
        name = entry_name(info).replace("\\", "/").lstrip("/")
        parts = [p for p in name.split("/") if p not in ("", ".")]
        if any(p == ".." for p in parts) or (not parts and not info.is_dir()):
            print("跳过异常条目: %s" % info.orig_filename, file=sys.stderr)
            failed += 1
            continue
        target = os.path.join(dest, *parts) if parts else dest
        if info.is_dir():
            os.makedirs(target, exist_ok=True)
            continue
        os.makedirs(os.path.dirname(target), exist_ok=True)
        try:
            with zf.open(info) as src, open(target, "wb") as out:
                shutil.copyfileobj(src, out, 1024 * 1024)
        except OSError as e:
            print("解压条目失败 %s: %s" % (name, e), file=sys.stderr)
            failed += 1

if failed:
    sys.exit(1)
PYEOF
}

# --- 插件包备份（Installed_Receipts 回执体系的上层应用） ---
# 备份 = 汇总全部回执 + 从服务器根目录收集回执所列文件 → 一个 tar.gz 归档；
# 归档内含 backup_manifest.json（元数据）与 .receipts/<插件名>.receipt（原样回执），
# 恢复 = 解包文件归位服务器 + 回执复制回 ReceiptsDir（覆盖合并，不卸载其他插件）。
BackupDir="$DataDir/Plugin_Backups"
BACKUP_RETENTION=10   # 默认时间戳命名的备份保留最近份数（超出清理最旧）；用户重命名过的视为收藏，不自动清理

# 校验备份归档名（防路径穿越）：默认时间戳名 plugins_backup_YYYYMMDD_HHMMSS.tar.gz，
# 或用户重命名过的自定义名（字母/数字/下划线/连字符/中文，1-64 字符，后缀 .tar.gz）。
# 注意 bash 正则的 UTF-8 字符类依赖运行环境 locale，这里用排除法保证跨 locale 可靠：
# 不含路径分隔符/空白/控制字符、不以 - 或 . 开头、总长 ≤ 80、以 .tar.gz 结尾
# 用法: Test-BackupName <name>，返回 0=合法
function Test-BackupName() {
    local name="$1"
    [ -n "$name" ] || return 1
    [ ${#name} -le 80 ] || return 1
    case "$name" in
        *[/\\\ \	]*|-*|.*|*..*) return 1 ;;
    esac
    [[ "$name" == *.tar.gz ]] || return 1
    return 0
}

# 打包当前全部已安装插件（有回执的即视为已安装）
# 用法: Do-BackupPlugins，成功输出 JSON {"success":true,"name":...,"plugins":N,"size":字节}
# 进度上报：向 $BackupDir/.backup_progress.json 写阶段与压缩百分比
# （{"stage":"collect"} 收集段 rsync 无确定总量不报百分比；{"stage":"compress","percent":N}
# 压缩段以 tar -v 已处理条目数/暂存总条目数 计真实工作量进度、封顶 99% 待 tar 退出后写
# 100——归档体积口径不可用：gzip 压缩后归档仅为源体积三四成，会停在半途），供面板轮询
function Do-BackupPlugins() {
    local receipts=()
    local f
    for f in "$ReceiptsDir"/*.receipt; do
        [ -f "$f" ] && receipts+=("$f")
    done
    if [ ${#receipts[@]} -eq 0 ]; then
        echo '{"success": false, "error": "没有已安装的插件可备份"}'
        return 1
    fi
    mkdir -p "$BackupDir"
    local progress_file="$BackupDir/.backup_progress.json"
    printf '{"stage": "collect"}' > "$progress_file"

    local staging
    staging=$(mktemp -d) || { echo '{"success": false, "error": "创建临时目录失败"}'; return 1; }
    mkdir -p "$staging/.receipts"

    # v2：实例卡片集合快照（恢复时整组替换实例配置；空列表亦合法）
    cp "$DataDir/instances.json" "$staging/instances.json" 2>/dev/null || printf '{}' > "$staging/instances.json"

    # 汇总各回执：文件清单进文件列表（rsync 用），回执原样归档，manifest 记元数据。
    # 清单工作文件必须放暂存目录之外——否则会被打进归档，恢复时又落进服务器根目录
    local filelist existing
    filelist=$(mktemp) || { rm -rf "$staging"; echo '{"success": false, "error": "创建临时文件失败"}'; return 1; }
    existing=$(mktemp) || { rm -rf "$staging"; rm -f "$filelist"; echo '{"success": false, "error": "创建临时文件失败"}'; return 1; }
    : > "$filelist"
    local manifest='{"plugins": [' first=true name files json_name
    local total_files=0
    for f in "${receipts[@]}"; do
        name=$(basename "$f" .receipt)
        cp "$f" "$staging/.receipts/$name.receipt"
        files=$(wc -l < "$f")
        total_files=$((total_files + files))
        # 回施行两种格式：v1 纯路径 / v2 "路径|SHA-256"。cut 取首段即路径
        #（v1 行无 | 时 cut 原样返回整行），两种格式兼容
        grep -v '^[[:space:]]*$' "$f" | cut -d'|' -f1 >> "$filelist"
        $first && first=false || manifest+=", "
        json_name=$(json_escape "$name")
        manifest+="{\"name\": $json_name, \"files\": $files}"
    done
    local created_stamp
    created_stamp=$(date '+%Y-%m-%d %H:%M:%S')
    manifest+="], \"version\": 2, \"total_files\": $total_files, \"created\": \"$created_stamp\"}"

    # 从服务器根目录按清单收集文件（rsync --files-from 保持相对路径；缺失文件跳过不阻断）
    if ! rsync -a --files-from="$filelist" --ignore-missing-args "$ServerRoot/" "$staging/" 2>/dev/null; then
        # 兼容不支持 --ignore-missing-args 的旧版 rsync：过滤掉服务器上已不存在的文件后重试
        : > "$existing"
        local p
        while IFS= read -r p; do
            [ -f "$ServerRoot/$p" ] && printf '%s\n' "$p" >> "$existing"
        done < "$filelist"
        rsync -a --files-from="$existing" "$ServerRoot/" "$staging/" || {
            rm -rf "$staging" "$filelist" "$existing"
            rm -f "$progress_file"
            echo '{"success": false, "error": "收集服务器文件失败"}'
            return 1
        }
    fi
    # 实际收集数对账（共享文件在清单中按插件重复出现，去重后才是唯一路径数）；
    # 记入 manifest 供面板展示"缺失 N 文件"，防止缺失被静默吞掉再造出空壳备份
    local collected expected missing
    collected=$(find "$staging" -type f -not -path "$staging/.receipts/*" -not -name instances.json | wc -l)
    case "$collected" in ''|*[!0-9]*) collected=0 ;; esac
    expected=$(sort -u "$filelist" 2>/dev/null | grep -c .)
    case "$expected" in ''|*[!0-9]*) expected=0 ;; esac
    missing=$((expected > collected ? expected - collected : 0))
    manifest="${manifest%?}, \"collected_files\": $collected, \"missing_files\": $missing}"
    printf '%s' "$manifest" > "$staging/backup_manifest.json"
    rm -f "$filelist" "$existing"

    mkdir -p "$BackupDir"
    local archive_name="plugins_backup_$(date +%Y%m%d_%H%M%S)"
    # 压缩段进度：tar -v 转后台，边压缩边把已处理条目名写入清单文件，轮询
    # 已处理数/暂存总条目数得真实工作量百分比（封顶 99%，tar 退出后写 100）；
    # 总条目不可测时退化为同步打包。tar 清单放暂存目录外，避免被打进归档
    local staging_entries
    staging_entries=$(find "$staging" 2>/dev/null | wc -l)
    case "$staging_entries" in ''|*[!0-9]*) staging_entries=0 ;; esac
    if [ "$staging_entries" -gt 0 ]; then
        local tar_list tar_pid=0 tar_rc=0 done_count pct
        tar_list=$(mktemp) || tar_list=""
        tar -C "$staging" -czvf "$BackupDir/$archive_name.tar.gz" . > "$tar_list" 2>/dev/null &
        tar_pid=$!
        while kill -0 "$tar_pid" 2>/dev/null; do
            done_count=$(wc -l < "$tar_list" 2>/dev/null)
            case "$done_count" in ''|*[!0-9]*) done_count=0 ;; esac
            pct=$((done_count * 100 / staging_entries))
            [ "$pct" -gt 99 ] && pct=99
            printf '{"stage": "compress", "percent": %d}' "$pct" > "$progress_file"
            sleep 0.5
        done
        wait "$tar_pid" || tar_rc=$?
        [ -n "$tar_list" ] && rm -f "$tar_list"
        if [ "$tar_rc" -ne 0 ]; then
            rm -rf "$staging"
            rm -f "$progress_file"
            echo '{"success": false, "error": "打包失败"}'
            return 1
        fi
    else
        if ! tar -C "$staging" -czf "$BackupDir/$archive_name.tar.gz" . 2>/dev/null; then
            rm -rf "$staging"
            rm -f "$progress_file"
            echo '{"success": false, "error": "打包失败"}'
            return 1
        fi
    fi
    printf '{"stage": "compress", "percent": 100}' > "$progress_file"
    rm -rf "$staging"

    # 保留上限：超出时按名字（即时间序）清理最旧的
    local keep=0
    local old
    for old in $(ls -1 "$BackupDir" | grep -E '^plugins_backup_[0-9]{8}_[0-9]{6}\.tar\.gz$' | sort -r); do
        keep=$((keep + 1))
        if [ "$keep" -gt "$BACKUP_RETENTION" ]; then
            rm -f "$BackupDir/$old"
        fi
    done

    local size
    size=$(stat -c %s "$BackupDir/$archive_name.tar.gz")
    echo "{\"success\": true, \"name\": \"$archive_name.tar.gz\", \"plugins\": ${#receipts[@]}, \"total_files\": $total_files, \"missing\": $missing, \"created\": \"$created_stamp\", \"size\": $size}"
}

# 恢复备份（切换语义）：先卸载当前全部已安装插件，再归位备份文件与回执——
# 恢复后已安装列表 = 备份名单，而非新旧并集。实例卡片集合整组替换
# 用法: restore_plugins <归档名>，成功输出 JSON {"success":true,"restored":N,"removed":M}
function Do-RestorePlugins() {
    local archive_name="$1"
    Test-BackupName "$archive_name" || { echo '{"success": false, "error": "备份名不合法"}'; return 1; }
    local archive="$BackupDir/$archive_name"
    [ -f "$archive" ] || { echo '{"success": false, "error": "备份不存在"}'; return 1; }

    local staging
    staging=$(mktemp -d) || { echo '{"success": false, "error": "创建临时目录失败"}'; return 1; }
    if ! tar -xzf "$archive" -C "$staging" 2>/dev/null; then
        rm -rf "$staging"
        echo '{"success": false, "error": "备份解压失败（文件可能已损坏）"}'
        return 1
    fi
    if [ ! -f "$staging/backup_manifest.json" ]; then
        rm -rf "$staging"
        echo '{"success": false, "error": "备份缺少清单文件（可能不是本面板生成的备份）"}'
        return 1
    fi

    # 切换第一步：卸载当前全部已安装插件。强制删除（force）：改动保护不启用，
    # 被改过的文件也删——恢复的目标是把状态切回备份时刻；引用保护仍生效，
    # 共享文件留到最后一个引用者卸载时才删。卸载过程输出与最终 JSON 分离
    #（app 层按纯 JSON 解析 stdout）
    local removed=0 cur_receipt
    for cur_receipt in "$ReceiptsDir"/*.receipt; do
        [ -f "$cur_receipt" ] || continue
        if ! Invoke-PluginUninstallation "$(basename "$cur_receipt" .receipt)" force > /dev/null; then
            rm -rf "$staging"
            echo '{"success": false, "error": "卸载现有插件失败，恢复中止（已卸载部分不可自动回退，可重新恢复本备份）"}'
            return 1
        fi
        removed=$((removed + 1))
    done

    # 切换第二步：文件归位服务器（排除清单/回执目录/实例配置）+ 回执复制回
    # ReceiptsDir（覆盖同名）。实例配置单独归位 DataDir：instances.json 是面板运行
    # 数据，不属于服务端目录。filelist*.txt 是旧版备份混入归档的清单工作文件，一并排除
    rsync -a --exclude='.receipts' --exclude='backup_manifest.json' --exclude='instances.json' \
        --exclude='filelist.txt' --exclude='filelist_existing.txt' "$staging/" "$ServerRoot/" || {
        rm -rf "$staging"
        echo '{"success": false, "error": "文件归位服务器失败"}'
        return 1
    }
    local restored=0
    for f in "$staging/.receipts/"*.receipt; do
        [ -f "$f" ] || continue
        cp "$f" "$ReceiptsDir/"
        restored=$((restored + 1))
    done
    # v2：实例卡片集合整体替换（app 层已保证无运行中实例；v1 备份无此文件则跳过）
    if [ -f "$staging/instances.json" ]; then
        cp "$staging/instances.json" "$DataDir/instances.json"
    fi
    rm -rf "$staging"
    echo "{\"success\": true, \"restored\": $restored, \"removed\": $removed}"
}

# --- API 调用入口 ---
ACTION="$1"
shift

case "$ACTION" in
    get_status)
        server_deployed="false"
        sm_installed="false"
        if [ -f "$ServerRoot/srcds_run" ]; then server_deployed="true"; fi
        if [ -f "$L4d2Dir/addons/metamod.vdf" ]; then sm_installed="true"; fi
        echo "{\"serverDeployed\": $server_deployed, \"smInstalled\": $sm_installed, \"success\": true}"
        ;;

    list_running)
        # 输出正在运行的实例名 JSON 数组（实例配置本身由面板的 instances.json 管理）。
        # 崩溃/异常退出留下的 Dead 僵尸会话无法响应任何指令，实例删除或改名后更会成为
        # 无人认领的残留，这里在每次列取时顺带统一清扫（wipe 只影响 Dead 会话）。
        json="["
        first=true
        has_dead=false
        while read -r name; do
            [ -z "$name" ] && continue
            if ! Test-SessionAlive "l4d2_manager_${name}"; then
                has_dead=true
                continue
            fi
            if ! $first; then json+=","; fi
            json+="\"$name\""
            first=false
        done < <(screen -ls 2>/dev/null | grep -oE "l4d2_manager_[^[:space:]]+" | sed 's/^l4d2_manager_//')
        if $has_dead; then
            screen -wipe >/dev/null 2>&1
        fi
        json+="]"
        echo "$json"
        ;;

    get_plugins)
        available="["
        first=true
        for d in "$PluginSourceDir"/*/; do
            [ -d "$d" ] || continue
            pname=$(basename "$d")
            if [ ! -f "$ReceiptsDir/$pname.receipt" ]; then
                if ! $first; then available+=","; fi
                available+="\"$pname\""
                first=false
            fi
        done
        available+="]"

        installed="["
        first=true
        for f in "$ReceiptsDir"/*.receipt; do
            [ -f "$f" ] || continue
            pname=$(basename "$f" .receipt)
            if ! $first; then installed+=","; fi
            installed+="\"$pname\""
            first=false
        done
        installed+="]"
        
        echo "{\"available\": $available, \"installed\": $installed}"
        ;;

    backup_plugins)
        # 备份全部已安装插件: backup_plugins（无参数）
        Do-BackupPlugins
        ;;

    restore_plugins)
        # 恢复指定备份: restore_plugins <归档名>
        if [ "$#" -lt 1 ]; then
            echo "错误: restore_plugins 需要 1 个参数（备份归档名）" >&2
            exit 1
        fi
        Do-RestorePlugins "$1"
        ;;
    
    deploy_server)
        Deploy-L4D2Server_NonInteractive
        ;;

    start_instance)
        # 参数式启动: <name> <hostname> <port> <map> <mode> <cfg> <tick> <extra_params>
        if [ "$#" -lt 8 ]; then
            echo "错误: start_instance 需要 8 个参数（实例配置请在面板中操作）" >&2
            exit 1
        fi
        Start-L4D2ServerInstance_NonInteractive "$1" "$2" "$3" "$4" "$5" "$6" "$7" "$8"
        ;;

    stop_instance)
        # 停止判据为端口释放: stop_instance <实例名> <端口>
        if [ "$#" -lt 2 ]; then
            echo "错误: stop_instance 需要 2 个参数（实例名与端口）" >&2
            exit 1
        fi
        Stop-L4D2ServerInstance_NonInteractive "$1" "$2"
        ;;

    cleanup_session)
        # 清理实例的残留 screen 僵尸会话（面板在删除实例/改名前调用，防止留下无人认领的会话）
        Remove-SessionLeftover "$1"
        ;;

    send_command)
        # 向运行中实例的 screen 会话发送一行控制台命令: send_command <name> <command>
        if [ "$#" -lt 2 ]; then
            echo "错误: send_command 需要 2 个参数" >&2
            exit 1
        fi
        Send-InstanceCommand "$1" "$2"
        ;;
    
    install_plugin)
        Invoke-PluginInstallation "$1"
        ;;

    uninstall_plugin)
        Invoke-PluginUninstallation "$1"
        ;;

    # --- 地图管理指令（文件管理页"地图管理"弹窗） ---
    install_map)
        # 安装上传的地图包: install_map <暂存文件名> <原始文件名>
        if [ "$#" -lt 2 ]; then
            echo "错误: install_map 需要 2 个参数" >&2
            exit 1
        fi
        Install-MapPackage "$1" "$2"
        ;;

    list_maps)
        List-MapFiles
        ;;

    apply_map)
        # 应用地图（在 addons 创建指向地图库的软链接）: apply_map <文件名>
        Apply-MapFile "$1"
        ;;

    unapply_map)
        # 取消应用（摘除软链接，库文件保留）: unapply_map <文件名>
        Unapply-MapFile "$1"
        ;;

    rename_map)
        # 重命名地图: rename_map <旧名> <新名>（双方均须为 addons 根目录下的纯文件名）
        if [ "$#" -lt 2 ]; then
            echo "错误: rename_map 需要 2 个参数" >&2
            exit 1
        fi
        Rename-MapFile "$1" "$2"
        ;;

    delete_map)
        # 删除单个地图: delete_map <文件名>
        Delete-MapFile "$1"
        ;;

    clear_maps)
        Clear-MapFiles
        ;;

    # --- 文件管理指令 ---
    list_files)
        if ! Resolve-Path "$1"; then exit 1; fi

        if [ ! -d "$REAL_TARGET_PATH" ]; then
            echo "{\"success\": false, \"error\": \"目录不存在: $REAL_TARGET_PATH\"}" >&2; exit 1
        fi
        
        json_parts=""
        shopt -s nullglob
        for f in "$REAL_TARGET_PATH"/*; do
            filename=$(basename "$f")
            filesize=$(du -sh "$f" | awk '{print $1}')
            filetype="file"
            if [ -d "$f" ]; then filetype="directory"; fi
            modtime=$(date -r "$f" +"%Y-%m-%d %H:%M:%S")

            file_json="{\"name\": $(json_escape "$filename"), \"size\": \"$filesize\", \"type\": \"$filetype\", \"mtime\": \"$modtime\"}"
            if [ -z "$json_parts" ]; then
                json_parts="$file_json"
            else
                json_parts="$json_parts,$file_json"
            fi
        done
        
        echo "{\"path\": $(json_escape "$1"), \"files\": [$json_parts], \"success\": true}"
        ;;

    get_file_content)
        if ! Resolve-Path "$1"; then exit 1; fi

        if [ ! -f "$REAL_TARGET_PATH" ]; then
            echo "错误: 文件不存在" >&2; exit 1
        fi
        cat "$REAL_TARGET_PATH"
        ;;

    save_file_content)
        if ! Resolve-Path "$1"; then exit 1; fi
        
        mkdir -p "$(dirname "$REAL_TARGET_PATH")"
        
        if cat > "$REAL_TARGET_PATH"; then
            echo "文件 '$1' 已保存。"
        else
            echo "错误: 写入文件 '$1' 失败。" >&2; exit 1
        fi
        ;;

    delete_path)
        if ! Resolve-Path "$1"; then exit 1; fi
        
        if [[ -z "$1" ]]; then echo "错误: 路径不能为空" >&2; exit 1; fi
        if [ ! -e "$REAL_TARGET_PATH" ]; then
            echo "错误: 路径不存在: '$REAL_TARGET_PATH'" >&2; exit 1
        fi
        
        if rm -rf "$REAL_TARGET_PATH"; then
            echo "路径 '$1' 已被删除。"
        else
            echo "错误: 删除路径 '$1' 失败。" >&2; exit 1
        fi
        ;;

    create_folder)
        if ! Resolve-Path "$1"; then exit 1; fi

        if mkdir -p "$REAL_TARGET_PATH"; then
            echo "文件夹 '$1' 已创建。"
        else
            echo "错误: 创建文件夹 '$1' 失败。" >&2; exit 1
        fi
        ;;

    create_file)
        if ! Resolve-Path "$1"; then exit 1; fi

        if [ -e "$REAL_TARGET_PATH" ]; then
            echo "错误: '$1' 已存在。" >&2; exit 1
        fi
        # 创建空文件，内容随后通过编辑器写入
        if : > "$REAL_TARGET_PATH"; then
            echo "文件 '$1' 已创建。"
        else
            echo "错误: 创建文件 '$1' 失败。" >&2; exit 1
        fi
        ;;
    
    unzip_file)
        if ! Resolve-Path "$1"; then exit 1; fi

        # 注意：本分支位于主调度的 case 中而非函数内，不能用 local
        # （local 会在 stderr 报错，后端拼错误信息时会优先取 stderr，噪音会混入提示）
        target_dir=$(dirname "$REAL_TARGET_PATH")

        if [ ! -f "$REAL_TARGET_PATH" ]; then
            echo "错误: 文件不存在: '$REAL_TARGET_PATH'" >&2; exit 1
        fi

        output=""
        exit_code=0
        success=false

        case "$REAL_TARGET_PATH" in
            *.tar.gz|*.tgz)
                output=$(tar -xzf "$REAL_TARGET_PATH" -C "$target_dir" 2>&1)
                exit_code=$?
                if [ "$exit_code" -eq 0 ]; then success=true; fi
                ;;
            *.tar.bz2|*.tbz2)
                output=$(tar -xjf "$REAL_TARGET_PATH" -C "$target_dir" 2>&1)
                exit_code=$?
                if [ "$exit_code" -eq 0 ]; then success=true; fi
                ;;
            *.tar)
                output=$(tar -xf "$REAL_TARGET_PATH" -C "$target_dir" 2>&1)
                exit_code=$?
                if [ "$exit_code" -eq 0 ]; then success=true; fi
                ;;
            *.zip)
                output=$(Unzip-Smart "$REAL_TARGET_PATH" "$target_dir" 2>&1)
                exit_code=$?
                if [ "$exit_code" -eq 0 ]; then success=true; fi
                ;;
            *.rar)
                output=$(unrar x -o+ "$REAL_TARGET_PATH" "$target_dir" 2>&1)
                exit_code=$?
                if [ "$exit_code" -eq 0 ]; then success=true; fi
                ;;
            *.7z)
                output=$(7z x "$REAL_TARGET_PATH" -o"$target_dir" -y 2>&1)
                exit_code=$?
                if [ "$exit_code" -eq 0 ]; then success=true; fi
                ;;
            *)
                echo "错误: 不支持的压缩文件格式。" >&2
                exit 1
                ;;
        esac

        if [ "$success" = true ]; then
            echo "文件 '$1' 已成功解压。"
        else
            echo "解压 '$1' 失败: ${output:-未知错误}" >&2
            exit 1
        fi
        ;;

    rename_path)
        # 重命名: rename_path <路径> <新名称>（新名称不含路径分隔符，目标已存在时拒绝）
        if [ "$#" -lt 2 ]; then
            echo "错误: rename_path 需要 2 个参数（路径与新名称）" >&2
            exit 1
        fi
        case "$2" in
            */*|*\\*|.*|-*) echo "错误: 新名称不合法（不能包含路径分隔符，也不能以 . 或 - 开头）" >&2
                exit 1 ;;
        esac
        if ! Resolve-Path "$1"; then exit 1; fi
        if [ ! -e "$REAL_TARGET_PATH" ]; then
            echo "错误: 路径不存在: '$1'" >&2
            exit 1
        fi
        NEW_TARGET_PATH="$(dirname "$REAL_TARGET_PATH")/$2"
        if [ -e "$NEW_TARGET_PATH" ]; then
            echo "错误: 已存在同名文件或文件夹，已拒绝: '$2'" >&2
            exit 1
        fi
        if mv "$REAL_TARGET_PATH" "$NEW_TARGET_PATH"; then
            echo "已重命名 '$1' 为 '$2'。"
        else
            echo "错误: 重命名 '$1' 失败" >&2
            exit 1
        fi
        ;;
    *)
        echo "错误: 未知的 API Action: $ACTION" >&2
        exit 1
        ;;
esac