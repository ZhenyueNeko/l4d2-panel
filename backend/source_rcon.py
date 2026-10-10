# -*- coding: utf-8 -*-
"""Source RCON 客户端（纯标准库实现）。

协议参照 Valve Developer Wiki 的 Source RCON 规范：TCP 连接游戏端口，
认证（SERVERDATA_AUTH → SERVERDATA_AUTH_RESPONSE）后发送控制台命令
（SERVERDATA_EXECCOMMAND → SERVERDATA_RESPONSE_VALUE）。
多包结束判定与认证失败语义（响应 id = -1）对齐 SourceBans++ 内置的
xPaw/PHP-Source-Query（Rcon.php）的生产级处理方式。

用法：
    with SourceRcon(host, port, password) as rcon:
        text = rcon.command('status')
"""
import socket
import struct

SERVERDATA_AUTH = 3
SERVERDATA_AUTH_RESPONSE = 2
SERVERDATA_EXECCOMMAND = 2
SERVERDATA_RESPONSE_VALUE = 0


class SourceRconError(Exception):
    """RCON 通信或认证异常。"""


class SourceRconAuthError(SourceRconError):
    """RCON 认证失败（密码错误）。与连接类故障区分开：上层据此停止重试
    （反复用错密码认证会被服务器的 RCON 风控按暴力破解封禁来源 IP）。"""


def _encode_packet(req_id, ptype, body: bytes) -> bytes:
    """组包：4字节LE总长 + 4字节LE id + 4字节LE type + body + 双 \\0 结尾。"""
    payload = struct.pack('<ii', req_id, ptype) + body + b'\x00\x00'
    return struct.pack('<i', len(payload)) + payload


def _encode_command(text: str) -> bytes:
    return text.encode('utf-8') + b'\x00'


class SourceRcon:
    """一次性连接用法（每次查询建立连接、认证、执行、关闭：
    玩家列表 10s 级刷新频率下开销可忽略，且免维护长连接状态）。"""

    def __init__(self, host, port, password, timeout=3.0):
        self._addr = (host, int(port))
        self._password = password
        self._timeout = timeout
        self._sock = None
        self._req_id = 0

    def __enter__(self):
        self.connect()
        return self

    def __exit__(self, *exc):
        self.close()

    def _send(self, req_id, ptype, body):
        self._sock.sendall(_encode_packet(req_id, ptype, body))

    def _recv(self):
        """读取一个完整包，返回 (id, type, body)。连接中断抛 OSError。"""
        header = self._recv_exact(4)
        (size,) = struct.unpack('<i', header)
        if size < 10 or size > 8192:
            raise SourceRconError(f'RCON 包长度异常: {size}')
        payload = self._recv_exact(size)
        rid, rtype = struct.unpack('<ii', payload[:8])
        return rid, rtype, payload[8:-2]

    def _recv_exact(self, n):
        buf = b''
        while len(buf) < n:
            chunk = self._sock.recv(n - len(buf))
            if not chunk:
                raise SourceRconError('RCON 连接被对端关闭')
            buf += chunk
        return buf

    def connect(self):
        """建立 TCP 连接并完成密码认证。失败抛 SourceRconError/OSError。"""
        self._sock = socket.create_connection(self._addr, timeout=self._timeout)
        self._req_id += 1
        auth_id = self._req_id
        self._send(auth_id, SERVERDATA_AUTH, _encode_command(self._password))
        while True:
            rid, rtype, _body = self._recv()
            if rtype != SERVERDATA_AUTH_RESPONSE:
                continue   # 部分服务器认证后会先回一个空 RESPONSE_VALUE，跳过
            if rid == -1:
                raise SourceRconAuthError('RCON 认证失败（密码错误）')
            if rid != auth_id:
                raise SourceRconAuthError('RCON 认证响应不匹配')
            return

    def command(self, command):
        """执行控制台命令并返回纯文本输出。"""
        if self._sock is None:
            raise SourceRconError('RCON 未连接')
        self._req_id += 1
        cmd_id = self._req_id
        self._send(cmd_id, SERVERDATA_EXECCOMMAND, _encode_command(command))
        # 多包结束判定：紧跟一个空命令，读到它的响应即代表上一条命令的输出已收全
        # （Valve 规范推荐做法；个别服务器不支持时空等超时，按已收内容返回）
        self._req_id += 1
        end_id = self._req_id
        self._send(end_id, SERVERDATA_EXECCOMMAND, b'')
        chunks = []
        try:
            while True:
                rid, rtype, body = self._recv()
                if rid == -1:
                    raise SourceRconError('RCON 认证失效')
                if rtype != SERVERDATA_RESPONSE_VALUE:
                    continue
                if rid == end_id:
                    break
                chunks.append(body)
        except socket.timeout:
            pass   # 结束包未及时到达：按已收到的内容返回
        return b''.join(chunks).decode('utf-8', 'replace')

    def close(self):
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass
            self._sock = None
