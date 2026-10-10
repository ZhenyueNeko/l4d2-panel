# L4D2 管理面板（裸机部署版）

一个现代化、响应式的 Left 4 Dead 2 服务器可视化管理面板。通过一个简洁的 Web 界面，简化了服务器的部署、配置、实例管理和插件安装等操作，专为服主设计。本版本**不再依赖 Docker**，可直接部署在轻量云服务器（Ubuntu/Debian/CentOS）上。

## ✨ 功能特性

  - **仪表盘概览**: 实时查看服务器核心状态（每 2 秒刷新 CPU/内存/磁盘/网络监控，每 10 秒刷新实例与插件状态）
  - **一键部署**: 网页直接调用 SteamCMD 下载或更新求生之路2服务端（支持匿名四步混合平台下载，无需 Steam 账号），实时日志输出
  - **实例管理**: 网页上在线新增/编辑/删除服务器实例（名称/IP/端口/地图/模式/Tickrate/额外参数），无需修改任何脚本文件
  - **插件管理系统**: 在文件管理页"可安装插件目录"上传压缩包/.smx 后一键"识别导入"（按路径落点规则识别，兼容 JSGME 整合包/论坛直根包/裸 .smx，不判定内容性质），自动拆分入库；插件管理页批量安装和卸载
  - **文件管理**: 网页浏览、编辑、上传、解压、重命名服务器文件
  - **地图管理**: 弹窗式上传/管理第三方地图（.vpk/.zip/.rar/.7z，压缩包自动提取 vpk 入库），入库后一键"应用"以软链接接入 addons，删除/清空只操作地图库、不触碰 addons 其他文件；支持按 missions/addoninfo 标题一键重命名（可选前缀、同名自动追加序号）
  - **安全认证**: 独立的面板登录认证机制
  - **双主题**: Claude 风格亮/暗主题，可切换记忆

## 🚀 快速启动（裸机/云服务器）

### 1. 上传项目

将整个项目目录上传到云服务器（如 `/opt/l4d2-panel`），进入目录：

```bash
cd /opt/l4d2-panel
```

### 2. 安装并启动面板

```bash
bash scripts/manage.sh
```

在交互式菜单中按提示逐项完成即可：

| 菜单选项 | 说明 |
|---|---|
| 1) 安装系统依赖 | 等同 `sudo bash scripts/setup.sh`，自动安装 Python3、screen、expect、rsync、32 位运行库（SteamCMD 需要）等全部依赖 |
| 2) 配置面板信息 | 逐项交互生成 `.env`：登录账号/密码必填（密码隐藏输入并二次确认），SECRET_KEY 默认自动生成，面板端口默认 8080（可自行输入避免冲突），Steam 账号、服务端/SteamCMD 目录为可选项，直接回车即使用默认值；已有配置会显示为默认值，回车即保留，修改前自动备份为 `.env.bak` |
| 3) 启动面板 | 在独立的 screen 后台会话（`l4d2_panel`）中运行，**不占用当前终端**，日志写入 `panel.log` |
| 4/5/6) 停止/重启/查看状态 | 日常管理面板；停止面板不影响正在运行的游戏实例 |

启动后访问 `http://服务器IP:8080`（默认端口，可在"配置面板信息"中修改 `PANEL_PORT`，记得在云厂商防火墙/安全组放行对应端口）。

查看面板运行日志的两种方式：

```bash
tail -f panel.log              # 查看日志文件
screen -r l4d2_panel           # 进入面板会话查看实时输出（按 Ctrl+A 再按 D 返回终端）
```

> 提示：面板后续的启动/停止/重启/状态查看，都通过 `bash manage.sh` 菜单完成。请勿混用 `bash start.sh` 等其他方式直接启动，否则面板进程不在 `l4d2_panel` 会话中，菜单将无法查看和管理它。

## ⚙️ 配置指南

### 1. 服务器实例（网页在线管理）

进入"实例管理"页面，点击"新增实例"即可在网页上填写全部配置，无需修改任何脚本文件：

| 配置项 | 说明 | 示例 |
|---|---|---|
| 实例名称 | 中英文均可，1-64 字符；既是面板显示名，也是游戏内玩家看到的服务器名（以 `+hostname "实例名"` 传入启动命令，不支持 `` " ` $ \ ; & | < > ' `` 等字符；screen 会话按端口命名，如 l4d2_manager_inst_27015）。如需中文等更丰富的服务器名，可安装"服务器中文名"类插件 | 主服 / main |
| 端口 | 1024-65535 | 27015 |
| 初始地图 | 下拉选择全部 57 张官方地图（c1m1_hotel ~ c14m2_lighthouse，含 c6m3_port），无需手输 | c2m1_highway |
| 游戏模式 | coop/realism/versus/survival/scavenge 等 | coop |
| 配置文件 | 服务端 cfg 文件名 | server.cfg |
| Tickrate | 10-200，官服为 30 | 100 |
| 额外参数 | 启动附加参数（新建时默认 `-insecure +sv_setmax 31`，可自行增删；禁止 `; \| & $` 等字符）。`-strictportbind/-nobreakpad/-noassert` 三个稳定性参数由后端启动时自动追加，无需填写 | +sv_setmax 31 |

> 监听 IP 固定为 `0.0.0.0`（监听所有网卡），不再单独开放配置。

配置保存在 `data/instances.json`，编辑/删除也在网页完成；正在运行的实例需先停止才能改名或删除。

### 2. Steam 账号（可选）

求生之路2服务端（App ID 222860）支持**匿名下载**，通常无需 Steam 账号。若匿名下载持续失败，可在 `.env` 中配置 `STEAM_USER` / `STEAM_PASSWORD`（建议使用小号），面板会自动改用账号登录。

### 3. 服务端安装目录

默认安装到 `/home/steam/l4d2server`，可在 `.env` 中通过 `SERVER_ROOT` 修改。该目录同时是"文件管理"页的根目录。

### 4. 插件

  - **标准封装**: 每个插件一个文件夹，内部带 `left4dead2` 游戏目录层，即
    `<插件名>/left4dead2/addons/sourcemod/plugins/xxx.smx` 是典型形态；
    metamod 扩展（`left4dead2/addons/metamod/`）、vpk MOD（`left4dead2/addons/*.vpk`）、
    纯配置包（`left4dead2/cfg/`）同为合法形态
  - **上传与识别导入**: 上传入口统一在 **文件管理 → 可安装插件目录**（即 `data/Available_Plugins/`）。
    把 .zip/.rar/.7z/.tar 系压缩包上传到该目录后，点文件行的「识别导入」按钮
    （文件夹上传完成时自动触发校验）。判定唯一标准：**文件夹内直接包含 `left4dead2`
    目录层即为一个插件**——不看内容性质、不要求含 .smx、不校验内部路径对错，
    确认弹窗中勾选/改名后一键拆分为独立的可安装插件，导入完成时库内源压缩包自动清理：
    - **JSGME 整合包**（如 `JS-MODS/插件名/left4dead2/...`）：自动穿透外壳逐个识别，`!BACKUP`、
      `!INSTLOGS` 备份目录自动排除；整包上传时"已启用合集"会以 ⚠ 提示与逐项插件重复
    - 与现有插件同名时自动追加序号；候选与已有插件含同名 .smx 时提示"疑似同一插件的其他版本"；
      未确认导入的暂存包 6 小时后自动清理
    - 面板不自动整形包装：压缩包内没有 `left4dead2` 层（如论坛直根包、裸 .smx）不会入库，
      拒绝文案会给出标准封装示例
  - 该目录下的压缩包不再提供通用"解压"按钮（避免绕过校验产生垃圾目录），统一走「识别导入」
  - **配置/依赖文件编辑**: 插件管理页"可安装插件"条目上，「配置」按钮（检测到可编辑文件时显示）
    可直接编辑库内主副本的文本文件，范围限定于 `left4dead2/cfg/**`、`left4dead2/addons/sourcemod/`
    下的 `configs/**`、`data/**`、`translations/**`、`gamedata/**`，扩展名 `.cfg/.txt/.ini`，
    单文件 ≤1MB。库内是主副本：改一次，装卸/重装都不丢；已安装插件的服务器侧文件请用
    文件管理页修改；GBK 编码的老配置保存时自动转为 UTF-8

## 🔌 前后端接口对照表

| 页面 | 前端调用的接口 | 后端转发到的脚本命令 |
|---|---|---|
| 登录页 | POST /login | —（Flask 会话） |
| 仪表盘 | GET /api/status · /api/instances · /api/plugins · /api/metrics | get_status · —（JSON 直读）· get_plugins · —（psutil） |
| 实例管理 | POST /api/action(start/stop_instance) · /api/instances/save · /api/instances/delete | start_instance（8 参数）· stop_instance · —（instances.json） |
| 文件管理 | /api/files/* 八件套 · POST /api/action(deploy_server 流式) · POST /api/plugins/recognize（可安装插件目录"识别导入"） | list_files · get/save_file_content · delete_path · create_folder · unzip_file · rename_path · upload · Deploy-L4D2Server · —（Python 解压/扫描） |
| 地图管理弹窗 | POST /api/maps/upload · list · apply · unapply · rename · rename_display · delete · clear | install_map · list_maps · apply_map · unapply_map · rename_map · delete_map · clear_maps |
| 插件管理 | GET /api/plugins · POST /api/action(install/uninstall_plugin) · POST /api/plugins/import_archive | get_plugins · Invoke-PluginInstallation/Uninstallation · —（Python 导入） |
| 操作入口 | GET /api/logs（已移除） | — |

## 🎨 主题与字体

面板内置 Claude 风格的亮/暗双主题（暖纸白 `#faf9f5` / 暖炭 `#262624`，强调色陶土橙 `#D97757`）。中文衬线字体内置 OFL 协议的思源宋体（可合法分发）；如需 1:1 还原 Claude 西文字体，可自行安装 [typora_claude](https://github.com/blaxisomu/typora_claude) 仓库提供的字体（仅本机安装，本项目不打包分发）。

## 🛠️ 技术栈

  - **后端**: Flask + Gunicorn + psutil
  - **前端**: 原生 HTML/CSS/JavaScript, SweetAlert2, Feather Icons
  - **核心逻辑**: Bash Script (`screen` 用于会话管理)
  - **部署**: 裸机/云服务器（scripts/manage.sh 交互式管理，底层为 setup.sh + start.sh）

## 📁 目录结构

```
.
├── backend/                        # 应用代码（不含任何运行数据）
│   ├── app.py                      # Flask 应用主文件（页面路由 + JSON API）
│   ├── requirements.txt            # Python 依赖
│   ├── shell/
│   │   └── L4D2_Manager_API.sh     # 核心功能的 Shell 脚本（部署/实例/插件/地图）
│   ├── static/
│   │   ├── fonts/                  # 内置思源宋体（OFL 协议）
│   │   ├── style.css               # 全局样式表（亮/暗双主题）
│   │   ├── editor.js               # 配置文件编辑器弹窗（页面共用）
│   │   ├── plugin_import.js        # 插件"识别导入"确认弹窗（页面共用）
│   │   ├── theme.js                # 主题与侧边栏状态管理
│   │   ├── login_logo.png          # 登录页/侧边栏标志图
│   │   └── favicon-32.png / .ico   # 浏览器标签图标
│   └── templates/
│       ├── sidebar.html            # 侧边导航栏（各页面共用）
│       ├── login.html              # 登录页面
│       ├── dashboard.html          # 仪表盘页面
│       ├── instances.html          # 实例管理页面
│       ├── files.html              # 文件管理页面
│       ├── plugins.html            # 插件管理页面
│       └── editor_modal.html       # 配置文件编辑器模态（files/plugins 共用）
├── scripts/                        # 部署运维脚本
│   ├── manage.sh                   # 交互式管理脚本（依赖安装/配置/后台启停）
│   ├── setup.sh                    # 系统依赖安装
│   └── start.sh                    # 面板启动脚本（gunicorn）
├── tests/                          # 本地测试（Flask test client 端到端）
│   ├── test_plugin_import.py       # 插件识别导入流程
│   ├── test_plugin_config.py       # 插件配置/依赖文件编辑接口
│   └── test_instance_players.py    # 在线玩家 A2S 查询接口
├── data/                           # 运行数据（自动生成，与代码分离，不入库）
│   ├── instances.json              # 实例配置（网页在线管理）
│   ├── Available_Plugins/          # 可安装插件库（上传/识别导入的去处）
│   ├── Installed_Receipts/         # 插件安装回执，请勿修改
│   ├── plugin_uploads/             # 识别导入暂存（源包导入完成后随之清理）
│   ├── Maps/                       # 第三方地图库（"应用"后以软链接接入服务器 addons）
│   ├── map_uploads/                # 地图上传暂存（安装后自动清理）
│   ├── upload_chunks/              # 大文件分片上传会话（组装完成后清理，超 24h 由清扫兜底）
│   ├── avatar_cache/               # Steam 头像缓存（配置 STEAM_API_KEY 后自动生成）
│   ├── Plugin_Backups/             # 插件包备份归档（备份管理弹窗生成）
│   ├── console_logs/               # 实例终端日志（screen 会话流）
│   └── launch_scripts/             # 各实例启动包装脚本（每次启动重建）
├── .env.example                    # 配置文件模板（复制为 .env 使用）
└── LICENSE
```

## 🤝 贡献

欢迎提交 Pull Requests 或 Issues。

## 📄 许可证

本项目采用 [AGPLV3](https://www.gnu.org/licenses/agpl-3.0.html) 许可证。
