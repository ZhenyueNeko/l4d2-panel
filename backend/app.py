import errno
import hashlib
import json
import os
import platform
import re
import secrets
import shutil
import socket
import struct
import subprocess
import tarfile
import threading
import time
import urllib.parse
import urllib.request
import uuid
import zipfile
from functools import wraps
from flask import Flask, render_template, request, redirect, url_for, session, jsonify, Response
import psutil
import a2s
import geo as _geo
from source_rcon import SourceRcon, SourceRconError, SourceRconAuthError
from dotenv import load_dotenv

# 裸机部署配置：优先读取 backend/.env，其次项目根目录 .env（键名见 .env.example）
_BASE_DIR = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(_BASE_DIR, '.env'))
load_dotenv(os.path.join(os.path.dirname(_BASE_DIR), '.env'))

app = Flask(__name__)

# --- 配置 ---
app.config['SECRET_KEY'] = os.getenv('SECRET_KEY', os.urandom(24))
app.config['MAX_CONTENT_LENGTH'] = 2 * 1024 * 1024 * 1024  # 上传体积上限 2GB（插件包/地图包共用）
PANEL_USER = os.getenv('PANEL_USER', 'admin')
PANEL_PASSWORD = os.getenv('PANEL_PASSWORD', 'password')
# 运行数据与代码分离：插件库/回执/地图/日志等统一放项目根 data/（Shell 脚本侧指向同一处）
DATA_DIR = os.path.join(os.path.dirname(_BASE_DIR), 'data')
SCRIPT_PATH = os.path.join(_BASE_DIR, 'shell', 'L4D2_Manager_API.sh')
SERVER_ROOT = os.getenv('SERVER_ROOT', '/home/steam/l4d2server')
# A2S 玩家查询目标地址：留空 = 自动（先查 127.0.0.1，超时自动换本机内网 IP 重试——
# 部分服务器环境会丢弃回环 UDP 查询，实测内网 IP 路径更可靠）；面板与游戏服分机
# 部署时显式填游戏服务器 IP
A2S_HOST = os.getenv('A2S_HOST', '').strip()
# 文件管理页"三方上传目录"快捷入口：服务器目录下的 left4dead2/addons
ADDONS_DIR = os.path.join(SERVER_ROOT, 'left4dead2', 'addons')
PLUGIN_SOURCE_DIR = os.path.join(DATA_DIR, 'Available_Plugins')
# 地图上传暂存目录：/api/maps/upload 先把文件落盘到这里，再交由脚本 install_map 安装
MAP_UPLOAD_DIR = os.path.join(DATA_DIR, 'map_uploads')
os.makedirs(MAP_UPLOAD_DIR, exist_ok=True)
# 大文件分片上传会话目录：前端对超过 50MB 的文件切片上传，组装完成后即清理；
# 中断的会话保留 24h 供续传，超时由清扫兜底删除
UPLOAD_CHUNK_DIR = os.path.join(DATA_DIR, 'upload_chunks')
UPLOAD_CHUNK_SIZE = 8 * 1024 * 1024      # 单片 8MB
UPLOAD_CHUNK_TTL = 24 * 3600             # 中断会话保留时长
UPLOAD_DISK_MARGIN = 256 * 1024 * 1024   # 上传前磁盘余量要求 = 文件体积 + 此兜底值
RE_UPLOAD_ID = re.compile(r'^[a-f0-9]{32}$')
# Steam 头像：STEAM_API_KEY 为可选配置（steamcommunity.com/dev/apikey 免费注册），
# 不填则头像自动降级为名字首字圆；缓存于 data/avatar_cache/，正缓存 7 天
# Steam 头像 Key 的旧版配置入口：仅用于旧部署升级时的一次性迁移（见 _migrate_env_steam_key），
# 现行配置入口为面板"设置"页（data/panel_settings.json，在线配置、即时生效）
STEAM_API_KEY = os.getenv('STEAM_API_KEY', '').strip()
AVATAR_CACHE_DIR = os.path.join(DATA_DIR, 'avatar_cache')
os.makedirs(AVATAR_CACHE_DIR, exist_ok=True)
AVATAR_TTL = 7 * 24 * 3600               # 头像文件的有效期（头像极少更换）
AVATAR_FETCH_TIMEOUT = (4, 8)            # API 与图片下载的超时（秒）
AVATAR_NEG_TTL = 600                     # 获取失败后的负缓存时长，期间不再请求 Steam
RE_STEAMID64 = re.compile(r'^\d{17}$')

# --- 辅助函数：运行脚本 ---
def run_script(args, payload_stdin=None):
    command = ['bash', SCRIPT_PATH] + args

    try:
        if not os.path.exists(SCRIPT_PATH):
            raise FileNotFoundError(f"脚本文件不存在: {SCRIPT_PATH}")

        process_input = str(payload_stdin) if payload_stdin is not None else None

        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            encoding='utf-8',
            errors='replace',
            input=process_input
        )

        if result.returncode != 0:
            error_message = result.stderr or result.stdout or "未知脚本错误"
            return {"success": False, "message": error_message.strip()}

        return {"success": True, "output": result.stdout.strip()}

    except (subprocess.CalledProcessError, FileNotFoundError, Exception) as e:
        error_message = f"执行脚本时发生内部错误: {str(e)}"
        return {"success": False, "message": error_message}


# --- 认证 ---
def login_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'logged_in' not in session:
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated_function

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        username = request.form.get('username')
        password = request.form.get('password')

        if username == PANEL_USER and password == PANEL_PASSWORD:
            session['logged_in'] = True
            return redirect(url_for('dashboard'))
        else:
            return render_template('login.html', error="用户名或密码错误！")
    return render_template('login.html')

@app.route('/logout')
def logout():
    session.pop('logged_in', None)
    return redirect(url_for('login'))

# --- 页面路由 ---
@app.route('/')
@login_required
def dashboard():
    return render_template('dashboard.html')

@app.route('/instances')
@login_required
def instances_page():
    return render_template('instances.html')

@app.route('/plugins')
@login_required
def plugins_page():
    return render_template('plugins.html')


@app.route('/backups')
@login_required
def backups_page():
    return render_template('backups.html')

@app.route('/files')
@login_required
def files_page():
    return render_template('files.html')

# 关于页展示的外链
ABOUT_LINKS = {
    'github_repo': 'ZhenyueNeko/l4d2-panel',
    'github_url': 'https://github.com/ZhenyueNeko/l4d2-panel',
    'bilibili_name': 'ZhenyueNeko',
    'bilibili_url': 'https://space.bilibili.com/',
    'upstream_repo': 'Q1en/L4D2-Manager-Panel',
    'upstream_url': 'https://github.com/Q1en/L4D2-Manager-Panel',
}

@app.route('/about')
@login_required
def about_page():
    return render_template('about.html', links=ABOUT_LINKS)


# --- API 路由 ---

@app.route('/api/status', methods=['GET'])
@login_required
def api_get_status():
    result = run_script(['get_status'])
    if not result['success']:
        return jsonify({"success": False, "error": result['message']}), 500
    try:
        return jsonify(json.loads(result['output']))
    except json.JSONDecodeError:
        return jsonify({"success": False, "error": "无法解析脚本状态输出"}), 500

@app.route('/api/instances', methods=['GET'])
@login_required
def api_get_instances():
    instances = _load_instances()
    running = _get_running_names()
    merged = {}
    # 按显示名排序，前端按返回顺序展示
    for inst_id in sorted(instances, key=lambda k: instances[k].get('name', k)):
        cfg = instances[inst_id]
        merged[inst_id] = {
            "name": cfg.get("name", inst_id),
            "port": cfg.get("port"),
            "map": cfg.get("map"),
            "mode": cfg.get("mode"),
            "cfg": cfg.get("cfg"),
            "tickrate": cfg.get("tick"),
            "extra_params": cfg.get("params"),
            "hostname_sync": bool(cfg.get("hostname_sync")),
            "running": f"inst_{cfg.get('port')}" in running,
        }
    # 服务器命名插件文件状态（全局一份，与具体实例无关）；前端以 '_' 前缀
    # 键识别并从实例列表中过滤，实例的绑定名由其端口在 names 中查找
    merged['_hostname_plugin'] = _hostname_plugin_info()
    return jsonify(merged)

@app.route('/api/instances/save', methods=['POST'])
@login_required
def api_save_instance():
    data = request.get_json() or {}
    with INSTANCES_LOCK:
        instances = _load_instances()
        original = str(data.get('original_id', '') or '').strip()
        cleaned, error = _validate_instance(data)
        if error:
            return jsonify({"success": False, "message": error}), 400
        display_name, cfg = cleaned
        # 服名同步开关（仅前端检测到命名插件文件时才会提交 true）
        sync_hostname = bool(data.get('hostname_sync'))
        cfg['hostname_sync'] = sync_hostname
        if any(iid != original and inst.get('name') == display_name for iid, inst in instances.items()):
            return jsonify({"success": False, "message": f"已存在同名实例 '{display_name}'"}), 400
        if original:
            if original not in instances:
                return jsonify({"success": False, "message": f"原实例 '{original}' 不存在"}), 404
            # 显示名与内部 ID 分离，改名只改配置，screen 会话不受影响，运行中也允许改名
            instances[original]['name'] = display_name
            for key, value in cfg.items():
                instances[original][key] = value
            inst_id = original
        else:
            if any(cfg['name'] == display_name for cfg in instances.values()):
                return jsonify({"success": False, "message": f"已存在同名实例 '{display_name}'"}), 400
            inst_id = _next_instance_id(instances)
            cfg['name'] = display_name
            instances[inst_id] = cfg
        _save_instances(instances)
        # 写回命名插件文件：kv 模式更新端口节；plain 模式（旧版单服名）仅在单实例
        # 时写（与前端开关的禁用条件一致，服务端再守一道）；broken（KV 损坏）一律
        # 不写——避免把半截结构行当服名写坏文件；失败不阻塞实例保存
        if sync_hostname:
            hp_state = _hostname_plugin_info()
            bindable_now = hp_state['exists'] and (
                hp_state['mode'] == 'kv' or (hp_state['mode'] == 'plain' and len(instances) == 1))
            if bindable_now and not _write_hostname_file(cfg['port'], display_name):
                app.logger.warning('实例 %s 的服名同步写回失败（文件不可写）', inst_id)
    # 绑定实例运行中时尽力通过 RCON 实时刷新运行时服名：先直设 hostname 立即生效，
    # 再重载命名插件使其重读 l4d2_hostname.txt、对齐插件内部状态；失败静默——
    # 文件已正确，换图后自然一致
    if sync_hostname and f"inst_{cfg['port']}" in _get_running_names():
        rcon_password = _global_rcon_password()
        if rcon_password:
            for host in _a2s_targets():
                try:
                    with SourceRcon(host, int(cfg['port']), rcon_password,
                                    timeout=RCON_TIMEOUT) as rcon:
                        rcon.command(f'hostname {_hostname_clip(display_name)}')
                        rcon.command('sm plugins reload l4d2_hostname')
                    break
                except (OSError, SourceRconError):
                    continue
    return jsonify({"success": True, "message": f"实例 '{display_name}' 已保存"})

@app.route('/api/instances/delete', methods=['POST'])
@login_required
def api_delete_instance():
    data = request.get_json() or {}
    inst_id = str(data.get('id', '')).strip()
    if not RE_INSTANCE_NAME.match(inst_id):
        return jsonify({"success": False, "message": "实例标识不合法"}), 400
    with INSTANCES_LOCK:
        instances = _load_instances()
        running = _get_running_names()
        if inst_id not in instances:
            return jsonify({"success": False, "message": f"实例 '{inst_id}' 不存在"}), 404
        display_name = instances[inst_id].get('name', inst_id)
        # screen 会话按端口命名（inst_<端口>）
        session_name = f"inst_{instances[inst_id].get('port')}"
        if session_name in running:
            return jsonify({"success": False, "message": "实例正在运行，请先停止后再删除"}), 400
        # 删除前清掉该实例可能残留的 screen 僵尸会话（如崩溃后留下的），避免留下无人认领的会话
        cleanup = run_script(['cleanup_session', session_name])
        if not cleanup['success']:
            return jsonify({"success": False, "message": f"清理残留会话失败: {cleanup['message']}"}), 500
        del instances[inst_id]
        _save_instances(instances)
    return jsonify({"success": True, "message": f"实例 '{display_name}' 已删除"})

@app.route('/api/plugins', methods=['GET'])
@login_required
def api_get_plugins():
    result = run_script(['get_plugins'])
    if not result['success']:
        return jsonify({"success": False, "error": result['message']}), 500
    try:
        plugins_data = json.loads(result['output'])
        return jsonify(plugins_data)
    except json.JSONDecodeError:
        return jsonify({"success": False, "error": "无法解析插件列表"}), 500


@app.route('/api/settings', methods=['GET'])
@login_required
def api_get_settings():
    """面板级设置读取（设置页回显）。"""
    settings = _load_panel_settings()
    return jsonify({
        "steam_api_key": settings.get('steam_api_key', ''),
        "rcon_password": settings.get('rcon_password', ''),
    })


@app.route('/api/settings/save', methods=['POST'])
@login_required
def api_save_settings():
    """保存面板级设置（即时生效，无需重启面板）。按提交的字段更新（未提交的字段保持原值）；
    字段留空 = 清除该面板值（RCON 密码清除后由实例启动时懒生成）。"""
    data = request.get_json() or {}
    with PANEL_SETTINGS_LOCK:
        settings = _load_panel_settings()
        if 'steam_api_key' in data:
            steam_key = str(data.get('steam_api_key', '')).strip()
            if steam_key and not RE_STEAM_API_KEY.match(steam_key):
                return jsonify({"success": False,
                                "message": "Steam Web API Key 应为 32 位十六进制字符"
                                           "（steamcommunity.com/dev/apikey 免费申请）"}), 400
            if steam_key:
                settings['steam_api_key'] = steam_key
            else:
                settings.pop('steam_api_key', None)
        if 'rcon_password' in data:
            rcon_password = str(data.get('rcon_password', '')).strip()
            if rcon_password and not RE_SAFE_TOKEN.match(rcon_password):
                return jsonify({"success": False,
                                "message": "RCON 密码只能包含字母、数字、点、下划线和短横线"
                                           "（1-64 位，无空格；复杂密码请写入 server.cfg 后在设置页导入）"}), 400
            if rcon_password:
                settings['rcon_password'] = rcon_password
            else:
                settings.pop('rcon_password', None)
        _save_panel_settings(settings)
    if 'rcon_password' in data and rcon_password:
        # 全局密码变更：解除所有实例的认证失败退避，下一轮查询即用新密码重试
        with _RCON_AUTH_FAIL_LOCK:
            _RCON_AUTH_FAIL.clear()
    if 'steam_api_key' in data:
        # Key 变更即清空头像负缓存：新填/换 Key 后下一轮轮询立即重试此前失败的玩家，
        # 不必等 10 分钟负缓存过期（未配置 Key 时解析接口整体短路，清空操作的清理无副作用）
        with _avatar_neg_lock:
            _avatar_neg_cache.clear()
    return jsonify({"success": True, "message": "面板设置已保存"})


@app.route('/api/action', methods=['POST'])
@login_required
def api_action():
    data = request.get_json()
    action = data.get('action')
    payload = data.get('payload')

    if not action:
        return jsonify({"error": "缺少 'action' 参数", "success": False}), 400

    # 启动实例：配置从 instances.json 读取后作为参数传给脚本；
    # payload 是内部实例 ID（持久主键），screen 会话按端口命名（l4d2_manager_inst_<端口>）
    if action == 'start_instance':
        if not payload:
            return jsonify({"success": False, "message": "缺少 'payload' 参数"}), 400
        instances = _load_instances()
        cfg = instances.get(payload)
        if not cfg:
            return jsonify({"success": False, "message": f"实例 '{payload}' 不存在"}), 400
        result = run_script(['start_instance', f"inst_{cfg.get('port')}", str(cfg.get('name', payload)), str(cfg.get('port')),
                             str(cfg.get('map')), str(cfg.get('mode')), str(cfg.get('cfg')),
                             str(cfg.get('tick')), _build_start_params(cfg)])
        display_name = cfg.get('name', payload)
        if result['success']:
            # 强制刷新运行缓存：实例刚启动，若等后台线程 2.5 秒后再刷新，
            # 控制台接口会查到旧缓存而误判"未在运行"，导致终端窗口打开即黑屏
            _get_running_names(max_age=0)
            # 重启即注入面板全局 RCON 密码，此前认证失败导致的退避随之解除
            with _RCON_AUTH_FAIL_LOCK:
                _RCON_AUTH_FAIL.pop(payload, None)
            return jsonify({"success": True, "message": f"实例 '{display_name}' 启动指令已下发"})
        return jsonify({"success": False, "message": result.get('message', '操作失败')}), 400

    # 在线玩家查询：A2S 先行探测（快速判离线），RCON status 提供详情（SteamID/ping/归属地），
    # RCON 不可用时回退 A2S（仅名字/得分/时长）
    if action == 'players':
        if not payload:
            return jsonify({"success": False, "message": "缺少 'payload' 参数"}), 400
        instances = _load_instances()
        cfg = instances.get(payload)
        if not cfg:
            return jsonify({"success": False, "message": f"实例 '{payload}' 不存在"}), 404
        query_port = int(cfg.get('port', 0))
        try:
            a2s_players = _a2s_player_query(query_port)
        except (socket.timeout, OSError) as e:
            # 不在提示中展示查询目标，避免暴露服务器内网 IP
            detail = str(e)
            return jsonify({"success": False, "offline": True,
                            "message": "服务器未运行或查询超时" + (f"：{detail}" if detail else "")})
        except Exception as e:   # a2s 协议解析异常不直接判死，仍尝试 RCON
            a2s_players = None
            a2s_error = str(e)
        else:
            a2s_error = None

        rows, rcon_state = _players_via_rcon(cfg, query_port, payload)
        if rows is not None:
            return jsonify({"success": True, "source": "rcon",
                            "count": len(rows), "players": rows, "rcon": rcon_state})

        # RCON 不可用 → A2S 回退
        if a2s_players is None:
            return jsonify({"success": False, "message": f"A2S 响应异常: {a2s_error or '未知'}"}), 502
        for p in a2s_players:
            secs = int(p['duration'])
            p['duration_text'] = f"{secs // 3600:02d}:{secs % 3600 // 60:02d}:{secs % 60:02d}"
        return jsonify({"success": True, "source": "a2s", "count": len(a2s_players),
                        "players": a2s_players, "rcon": rcon_state})

    # 对于长时间运行的部署任务，使用流式响应
    if action == 'deploy_server':
        def generate_log():
            command = ['bash', SCRIPT_PATH, action]
            process = subprocess.Popen(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding='utf-8',
                errors='replace',
                bufsize=1
            )
            for line in iter(process.stdout.readline, ''):
                yield f"data: {line}\n\n"
            process.stdout.close()
            process.wait()
        return Response(generate_log(), mimetype='text/event-stream')

    # 停止实例：screen 会话按端口命名，需从实例配置解析出会话名
    if action == 'stop_instance':
        if not payload:
            return jsonify({"success": False, "message": "缺少 'payload' 参数"}), 400
        instances = _load_instances()
        cfg = instances.get(payload)
        if not cfg:
            return jsonify({"success": False, "message": f"实例 '{payload}' 不存在"}), 400
        result = run_script(['stop_instance', f"inst_{cfg.get('port')}", str(cfg.get('port', 0))])
        display_name = cfg.get('name', payload)
        if result['success']:
            # 与启动对称：强制刷新运行缓存，避免停止后卡片状态滞后到下次缓存到期
            _get_running_names(max_age=0)
            # 完整过程输出（发送 quit/终止会话/清理进程链等）写入服务器日志供排障；
            # 前端 toast 只显示一行简略结果
            output = (result.get('output') or '').strip()
            if output:
                app.logger.info('实例 %s 停止输出:\n%s', display_name, output)
            if '暂仍被占用' in output:
                message = f"实例 '{display_name}' 已停止，端口清理将在稍后自动完成"
            else:
                message = f"实例 '{display_name}' 已停止"
            return jsonify({"success": True, "message": message})
        return jsonify({"success": False, "message": result.get('message', '操作失败')}), 400

    # 对于快速完成的任务
    else:
        command_args = [action]
        if payload:
            command_args.append(payload)

        result = run_script(command_args)

        if result['success']:
            return jsonify({"success": True, "message": result.get('output', '操作成功')})
        else:
            return jsonify({"success": False, "message": result.get('message', '操作失败')}), 400


# --- 文件管理 API ---
@app.route('/api/files/list', methods=['POST'])
@login_required
def api_list_files():
    data = request.get_json()
    path = data.get('path', '')
    result = run_script(['list_files', path])
    if not result['success']:
        return jsonify({"success": False, "error": result['message']}), 500
    try:
        return jsonify(json.loads(result['output']))
    except json.JSONDecodeError:
        return jsonify({"success": False, "error": f"无法解析文件列表: {result['output']}"}), 500


@app.route('/api/files/get_content', methods=['POST'])
@login_required
def api_get_file_content():
    data = request.get_json()
    path = data.get('path')
    if not path: return jsonify({"success": False, "error": "缺少路径"}), 400

    result = run_script(['get_file_content', path])
    if result['success']:
        return Response(result['output'], mimetype='text/plain')
    else:
        return Response(f"读取文件失败: {result['message']}", status=500, mimetype='text/plain')


@app.route('/api/files/save_content', methods=['POST'])
@login_required
def api_save_file_content():
    data = request.get_json()
    path = data.get('path')
    content = data.get('content')
    if not path or content is None: return jsonify({"success": False, "error": "缺少参数"}), 400
    result = run_script(['save_file_content', path], payload_stdin=content)
    return jsonify(result)

@app.route('/api/files/delete', methods=['POST'])
@login_required
def api_delete_path():
    data = request.get_json()
    path = data.get('path')
    if not path: return jsonify({"success": False, "error": "缺少路径"}), 400
    result = run_script(['delete_path', path])
    return jsonify(result)

@app.route('/api/files/create_folder', methods=['POST'])
@login_required
def api_create_folder():
    data = request.get_json()
    path = data.get('path')
    if not path: return jsonify({"success": False, "error": "缺少路径"}), 400
    result = run_script(['create_folder', path])
    return jsonify(result)


@app.route('/api/files/create_file', methods=['POST'])
@login_required
def api_create_file():
    data = request.get_json()
    path = data.get('path')
    if not path: return jsonify({"success": False, "error": "缺少路径"}), 400
    result = run_script(['create_file', path])
    return jsonify(result)

@app.route('/api/files/unzip', methods=['POST'])
@login_required
def api_unzip_file():
    data = request.get_json()
    path = data.get('path')
    if not path: return jsonify({"success": False, "error": "缺少路径"}), 400
    result = run_script(['unzip_file', path])
    return jsonify(result)


@app.route('/api/files/rename', methods=['POST'])
@login_required
def api_rename_path():
    data = request.get_json()
    path, new_name = data.get('path'), data.get('new_name')
    if not path or not new_name:
        return jsonify({"success": False, "error": "缺少路径或新名称"}), 400
    return jsonify(run_script(['rename_path', path, str(new_name).strip()]))


@app.route('/api/files/upload', methods=['POST'])
@login_required
def api_upload_file():
    if 'file' not in request.files:
        return jsonify({"success": False, "error": "没有文件部分"}), 400

    files = request.files.getlist('file')
    current_path = request.form.get('path', '')

    # 磁盘余量预检：空间不足时提前拒绝，避免把"磁盘写满"伪装成莫名失败
    if not _disk_free_ok(UPLOAD_CHUNK_DIR, request.content_length or 0):
        return jsonify({"success": False, "error": "服务器磁盘空间不足，请清理服务器存储后再试"}), 507

    if current_path.startswith('plugin_dir'):
        base_dir = PLUGIN_SOURCE_DIR
        current_path = current_path.replace('plugin_dir', '', 1).lstrip('/')
    elif current_path.startswith('addons_dir'):
        base_dir = ADDONS_DIR
        current_path = current_path.replace('addons_dir', '', 1).lstrip('/')
    else:
        base_dir = SERVER_ROOT

    # 安全检查，防止目录遍历
    if '..' in current_path or current_path.startswith('/'):
        return jsonify({"success": False, "error": "无效的上传目标路径"}), 400

    # 同名文件默认拒绝覆盖（前端冲突预检后用户明确选了"覆盖"才带 overwrite 标志放行）
    overwrite = str(request.form.get('overwrite', '')) in ('1', 'true', 'True')

    uploaded_files_count = 0
    conflicts = []
    for file in files:
        if '..' in file.filename:
            continue

        # 分割路径，并确保每一部分都不是空字符串
        parts = file.filename.split('/')
        if any(not part for part in parts):
            continue

        # 安全地拼接路径
        safe_relative_path = os.path.join(*parts)
        destination_path = os.path.join(base_dir, current_path, safe_relative_path)

        if os.path.exists(destination_path) and not overwrite:
            conflicts.append(file.filename)
            continue

        try:
            destination_folder = os.path.dirname(destination_path)
            os.makedirs(destination_folder, exist_ok=True)

            file.save(destination_path)
            uploaded_files_count += 1
        except Exception as e:
            return jsonify({"success": False, "error": f"保存文件 '{file.filename}' 时出错: {e}"}), 500

    if conflicts:
        preview = '、'.join(conflicts[:5]) + ('等' if len(conflicts) > 5 else '')
        return jsonify({"success": False,
                        "error": f"目标位置已存在同名文件（未选择覆盖）: {preview}",
                        "conflicts": conflicts}), 409

    if uploaded_files_count == 0:
        return jsonify({"success": False, "error": "没有成功上传的文件"}), 400

    return jsonify({"success": True, "message": f"成功上传 {uploaded_files_count} 个文件/文件夹"})


# --- 地图管理 API（文件管理页"地图管理"弹窗） ---
# 模型：vpk 永远只存于地图库 data/Maps；"应用"= 在 addons 创建软链接，删除/清空只操作地图库
# 地图文件名白名单：拒绝控制字符、Windows 保留字符、引号与 shell 危险字符，允许中文/空格/括号
RE_MAP_FILENAME = re.compile(r"^[^\\/:*?\"<>|'`;&$\x00-\x1f\x7f]{1,150}$")


def _clean_map_filename(raw):
    """清洗上传的地图文件名：取纯文件名（兼容反斜杠分隔符）、拒绝隐藏名与危险字符。"""
    name = os.path.basename(str(raw).replace('\\', '/')).strip()
    if not name or name.startswith('.') or name.startswith('-'):
        return None
    return name if RE_MAP_FILENAME.match(name) else None


def _map_script_result(result):
    """把 run_script 的结果转成统一的 JSON 响应。"""
    if result['success']:
        return jsonify({"success": True, "message": result.get('output', '操作成功')})
    return jsonify({"success": False, "message": result.get('message', '操作失败')}), 400


@app.route('/api/maps/list', methods=['POST'])
@login_required
def api_maps_list():
    result = run_script(['list_maps'])
    if not result['success']:
        return jsonify({"success": False, "error": result['message']}), 500
    try:
        data = json.loads(result['output'])
    except json.JSONDecodeError:
        return jsonify({"success": False, "error": "无法解析地图列表"}), 500
    return jsonify(data)


@app.route('/api/maps/upload', methods=['POST'])
@login_required
def api_maps_upload():
    files = request.files.getlist('file')
    if not files:
        return jsonify({"success": False, "error": "没有文件部分"}), 400

    # 磁盘余量预检：空间不足时提前拒绝，避免把"磁盘写满"伪装成莫名失败
    if not _disk_free_ok(MAP_UPLOAD_DIR, request.content_length or 0):
        return jsonify({"success": False, "error": "服务器磁盘空间不足，请清理服务器存储后再试"}), 507

    results = []
    for file in files:
        original_name = _clean_map_filename(file.filename)
        ext = original_name.rsplit('.', 1)[-1].lower() if original_name and '.' in original_name else ''
        if ext not in ('vpk', 'zip', 'rar', '7z'):
            results.append({"filename": file.filename, "success": False,
                            "message": "不支持的格式（仅支持 .vpk/.zip/.rar/.7z）"})
            continue
        # 以随机名暂存，避免并发上传同名文件互相覆盖；原始文件名经参数传给脚本
        staged_path = os.path.join(MAP_UPLOAD_DIR, uuid.uuid4().hex)
        try:
            file.save(staged_path)
            result = run_script(['install_map', os.path.basename(staged_path), original_name])
            if result['success']:
                results.append({"filename": original_name, "success": True,
                                "message": result.get('output', '安装成功')})
            else:
                results.append({"filename": original_name, "success": False,
                                "message": result.get('message', '安装失败')})
        except Exception as e:
            results.append({"filename": original_name, "success": False, "message": f"处理失败: {e}"})
        finally:
            try:
                os.remove(staged_path)  # vpk 已被脚本移走时忽略
            except OSError:
                pass

    has_ok = any(r['success'] for r in results)
    return jsonify({"success": has_ok, "results": results})


# --- 大文件分片上传（地图上传与文件浏览器共用） ---
# 前端对超过 50MB 的文件切片直传：单片失败只重传该片；会话按文件特征（用途/目标路径/
# 文件名/大小/改动时间）哈希建立，同一文件重新上传时自动续传已收分片。
# 小文件维持单请求直传，不经过此通道。

def _disk_free_ok(path, needed):
    """磁盘剩余空间是否足够容纳 needed 字节（含兜底余量）。统计失败时不阻断，由后续写入失败兜底。"""
    try:
        return shutil.disk_usage(path).free >= needed + UPLOAD_DISK_MARGIN
    except OSError:
        return True


def _remove_upload_session(session_dir):
    shutil.rmtree(session_dir, ignore_errors=True)


def _sweep_upload_chunks():
    """清理超过保留时长的分片会话目录（中断上传的遗留）。"""
    now = time.time()
    try:
        entries = os.listdir(UPLOAD_CHUNK_DIR)
    except OSError:
        return
    for name in entries:
        if not RE_UPLOAD_ID.match(name):
            continue
        session_dir = os.path.join(UPLOAD_CHUNK_DIR, name)
        try:
            if now - os.path.getmtime(session_dir) > UPLOAD_CHUNK_TTL:
                shutil.rmtree(session_dir, ignore_errors=True)
        except OSError:
            pass


def _resolve_upload_base(path_str):
    """文件上传目标解析：与 /api/files/upload 的 base_dir 规则一致。返回 (base_dir, rel_inside, err)。"""
    if path_str.startswith('plugin_dir'):
        base_dir, rel = PLUGIN_SOURCE_DIR, path_str[len('plugin_dir'):].lstrip('/')
    elif path_str.startswith('addons_dir'):
        base_dir, rel = ADDONS_DIR, path_str[len('addons_dir'):].lstrip('/')
    else:
        base_dir, rel = SERVER_ROOT, path_str
    if '..' in rel or rel.startswith('/'):
        return None, None, '无效的上传目标路径'
    return base_dir, rel, None


def _next_available_upload_name(dir_path, filename):
    """上传同名冲突时的自动改名：xxx (1).ext 风格，与插件导入的同名规则保持一致。"""
    root, ext = os.path.splitext(filename)
    candidate = filename
    n = 1
    while os.path.exists(os.path.join(dir_path, candidate)):
        candidate = f'{root} ({n}){ext}'
        n += 1
    return candidate


@app.route('/api/files/upload/conflicts', methods=['POST'])
@login_required
def api_upload_conflicts():
    """上传前同名预检：返回目标位置已存在的相对路径及自动改名建议。
    前端据此整批弹一次"覆盖/跳过同名/自动改名"，避免逐文件打断。"""
    data = request.get_json() or {}
    base_dir, rel_inside, err = _resolve_upload_base(str(data.get('path', '')))
    if err:
        return jsonify({"success": False, "error": err}), 400
    rels = data.get('paths')
    if not isinstance(rels, list) or not rels or len(rels) > 10000:
        return jsonify({"success": False, "error": "paths 不合法"}), 400
    conflicts = []
    for rel in rels:
        parts = [p for p in str(rel).replace('\\', '/').split('/') if p and p not in ('.', '..')]
        if not parts:
            continue
        destination = os.path.join(base_dir, rel_inside, *parts)
        if os.path.exists(destination):
            new_name = _next_available_upload_name(os.path.dirname(destination), parts[-1])
            conflicts.append({"path": '/'.join(parts),
                              "suggestion": '/'.join(parts[:-1] + [new_name])})
    return jsonify({"success": True, "conflicts": conflicts})


@app.route('/api/upload/init', methods=['POST'])
@login_required
def api_upload_init():
    data = request.get_json() or {}
    purpose = data.get('purpose')
    filename = str(data.get('filename', ''))
    try:
        size = int(data.get('size', 0))
    except (TypeError, ValueError):
        size = 0
    if purpose not in ('map', 'file'):
        return jsonify({"success": False, "error": "purpose 不合法"}), 400
    if size <= 0 or size > app.config['MAX_CONTENT_LENGTH']:
        return jsonify({"success": False, "error": "文件大小超出可上传范围（单文件上限 2GB）"}), 400

    relative_path = str(data.get('relative_path', '')).replace('\\', '/')
    target_path = str(data.get('path', ''))
    if purpose == 'map':
        clean_name = _clean_map_filename(filename)
        if not clean_name:
            return jsonify({"success": False, "error": "文件名不合法"}), 400
        ext = clean_name.rsplit('.', 1)[-1].lower() if '.' in clean_name else ''
        if ext not in ('vpk', 'zip', 'rar', '7z'):
            return jsonify({"success": False, "error": "不支持的格式（仅支持 .vpk/.zip/.rar/.7z）"}), 400
        filename = clean_name
    else:
        parts = [p for p in relative_path.split('/') if p]
        if not parts or any(p in ('.', '..') for p in parts):
            return jsonify({"success": False, "error": "文件路径不合法"}), 400
        relative_path = '/'.join(parts)
        base_dir, rel_inside, err = _resolve_upload_base(target_path)
        if err:
            return jsonify({"success": False, "error": err}), 400
        # 同名文件默认拒绝覆盖（与直传通道同规则，前端冲突预检后明确选择才放行）
        if not data.get('overwrite'):
            destination = os.path.join(base_dir, rel_inside, *parts)
            if os.path.exists(destination):
                return jsonify({"success": False,
                                "error": f"目标位置已存在同名文件（未选择覆盖）: {relative_path}",
                                "conflicts": [relative_path]}), 409

    # 磁盘余量预检：空间不足直接拒绝，避免传到一半才失败
    if not _disk_free_ok(UPLOAD_CHUNK_DIR, size):
        return jsonify({"success": False, "error": "服务器磁盘空间不足，请清理服务器存储后再试"}), 507

    _sweep_upload_chunks()

    key = hashlib.sha256('\n'.join([purpose, target_path, relative_path,
                                    filename, str(size), str(data.get('last_modified', ''))])
                         .encode('utf-8')).hexdigest()[:32]
    session_dir = os.path.join(UPLOAD_CHUNK_DIR, key)
    chunks_dir = os.path.join(session_dir, 'chunks')
    os.makedirs(chunks_dir, exist_ok=True)
    total_chunks = (size + UPLOAD_CHUNK_SIZE - 1) // UPLOAD_CHUNK_SIZE
    meta_path = os.path.join(session_dir, 'meta.json')
    if not os.path.exists(meta_path):
        meta = {"filename": filename, "size": size, "purpose": purpose, "path": target_path,
                "relative_path": relative_path, "chunk_size": UPLOAD_CHUNK_SIZE,
                "total_chunks": total_chunks}
        with open(meta_path, 'w', encoding='utf-8') as fh:
            json.dump(meta, fh, ensure_ascii=False)
    received = sorted(int(n[:-5]) for n in os.listdir(chunks_dir) if n.endswith('.part'))
    return jsonify({"success": True, "upload_id": key, "chunk_size": UPLOAD_CHUNK_SIZE,
                    "total_chunks": total_chunks, "received": received})


@app.route('/api/upload/chunk', methods=['POST'])
@login_required
def api_upload_chunk():
    upload_id = request.args.get('upload_id', '')
    if not RE_UPLOAD_ID.match(upload_id):
        return jsonify({"success": False, "error": "upload_id 不合法"}), 400
    session_dir = os.path.join(UPLOAD_CHUNK_DIR, upload_id)
    meta_path = os.path.join(session_dir, 'meta.json')
    if not os.path.exists(meta_path):
        return jsonify({"success": False, "error": "上传会话不存在或已过期，请重新上传"}), 404
    with open(meta_path, encoding='utf-8') as fh:
        meta = json.load(fh)
    try:
        index = int(request.args.get('index', ''))
    except ValueError:
        return jsonify({"success": False, "error": "分片序号不合法"}), 400
    total = meta['total_chunks']
    if not 0 <= index < total:
        return jsonify({"success": False, "error": "分片序号超出范围"}), 400
    data = request.get_data(cache=False)
    if len(data) > meta['chunk_size']:
        return jsonify({"success": False, "error": "分片体积超出限制"}), 400
    chunks_dir = os.path.join(session_dir, 'chunks')
    final_path = os.path.join(chunks_dir, f'{index}.part')
    tmp_path = final_path + '.tmp'
    with open(tmp_path, 'wb') as fh:
        fh.write(data)
    os.replace(tmp_path, final_path)
    received = sum(1 for n in os.listdir(chunks_dir) if n.endswith('.part'))
    return jsonify({"success": True, "received": received, "total_chunks": total})


@app.route('/api/upload/complete', methods=['POST'])
@login_required
def api_upload_complete():
    data = request.get_json() or {}
    upload_id = str(data.get('upload_id', ''))
    if not RE_UPLOAD_ID.match(upload_id):
        return jsonify({"success": False, "error": "upload_id 不合法"}), 400
    session_dir = os.path.join(UPLOAD_CHUNK_DIR, upload_id)
    meta_path = os.path.join(session_dir, 'meta.json')
    if not os.path.exists(meta_path):
        return jsonify({"success": False, "error": "上传会话不存在或已过期，请重新上传"}), 404
    with open(meta_path, encoding='utf-8') as fh:
        meta = json.load(fh)
    chunks_dir = os.path.join(session_dir, 'chunks')
    total = meta['total_chunks']
    missing = [i for i in range(total) if not os.path.exists(os.path.join(chunks_dir, f'{i}.part'))]
    if missing:
        return jsonify({"success": False, "error": f"还有 {len(missing)} 个分片未上传完成"}), 400
    if not _disk_free_ok(session_dir, meta['size']):
        return jsonify({"success": False, "error": "服务器磁盘空间不足，请清理服务器存储后再试（已传分片保留，可续传）"}), 507

    # 顺序组装分片为完整文件，体积不符说明传输损坏，整体作废
    final_bin = os.path.join(session_dir, 'final.bin')
    try:
        with open(final_bin, 'wb') as out:
            for i in range(total):
                with open(os.path.join(chunks_dir, f'{i}.part'), 'rb') as part:
                    shutil.copyfileobj(part, out)
    except OSError:
        return jsonify({"success": False, "error": "服务器磁盘空间不足，组装写入失败，请清理后重试（已传分片保留，可续传）"}), 507
    if os.path.getsize(final_bin) != meta['size']:
        _remove_upload_session(session_dir)
        return jsonify({"success": False, "error": "分片组装后与原始文件大小不一致，请重新上传"}), 400

    if meta['purpose'] == 'map':
        # 与 /api/maps/upload 相同的入库管道：随机名暂存 → install_map → 回收暂存
        staged_path = os.path.join(MAP_UPLOAD_DIR, uuid.uuid4().hex)
        os.replace(final_bin, staged_path)
        try:
            result = run_script(['install_map', os.path.basename(staged_path), meta['filename']])
        finally:
            try:
                os.remove(staged_path)
            except OSError:
                pass
        _remove_upload_session(session_dir)
        return jsonify({"success": result['success'],
                        "results": [{"filename": meta['filename'], "success": result['success'],
                                     "message": result.get('output', '安装成功') if result['success']
                                     else result.get('message', '安装失败')}]})

    # purpose == 'file'：落到文件浏览器的目标目录
    base_dir, rel_inside, err = _resolve_upload_base(meta.get('path', ''))
    if err:
        _remove_upload_session(session_dir)
        return jsonify({"success": False, "error": err}), 400
    parts = [p for p in meta.get('relative_path', '').split('/') if p]
    destination = os.path.join(base_dir, rel_inside, *parts)
    os.makedirs(os.path.dirname(destination), exist_ok=True)
    try:
        os.replace(final_bin, destination)
    except OSError as e:
        return jsonify({"success": False, "error": f"保存文件 '{meta['filename']}' 时出错: {e}"}), 500
    _remove_upload_session(session_dir)
    return jsonify({"success": True, "message": f"已上传 {meta['filename']}"})


@app.route('/api/upload/abort', methods=['POST'])
@login_required
def api_upload_abort():
    data = request.get_json() or {}
    upload_id = str(data.get('upload_id', ''))
    if not RE_UPLOAD_ID.match(upload_id):
        return jsonify({"success": False, "error": "upload_id 不合法"}), 400
    _remove_upload_session(os.path.join(UPLOAD_CHUNK_DIR, upload_id))
    return jsonify({"success": True})


@app.route('/api/maps/rename', methods=['POST'])
@login_required
def api_maps_rename():
    data = request.get_json() or {}
    old = _clean_map_filename(data.get('old', ''))
    new = str(data.get('new', '')).strip()
    if not old:
        return jsonify({"success": False, "message": "原文件名不合法"}), 400
    if (not new or new.startswith('.') or new.startswith('-')
            or not RE_MAP_FILENAME.match(new) or not new.lower().endswith('.vpk')):
        return jsonify({"success": False, "message": "新名称不合法（不能包含 \\ / : * ? \" < > | ' ` ; & $ 等字符，且须以 .vpk 结尾）"}), 400
    return _map_script_result(run_script(['rename_map', old, new]))


@app.route('/api/maps/apply', methods=['POST'])
@login_required
def api_maps_apply():
    data = request.get_json() or {}
    name = _clean_map_filename(data.get('name', ''))
    if not name:
        return jsonify({"success": False, "message": "文件名不合法"}), 400
    return _map_script_result(run_script(['apply_map', name]))


@app.route('/api/maps/unapply', methods=['POST'])
@login_required
def api_maps_unapply():
    data = request.get_json() or {}
    name = _clean_map_filename(data.get('name', ''))
    if not name:
        return jsonify({"success": False, "message": "文件名不合法"}), 400
    return _map_script_result(run_script(['unapply_map', name]))


@app.route('/api/maps/delete', methods=['POST'])
@login_required
def api_maps_delete():
    data = request.get_json() or {}
    name = _clean_map_filename(data.get('name', ''))
    if not name:
        return jsonify({"success": False, "message": "文件名不合法"}), 400
    return _map_script_result(run_script(['delete_map', name]))


@app.route('/api/maps/clear', methods=['POST'])
@login_required
def api_maps_clear():
    return _map_script_result(run_script(['clear_maps']))


# --- 命名更新：解析 vpk 内部标题并批量重命名选中的地图文件 ---
# vpk 解析库（ValvePython/vpk，纯 Python）为可选依赖：未安装时"命名更新"降级为保持原名
try:
    import vpk as vpk_lib
except ImportError:
    vpk_lib = None

MAPS_LIBRARY_DIR = os.path.join(DATA_DIR, 'Maps')
RE_ILLEGAL_FILENAME_CHARS = re.compile(r"[\\/:*?\"<>|'`;&$\x00-\x1f\x7f]")
RE_DISPLAY_TITLE = re.compile(r'(?i)"DisplayTitle"\s*"([^"]*)"')
RE_ADDON_TITLE = re.compile(r'(?i)"addonTitle"\s*"([^"]*)"')


def _decode_keyvalues(data):
    """vpk 内文本文件解码：依次尝试常见编码，均失败时回退 latin-1"""
    for enc in ('utf-8-sig', 'utf-16-le', 'gb18030'):
        try:
            return data.decode(enc).replace('\x00', '')
        except (UnicodeDecodeError, UnicodeError):
            continue
    return data.decode('latin-1', errors='replace').replace('\x00', '')


def _sanitize_name_component(text):
    """标题/前缀清洗为合法文件名片段：点号→下划线、非法字符→空格、压缩空白。
    仅去掉开头的 - 和 .（防选项注入/隐藏文件），保留首尾下划线（如前缀 map_）"""
    t = (text or '').replace('.', '_')
    t = RE_ILLEGAL_FILENAME_CHARS.sub(' ', t)
    t = re.sub(r'\s+', ' ', t).strip().lstrip('-.')
    return re.sub(r'\s+', ' ', t).strip()[:120]


def _parse_map_title(path):
    """解析 vpk 内的地图展示名：missions DisplayTitle → addoninfo addonTitle。
    返回 (title, source)，source 为 missions/addoninfo/none/error"""
    if vpk_lib is None:
        return '', 'error'
    try:
        pak = vpk_lib.open(path)
        keys = list(pak)
    except Exception:
        return '', 'error'
    mission_key = next((k for k in keys if k.lower().startswith('missions/') and k.lower().endswith('.txt')), None)
    if mission_key:
        try:
            m = RE_DISPLAY_TITLE.search(_decode_keyvalues(pak[mission_key].read()))
            if m and m.group(1).strip():
                return m.group(1).strip(), 'missions'
        except Exception:
            pass
    addon_key = next((k for k in keys if k.lower() == 'addoninfo.txt'), None)
    if addon_key:
        try:
            m = RE_ADDON_TITLE.search(_decode_keyvalues(pak[addon_key].read()))
            if m and m.group(1).strip():
                return m.group(1).strip(), 'addoninfo'
        except Exception:
            pass
    return '', 'none'


def _natural_key(name):
    """文件名自然排序键：数字段按数值比较（Part2 排在 Part10 前）"""
    return [int(p) if p.isdigit() else p.lower() for p in re.split(r'(\d+)', name)]


def _build_rename_display_plan(names, prefix):
    """为选中的地图库文件生成重命名计划（不执行）。
    返回 (plan, error)；plan 元素含 old/new/changed/source/suffix_added/source_note。
    同名分组：组内 1 个直接用标题名，≥2 个全部追加 (n)；与库内既有文件冲突自动跳号。"""
    prefix_clean = _sanitize_name_component(prefix)
    selected = []
    for raw in names:
        name = _clean_map_filename(raw)
        if not name:
            return None, f'文件名不合法: {raw}'
        if not os.path.isfile(os.path.join(MAPS_LIBRARY_DIR, name)):
            return None, f'地图库中不存在: {name}'
        selected.append(name)
    selected = list(dict.fromkeys(selected))

    entries = []
    for name in selected:
        title, source = _parse_map_title(os.path.join(MAPS_LIBRARY_DIR, name))
        entries.append({'old': name, 'title': title, 'source': source})

    # 库内占用名（不含 .vpk 后缀），参与本次重命名的旧名视为可释放
    # 注意两侧都要去掉 .vpk 后缀再比较，否则文件自己的名字排不掉、重复执行会被误加序号
    selected_bare = {n[:-4] for n in selected if n.lower().endswith('.vpk')}
    occupied = {f[:-4] for f in os.listdir(MAPS_LIBRARY_DIR) if f.lower().endswith('.vpk')} - selected_bare

    # 分组：前缀+清洗标题相同的归为一组；无标题的保持原名
    groups = {}
    for e in entries:
        if e['source'] in ('missions', 'addoninfo'):
            base = (prefix_clean + _sanitize_name_component(e['title'])).strip()
            if base:
                e['base'] = base
                groups.setdefault(base, []).append(e)
                continue
        e['source'] = 'none' if e['source'] == 'missions' or e['source'] == 'addoninfo' else e['source']

    for base, group in groups.items():
        group.sort(key=lambda x: _natural_key(x['old']))
        n = 1
        for e in group:
            if len(group) == 1:
                target, suffix = base, False
                while target in occupied:
                    target, suffix = f'{base} ({n})', True
                    n += 1
            else:
                target, suffix = f'{base} ({n})', True
                while target in occupied:
                    n += 1
                    target = f'{base} ({n})'
                n += 1
            occupied.add(target)
            e['target_base'] = target
            e['suffix_added'] = suffix

    src_text = {'missions': 'missions DisplayTitle', 'addoninfo': 'addoninfo addonTitle',
                'none': '无 missions/addoninfo，保持原名', 'error': '解析失败，保持原名'}
    plan = []
    for e in entries:
        target_base = e.get('target_base')
        new = target_base + '.vpk' if target_base else e['old']
        changed = new != e['old']
        parts = [src_text.get(e['source'], '')]
        if e.get('suffix_added'):
            parts.append('同名追加序号')
        if not changed and e['source'] in ('missions', 'addoninfo'):
            parts.append('已是当前名称')
        plan.append({'old': e['old'], 'new': new, 'changed': changed,
                     'source_note': ' · '.join(p for p in parts if p)})
    return plan, None


def _execute_rename_display(plan):
    """两阶段执行：先全部改为临时名再改目标名，规避批内旧名/新名互撞的顺序问题。"""
    results = []
    pending = []
    for e in plan:
        if not e['changed']:
            results.append({'old': e['old'], 'new': e['old'], 'success': True,
                            'message': e.get('source_note', '无需更改')})
            continue
        tmp = 'tmp_' + uuid.uuid4().hex[:12] + '.vpk'
        r = run_script(['rename_map', e['old'], tmp])
        if not r['success']:
            results.append({'old': e['old'], 'new': e['new'], 'success': False,
                            'message': r.get('message', '重命名失败')})
        else:
            e['tmp'] = tmp
            pending.append(e)
    for e in pending:
        r = run_script(['rename_map', e['tmp'], e['new']])
        if r['success']:
            results.append({'old': e['old'], 'new': e['new'], 'success': True,
                            'message': e.get('source_note', '')})
        else:
            msg = r.get('message', '重命名失败')
            rb = run_script(['rename_map', e['tmp'], e['old']])
            if not rb['success']:
                msg += '；恢复原名失败，请手动检查临时文件 ' + e['tmp']
            results.append({'old': e['old'], 'new': e['new'], 'success': False, 'message': msg})
    return results


@app.route('/api/maps/rename_display', methods=['POST'])
@login_required
def api_maps_rename_display():
    data = request.get_json() or {}
    names = data.get('names')
    prefix = str(data.get('prefix', ''))
    execute = bool(data.get('execute'))
    if not isinstance(names, list) or not names:
        return jsonify({"success": False, "message": "未选择任何地图文件"}), 400
    plan, error = _build_rename_display_plan(names, prefix)
    if error:
        return jsonify({"success": False, "message": error}), 400
    if not execute:
        return jsonify({"success": True,
                        "plan": [{'old': e['old'], 'new': e['new'], 'changed': e['changed'],
                                  'source_note': e.get('source_note', '')} for e in plan]})
    results = _execute_rename_display(plan)
    return jsonify({"success": all(r['success'] for r in results), "results": results})


# --- 插件识别导入（文件管理页"可安装插件目录"的"识别导入"动作） ---
# 模型（v4，路径落点规则）：安装 = rsync 把插件文件夹内容按相对路径铺到服务器根目录，
# 因此只校验"路径落点"、不判定内容性质：文件夹内全部文件（去掉可选的 left4dead2 包装层后）
# 都位于 addons/** 或 cfg/** 下、且包装形式统一，即为可导入插件——无论是否含 .smx
# （扩展二进制、gamedata/依赖数据、纯 cfg 配置包、vpk MOD 均天然放行）。
#   - 缺 left4dead2 层（addons/ 或 cfg/ 开头）→ 导入时自动补层；
#   - 插件根直接散落文件（路径落点非法）→ 该层不作根，下钻取其中合法子树；散落文件计垃圾；
#   - 整合包外壳（如 JS-MODS）自然穿透；整包"已启用合集"与逐项插件同批出现时，
#     以"同批同名 .smx"提醒用户只勾选其一；
#   - 裸 .smx 单文件 → 自动包装为 <插件名>/left4dead2/addons/sourcemod/plugins/<插件名>.smx；
#   - JSGME 备份目录（!BACKUP/!INSTLOGS）与 __MACOSX/.DS_Store 等垃圾一律静默跳过；
#   - 导入完成后暂存与库内源压缩包一并清理；仅部分导入时暂存保留供补导，源包待全部导入后清除。
PLUGIN_UPLOAD_DIR = os.path.join(DATA_DIR, 'plugin_uploads')
os.makedirs(PLUGIN_UPLOAD_DIR, exist_ok=True)
PLUGIN_UPLOAD_TTL = 6 * 3600  # 上传后一直未确认导入的暂存包保留时长，超时在下次上传时清理
# 支持的压缩包后缀（长后缀在前，保证 .tar.gz/.tar.bz2 优先于 .tar 匹配）
ARCHIVE_SUFFIXES = ('.tar.gz', '.tar.bz2', '.tgz', '.tbz2', '.zip', '.rar', '.7z', '.tar')
GAME_DIR_NAME = 'left4dead2'    # 插件判定唯一标志：候选节点直接子级含此目录层
# 外壳目录排除：JSGME 备份/日志镜像（内部也含 left4dead2 层，不排除会识别出假插件）与 mac 压缩垃圾
JUNK_DIR_NAMES = {'__macosx', '!backup', '!instlogs'}
# 插件名即 Available_Plugins 下的一级目录名，禁止路径分隔符与 shell 危险字符
RE_PLUGIN_IMPORT_NAME = re.compile(r"^[^\\/:*?\"<>|'`;&$\x00-\x1f\x7f]{1,100}$")
RE_IMPORT_ID = re.compile(r'^[a-f0-9]{32}$')

# --- 插件配置/依赖文件编辑（插件管理页"配置"按钮，编辑的是库内主副本） ---
# 可编辑目录白名单（相对插件根）与扩展名白名单；路径与内容之外的一切（.smx/.dll、
# scripting 源码、超大文件）一律拒绝
PLUGIN_EDIT_DIRS = ('left4dead2/cfg/', 'left4dead2/addons/sourcemod/configs/',
                    'left4dead2/addons/sourcemod/data/',
                    'left4dead2/addons/sourcemod/translations/',
                    'left4dead2/addons/sourcemod/gamedata/')
PLUGIN_EDIT_EXTS = ('.cfg', '.txt', '.ini')
PLUGIN_EDIT_MAX_BYTES = 1024 * 1024  # 编辑器不处理超过 1MB 的文件


# --- 服务器命名插件联动（l4d2_hostname.sp 的 configs/l4d2_hostname.txt） ---
# 旧版插件（v1.1.2）：文件为纯文本，内容即服名（读取循环覆盖缓冲 → 最后一行生效，
# 自身写入不带换行）；新版插件（v1.1.2b）：文件为 Valve KeyValues（根 "hostport"），
# 按端口建节、每节一个 "ServerName"，"default" 节兜底。面板以文件格式自描述为准做
# 双向同步，无需关心装的是哪版插件：解析成功 = 新版模式（实例绑定自身端口节）；
# 失败 = 旧版模式（单服名，仅单实例可绑定，多实例由前端禁用开关）。写入一律原子
# 替换 + UTF-8，名字按 UTF-8 边界截断到插件读取缓冲（128 字节）的安全长度内，
# 避免插件侧 ReadLine 拦腰截断出乱码。
HOSTNAME_TXT_PATH = os.path.join(SERVER_ROOT, 'left4dead2', 'addons',
                                 'sourcemod', 'configs', 'l4d2_hostname.txt')
HOSTNAME_NAME_MAX_BYTES = 120   # 插件 ReadLine 缓冲 128 字节，留 8 字节余量
# stat 缓存：(mtime_ns, size) 不变时直接复用上次解析结果——游戏内 !host 改文件
# 后 stat 必变，外部编辑也能即时反映；面板每 10s 轮询读取零成本
_HOSTNAME_CACHE = {"stat": None, "info": None}


def _kv_tokenize(text):
    """Valve KeyValues 词法：返回 token 序列（'{'/'}' 或 ('str', 值)）。
    引号串支持 \\\" \\\\ \\n 等转义，// 到行尾为注释，裸词按无引号键兼容；
    词法异常（未闭合引号/悬挂转义）抛 ValueError。"""
    tokens = []
    i, n = 0, len(text)
    while i < n:
        c = text[i]
        if c.isspace():
            i += 1
        elif text.startswith('//', i):
            nl = text.find('\n', i)
            i = n if nl == -1 else nl + 1
        elif c in '{}':
            tokens.append((c, c))
            i += 1
        elif c == '"':
            i += 1
            buf = []
            closed = False
            while i < n:
                c = text[i]
                if c == '\\' and i + 1 < n:
                    nxt = text[i + 1]
                    buf.append({'n': '\n', 't': '\t', 'r': '\r'}.get(nxt, nxt))
                    i += 2
                elif c == '"':
                    i += 1
                    closed = True
                    break
                else:
                    buf.append(c)
                    i += 1
            if not closed:
                raise ValueError('未闭合的引号串')
            tokens.append(('str', ''.join(buf)))
        else:
            j = i
            while j < n and not text[j].isspace() and text[j] not in '{}"':
                j += 1
            tokens.append(('str', text[i:j]))
            i = j
    return tokens


def _parse_hostname_kv(text):
    """解析新版 KeyValues 文件：根块内若干 "节名" { "ServerName" "值" } 节。
    成功返回 {节名: ServerName}（仅收字符串值的 ServerName 键），任何结构
    不符（含旧版纯文本、空文件、损坏内容）返回 None。"""
    try:
        tokens = _kv_tokenize(text)
    except ValueError:
        return None
    pos = 0

    def take():
        nonlocal pos
        if pos >= len(tokens):
            raise ValueError('内容意外结束')
        tok = tokens[pos]
        pos += 1
        return tok

    try:
        if take()[0] != 'str' or take()[0] != '{':   # 根名任意（插件读自己的文件）
            return None
        sections = {}
        while True:
            kind, sec = take()
            if kind == '}':
                break
            if kind != 'str' or take()[0] != '{':
                return None
            entry = {}
            while True:
                k2, key = take()
                if k2 == '}':
                    break
                v2, val = take()
                if k2 != 'str' or v2 != 'str':
                    return None
                entry[key] = val
            if isinstance(entry.get('ServerName'), str):
                sections[sec] = entry['ServerName']
        if pos != len(tokens):   # 根块闭合后仍有残余内容：不当作新版文件
            return None
    except ValueError:
        return None
    return sections or None


def _hostname_plugin_info():
    """命名插件文件状态（带 stat 缓存）：{exists, mode, names}。mode 三态：
    'kv' = 新版 KeyValues（names 为 {端口串/'default': 名字}）；'plain' = 旧版
    纯文本（names 为 {'plain': 最后一行非空内容}）；'broken' = KV 形状（以引号
    开头）但结构解析失败——典型如编辑时丢了根块闭合括号，此时绝不回退 plain
    （否则会把 '}' 这样的结构行当服名读出），交给前端禁用开关并提示修复。"""
    try:
        st = os.stat(HOSTNAME_TXT_PATH)
        stat_key = (st.st_mtime_ns, st.st_size)
    except OSError:
        return {'exists': False, 'mode': None, 'names': {}}
    if _HOSTNAME_CACHE['stat'] == stat_key:
        return _HOSTNAME_CACHE['info']
    try:
        with open(HOSTNAME_TXT_PATH, encoding='utf-8', errors='replace') as fh:
            text = fh.read().lstrip('\ufeff')   # 容忍编辑器写入的 UTF-8 BOM
        kv = _parse_hostname_kv(text)
        if kv is not None:
            info = {'exists': True, 'mode': 'kv', 'names': kv}
        elif text.lstrip().startswith('"'):
            info = {'exists': True, 'mode': 'broken', 'names': {}}
        else:
            lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
            info = {'exists': True, 'mode': 'plain',
                    'names': {'plain': lines[-1] if lines else ''}}
    except OSError:
        info = {'exists': False, 'mode': None, 'names': {}}
    _HOSTNAME_CACHE['stat'] = stat_key
    _HOSTNAME_CACHE['info'] = info
    return info


def _hostname_clip(name):
    """按 UTF-8 字符边界截断到插件读取缓冲的安全字节长度内。"""
    raw = str(name).encode('utf-8')[:HOSTNAME_NAME_MAX_BYTES]
    while raw:
        try:
            return raw.decode('utf-8')
        except UnicodeDecodeError:
            raw = raw[:-1]   # 截掉被拦腰截断的多字节序列尾部
    return ''


def _hostname_kv_escape(text):
    return str(text).replace('\\', '\\\\').replace('"', '\\"')


def _render_hostname_kv(sections, port, name):
    """渲染新版 KeyValues：port 节的 ServerName 更新为 name（节不存在则追加），
    其余节原样保留。sections 为 {节名: 名字}，返回完整文件文本。"""
    merged = dict(sections)
    merged[str(port)] = name
    lines = ['"hostport"', '{']
    for sec, val in merged.items():
        lines.append(f'\t"{_hostname_kv_escape(sec)}"')
        lines.append('\t{')
        lines.append(f'\t\t"ServerName"\t"{_hostname_kv_escape(val)}"')
        lines.append('\t}')
    lines.append('}')
    return '\n'.join(lines) + '\n'


def _atomic_write_text(path, text):
    """原子写文本文件（临时文件 + os.replace），不转换换行符。"""
    tmp = path + '.panel-tmp'
    with open(tmp, 'w', encoding='utf-8', newline='') as fh:
        fh.write(text)
    os.replace(tmp, path)


def _write_hostname_file(port, name):
    """把 name 写入命名插件文件（调用方已确认文件存在且可绑定）：
    kv 模式更新端口节；plain 模式整文件覆写为单行（不带尾换行——旧版插件
    读取到空行会把服名设成空）。成功返回 True。"""
    hp = _hostname_plugin_info()
    if not hp['exists']:
        return False
    name = _hostname_clip(name)
    try:
        if hp['mode'] == 'kv':
            _atomic_write_text(HOSTNAME_TXT_PATH, _render_hostname_kv(hp['names'], port, name))
        elif hp['mode'] == 'plain':
            _atomic_write_text(HOSTNAME_TXT_PATH, name)
        else:
            return False
    except OSError:
        return False
    return True


def _match_archive_suffix(filename):
    lower = str(filename).lower()
    for suffix in ARCHIVE_SUFFIXES:
        if lower.endswith(suffix):
            return suffix
    return None


def _sanitize_plugin_import_name(raw):
    name = os.path.basename(str(raw).replace('\\', '/')).strip()
    if not name or name.startswith('.') or name.startswith('-'):
        return None
    return name if RE_PLUGIN_IMPORT_NAME.match(name) else None


def _safe_join_under(root, rel_name):
    """把压缩包成员路径拼到 root 下；越出 root（绝对路径、.. 穿越）时返回 None。"""
    root_abs = os.path.abspath(root)
    parts = [p for p in str(rel_name).replace('\\', '/').split('/') if p not in ('', '.')]
    target = os.path.abspath(os.path.join(root_abs, *parts)) if parts else root_abs
    if target != root_abs and not target.startswith(root_abs + os.sep):
        return None
    return target


def _extract_zip(archive_path, dest_root):
    try:
        with zipfile.ZipFile(archive_path) as zf:
            for info in zf.infolist():
                name = info.filename
                if not info.flag_bits & 0x800:
                    # Windows 下压缩的中文包常以 GBK 存名且无 UTF-8 标志，zipfile 会误按 cp437 解码
                    try:
                        name = name.encode('cp437').decode('gbk')
                    except (UnicodeEncodeError, UnicodeDecodeError):
                        pass
                target = _safe_join_under(dest_root, name)
                if target is None:
                    return '压缩包内包含不安全的路径成员，已中止解压'
                if name.endswith(('/', '\\')):
                    os.makedirs(target, exist_ok=True)
                    continue
                os.makedirs(os.path.dirname(target), exist_ok=True)
                with zf.open(info) as src, open(target, 'wb') as out:
                    shutil.copyfileobj(src, out)
        return None
    except zipfile.BadZipFile:
        return '压缩包损坏或不是有效的 zip 文件，请重新打包后再上传'
    except RuntimeError as e:
        if 'encrypt' in str(e).lower() or 'password' in str(e).lower():
            return '压缩包有密码，请解密后重新打包再上传'
        return f'解压失败: {e}'
    except NotImplementedError:
        return '压缩包使用了不支持的压缩算法，请改用常规 zip 压缩'


def _extract_tar(archive_path, dest_root):
    # 'r:*' 自动识别 tar/gzip/bzip2；跳过软链接等特殊成员（插件包无需，且可借此防御链接逃逸）
    try:
        with tarfile.open(archive_path, 'r:*') as tf:
            for member in tf.getmembers():
                if not (member.isreg() or member.isdir()):
                    continue
                target = _safe_join_under(dest_root, member.name)
                if target is None:
                    return '压缩包内包含不安全的路径成员，已中止解压'
                if member.isdir():
                    os.makedirs(target, exist_ok=True)
                    continue
                os.makedirs(os.path.dirname(target), exist_ok=True)
                src = tf.extractfile(member)
                if src is None:
                    continue
                with src, open(target, 'wb') as out:
                    shutil.copyfileobj(src, out)
        return None
    except tarfile.ReadError:
        return '压缩包损坏或不是有效的 tar/tar.gz/tar.bz2 文件，请重新打包后再上传'


def _extract_external(tool, apt_package, archive_path, dest_root):
    """调用系统解压工具处理 rar/7z（与文件管理页解压功能依赖相同工具）。"""
    if shutil.which(tool) is None:
        return f"服务器未安装 {tool}，无法解压该格式（需先安装 {apt_package}）"
    if tool == 'unrar':
        command = ['unrar', 'x', '-o+', str(archive_path), dest_root + os.sep]
    else:
        command = ['7z', 'x', f'-o{dest_root}', '-y', str(archive_path)]
    proc = subprocess.run(command, capture_output=True, text=True,
                          encoding='utf-8', errors='replace')
    if proc.returncode != 0:
        blob = ((proc.stderr or '') + (proc.stdout or '')).lower()
        if 'wrong password' in blob or 'encrypted' in blob:
            return '压缩包有密码，请解密后重新打包再上传'
        tail = [line for line in (proc.stderr or proc.stdout or '').strip().splitlines() if line][-3:]
        return '解压失败: ' + ('；'.join(tail) or '未知错误')
    return None


def _extract_plugin_archive(archive_path, suffix, dest_root):
    """解压插件包到 dest_root，成功返回 None，失败返回错误信息。"""
    try:
        if suffix == '.zip':
            return _extract_zip(archive_path, dest_root)
        if suffix == '.rar':
            return _extract_external('unrar', 'unrar', archive_path, dest_root)
        if suffix == '.7z':
            return _extract_external('7z', 'p7zip-full', archive_path, dest_root)
        return _extract_tar(archive_path, dest_root)
    except Exception as e:
        return f'解压失败: {e}'


def _rel_children(path):
    try:
        return sorted(os.listdir(path), key=str.lower)
    except OSError:
        return []


def _is_plugin_node(node_abs):
    """插件唯一判定：节点直接子级含 left4dead2 目录（不区分大小写）即视为一个插件，
    整节点原样入库；节点内至少含 1 个文件（防空壳）。
    返回 (是否插件, 节点内 .smx 文件名列表)——smx 清单仅用于同源版本冲突提醒。"""
    if not any(entry.lower() == GAME_DIR_NAME and os.path.isdir(os.path.join(node_abs, entry))
               for entry in _rel_children(node_abs)):
        return False, []
    smx_files = []
    file_count = 0
    for _dirpath, dirnames, filenames in os.walk(node_abs):
        dirnames[:] = [d for d in dirnames if d.lower() not in JUNK_DIR_NAMES]
        file_count += len(filenames)
        smx_files.extend(f for f in filenames if f.lower().endswith('.smx'))
    return file_count > 0, smx_files


def _existing_smx_index():
    """现有插件库与安装回执中的 .smx 文件名索引：小写 basename -> {插件名}，用于同源版本冲突提醒。"""
    index = {}
    for entry in _rel_children(PLUGIN_SOURCE_DIR):
        full = os.path.join(PLUGIN_SOURCE_DIR, entry)
        if not os.path.isdir(full):
            continue
        for dirpath, _dirnames, filenames in os.walk(full):
            for f in filenames:
                if f.lower().endswith('.smx'):
                    index.setdefault(f.lower(), set()).add(entry)
    receipts_dir = os.path.join(DATA_DIR, 'Installed_Receipts')
    for entry in _rel_children(receipts_dir):
        if not entry.endswith('.receipt'):
            continue
        try:
            with open(os.path.join(receipts_dir, entry), encoding='utf-8', errors='replace') as fh:
                for line in fh:
                    basename = line.strip().replace('\\', '/').split('/')[-1]
                    if basename.lower().endswith('.smx'):
                        index.setdefault(basename.lower(), set()).add(entry[:-len('.receipt')])
        except OSError:
            continue
    return index


def _conflict_names(smx_basenames, index):
    hits = set()
    for n in smx_basenames:
        hits |= index.get(n.lower(), set())
    return sorted(hits)


def _scan_archive_content(unpacked_root, default_name):
    """按唯一判定规则扫描解压结果，返回 {'candidates': [...]}。
    candidate：type('plugin') + folder(''/相对路径) + name；节点直接子级含
    left4dead2 目录即视为一个插件，整节点原样入库、不再下钻；其余内容一律无视。"""
    roots = []      # (rel, smx_files)：含 left4dead2 层的插件节点

    def find_roots(node_abs, rel):
        valid, smx_files = _is_plugin_node(node_abs)
        if valid:
            roots.append((rel, sorted(set(smx_files))))
            return  # 已认领的插件节点不再下钻（内部子目录不是独立插件）
        for entry in _rel_children(node_abs):
            if entry.lower() in JUNK_DIR_NAMES:
                continue
            child_abs = os.path.join(node_abs, entry)
            if os.path.isdir(child_abs):
                find_roots(child_abs, f'{rel}/{entry}' if rel else entry)

    find_roots(unpacked_root, '')

    # 组装插件候选；节点名恰为 left4dead2 时改用其上层文件夹名（缺省用压缩包名）。
    # 名字含非法字符（zip 内文件夹名在 Linux 下几乎不受限）不拒绝入库流程，而是标记
    # name_invalid 交由导入弹窗提示用户改名
    candidates = []
    for rel, smx_files in roots:
        base = rel.split('/')[-1] if rel else ''
        if base.lower() == GAME_DIR_NAME:
            parent_rel = '/'.join(rel.split('/')[:-1])
            name = parent_rel.split('/')[-1] if parent_rel else (default_name or 'plugin_pack')
        else:
            name = base or default_name or 'plugin_pack'
        candidates.append({'type': 'plugin', 'folder': rel, 'name': name,
                           'smx_files': smx_files,
                           'name_invalid': not _sanitize_plugin_import_name(name)})

    # 同批"覆盖式重复"提醒：某候选项的 .smx 集合完整覆盖另一个候选项时（典型：整包
    # "已启用合集" ⊇ 逐项插件），只给覆盖方打标，避免逐项插件满屏警告；两个候选完全
    # 同名集（如同一插件的两个版本）则互相打标。不自动取舍，交由用户勾选
    smx_sets = [set(c['smx_files']) for c in candidates]
    for i, s_i in enumerate(smx_sets):
        if not s_i:
            continue
        if any(j != i and s_j and s_j <= s_i for j, s_j in enumerate(smx_sets)):
            candidates[i]['batch_conflict'] = True

    return {'candidates': candidates}


def _sweep_plugin_uploads():
    """清理超过保留时长的暂存包（上传后一直未确认导入的遗留目录）。"""
    now = time.time()
    try:
        entries = os.listdir(PLUGIN_UPLOAD_DIR)
    except OSError:
        return
    for name in entries:
        if not RE_IMPORT_ID.match(name):
            continue
        try:
            if now - os.path.getmtime(os.path.join(PLUGIN_UPLOAD_DIR, name)) > PLUGIN_UPLOAD_TTL:
                shutil.rmtree(os.path.join(PLUGIN_UPLOAD_DIR, name), ignore_errors=True)
        except OSError:
            pass


def _next_available_name(name):
    """目标同名时自动追加序号，避免覆盖既有插件。"""
    final_name = name
    n = 1
    while os.path.exists(os.path.join(PLUGIN_SOURCE_DIR, final_name)):
        final_name = f'{name} ({n})'
        n += 1
    return final_name


@app.errorhandler(413)
def _upload_too_large(_e):
    return jsonify({"error": "上传内容超过 2GB 限制，请压缩后再试"}), 413


@app.errorhandler(OSError)
def _upload_os_error(e):
    # 磁盘写满（多发生在上传内容解析落盘阶段，早于视图代码）转成明确提示；
    # 其余 OSError 返回 None 交回 Flask 默认处理，不掩盖原有错误
    if getattr(e, 'errno', None) == errno.ENOSPC:
        return jsonify({"error": "服务器磁盘空间不足，上传失败，请清理服务器存储后重试"}), 507
    return None


@app.route('/api/plugins/recognize', methods=['POST'])
@login_required
def api_plugins_recognize():
    """识别导入第一步：库内压缩包/文件夹 → 暂存 → 扫描，返回候选清单。
    压缩包复制暂存；文件夹复制树暂存（库内原件不动，导入完成后由
    import_archive 依据 source.txt 清理原件）。"""
    data = request.get_json() or {}
    rel_path = str(data.get('path', '')).strip().replace('\\', '/')
    if not rel_path.startswith('plugin_dir'):
        return jsonify({"success": False, "message": "仅支持识别可安装插件目录中的文件"}), 400
    rel_inside = rel_path[len('plugin_dir'):].lstrip('/')
    if not rel_inside or any(p in ('', '.', '..') for p in rel_inside.split('/')):
        return jsonify({"success": False, "message": "路径不合法"}), 400
    source_path = _safe_join_under(PLUGIN_SOURCE_DIR, rel_inside)
    if not source_path or not (os.path.isfile(source_path) or os.path.isdir(source_path)):
        return jsonify({"success": False, "message": "文件不存在（可能已被删除或导入）"}), 400

    filename = os.path.basename(rel_inside)
    is_dir = os.path.isdir(source_path)
    suffix = None if is_dir else _match_archive_suffix(filename)
    if not is_dir and not suffix:
        return jsonify({"success": False,
                        "message": "仅支持识别 .zip/.rar/.7z/.tar 系压缩包或文件夹"}), 400

    _sweep_plugin_uploads()
    smx_index = _existing_smx_index()
    if is_dir:
        # 文件夹识别：候选 .smx 来自源目录自身，把它从"已有插件"冲突索引中排除，避免自己撞自己
        src_top = rel_inside.split('/')[0]
        smx_index = {k: vs - {src_top} for k, vs in smx_index.items() if vs - {src_top}}
    import_id = uuid.uuid4().hex
    workdir = os.path.join(PLUGIN_UPLOAD_DIR, import_id)
    unpacked_root = os.path.join(workdir, 'unpacked')

    try:
        if is_dir:
            # 文件夹：整树复制到暂存扫描（库内原件保留，拒绝时不受影响）
            os.makedirs(workdir)
            shutil.copytree(source_path, unpacked_root)
        else:
            os.makedirs(unpacked_root)
            shutil.copy2(source_path, os.path.join(workdir, f'archive{suffix}'))
    except Exception as e:
        shutil.rmtree(workdir, ignore_errors=True)
        return jsonify({"success": False, "message": f"暂存失败: {e}"}), 500

    default_name = _sanitize_plugin_import_name(filename if is_dir else filename[:-len(suffix)])
    if is_dir and default_name is None:
        shutil.rmtree(workdir, ignore_errors=True)
        return jsonify({"success": False, "message": "文件夹名不合法，无法识别导入"})
    error = None
    if not is_dir:
        error = _extract_plugin_archive(os.path.join(workdir, f'archive{suffix}'),
                                        suffix, unpacked_root)
    if error or default_name is None:
        shutil.rmtree(workdir, ignore_errors=True)
        return jsonify({"success": False, "message": error or '压缩包文件名不合法'})
    scan = _scan_archive_content(unpacked_root, default_name)
    candidates = scan['candidates']
    for cand in candidates:
        cand['conflict_with'] = _conflict_names(cand.get('smx_files', []), smx_index)
    if not candidates:
        shutil.rmtree(workdir, ignore_errors=True)
        # 拒绝文案只给封装指引，不解释判定规则
        message = ("未在包内找到任何插件（判定唯一标准：文件夹内直接包含 left4dead2 目录层）。"
                   "请按以下结构封装后重新上传："
                   "插件名/left4dead2/addons/sourcemod/plugins/xxx.smx，"
                   "cfg、translations 等配套文件一并放入 left4dead2/ 下。")
        if is_dir:
            # 文件夹识别仅发生在上传完成的瞬间：不合格即自动移除，保持插件库干净
            shutil.rmtree(source_path, ignore_errors=True)
            message += "原文件夹已自动移除。"
        return jsonify({"success": False, "message": message})

    with open(os.path.join(workdir, 'source.txt'), 'w', encoding='utf-8') as fh:
        fh.write(source_path)
    return jsonify({"success": True, "import_id": import_id, "filename": filename,
                    "candidates": candidates})


# --- 插件包备份（Installed_Receipts 回执体系的上层应用） ---
# 备份 = Shell 汇总全部回执 + 从服务器根目录收集文件 → data/Plugin_Backups/ 下一个
# tar.gz 归档（含 backup_manifest.json 与 .receipts/ 原样回执）；恢复 = 切换语义
# （先强制卸载当前全部已安装插件，再归位备份文件与回执，实例卡片整组替换）。
# 列表/下载/删除由 Python 直接操作归档文件（manifest 从 tar 包内读取）。
BACKUP_DIR = os.path.join(DATA_DIR, 'Plugin_Backups')
os.makedirs(BACKUP_DIR, exist_ok=True)
# 备份进度文件：shell 压缩期间覆写阶段/百分比（见 Do-BackupPlugins），备份路由起止时
# 创建/删除，/api/plugins/backup/progress 轮询读取；不以 .tar.gz 结尾，不影响列表与清理
BACKUP_PROGRESS_FILE = os.path.join(BACKUP_DIR, '.backup_progress.json')
# 备份异步化：启动即返回、后台线程执行，避免长请求被浏览器/代理/超时掐断导致空响应
_backup_start_lock = threading.Lock()
_backup_job_running = threading.Event()
_PROGRESS_STAGES = ('collect', 'compress', 'done', 'error')
# 备份名：默认时间戳名 plugins_backup_YYYYMMDD_HHMMSS.tar.gz，或用户重命名的自定义名
# （字母/数字/下划线/连字符/中文，1-64 字符）。自定义名不含路径分隔符，Python 侧
# os.rename/remove 无 shell 参与，无注入风险；Shell 侧另有排除法校验兜底。
RE_BACKUP_NAME = re.compile(r'^(?:plugins_backup_\d{8}_\d{6}|[A-Za-z0-9_\u4e00-\u9fa5-]{1,64})\.tar\.gz$')
RE_BACKUP_NEW_NAME = re.compile(r'^[A-Za-z0-9_\u4e00-\u9fa5-]{1,64}$')


def _sidecar_path(path):
    """归档旁车元数据路径：列表接口要读的插件数/实例数/缺失数等都在 gzip 压缩包深处，
    每次读都要全包顺序解压（实测单份归档两次全扫近秒级 × 最多 10 份归档 = 页面卡顿
    数秒）。旁车是归档旁的几 KB 小 JSON，读取微秒级。"""
    return path + '.meta.json'


def _read_sidecar(path):
    """读归档旁车元数据；缺失/损坏/名字对不上（改名未同步的陈旧旁车）返回 None。"""
    try:
        with open(_sidecar_path(path), encoding='utf-8') as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) and data.get('name') == os.path.basename(path) else None
    except (OSError, ValueError):
        return None


def _write_sidecar(path, meta):
    """写旁车元数据；失败不影响主流程（_backup_meta 会扫描回填）。"""
    try:
        with open(_sidecar_path(path), 'w', encoding='utf-8') as fh:
            json.dump(meta, fh, ensure_ascii=False)
    except OSError:
        pass


def _scan_backup_archive(path):
    """单遍流式扫描归档：顺序解压找 backup_manifest.json 与 instances.json，
    两样齐了立即停（gzip 不可随机访问，早停可少解压大半包；旧实现 getmembers()
    必读全包且开了两次）。返回 (manifest, instances)，损坏/缺失的项为 None。"""
    manifest = None
    instances = None
    try:
        with tarfile.open(path, 'r:gz') as tf:
            for m in tf:
                base = m.name[2:] if m.name.startswith('./') else m.name
                if manifest is None and base == 'backup_manifest.json':
                    fh = tf.extractfile(m)
                    try:
                        manifest = json.loads(fh.read().decode('utf-8')) if fh else None
                    except json.JSONDecodeError:
                        manifest = None
                elif instances is None and base == 'instances.json':
                    fh = tf.extractfile(m)
                    try:
                        data = json.loads(fh.read().decode('utf-8')) if fh else None
                    except json.JSONDecodeError:
                        data = None
                    instances = data if isinstance(data, dict) else None
                if manifest is not None and instances is not None:
                    break
    except (tarfile.TarError, OSError, KeyError):
        pass
    return manifest, instances


def _backup_meta(path):
    """单个备份的展示元数据（名称/大小/时间/插件数/实例数/格式版本/文件缺失数）。
    优先读旁车；没有旁车（存量旧归档）才做一次流式扫描并回填旁车，此后列表零扫描。
    missing_files 只在新版 manifest 中存在——空壳备份（早期 v2 归档只装了回执没装
    文件）没有该字段，无法事后甄别，靠前端提示重新备份兜底。"""
    sidecar = _read_sidecar(path)
    if sidecar is not None and sidecar.get('size') == os.path.getsize(path):
        return sidecar
    manifest, instances_snap = _scan_backup_archive(path)
    is_dict = isinstance(manifest, dict)
    plugins = manifest.get('plugins') if is_dict else None
    missing = manifest.get('missing_files') if is_dict else None
    meta = {"name": os.path.basename(path),
            "size": os.path.getsize(path),
            "created": manifest.get('created') if is_dict else None,
            "plugins": len(plugins) if isinstance(plugins, list) else None,
            "total_files": manifest.get('total_files') if is_dict else None,
            "missing_files": missing if isinstance(missing, int) else None,
            "instances": len(instances_snap) if instances_snap is not None else None,
            "version": 2 if instances_snap is not None else 1}
    _write_sidecar(path, meta)
    return meta


def _read_backup_progress():
    """读取进度文件；不存在/损坏/超期（30 分钟未更新，脚本异常残留，顺带清理）返回 None。"""
    try:
        if time.time() - os.path.getmtime(BACKUP_PROGRESS_FILE) > 30 * 60:
            os.remove(BACKUP_PROGRESS_FILE)
            raise OSError('stale')
        with open(BACKUP_PROGRESS_FILE, encoding='utf-8') as fh:
            data = json.load(fh)
        if str(data.get('stage') or '') not in _PROGRESS_STAGES:
            raise ValueError('bad stage')
        return data
    except (OSError, ValueError, json.JSONDecodeError, TypeError):
        return None


@app.route('/api/plugins/backup', methods=['POST'])
@login_required
def api_plugins_backup():
    """异步启动备份：立即返回（不占住连接），进度与结果经 progress 接口轮询。
    同步等待会撑爆连接——大库备份动辄数分钟，期间响应零字节流动，浏览器/中间设备
    掐断连接后前端报 "Unexpected end of JSON input"（备份脚本作为孤儿进程其实会跑完）。
    已有备份进行中（进度处于 collect/compress）返回 409 拒绝并发。"""
    state = _read_backup_progress()
    if state and state.get('stage') in ('collect', 'compress'):
        return jsonify({"success": False, "message": "已有备份正在进行中，请稍候"}), 409
    with _backup_start_lock:
        if _backup_job_running.is_set():
            return jsonify({"success": False, "message": "已有备份正在进行中，请稍候"}), 409
        _backup_job_running.set()
    try:
        with open(BACKUP_PROGRESS_FILE, 'w', encoding='utf-8') as fh:
            fh.write('{"stage": "collect"}')
    except OSError as e:
        _backup_job_running.clear()
        return jsonify({"success": False, "message": f'无法写入进度文件: {e}'}), 500
    threading.Thread(target=_run_backup_job, daemon=True, name='plugin-backup').start()
    return jsonify({"success": True, "started": True, "message": "备份已开始，进度见进度条"})


def _write_backup_sidecar_quick(payload):
    """备份刚完成时应用层已掌握全部展示字段（插件数/文件数/缺失数/创建时间来自
    shell 输出，实例数来自当前 instances.json——正是打包时的快照来源），直接落一份
    旁车元数据，列表接口零扫描展示；落盘失败无碍，_backup_meta 会扫描回填兜底。"""
    try:
        name = payload.get('name') or ''
        archive = os.path.join(BACKUP_DIR, name)
        if not name or not os.path.isfile(archive):
            return
        meta = {"name": name,
                "size": os.path.getsize(archive),
                "created": payload.get('created') or time.strftime('%Y-%m-%d %H:%M:%S'),
                "plugins": payload.get('plugins'),
                "total_files": payload.get('total_files'),
                "missing_files": payload.get('missing'),
                "instances": len(_load_instances()),
                "version": 2}
        _write_sidecar(archive, meta)
    except (OSError, ValueError, TypeError):
        pass


def _run_backup_job():
    """后台执行备份 shell，并把最终结果写回进度文件（done 携带结果 / error 携带原因）。
    即便 HTTP 侧发生任何意外，备份与进度上报也不再依赖请求连接的存活。"""
    try:
        result = run_script(['backup_plugins'])
        if result.get('success'):
            try:
                payload = json.loads(result['output'])
            except json.JSONDecodeError:
                payload = {"success": False, "message": "备份输出无法解析"}
        else:
            # 失败时脚本把错误 JSON 打在 stdout，run_script 已包成 message——尽量还原可读原因
            blob = result.get('message') or ''
            try:
                parsed = json.loads(blob)
                payload = {"success": False, "message": parsed.get('error') or blob}
            except (json.JSONDecodeError, AttributeError):
                payload = {"success": False, "message": blob or '备份失败'}
    except Exception as e:
        payload = {"success": False, "message": f'备份执行异常: {e}'}
    final = ({"stage": "done", "percent": 100, "result": payload} if payload.get('success')
             else {"stage": "error", "message": payload.get('message') or payload.get('error') or '备份失败'})
    if payload.get('success'):
        _write_backup_sidecar_quick(payload)
    try:
        with open(BACKUP_PROGRESS_FILE, 'w', encoding='utf-8') as fh:
            json.dump(final, fh, ensure_ascii=False)
    except OSError:
        pass
    finally:
        _backup_job_running.clear()


@app.route('/api/plugins/backup/progress', methods=['GET'])
@login_required
def api_plugins_backup_progress():
    """备份进度与结果轮询：collect/compress 为进行中（compress 带百分比），
    done 携带备份结果，error 携带失败原因；无文件/损坏/超期（30 分钟未更新的
    脚本异常残留，顺带清理）= 空闲。done/error 为一次性事件：报告给第一个
    访问者后即删除进度文件——避免页面每次打开都重播"备份完成"提示。"""
    data = _read_backup_progress()
    if data is None:
        return jsonify({"stage": "idle", "percent": None})
    stage = data['stage']
    if stage in ('collect', 'compress'):
        percent = data.get('percent')
        return jsonify({"stage": stage,
                        "percent": max(0, min(100, int(percent))) if isinstance(percent, (int, float)) else None})
    try:
        os.remove(BACKUP_PROGRESS_FILE)   # 终态一次性消费
    except OSError:
        pass
    if stage == 'done':
        return jsonify({"stage": "done", "result": data.get('result') or {}})
    return jsonify({"stage": "error", "message": data.get('message') or '备份失败'})


@app.route('/api/plugins/backups', methods=['GET'])
@login_required
def api_plugins_backups():
    backups = []
    for entry in sorted(os.listdir(BACKUP_DIR), reverse=True):
        path = os.path.join(BACKUP_DIR, entry)
        if not entry.endswith('.tar.gz') or not os.path.isfile(path):
            continue
        if not RE_BACKUP_NAME.match(entry):
            continue
        backups.append(_backup_meta(path))
    return jsonify({"success": True, "backups": backups})


@app.route('/api/plugins/backup/detail', methods=['POST'])
@login_required
def api_plugins_backup_detail():
    """单份备份的清单明细（备份详情抽屉用）：插件清单/文件对账/实例快照。
    纯只读流式扫描，不改任何状态；旧版 v1 归档无 manifest 返回 404 由前端兜底。"""
    data = request.get_json() or {}
    name = str(data.get('name', ''))
    if not RE_BACKUP_NAME.match(name):
        return jsonify({"success": False, "error": "备份名不合法"}), 400
    path = os.path.join(BACKUP_DIR, name)
    if not os.path.isfile(path):
        return jsonify({"success": False, "error": "备份不存在"}), 404
    manifest, instances_snap = _scan_backup_archive(path)
    if not isinstance(manifest, dict):
        return jsonify({"success": False, "error": "旧版备份不含清单明细"}), 404
    instances_list = []
    if isinstance(instances_snap, dict):
        for key, val in instances_snap.items():
            # '_hostname_plugin' 等下划线元数据键不是实例
            if key.startswith('_') or not isinstance(val, dict):
                continue
            instances_list.append({"name": val.get('name', key),
                                   "port": val.get('port'),
                                   "mode": val.get('mode') or 'coop'})
    missing = manifest.get('missing_files')
    return jsonify({"success": True, "name": name,
                    "created": manifest.get('created'),
                    "plugins": manifest.get('plugins') if isinstance(manifest.get('plugins'), list) else [],
                    "total_files": manifest.get('total_files'),
                    "collected_files": manifest.get('collected_files'),
                    "missing_files": missing if isinstance(missing, int) else 0,
                    "instances": instances_list})


@app.route('/api/plugins/backup/restore', methods=['POST'])
@login_required
def api_plugins_backup_restore():
    data = request.get_json() or {}
    name = str(data.get('name', ''))
    if not RE_BACKUP_NAME.match(name):
        return jsonify({"success": False, "message": "备份名不合法"}), 400
    # 恢复会替换实例卡片集合与插件文件——运行中的实例正使用这些内容，必须先停止
    running = _get_running_names()
    instances = _load_instances()
    busy = [f"{cfg.get('name', inst_id)}（端口 {cfg.get('port')}）"
            for inst_id, cfg in instances.items()
            if f"inst_{cfg.get('port')}" in running]
    if busy:
        return jsonify({"success": False,
                        "message": "有实例正在运行，请先停止后再恢复：" + "、".join(busy)}), 400
    result = run_script(['restore_plugins', name])
    if not result['success']:
        return jsonify({"success": False, "message": result.get('message', '恢复失败')}), 400
    try:
        payload = json.loads(result['output'])
    except json.JSONDecodeError:
        return jsonify({"success": False, "message": "恢复输出无法解析"}), 500
    return jsonify(payload)


@app.route('/api/plugins/backup/rename', methods=['POST'])
@login_required
def api_plugins_backup_rename():
    """重命名备份：新名只改文件名便于辨认（内容不变），默认时间戳名可随时被覆盖。"""
    data = request.get_json() or {}
    name = str(data.get('name', ''))
    new_base = str(data.get('new_name', '')).strip()
    if not RE_BACKUP_NAME.match(name):
        return jsonify({"success": False, "message": "备份名不合法"}), 400
    if not RE_BACKUP_NEW_NAME.match(new_base):
        return jsonify({"success": False, "message":
                        "新名称不合法（1-64 字符，可用中文/字母/数字/下划线/连字符）"}), 400
    old_path = os.path.join(BACKUP_DIR, name)
    new_name = f'{new_base}.tar.gz'
    new_path = os.path.join(BACKUP_DIR, new_name)
    if not os.path.isfile(old_path):
        return jsonify({"success": False, "message": "备份不存在"}), 404
    if os.path.exists(new_path):
        return jsonify({"success": False, "message": "已存在同名备份，请换一个名字"}), 400
    try:
        os.rename(old_path, new_path)
    except OSError as e:
        return jsonify({"success": False, "message": f"重命名失败: {e}"}), 500
    # 旁车元数据随归档改名，name 字段对齐新名（对不齐会被列表视为陈旧而扫描回填）
    old_side = _sidecar_path(old_path)
    if os.path.isfile(old_side):
        new_side = _sidecar_path(new_path)
        try:
            os.rename(old_side, new_side)
            with open(new_side, encoding='utf-8') as fh:
                side = json.load(fh)
            side['name'] = new_name
            with open(new_side, 'w', encoding='utf-8') as fh:
                json.dump(side, fh, ensure_ascii=False)
        except (OSError, ValueError):
            try:
                os.remove(new_side)
            except OSError:
                pass
    return jsonify({"success": True, "message": "备份已重命名", "name": new_name})


@app.route('/api/plugins/backup/delete', methods=['POST'])
@login_required
def api_plugins_backup_delete():
    data = request.get_json() or {}
    name = str(data.get('name', ''))
    if not RE_BACKUP_NAME.match(name):
        return jsonify({"success": False, "message": "备份名不合法"}), 400
    path = os.path.join(BACKUP_DIR, name)
    try:
        os.remove(path)
    except FileNotFoundError:
        return jsonify({"success": False, "message": "备份不存在"}), 404
    except OSError as e:
        return jsonify({"success": False, "message": f"删除失败: {e}"}), 500
    try:
        os.remove(_sidecar_path(path))
    except OSError:
        pass
    return jsonify({"success": True, "message": "备份已删除"})


@app.route('/api/plugins/import_archive', methods=['POST'])
@login_required
def api_plugins_import_archive():
    data = request.get_json() or {}
    items = data.get('plugins')
    if not isinstance(items, list) or not items:
        return jsonify({"success": False, "message": "未选择任何要导入的插件"}), 400

    results = []
    touched_import_ids = set()
    for item in items:
        if not isinstance(item, dict):
            results.append({"success": False, "message": "参数不合法"})
            continue
        import_id = str(item.get('import_id', '')).strip()
        if not RE_IMPORT_ID.match(import_id):
            results.append({"success": False, "message": "导入标识不合法"})
            continue
        raw_name = item.get('name', '')
        name = _sanitize_plugin_import_name(raw_name)
        if not name:
            # 常规入口已在识别弹窗拦截非法名，此处为兜底；带上实际名称便于错误详情展示
            results.append({"success": False, "name": str(raw_name)[:60],
                            "message": "插件名不合法（不能包含 \\ / : * ? \" < > | ' ` ; & 等字符）"})
            continue
        unpacked_root = os.path.abspath(os.path.join(PLUGIN_UPLOAD_DIR, import_id, 'unpacked'))

        folder = str(item.get('folder', ''))
        parts = [p for p in folder.split('/') if p]
        if any(p in ('.', '..') for p in parts):
            results.append({"success": False, "message": "插件路径不合法"})
            continue
        source_dir = os.path.abspath(os.path.join(unpacked_root, *parts)) if parts else unpacked_root
        if source_dir != unpacked_root and not source_dir.startswith(unpacked_root + os.sep):
            results.append({"success": False, "message": "插件路径不合法"})
            continue
        if not os.path.isdir(source_dir):
            results.append({"success": False, "message": "源目录不存在（可能已导入或暂存已过期）"})
            continue

        # 文件夹识别导入（source.txt 指向库内目录）候选的三种去向：
        #   a) 整文件夹本身即候选（folder == ''，外壳自身合法）：未改名 → 原地保留（仅校验）；
        #      改名 → 就地重命名。均不经暂存搬运，随即丢弃暂存副本
        #   b) 嵌套候选且以外壳名导入（如"外壳/left4dead2"以外壳名入库，外壳内仅有散落垃圾）：
        #      当其成为唯一待导项时先清除外壳再以原名入库；否则按同名规则追加序号
        #   c) 其余嵌套候选：从暂存搬运；全部导入完成后由收尾清理删除外壳原件（含散落垃圾）
        src_marker = os.path.join(PLUGIN_UPLOAD_DIR, import_id, 'source.txt')
        dir_src = None
        if os.path.isfile(src_marker):
            try:
                with open(src_marker, encoding='utf-8') as fh:
                    dir_src = fh.read().strip()
            except OSError:
                dir_src = None
        src_abs = os.path.abspath(dir_src) if dir_src else None
        is_dir_flow = bool(src_abs and src_abs.startswith(os.path.abspath(PLUGIN_SOURCE_DIR) + os.sep)
                           and os.path.isdir(src_abs))

        if is_dir_flow and not parts:
            staging_dir = os.path.join(PLUGIN_UPLOAD_DIR, import_id)
            if name == os.path.basename(src_abs):
                shutil.rmtree(staging_dir, ignore_errors=True)
                results.append({"success": True, "name": name, "final_name": name,
                                "message": f"文件夹结构校验通过，已保留为可安装插件 '{name}'"})
            else:
                final_name = _next_available_name(name)
                shutil.move(src_abs, os.path.join(PLUGIN_SOURCE_DIR, final_name))
                shutil.rmtree(staging_dir, ignore_errors=True)
                results.append({"success": True, "name": name, "final_name": final_name,
                                "message": f"已导入为可安装插件 '{final_name}'"})
            continue

        if is_dir_flow and name == os.path.basename(src_abs):
            others = [c for c in _scan_archive_content(unpacked_root, '')['candidates']
                      if c['folder'] != folder]
            if not others:
                # 外壳内已无其他待导项：清掉外壳原件并以原名入库；同时删除标记文件——
                # 导入副本将占用与外壳相同的路径，收尾清理若再按标记删目录会误删刚导入的插件
                shutil.rmtree(src_abs, ignore_errors=True)
                try:
                    os.remove(src_marker)
                except OSError:
                    pass

        final_name = _next_available_name(name)
        try:
            # 候选节点整目录原样入库（目标名经 _next_available_name 保证不存在）
            shutil.move(source_dir, os.path.join(PLUGIN_SOURCE_DIR, final_name))
            results.append({"success": True, "name": name, "final_name": final_name,
                            "message": f"已导入为可安装插件 '{final_name}'"})
            touched_import_ids.add(import_id)
        except Exception as e:
            results.append({"success": False, "name": name, "message": f"导入失败: {e}"})

    # 已成功导入的暂存包：已无可继续导入的候选时随即清理，有剩余则保留暂存供补导；
    # 清理的同时删除库内的源压缩包/.smx/文件夹（文件夹识别导入的原件随全部导入完成而清除，
    # 避免识别过的内容积压在插件库）
    for import_id in touched_import_ids:
        workdir = os.path.join(PLUGIN_UPLOAD_DIR, import_id)
        unpacked_root = os.path.join(workdir, 'unpacked')
        if os.path.isdir(unpacked_root):
            if _scan_archive_content(unpacked_root, '')['candidates']:
                continue
        source_file = None
        source_marker = os.path.join(workdir, 'source.txt')
        if os.path.isfile(source_marker):
            try:
                with open(source_marker, encoding='utf-8') as fh:
                    source_file = fh.read().strip()
            except OSError:
                source_file = None
        shutil.rmtree(workdir, ignore_errors=True)
        if source_file:
            source_abs = os.path.abspath(source_file)
            if source_abs.startswith(os.path.abspath(PLUGIN_SOURCE_DIR) + os.sep):
                if os.path.isfile(source_abs):
                    try:
                        os.remove(source_abs)
                    except OSError:
                        pass
                elif os.path.isdir(source_abs):
                    shutil.rmtree(source_abs, ignore_errors=True)

    return jsonify({"success": all(r['success'] for r in results), "results": results})


# --- 插件配置/依赖文件编辑 API（库内主副本；已安装插件无库内副本，服务器侧用文件管理页改） ---
def _plugin_edit_rel_allowed(rel):
    rel_norm = rel.replace('\\', '/').lower()
    return rel_norm.startswith(PLUGIN_EDIT_DIRS) and rel_norm.endswith(PLUGIN_EDIT_EXTS)


def _plugin_edit_files(plugin_name):
    """收集插件库内白名单内的可编辑文本文件（相对插件根的 '/' 分隔路径，已排序）。"""
    root = os.path.join(PLUGIN_SOURCE_DIR, plugin_name)
    result = []
    for dirpath, dirnames, filenames in os.walk(root):
        for f in filenames:
            rel = os.path.relpath(os.path.join(dirpath, f), root).replace(os.sep, '/')
            if _plugin_edit_rel_allowed(rel):
                result.append(rel)
    return sorted(result, key=str.lower)


def _resolve_plugin_edit_target(plugin, rel):
    """校验并解析待编辑文件。成功返回 (绝对路径, None)，失败返回 (None, 错误信息)。"""
    plugin_name = _sanitize_plugin_import_name(plugin)
    if not plugin_name:
        return None, '插件名不合法'
    plugin_root = os.path.join(PLUGIN_SOURCE_DIR, plugin_name)
    if not os.path.isdir(plugin_root):
        return None, '插件不存在（可能处于已安装状态，库内暂无副本）'
    target = _safe_join_under(plugin_root, rel)
    if not target or not os.path.isfile(target):
        return None, '文件不存在'
    if not _plugin_edit_rel_allowed(rel.replace('\\', '/')):
        return None, '该路径不在可编辑范围内'
    if os.path.splitext(target)[1].lower() not in PLUGIN_EDIT_EXTS:
        return None, '仅支持编辑 .cfg/.txt 文本文件'
    if os.path.getsize(target) > PLUGIN_EDIT_MAX_BYTES:
        return None, '文件超过 1MB，编辑器不支持'
    real_base = os.path.realpath(PLUGIN_SOURCE_DIR)
    if os.path.islink(target) or not os.path.realpath(target).startswith(real_base + os.sep):
        return None, '文件路径不合法'
    return target, None


def _read_text_file(path):
    """读取文本文件：UTF-8 优先、GBK 兜底；含空字节视为二进制文件。
    返回 (文本, 编码)；无法按文本解码时返回 (None, None)。"""
    with open(path, 'rb') as fh:
        raw = fh.read()
    if b'\x00' in raw:
        return None, None
    for enc in ('utf-8', 'gbk'):
        try:
            return raw.decode(enc), enc
        except UnicodeDecodeError:
            continue
    return None, None


@app.route('/api/plugins/config/list', methods=['GET'])
@login_required
def api_plugin_config_list():
    """扫描库内全部插件，返回 {插件名: [可编辑文件相对路径...]}（仅含存在可编辑文件的插件）。"""
    result = {}
    for name in _rel_children(PLUGIN_SOURCE_DIR):
        if not os.path.isdir(os.path.join(PLUGIN_SOURCE_DIR, name)):
            continue
        files = _plugin_edit_files(name)
        if files:
            result[name] = files
    return jsonify({"success": True, "configurable": result})


@app.route('/api/plugins/config/get', methods=['POST'])
@login_required
def api_plugin_config_get():
    data = request.get_json() or {}
    target, err = _resolve_plugin_edit_target(data.get('plugin', ''), str(data.get('rel', '')))
    if err:
        return jsonify({"success": False, "error": err}), 400
    text, encoding = _read_text_file(target)
    if text is None:
        return jsonify({"success": False, "error": "非文本文件或编码不受支持，无法编辑"}), 400
    return jsonify({"success": True, "content": text, "encoding": encoding})


@app.route('/api/plugins/config/save', methods=['POST'])
@login_required
def api_plugin_config_save():
    data = request.get_json() or {}
    target, err = _resolve_plugin_edit_target(data.get('plugin', ''), str(data.get('rel', '')))
    if err:
        return jsonify({"success": False, "error": err}), 400
    text, encoding = _read_text_file(target)
    if text is None:
        return jsonify({"success": False, "error": "非文本文件或编码不受支持，无法编辑"}), 400
    content = data.get('content')
    if content is None:
        return jsonify({"success": False, "error": "缺少内容"}), 400
    if len(content.encode('utf-8', errors='replace')) > PLUGIN_EDIT_MAX_BYTES:
        return jsonify({"success": False, "error": "内容超过 1MB，编辑器不支持"}), 400
    # 统一以 UTF-8 落盘（GBK 老配置自动转码，SourceMod 完全支持 UTF-8）
    with open(target, 'w', encoding='utf-8', newline='') as fh:
        fh.write(content)
    note = '原文件为 GBK 编码，保存时已转换为 UTF-8' if encoding == 'gbk' else None
    return jsonify({"success": True, "note": note})


# --- 实例控制台（screen 会话日志流式查看 + 命令交互） ---
# 日志文件由脚本启动实例时通过 script -f 生成（console_logs/console_<实例ID>.log）：会话内再套一层
# 终端（stdout 保持 TTY，程序行为与直连一致），终端流逐块无缓冲同步落盘，保证面板侧实时可读；
# 每次启动重建；控制台首帧只回传日志尾部快照、之后增量推送，前端再裁剪到固定行数，
# 三层配合保证展示上下文不会从开局一直累积到结尾。
CONSOLE_LOG_DIR = os.path.join(DATA_DIR, 'console_logs')
CONSOLE_SNAPSHOT_BYTES = 64 * 1024   # 首帧快照最多回传日志尾部 64KB
CONSOLE_ALIVE_CHECK_INTERVAL = 5     # 每 5 秒确认一次会话存活，实例停止后结束日志流
RE_CONSOLE_COMMAND = re.compile(r'^[^\x00-\x1f\x7f]{1,128}$')  # 单行可打印字符，≤128 字符


def _console_log_path(name):
    return os.path.join(CONSOLE_LOG_DIR, f'console_{name}.log')


@app.route('/api/instances/console', methods=['GET'])
@login_required
def api_instance_console():
    """实例控制台日志流（SSE）：snapshot/reset 携带快照正文，log 携带增量，exit 表示会话结束。"""
    inst_id = (request.args.get('id') or '').strip()
    if not RE_INSTANCE_NAME.match(inst_id):
        return jsonify({"success": False, "message": "实例标识不合法"}), 400
    instances = _load_instances()
    if inst_id not in instances:
        return jsonify({"success": False, "message": f"实例 '{inst_id}' 不存在"}), 404
    # screen 会话按端口命名（inst_<端口>）
    session_name = f"inst_{instances[inst_id].get('port')}"
    # 缓存未命中时强制复核一次（缓存最长滞后 2.5 秒，实例刚启动时可能还没被后台线程刷进来）
    if session_name not in _get_running_names() and session_name not in _get_running_names(max_age=0):
        return jsonify({"success": False, "message": "实例未在运行，无法打开控制台"}), 400
    log_path = _console_log_path(session_name)

    def sse_event(payload):
        return f"data: {payload}\n\n"

    def read_tail(path, max_bytes):
        """读取文件尾部最多 max_bytes 字节，返回 (文本, 文件大小)；文件不存在时返回空串。"""
        try:
            size = os.path.getsize(path)
            with open(path, 'rb') as f:
                f.seek(max(0, size - max_bytes))
                return f.read().decode('utf-8', errors='replace'), size
        except OSError:
            return '', 0

    def generate():
        text, size = read_tail(log_path, CONSOLE_SNAPSHOT_BYTES)
        yield sse_event(json.dumps({"type": "snapshot", "content": text}, ensure_ascii=False))
        offset = size
        last_alive = time.time()
        while True:
            time.sleep(0.1)
            try:
                size = os.path.getsize(log_path)
            except OSError:
                size = offset  # 日志暂时不可读时原地等待，由存活检查兜底
            if size < offset:
                # 日志被重建/清空：重置偏移并重发快照
                text, size = read_tail(log_path, CONSOLE_SNAPSHOT_BYTES)
                yield sse_event(json.dumps({"type": "reset", "content": text}, ensure_ascii=False))
                offset = size
            elif size > offset:
                with open(log_path, 'rb') as f:
                    f.seek(offset)
                    chunk = f.read().decode('utf-8', errors='replace')
                offset = size
                yield sse_event(json.dumps({"type": "log", "content": chunk}, ensure_ascii=False))
            if time.time() - last_alive >= CONSOLE_ALIVE_CHECK_INTERVAL:
                last_alive = time.time()
                if session_name not in _get_running_names():
                    yield sse_event(json.dumps({"type": "exit"}, ensure_ascii=False))
                    return

    resp = Response(generate(), mimetype='text/event-stream')
    resp.headers['Cache-Control'] = 'no-cache'
    resp.headers['X-Accel-Buffering'] = 'no'
    return resp


@app.route('/api/instances/console/send', methods=['POST'])
@login_required
def api_instance_console_send():
    """向运行中实例的 screen 会话发送一行控制台命令。"""
    data = request.get_json() or {}
    inst_id = str(data.get('id', '')).strip()
    command = str(data.get('command', '')).strip()
    if not RE_INSTANCE_NAME.match(inst_id):
        return jsonify({"success": False, "message": "实例标识不合法"}), 400
    if not command or not RE_CONSOLE_COMMAND.match(command):
        return jsonify({"success": False, "message": "命令不能为空，且须为单行可打印字符（≤128 字符）"}), 400
    instances = _load_instances()
    if inst_id not in instances:
        return jsonify({"success": False, "message": f"实例 '{inst_id}' 不存在"}), 404
    result = run_script(['send_command', f"inst_{instances[inst_id].get('port')}", command])
    if not result['success']:
        return jsonify({"success": False, "message": result.get('message', '发送失败')}), 400
    return jsonify({"success": True, "message": result.get('output', '命令已发送')})


# --- 实例配置管理（存储于 data/instances.json，面板"实例管理"页在线编辑） ---
INSTANCES_FILE = os.path.join(DATA_DIR, 'instances.json')
INSTANCES_LOCK = threading.Lock()

# 内部实例 ID 仅作 instances.json 的持久主键（inst_N 递增，与端口解耦——端口可编辑）；
# screen 会话名与控制台日志文件名按端口生成（inst_<端口>）
RE_INSTANCE_NAME = re.compile(r'^[A-Za-z0-9_-]{1,32}$')
# 显示名：用户自由填写（中英文均可），仅禁止控制字符；只用于展示，不参与 shell 命令拼接
RE_INSTANCE_DISPLAY = re.compile(r'^[^\x00-\x1f\x7f]{1,64}$')
# 服务器名称会以双引号嵌入启动命令，禁止可逃逸引号或构造 shell 语法的字符
RE_HOSTNAME = re.compile(r"^[^\"`$\\;&|<>'\n\r]{1,64}$")
RE_SAFE_TOKEN = re.compile(r'^[A-Za-z0-9_.-]{1,64}$')
RE_MODE = re.compile(r'^[A-Za-z0-9_]{1,32}$')
# 额外参数将拼入启动命令，白名单中刻意排除 ; | & $ ` < > ( ) 引号 等字符以防命令注入
RE_PARAMS = re.compile(r"^[A-Za-z0-9 +_=.:/-]{0,200}$")


def _load_instances():
    """读取实例配置；文件不存在时初始化为空（实例由用户在面板中自行创建）。
    旧版数据以实例名为主键且无 name 字段，迁移时把旧实例名同时作为内部 ID 与显示名；
    旧实例名满足 RE_INSTANCE_NAME，screen 会话 l4d2_manager_<旧名> 因此不受影响，
    运行中的实例升级后状态依旧可正确识别。"""
    migrated = False
    if not os.path.exists(INSTANCES_FILE):
        _save_instances({})
        return {}
    try:
        with open(INSTANCES_FILE, encoding='utf-8') as f:
            data = json.load(f)
        if not isinstance(data, dict):
            data = {}
    except (json.JSONDecodeError, OSError):
        data = {}

    for inst_id, cfg in data.items():
        if not isinstance(cfg, dict):
            continue
        if 'name' not in cfg:
            cfg['name'] = inst_id
            migrated = True
        if 'hostname' in cfg:  # 旧字段：服务器名称已并入实例名称（+hostname 直接用实例名）
            cfg.pop('hostname')
            migrated = True
        if 'ip' in cfg:  # 旧字段：监听 IP 已固定为 0.0.0.0，不再使用
            cfg.pop('ip')
            migrated = True

    if migrated:
        _save_instances(data)
    return data


def _next_instance_id(instances):
    """生成未占用的内部实例 ID：inst_1、inst_2 … 递增。"""
    used = set(instances)
    n = 1
    while f'inst_{n}' in used:
        n += 1
    return f'inst_{n}'


def _save_instances(instances):
    with open(INSTANCES_FILE, 'w', encoding='utf-8') as f:
        json.dump(instances, f, ensure_ascii=False, indent=2)


# --- 面板级设置（data/panel_settings.json，"设置"页在线编辑，保存即生效无需重启） ---
# 与实例属性（instances.json）相对：这里放与具体实例无关的面板全局配置
PANEL_SETTINGS_FILE = os.path.join(DATA_DIR, 'panel_settings.json')
PANEL_SETTINGS_LOCK = threading.Lock()
# Steam Web API Key 为 32 位十六进制（steamcommunity.com/dev/apikey 免费申请）
RE_STEAM_API_KEY = re.compile(r'^[0-9A-Fa-f]{32}$')


def _load_panel_settings():
    if not os.path.exists(PANEL_SETTINGS_FILE):
        return {}
    try:
        with open(PANEL_SETTINGS_FILE, encoding='utf-8') as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, OSError):
        return {}


def _save_panel_settings(settings):
    with open(PANEL_SETTINGS_FILE, 'w', encoding='utf-8') as f:
        json.dump(settings, f, ensure_ascii=False, indent=2)


def _steam_api_key():
    """当前生效的 Steam Web API Key（面板"设置"页保存的值，即时生效无需重启面板）。"""
    return str(_load_panel_settings().get('steam_api_key') or '').strip()


def _global_rcon_password():
    """当前生效的面板全局 RCON 密码（"设置"页保存的值）。空 = 用户未启用 RCON：
    实例启动不注入 -usercon/+rcon_password，玩家查询仅走 A2S（尊重用户偏好，
    不自动生成——避免与 server.cfg 出现错寘认证而触发服务器 RCON 封禁）。"""
    return str(_load_panel_settings().get('rcon_password') or '').strip()


def _migrate_env_steam_key():
    """旧部署升级迁移：panel_settings.json 尚不存在且 .env 里配了 STEAM_API_KEY 时，
    把它种入面板设置，避免升级后头像静默失效；此后 .env 该项不再参与（可自行删除）。
    设置文件一经存在（含用户在设置页主动清空 Key 的情况）即不再迁移，尊重面板当前配置。"""
    if not STEAM_API_KEY or os.path.exists(PANEL_SETTINGS_FILE):
        return
    try:
        _save_panel_settings({'steam_api_key': STEAM_API_KEY})
    except OSError:
        pass


_migrate_env_steam_key()


# 运行中实例集合缓存（供 _get_running_names 使用）
RUNNING_CACHE_TTL = 3.0
_RUNNING_CACHE_LOCK = threading.Lock()
_RUNNING_CACHE = {"names": set(), "ts": 0.0}


def _get_running_names(max_age=RUNNING_CACHE_TTL):
    """运行中实例集合（带 TTL 缓存）。后台线程 _running_refresher 每 2.5 秒刷新一次，
    正常调用直接命中缓存、零开销；缓存意外过期时才同步调脚本兜底（max_age=0 强制刷新）。
    控制台 SSE 的推送循环依赖本函数做存活检查，若每次都同步起 bash 子进程
    （内部还要多次 screen -ls），会造成周期性的推送卡顿。"""
    with _RUNNING_CACHE_LOCK:
        if time.time() - _RUNNING_CACHE['ts'] <= max_age:
            return set(_RUNNING_CACHE['names'])
    result = run_script(['list_running'])
    names = set()
    if result['success']:
        try:
            parsed = json.loads(result['output'])
            if isinstance(parsed, list):
                names = set(parsed)
        except json.JSONDecodeError:
            pass
    with _RUNNING_CACHE_LOCK:
        _RUNNING_CACHE['names'] = names
        _RUNNING_CACHE['ts'] = time.time()
    return names


def _running_refresher():
    """后台线程：持续刷新运行中实例缓存，让所有 SSE 连接与接口共用一份检查结果。"""
    while True:
        try:
            _get_running_names(max_age=0)
        except Exception:
            pass  # 刷新失败时保留旧缓存，下轮重试
        time.sleep(2.5)


threading.Thread(target=_running_refresher, daemon=True, name='running-cache-refresher').start()


def _primary_internal_ip():
    """探测本机主内网 IP（UDP connect 只选路由源地址、不实际发包）。失败返回 None。"""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(('8.8.8.8', 80))
            return s.getsockname()[0]
    except OSError:
        return None


def _a2s_targets():
    """玩家查询的目标地址序列：显式配置 A2S_HOST 时只用配置值；
    默认先查回环地址，超时自动换本机内网 IP 重试。"""
    if A2S_HOST:
        return [A2S_HOST]
    targets = ['127.0.0.1']
    internal = _primary_internal_ip()
    if internal and internal != '127.0.0.1':
        targets.append(internal)
    return targets


def _a2s_raw_probe_reply(host, port, timeout=1.5):
    """A2S 协议异常时用裸 UDP 复发一次同款玩家查询，返回原始回包 bytes（失败返回 b''）。
    python-a2s 解析失败只抛类型号（如 0x6c），'l' 后面的文本才是服务器真正想说的话
    （如 'Banned by server'），必须抓原始包才能看到。"""
    try:
        header = b'\xff\xff\xff\xff'
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.settimeout(timeout)
            s.sendto(header + b'\x55\x00\x00\x00\x00', (host, int(port)))
            data = s.recvfrom(65535)[0]
            if data[:4] == header and data[4:5] == b'\x41':   # 先回挑战 → 带挑战完成握手
                s.sendto(header + b'\x55' + data[5:9], (host, int(port)))
                data = s.recvfrom(65535)[0]
            return data
    except OSError:
        return b''


def _describe_a2s_broken_reply(host, port, exc):
    """把 A2S 协议异常翻译成可读文案：原始回包完整写入日志（排障关键证据），
    若是 'l' + 文本的 HL1 遗留拒绝包（A2C_PRINT），提取文本直接说明原因。"""
    data = _a2s_raw_probe_reply(host, port) if host else b''
    if data:
        app.logger.warning('A2S 原始回包 (%s:%s, %d 字节): %s | %r',
                           host, port, len(data), data.hex(' '), data)
    if data[:4] == b'\xff\xff\xff\xff' and data[4:5] == b'l':
        text = data[5:].split(b'\x00', 1)[0].decode('utf-8', 'replace').strip()
        if text:
            return f'服务器拒绝了查询（{text}）——本机 IP 可能在游戏服务器的封禁名单中'
    return str(exc)


def _a2s_player_query(port, timeout=2.0):
    """A2S_PLAYER 在线玩家查询：基于 python-a2s 库的 a2s.players() 实现
    （参照社区验证过的 NoneBot-A2S-Query 插件的做法），挑战握手、多包重组
    与解析全部由库完成；目标地址按 _a2s_targets() 顺序逐个尝试。
    返回 [{'name': str, 'score': int, 'duration': float(已连接秒数)}]；
    超时抛 socket.timeout，端口不通抛 OSError；协议异常（a2s.BrokenMessageError）
    换下一目标重试，全部失败时抓原始回包定位原因后抛出。"""
    broken = None    # (host, BrokenMessageError)：协议异常单独记录，供最终归因
    last_error = None
    for host in _a2s_targets():
        try:
            players = a2s.players((host, int(port)), timeout=timeout)
            return [{'name': p.name, 'score': p.score, 'duration': max(0.0, float(p.duration))}
                    for p in players]
        except (socket.timeout, OSError) as e:
            last_error = e
        except a2s.BrokenMessageError as e:
            last_error = broken = (host, e)
    if last_error is None:
        raise OSError('查询无响应')
    if isinstance(last_error, tuple):
        raise a2s.BrokenMessageError(_describe_a2s_broken_reply(last_error[0], port, last_error[1]))
    raise last_error


# --- 在线玩家详情（RCON status 增强） ---
# A2S 协议只提供名字/得分/时长；userid、SteamID、ping、丢包、玩家 IP 等通过
# RCON（TCP 直连游戏端口）执行 status 命令获得。输出样例（L4D2，BOT 行解析时跳过）:
#   # userid name uniqueid connected ping loss state rate adr
#   #  7 1 "白尾" STEAM_1:1:730687327 00:51 20 0 active 100000 14.212.3.152:16351
#   # 8 "Coach" BOT active
#   #end
RE_ANSI_ESCAPE = re.compile(r'\x1b\[[0-9;?]*[A-Za-z]|\x1b[()][0-9A-B]')
# 人类行：引号后固定 7 字段（SteamID 连接时长 ping 丢包 state rate IP:端口）；
# userid 与名字之间可能夹杂版本相关的附加数字列，用 [^"]* 跳过
RE_STATUS_HUMAN = re.compile(
    r'^#\s+(\d+)[^"]*"(.*)"\s+(STEAM_\d+:[01]:\d+)\s+(\S+)\s+(\d+)\s+(\d+)\s+(\S+)\s+(\S+)\s+([0-9.]+):(\d+)\s*$')
RCON_TIMEOUT = 3.0   # RCON 连接/认证/执行的 socket 超时（秒）

# RCON 认证失败的退避：失败后暂停自动重试一段时间。服务器的 RCON 风控
# （sv_rcon_minfailures/banpenalty）把反复认证失败视为暴力破解并封禁来源 IP——
# 面板 10 秒一轮的查询节奏若不停手，几分钟就能把自己喂进封禁名单（A2S 查询同受其害，
# 表现为回包 "lBanned by server"，python-a2s 报 Invalid response type: 0x6c）。
_RCON_AUTH_BACKOFF = 600   # 退避时长（秒）；期间玩家卡仅展示 A2S 数据与修复提示
_RCON_AUTH_FAIL = {}       # 实例 ID -> {'until': ts, 'hint': str}
_RCON_AUTH_FAIL_LOCK = threading.Lock()


def _detect_cfg_rcon_password(cfg):
    """读取实例 cfg 文件中的 rcon_password 设置，返回密码字符串；未设置/文件不可读返回 None。
    server.cfg 由引擎在地图加载时自上而下执行、会覆盖启动命令里的 +rcon_password——两者
    不一致时每轮 RCON 查询都会认证失败；若多行设置，以最后出现的为准（与引擎执行顺序一致）。
    cfg 文件位于 <SERVER_ROOT>/left4dead2/cfg/<cfg>。"""
    cfg_dir = os.path.abspath(os.path.join(SERVER_ROOT, 'left4dead2', 'cfg'))
    path = os.path.abspath(os.path.join(cfg_dir, str(cfg.get('cfg') or 'server.cfg')))
    if not path.startswith(cfg_dir + os.sep):
        return None
    found = None
    try:
        with open(path, encoding='utf-8', errors='replace') as f:
            for raw in f:
                line = raw.strip()
                if not line or line.startswith('//'):   # cfg 注释以 // 开头
                    continue
                # 行内注释：值后常跟 // 说明（如 rcon_password xxx // rcon远程控制密码），
                # cfg 语义里 // 到行尾都是注释，先剥掉再解析（面板可管理的密码本就不含 /）
                pos = line.find('//')
                if pos != -1:
                    line = line[:pos].strip()
                    if not line:
                        continue
                # 兼容两种写法：引擎原生的 rcon_password，以及经 SourceMod 拦截生效的
                # sm_cvar rcon_password（后者在装有 SM 的服务器上同样会覆盖面板注入值）
                m = re.match(r'^(?:sm_cvar\s+)?rcon_password\s+(.+?)\s*$', line)
                if not m:
                    continue
                value = m.group(1).strip()
                if len(value) >= 2 and value[0] == '"' and value[-1] == '"':
                    value = value[1:-1].strip()
                if value:
                    found = value   # 自上而下执行，后写的行才是最终生效值
    except OSError:
        return None
    return found


def _migrate_rcon_password():
    """旧部署升级迁移：面板全局 RCON 密码未设置且面板设置文件尚不存在时，优先取
    server.cfg 中生效的 rcon_password（各实例运行时的实际值），其次取首个实例的存储值；
    都没有则留空，由实例启动时懒生成。设置文件一经存在（含用户在设置页主动清空密码的
    情况）即不再迁移，尊重面板当前配置。"""
    if _global_rcon_password() or os.path.exists(PANEL_SETTINGS_FILE):
        return
    candidates = []
    detected = _detect_cfg_rcon_password({'cfg': 'server.cfg'})
    if detected:
        candidates.append(detected)
    for inst_cfg in _load_instances().values():
        pw = str(inst_cfg.get('rcon') or '').strip()
        if pw:
            candidates.append(pw)
    if not candidates:
        return
    with PANEL_SETTINGS_LOCK:
        settings = _load_panel_settings()
        if settings.get('rcon_password'):
            return   # 双检：避免并发启动时覆盖用户刚保存的值
        settings['rcon_password'] = candidates[0]
        _save_panel_settings(settings)


_migrate_rcon_password()


def _steamid_to_steamid64(steamid):
    """STEAM_X:Y:Z → 17 位 SteamID64（76561197960265728 + Z*2 + Y），格式不符返回 None。"""
    m = re.match(r'^STEAM_\d+:[01]:(\d+)$', steamid or '')
    if not m:
        return None
    return str(76561197960265728 + int(m.group(1)) * 2 + (1 if ':1:' in steamid else 0))


def _build_start_params(cfg):
    """组装启动额外参数：用户参数 + 面板统一注入项。
    面板"设置"页配置了全局 RCON 密码时注入 -usercon +rcon_password（玩家详情查询
    依赖）；未配置 = 用户不启用 RCON，不注入任何 RCON 参数（尊重偏好，不自动生成，
    避免与 server.cfg 错寘认证触发服务器 RCON 封禁）。server.cfg 若另设
    rcon_password 会在启动后期覆盖注入值（面板可检测并一键导入）。"""
    user_params = str(cfg.get('params', '') or '')
    rcon_password = _global_rcon_password()
    if not rcon_password:
        return user_params.strip()
    params = f'{user_params} -usercon'
    # 全局密码同时用于启动注入，须可安全拼入命令行；含空格等不安全字符时跳过注入，
    # 依赖 server.cfg 的同名设置（认证用值不受注入影响）
    if RE_SAFE_TOKEN.match(rcon_password):
        params += f' +rcon_password {rcon_password}'
    return params.strip()


def _parse_status_block(text):
    """解析 status 输出块，返回真人玩家行列表（BOT 行与表头/结尾行跳过不统计）。"""
    players = []
    for raw_line in text.splitlines():
        line = RE_ANSI_ESCAPE.sub('', raw_line).rstrip('\r\x00').strip()
        if not line.startswith('#') or line == '#end' or line.startswith('# userid'):
            continue
        m = RE_STATUS_HUMAN.match(line)
        if m:
            userid, name, steamid, connected, ping, loss, _state, _rate, ip, port = m.groups()
            players.append({
                'userid': int(userid), 'name': name.strip(),
                'steamid': steamid, 'steamid64': _steamid_to_steamid64(steamid),
                'duration_text': connected, 'ping': int(ping), 'loss': int(loss),
                'ip': ip, 'port': int(port),
            })
    return players


def _players_via_rcon(cfg, port, inst_id=None):
    """RCON 详情通道：向实例游戏端口执行 status 并解析。返回 (rows, state)：
    rows 为玩家行列表（空列表 = 连接正常但当前无人在线，同样算成功），None = RCON 不可用
    （未配置密码/连接失败/输出异常），由调用方回退 A2S；state 为带给前端的通道状态与
    修复提示。认证失败进入退避：暂停自动重试，避免反复用错密码触发服务器 RCON 风控封禁。
    全局密码未设置 = 用户未启用 RCON：不发起认证，但主动检测 server.cfg——检测到密码时
    返回 detect 状态，提示在全局设置中填写相同密码（此时服务器实际开着 RCON，填对即解锁
    全量数据）。地区通过 ip2region 查询。"""
    password = _global_rcon_password()
    if not password:
        if _detect_cfg_rcon_password(cfg):
            return None, {'state': 'detect',
                          'hint': '检测到 server.cfg 中设置了 rcon_password，'
                                  '在全局设置中填写相同密码即可解锁 SteamID/ping/归属地等完整玩家信息'}
        return None, {'state': 'unavailable'}
    if inst_id is not None:
        with _RCON_AUTH_FAIL_LOCK:
            backoff = _RCON_AUTH_FAIL.get(inst_id)
        if backoff and backoff.get('until', 0) > time.time():
            return None, {'state': 'backoff',
                          'retry_after': int(backoff['until'] - time.time()) + 1,
                          'hint': backoff.get('hint', '')}
    for host in _a2s_targets():   # 复用 A2S 的主机序列（回环优先、内网 IP 兜底）
        try:
            with SourceRcon(host, int(port), password, timeout=RCON_TIMEOUT) as rcon:
                text = rcon.command('status')
        except SourceRconAuthError as e:
            if inst_id is None:
                return None, {'state': 'auth_failed', 'hint': str(e)}
            if _detect_cfg_rcon_password(cfg):
                hint = ('RCON 认证失败：server.cfg 中设置了另一个 rcon_password'
                        '（会在启动后期覆盖面板注入值）。请在全局设置中填写相同密码，'
                        '或删除该行后重启实例。')
            else:
                hint = ('RCON 认证失败（密码错误）。若刚修改过密码，重启实例后生效；'
                        '期间面板已暂停自动重试，避免触发服务器的 RCON 封禁。')
            with _RCON_AUTH_FAIL_LOCK:
                _RCON_AUTH_FAIL[inst_id] = {'until': time.time() + _RCON_AUTH_BACKOFF, 'hint': hint}
            return None, {'state': 'auth_failed', 'hint': hint}
        except (OSError, SourceRconError):
            continue
        # status 输出必带 '# userid' 表头与 '#end' 结尾，两者皆无说明输出异常（按不可用试下一地址）；
        # 表头存在而玩家行为空 = 空服，属正常成功，不再回退 A2S（空服误判会把 A2S 的协议异常漏到前端）
        if '# userid' not in text and '#end' not in text:
            continue
        rows = _parse_status_block(text)
        if inst_id is not None:
            with _RCON_AUTH_FAIL_LOCK:
                _RCON_AUTH_FAIL.pop(inst_id, None)   # 认证成功，解除退避
        for row in rows:
            row['region'] = _geo.format_region(row['ip']) if row.get('ip') else None
        return rows, {'state': 'ok'}
    return None, {'state': 'unavailable'}


# --- Steam 头像（可选增强：Key 在面板"设置"页配置，初始值取 .env 的 STEAM_API_KEY） ---
# 流程：前端渲染首字头像占位 → 批量解析接口（内存命中/磁盘缓存命中 → 未命中走一次
# GetPlayerSummaries 批量取头像 URL → 从 *.steamstatic.com CDN 下载落盘）→ 前端以
# <img> 覆盖首字圆；任何失败静默 404，前端 onerror 移除 <img> 露出首字头像。
# 正缓存 7 天（头像极少更换），失败负缓存 10 分钟（避免玩家列表 10s 自动刷新打爆 API）。
# 未配置 Key = 用户选择默认头像：解析接口整体短路返回空（不读缓存不请求 Steam），
# 设置页清空 Key 即时回退首字；重新填写时在保存接口同步清空负缓存，下一轮轮询即恢复。
_avatar_neg_cache = {}   # steamid64 -> 上次失败时间戳
_avatar_neg_lock = threading.Lock()


def _avatar_cache_path(steamid64, ext='.img'):
    return os.path.join(AVATAR_CACHE_DIR, f'{steamid64}{ext}')


AVATAR_EXTS = ('.jpg', '.jpeg', '.png', '.gif', '.webp')


def _avatar_fresh(steamid64):
    """磁盘正缓存是否命中（存在且未过期，扩展名以实际落盘为准）。"""
    for ext in AVATAR_EXTS:
        path = _avatar_cache_path(steamid64, ext)
        try:
            if os.path.getmtime(path) > time.time() - AVATAR_TTL:
                return True, path
        except OSError:
            continue
    return False, _avatar_cache_path(steamid64, '.jpg')


def _http_get(url, timeout):
    req = urllib.request.Request(url, headers={'User-Agent': 'l4d2-panel'})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def _save_avatar(steamid64, url, img):
    """把头像字节落盘（从 URL 后缀判扩展名，原子写入，清理旧格式残留）。成功返回扩展名。"""
    ext = os.path.splitext(urllib.parse.urlsplit(url).path)[1].lower()
    if ext not in AVATAR_EXTS:
        ext = '.jpg'
    tmp_path = _avatar_cache_path(steamid64, ext + '.tmp')
    os.makedirs(AVATAR_CACHE_DIR, exist_ok=True)   # 缓存目录可能被外部清理，落盘前兜底重建
    with open(tmp_path, 'wb') as fh:
        fh.write(img)
    os.replace(tmp_path, _avatar_cache_path(steamid64, ext))
    for old_ext in AVATAR_EXTS:   # 头像格式可能随玩家更新变化，清理旧格式残留
        if old_ext != ext:
            try:
                os.remove(_avatar_cache_path(steamid64, old_ext))
            except OSError:
                pass
    return ext


def _resolve_avatars(steamid64s):
    """批量解析头像：返回 {steamid64: 本地服务路径}。未配置 Key = 用户选择默认头像：
    整体短路返回空（不读磁盘缓存、不请求 Steam），清空 Key 后下一轮轮询即全员回退
    首字头像，重新填回时磁盘缓存仍立即生效。磁盘命中优先，未命中且不在负缓存期的
    合并为一次 API 调用。"""
    steam_key = _steam_api_key()
    if not steam_key:
        return {}
    resolved = {}
    pending = []
    for sid in steamid64s:
        fresh, path = _avatar_fresh(sid)
        if fresh:
            resolved[sid] = f'/api/players/avatar/{sid}'
        else:
            with _avatar_neg_lock:
                failed_at = _avatar_neg_cache.get(sid)
            if not failed_at or time.time() - failed_at > AVATAR_NEG_TTL:
                pending.append(sid)
    if pending:
        # GetPlayerSummaries 单次最多 100 个 steamid，逗号合并
        batch = ','.join(pending[:100])
        try:
            api_url = (f'https://api.steampowered.com/ISteamUser/GetPlayerSummaries/v0002/'
                       f'?key={steam_key}&steamids={batch}')
            data = json.loads(_http_get(api_url, AVATAR_FETCH_TIMEOUT[0]))
            url_map = {p.get('steamid'): (p.get('avatarmedium') or p.get('avatarfull'))
                       for p in data.get('response', {}).get('players', [])}
        except Exception:
            url_map = {}
        for sid in pending:
            url = url_map.get(sid)
            if not url:
                with _avatar_neg_lock:
                    _avatar_neg_cache[sid] = time.time()
                continue
            host = urllib.parse.urlsplit(url).hostname or ''
            if not host.endswith('steamstatic.com'):
                # 防 SSRF：API 响应里的 URL 必须落在 Steam CDN 域名内
                with _avatar_neg_lock:
                    _avatar_neg_cache[sid] = time.time()
                continue
            try:
                img = _http_get(url, AVATAR_FETCH_TIMEOUT[1])
                if not img:
                    raise ValueError('空响应')
                _save_avatar(sid, url, img)
                resolved[sid] = f'/api/players/avatar/{sid}'
            except Exception:
                # 直连 CDN 失败（国内云服务器访问 avatars.steamstatic.com 常被阻断）
                # → 经 weserv 图片代理中转重试一次；url 已通过上方 SSRF 域名校验。
                # output=jpg 固定返回格式，与落盘扩展名判定保持一致
                try:
                    proxy_url = ('https://images.weserv.nl/?url='
                                 + urllib.parse.quote(url, safe='') + '&output=jpg')
                    img = _http_get(proxy_url, AVATAR_FETCH_TIMEOUT[1] + 5)
                    if not img:
                        raise ValueError('空响应')
                    _save_avatar(sid, url, img)
                    resolved[sid] = f'/api/players/avatar/{sid}'
                except Exception:
                    with _avatar_neg_lock:
                        _avatar_neg_cache[sid] = time.time()
    return resolved


def _avatar_mimetype(path):
    ext = os.path.splitext(path)[1].lower()
    return {'.jpg': 'image/jpeg', '.jpeg': 'image/jpeg', '.png': 'image/png',
            '.gif': 'image/gif', '.webp': 'image/webp'}.get(ext, 'image/jpeg')


@app.route('/api/players/avatars', methods=['GET'])
@login_required
def api_players_avatars():
    """批量解析玩家头像：?ids=17位id64,17位id64 → {id64: 本地路径}。"""
    raw = request.args.get('ids', '')
    ids = [s for s in (x.strip() for x in raw.split(',')) if s][:100]
    bad = [s for s in ids if not RE_STEAMID64.match(s)]
    if bad:
        return jsonify({"success": False, "error": "steamid64 格式不合法"}), 400
    if not ids:
        return jsonify({"success": True, "avatars": {}})
    return jsonify({"success": True, "avatars": _resolve_avatars(ids)})


@app.route('/api/players/avatar/<steamid64>', methods=['GET'])
@login_required
def api_players_avatar(steamid64):
    """头像文件服务：命中磁盘缓存直接回传，未命中 404（前端回退首字头像）。"""
    if not RE_STEAMID64.match(steamid64):
        return jsonify({"success": False, "error": "steamid64 格式不合法"}), 400
    for ext in ('.jpg', '.jpeg', '.png', '.gif', '.webp'):
        path = _avatar_cache_path(steamid64, ext)
        if os.path.isfile(path):
            resp = Response(open(path, 'rb').read(), mimetype=_avatar_mimetype(path))
            resp.headers['Cache-Control'] = 'private, max-age=86400'
            return resp
    return jsonify({"success": False, "error": "头像不可用"}), 404


def _validate_instance(data):
    """校验并清洗实例配置，返回 ((显示名, cleaned), None) 或 (None, 错误信息)。
    服务器名称字段已移除：实例名称即游戏内服务器名（+hostname 参数），
    因此实例名称禁用可逃逸引号的字符（\" ` $ \\ ; & | < > '），其余（中文、空格、括号等）均可。"""
    display_name = str(data.get('name', '')).strip()
    if not RE_INSTANCE_DISPLAY.match(display_name):
        return None, "实例名称不能为空，且不超过 64 个字符（可用中文、英文等任意可见字符）"
    if not RE_HOSTNAME.match(display_name):
        return None, "实例名称包含被禁止的字符（不允许 \" ` $ \\ ; & | < > '）"
    try:
        port = int(data.get('port'))
    except (TypeError, ValueError):
        return None, "端口必须是数字"
    if not 1024 <= port <= 65535:
        return None, "端口范围须为 1024-65535"
    map_name = str(data.get('map', '')).strip()
    if not RE_SAFE_TOKEN.match(map_name):
        return None, "地图名称只能包含字母、数字、点、下划线和短横线"
    mode = str(data.get('mode', '')).strip() or 'coop'
    if not RE_MODE.match(mode):
        return None, "模式只能包含字母、数字和下划线（留空保存为默认 coop）"
    cfg = str(data.get('cfg', 'server.cfg')).strip() or 'server.cfg'
    if not RE_SAFE_TOKEN.match(cfg):
        return None, "配置文件名只能包含字母、数字、点、下划线和短横线"
    try:
        tick = int(data.get('tick', 100))
    except (TypeError, ValueError):
        return None, "tickrate 必须是数字"
    if not 10 <= tick <= 200:
        return None, "tickrate 范围须为 10-200"
    params = str(data.get('params', '')).strip()
    if not RE_PARAMS.match(params):
        return None, "额外参数包含被禁止的字符（不允许 ; | & $ ` 括号 引号 等）"
    cleaned = {"port": port, "map": map_name, "mode": mode,
               "cfg": cfg, "tick": tick, "params": params}
    return (display_name, cleaned), None


# --- 服务器监控 API ---
# 磁盘/网络速率需要两次采样求差；用一把锁共享"上次采样"，
# 多个浏览器同时轮询时共用同一条时间线，避免互相干扰。
_METRICS_LOCK = threading.Lock()
_METRICS_LAST = {"ts": 0.0, "disk": None, "net": None, "rates": None}
_PANEL_PROCESS = psutil.Process()


def _cpu_model():
    try:
        with open('/proc/cpuinfo', encoding='utf-8') as f:
            for line in f:
                if line.startswith('model name'):
                    return line.split(':', 1)[1].strip()
    except OSError:
        pass
    return platform.processor() or '未知'


def _pick_mount():
    """选择服务器目录所在的分区，找不到时回退到根分区/系统盘。"""
    target = os.path.abspath(SERVER_ROOT)
    best, best_len = None, -1
    for part in psutil.disk_partitions(all=False):
        mount = os.path.abspath(part.mountpoint)
        if target.startswith(mount) and len(mount) > best_len:
            best, best_len = part.mountpoint, len(mount)
    if best:
        return best
    return '/' if os.path.isdir('/') else os.path.splitdrive(os.getcwd())[0] + '\\'


@app.route('/api/metrics', methods=['GET'])
@login_required
def api_metrics():
    now = time.time()
    mem = psutil.virtual_memory()
    panel_cpu = _PANEL_PROCESS.cpu_percent(interval=None)
    panel_rss = _PANEL_PROCESS.memory_info().rss

    try:
        disk_io = psutil.disk_io_counters()
    except Exception:
        disk_io = None
    try:
        net_io = psutil.net_io_counters()
    except Exception:
        net_io = None

    zero_rates = {"read_kbps": 0.0, "write_kbps": 0.0, "read_iops": 0.0, "write_iops": 0.0,
                  "recv_kbps": 0.0, "sent_kbps": 0.0}

    with _METRICS_LOCK:
        dt = now - _METRICS_LAST['ts']
        prev = _METRICS_LAST
        if prev['rates'] is not None and dt < 0.5:
            # 距上次采样太近（多人同时轮询），直接复用刚算好的速率
            rates = prev['rates']
        elif prev['rates'] is not None and dt > 0 and disk_io and net_io and prev['disk'] and prev['net']:
            rates = {
                "read_kbps": max(0.0, (disk_io.read_bytes - prev['disk'].read_bytes) / 1024.0 / dt),
                "write_kbps": max(0.0, (disk_io.write_bytes - prev['disk'].write_bytes) / 1024.0 / dt),
                "read_iops": max(0.0, (disk_io.read_count - prev['disk'].read_count) / dt),
                "write_iops": max(0.0, (disk_io.write_count - prev['disk'].write_count) / dt),
                "recv_kbps": max(0.0, (net_io.bytes_recv - prev['net'].bytes_recv) / 1024.0 / dt),
                "sent_kbps": max(0.0, (net_io.bytes_sent - prev['net'].bytes_sent) / 1024.0 / dt),
            }
            _METRICS_LAST.update(ts=now, disk=disk_io, net=net_io, rates=rates)
        else:
            rates = zero_rates
            _METRICS_LAST.update(ts=now, disk=disk_io, net=net_io, rates=rates)

    mount = _pick_mount()
    try:
        disk_usage = psutil.disk_usage(mount)
        disk_total, disk_used = disk_usage.total, disk_usage.used
        disk_percent = disk_usage.percent
    except Exception:
        disk_total = disk_used = disk_percent = 0

    return jsonify({
        "cpu": {
            "percent": psutil.cpu_percent(interval=None),
            "per_core": psutil.cpu_percent(interval=None, percpu=True),
            "cores": psutil.cpu_count(logical=True),
            "physical_cores": psutil.cpu_count(logical=False),
            "model": _cpu_model(),
            "panel_percent": panel_cpu,
        },
        "memory": {
            "total": mem.total,
            "used": mem.total - mem.available,
            "available": mem.available,
            "cached": getattr(mem, 'cached', 0) + getattr(mem, 'buffers', 0),
            "percent": mem.percent,
            "panel_rss": panel_rss,
        },
        "disk": {
            "mount": mount,
            "total": disk_total,
            "used": disk_used,
            "percent": disk_percent,
            **rates,
        },
        "network": {
            "recv_kbps": rates["recv_kbps"],
            "sent_kbps": rates["sent_kbps"],
            "total_recv": net_io.bytes_recv if net_io else 0,
            "total_sent": net_io.bytes_sent if net_io else 0,
        },
    })


if __name__ == '__main__':
    # 开发模式直接运行；生产环境由 start.sh 以 gunicorn 启动，端口同样读取 PANEL_PORT。
    # exclude_patterns 把数据目录排除出 debug 重载器的监视范围（双保险）：
    # 运行数据已移至项目根 data/（不在 backend/ 监视树内），此清单兜底防止
    # 日后目录布局变动时，上传暂存文件被当成代码变更触发开发服务器重启把用户登出。
    # watchdog 重载器按 pathlib 后缀语义匹配且 * 不跨路径段，需分别覆盖目录本身与 1/2 级内容
    def _reloader_excludes(*dir_names):
        patterns = []
        for name in dir_names:
            patterns += [f'*/{name}', f'*/{name}/*', f'*/{name}/*/*']
        return patterns

    try:
        dev_port = int(os.getenv('PANEL_PORT', '8080'))
    except ValueError:
        dev_port = 8080
    app.run(host='0.0.0.0', port=dev_port, debug=True,
            exclude_patterns=_reloader_excludes(
                'plugin_uploads', 'map_uploads', 'console_logs', 'launch_scripts',
                'Available_Plugins', 'Installed_Receipts', 'Maps'))
