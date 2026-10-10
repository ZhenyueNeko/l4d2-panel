#!/bin/bash
# ============================================================
# L4D2 管理面板 - 系统依赖安装（裸机/云服务器，替代原 Dockerfile）
# 支持 Debian/Ubuntu（含树莓派等 arm 环境请自行确认源）与 CentOS
# 用法: sudo bash scripts/setup.sh
# ============================================================

if [ "$(id -u)" -ne 0 ]; then
    echo "错误: 请用 sudo 运行本脚本（需要安装系统软件包）"
    exit 1
fi

source /etc/os-release 2>/dev/null || { echo "错误: 无法识别操作系统"; exit 1; }

echo "== 检测到系统: ${ID} ${VERSION_ID} =="

install_debian_family() {
    export DEBIAN_FRONTEND=noninteractive
    dpkg --add-architecture i386
    apt-get update -y
    # lib32gcc-s1/lib32stdc++6: SteamCMD 32 位运行库
    # zlib1g:i386: SourceMod 的 dbi.mysql.ext 为 32 位扩展，其 MySQL 客户端库依赖 32 位 zlib，
    #              缺失时报 "libz.so.1: cannot open shared object file"
    # screen: 实例会话管理; expect: 提供 unbuffer(部署日志流式输出)
    # rsync: 插件安装; p7zip-full/unzip: 文件管理解压
    apt-get install -y \
        python3 python3-venv python3-pip \
        screen expect rsync wget curl ca-certificates \
        unzip p7zip-full lib32gcc-s1 lib32stdc++6 zlib1g:i386 \
        || echo "警告: zlib1g:i386 安装失败（不影响面板与其他插件，仅 MySQL 扩展不可用）"
    # RAR 解压组件：p7zip-rar 让 7z 能解压 .rar，unrar-free 作回退；缺失只影响 .rar 格式，不中断安装
    apt-get install -y p7zip-rar unrar-free \
        || echo "警告: RAR 解压组件安装失败，文件管理/地图管理将不支持 .rar 格式"
}

install_centos() {
    yum install -y epel-release || true
    yum install -y \
        python3 python3-pip \
        screen expect rsync wget curl ca-certificates \
        unzip p7zip-full glibc.i686 libstdc++.i686 zlib.i686
    # RAR 解压组件（p7zip-plugins 提供 7z 的 RAR 支持），缺失只影响 .rar 格式
    yum install -y p7zip-plugins unrar \
        || echo "警告: RAR 解压组件安装失败，文件管理/地图管理将不支持 .rar 格式"
}

case "${ID}" in
    ubuntu|debian)
        install_debian_family
        ;;
    centos|rhel|rocky|almalinux)
        install_centos
        ;;
    *)
        echo "错误: 暂不支持 ${ID}，请手动安装: python3 screen expect rsync wget curl unzip p7zip-full 及 32 位运行库"
        exit 1
        ;;
esac

if [ $? -eq 0 ]; then
    echo ""
    echo "== 系统依赖安装完成 =="
    echo "下一步: 运行 bash scripts/manage.sh 完成面板配置与后台启动"
    echo "  （也可手动配置: 复制 .env.example 为 .env 并修改账号/密码/SECRET_KEY,"
    echo "    然后运行 bash scripts/start.sh 前台启动面板，默认监听 8080 端口，可用 .env 的 PANEL_PORT 修改）"
else
    echo "== 依赖安装失败，请检查上方错误信息 =="
    exit 1
fi
