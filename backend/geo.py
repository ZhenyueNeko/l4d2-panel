# -*- coding: utf-8 -*-
"""IP 归属地查询（ip2region 离线库，数据文件自动获取）。

数据源：lionsoul2014/ip2region 项目的 xdb 格式离线数据库（Apache-2.0），
返回"国家|区域|省|市|运营商"格式的地区串，格式化为"[运营商] 国家-省-市"。
数据库文件 (data/ip2region_v4.xdb，约 11MB) 首次使用时自动下载，之后零外网依赖。
查询采用整库内存缓存模式：线程安全、单次查询零 IO（面板为 gunicorn 多线程）。
"""
import os
import threading
import time
import urllib.request

from ip2region_xdb import searcher as _xdb_searcher
from ip2region_xdb import util as _xdb_util

# 数据库与下载源：gitee 在国内服务器上通常更快，GitHub 作回退
# 数据文件与运行数据同放 data/ 目录（backend/geo.py 上溯一级即项目根）
GEO_DB_PATH = os.path.normpath(os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'data', 'ip2region_v4.xdb'))
GEO_DB_URLS = [
    'https://gitee.com/lionsoul/ip2region/raw/master/data/ip2region_v4.xdb',
    'https://raw.githubusercontent.com/lionsoul2014/ip2region/master/data/ip2region_v4.xdb',
]
GEO_DB_MIN_BYTES = 1024 * 1024   # 完整库远大于 1MB，用于识别残缺下载
GEO_RETRY_INTERVAL = 3600        # 下载失败后的重试间隔（秒），避免每次查询都撞外网

_geo_lock = threading.Lock()
_geo_searcher = None
_geo_failed_at = 0.0


def _download_geo_db(dest_path):
    """从下载源序列依次尝试拉取 xdb 到 dest_path（先写临时文件再原子替换）。"""
    for url in GEO_DB_URLS:
        tmp_path = dest_path + '.tmp'
        try:
            req = urllib.request.Request(url, headers={'User-Agent': 'l4d2-panel'})
            with urllib.request.urlopen(req, timeout=120) as resp, open(tmp_path, 'wb') as fh:
                while True:
                    chunk = resp.read(256 * 1024)
                    if not chunk:
                        break
                    fh.write(chunk)
            if os.path.getsize(tmp_path) < GEO_DB_MIN_BYTES:
                raise ValueError('下载数据不完整')
            os.replace(tmp_path, dest_path)
            return True
        except (OSError, ValueError):
            try:
                os.remove(tmp_path)
            except OSError:
                pass
    return False


def _load_geo_searcher():
    """惰性加载：库文件缺失/上次下载失败超过重试间隔时自动下载并整库载入内存。"""
    global _geo_searcher, _geo_failed_at
    with _geo_lock:
        if _geo_searcher is not None:
            return _geo_searcher
        if _geo_failed_at and time.time() - _geo_failed_at < GEO_RETRY_INTERVAL:
            return None
        if not os.path.isfile(GEO_DB_PATH) or os.path.getsize(GEO_DB_PATH) < GEO_DB_MIN_BYTES:
            if not _download_geo_db(GEO_DB_PATH):
                _geo_failed_at = time.time()
                return None
        try:
            with open(GEO_DB_PATH, 'rb') as fh:
                content = fh.read()
            _geo_searcher = _xdb_searcher.new_with_buffer(_xdb_util.IPv4, content)
            return _geo_searcher
        except (OSError, ValueError):
            _geo_failed_at = time.time()
            return None


def _lookup_raw(ip):
    """查询原始地区串（"国家|区域|省|市|运营商"），失败返回 None。"""
    searcher = _load_geo_searcher()
    if searcher is None:
        return None
    try:
        return searcher.search(ip) or None
    except (ValueError, OSError):
        return None


def format_region(ip):
    """把 IP 格式化为"[运营商] 国家-省-市"展示串；查不到或内网/保留地址返回 None。

    兼容两种库格式：
    v4 新版: 国家|省|市|运营商|国家代码（如 中国|安徽省|马鞍山市|移动|CN）
    2.0 旧版: 国家|区域|省|市|ISP（区域通常为 0 占位）
    """
    raw = _lookup_raw(ip)
    if not raw:
        return None
    parts = [p.strip() for p in raw.split('|')]
    while len(parts) < 5:
        parts.append('')
    country = parts[0]
    if parts[1] in ('', '0'):
        province, city, isp = parts[2], parts[3], parts[4]
    else:
        province, city, isp = parts[1], parts[2], parts[3]
    if country.lower() == 'reserved' or not country or country == '0':
        return None   # 内网/保留地址无归属地意义
    geo = '-'.join(p for p in (country, province, city) if p and p != '0')
    isp_text = f'[{isp}] ' if isp and isp != '0' else ''
    if not geo:
        return None
    return f'{isp_text}{geo}'
