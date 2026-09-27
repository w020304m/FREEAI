# -*- coding: utf-8 -*-
"""本机出口转发代理:让 CDP 浏览器(浏览器派 provider)用上代理池的可换 IP。

原理:浏览器 --proxy-server=http://127.0.0.1:<port> 指向本转发器;浏览器发起的
CONNECT 隧道被转发到「当前选定的池内上游代理」。配额触发(429/容量阻断)时
rotate() 从池子取新 IP,浏览器无需重启即可更换出口——解决 aifreeforever 这类
浏览器派 provider 无法享受代理轮换的问题。

约束与取舍:
- 换 IP 后,上游若绑定 IP 的凭据(cf_clearance/Turnstile)会重新质询,
  由既有的浏览器人机验证流程承接(窗口自动呼出);
- 仅实现浏览器必需的 CONNECT 隧道(https),上游支持 http/socks5;
- 转发器只监听 127.0.0.1,不做鉴权(本机回环,不对外暴露)。
"""
from __future__ import annotations

import asyncio
import logging
import struct
import time
from typing import Optional, Tuple
from urllib.parse import urlparse

from . import config

logger = logging.getLogger(__name__)

_state: dict = {"running": False, "upstream": None, "rotations": 0, "last_rotate": 0.0,
                "connects": 0, "fails": 0, "port": 0, "started_at": 0.0}
_server: Optional[asyncio.AbstractServer] = None
_lock = asyncio.Lock()


def enabled() -> bool:
    return bool(getattr(config.settings, "AIFREE_PROXY_ENABLED", False)
                and config.settings.PROXYHUB_KEY)


def address() -> str:
    """转发器本机地址(cdp_bridge 挂 --proxy-server 用);未运行返回空串。"""
    return f"http://127.0.0.1:{_state['port']}" if _state["running"] else ""


def stats() -> dict:
    return {k: (round(v - _state["started_at"]) if k == "uptime" else v)
            for k, v in _state.items()}


async def acquire_upstream() -> Optional[str]:
    """从代理池取上游 IP。注意:**不带 check_url**——aifreeforever 的 CF 会对
    数据中心 IP 出质询,curl 类质检必挂;但质询由真浏览器承接,IP 连通性由
    precheck 保障即可。"""
    try:
        from .providers import pool
        return await pool.acquire()
    except Exception as e:  # noqa: BLE001
        logger.warning("[outbound-proxy] 取上游 IP 失败: %s", str(e)[:120])
        return None


async def rotate(reason: str = "") -> Optional[str]:
    """换上游出口 IP(配额触发时调用);成功返回新 URI。"""
    async with _lock:
        uri = await acquire_upstream()
        if not uri:
            logger.warning("[outbound-proxy] 换 IP 失败: 池子无可用 IP(%s)", reason)
            return None
        if uri == _state["upstream"]:
            return uri
        _state["upstream"] = uri
        _state["rotations"] += 1
        _state["last_rotate"] = time.time()
        logger.info("[outbound-proxy] 出口已轮换(%s): %s", reason, uri)
        return uri


def note_quota(reason: str = "quota") -> None:
    """配额事件钩子(不阻塞调用方):后台触发一次换 IP。"""
    if not enabled():
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return  # 无运行中事件循环(如同步测试),跳过
    loop.create_task(_rotate_logged(reason))


async def _rotate_logged(reason: str) -> None:
    try:
        await rotate(reason)
    except Exception as e:  # noqa: BLE001
        logger.warning("[outbound-proxy] rotate 异常: %s", str(e)[:120])


# ---- 隧道实现 ----

async def _tunnel_via_socks4(upstream: Tuple[str, int], host: str, port: int):
    """SOCKS4 不支持域名,本机解析 IP 后按协议发 IP 连接请求。"""
    ur, uw = await asyncio.open_connection(upstream[0], upstream[1])
    try:
        infos = await asyncio.get_running_loop().getaddrinfo(host, port, proto=0)
        addr = infos[0][4][0]
        packed = bytes(int(x) for x in addr.split("."))
        if len(packed) != 4:
            raise ValueError("非 IPv4 目标,SOCKS4 不支持")
    except Exception:  # noqa: BLE001
        uw.close()
        return None
    uw.write(b"\x04\x01" + struct.pack(">H", port) + packed + b"\x00")
    await uw.drain()
    resp = await ur.readexactly(8)
    if resp[1] != 0x5A:  # 90=请求许可
        uw.close()
        return None
    return ur, uw


async def _tunnel_via_http(upstream: Tuple[str, int], host: str, port: int) -> Optional[Tuple[asyncio.StreamReader,
                                                                     asyncio.StreamWriter]]:
    ur, uw = await asyncio.open_connection(upstream[0], upstream[1])
    uw.write(f"CONNECT {host}:{port} HTTP/1.1\r\nHost: {host}:{port}\r\n\r\n".encode())
    await uw.drain()
    head = await ur.readuntil(b"\r\n\r\n")
    if b" 200" not in head.split(b"\r\n", 1)[0]:
        uw.close()
        return None
    return ur, uw


async def _tunnel_via_socks5(upstream: Tuple[str, int], host: str, port: int):
    ur, uw = await asyncio.open_connection(upstream[0], upstream[1])
    uw.write(b"\x05\x01\x00")  # 无鉴权
    await uw.drain()
    if (await ur.readexactly(2)) != b"\x05\x00":
        uw.close()
        return None
    hb = host.encode()
    uw.write(b"\x05\x01\x00\x03" + bytes([len(hb)]) + hb + struct.pack(">H", port))
    await uw.drain()
    head = await ur.readexactly(4)  # VER REP RSV ATYP
    if head[1] != 0:
        uw.close()
        return None
    atyp = head[3]
    if atyp == 1:
        await ur.readexactly(4 + 2)   # IPv4 + port
    elif atyp == 3:
        ln = (await ur.readexactly(1))[0]
        await ur.readexactly(ln + 2)  # domain + port
    elif atyp == 4:
        await ur.readexactly(16 + 2)  # IPv6 + port
    return ur, uw


async def _pipe(r: asyncio.StreamReader, w: asyncio.StreamWriter, tag: str = "") -> None:
    n = 0
    try:
        while True:
            data = await r.read(65536)
            if not data:
                break
            n += len(data)
            if n == len(data):
                logger.info("[outbound-proxy] %s 首块 %dB", tag, len(data))
            w.write(data)
            await w.drain()
    except Exception as e:  # noqa: BLE001
        logger.warning("[outbound-proxy] 管道中断(%s, 已传 %dB): %s: %s",
                       tag, n, type(e).__name__, str(e)[:100])
    finally:
        logger.info("[outbound-proxy] %s 结束,共 %dB", tag, n)
        try:
            w.close()
        except Exception:  # noqa: BLE001
            pass


async def _handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    _state["connects"] += 1
    try:
        line = (await reader.readuntil(b"\r\n\r\n")).decode("latin1", "replace")
        method = line.split(" ", 1)[0].upper()
        target = line.split(" ", 2)[1] if len(line.split(" ", 2)) > 1 else ""
        if method != "CONNECT":
            writer.write(b"HTTP/1.1 405 Method Not Allowed\r\n\r\n")
            await writer.drain()
            return
        host, _, port_s = target.partition(":")
        port = int(port_s or 443)
        logger.info("[outbound-proxy] CONNECT %s:%s (上游 %s)", host, port, _state.get("upstream"))
        writer.write(b"HTTP/1.1 200 Connection established\r\n\r\n")
        await writer.drain()
        upstream_uri = _state.get("upstream")
        if not upstream_uri:
            # 没有可用上游:直连兜底(至少不把浏览器弄瞎)
            ur, uw = await asyncio.open_connection(host, port)
        else:
            u = urlparse(upstream_uri)
            upstream = (u.hostname or "", u.port or (1080 if u.scheme.startswith("socks") else 8080))

            async def _establish():
                # 隧道建立必须有超时:劣质代理卡在握手上会拖死整条连接
                if u.scheme.startswith("socks5"):
                    return await asyncio.wait_for(
                        _tunnel_via_socks5(upstream, host, port), timeout=15)
                if u.scheme.startswith("socks4"):
                    return await asyncio.wait_for(
                        _tunnel_via_socks4(upstream, host, port), timeout=15)
                return await asyncio.wait_for(
                    _tunnel_via_http(upstream, host, port), timeout=15)

            for attempt in (0, 1):
                try:
                    tun = await _establish()
                    if tun:
                        ur, uw = tun
                        break
                except Exception:  # noqa: BLE001
                    tun = None
                if attempt == 0:
                    # 上游代理失败:当场换一个 IP 重试一次
                    await rotate("tunnel fail")
                    if not _state.get("upstream"):
                        ur, uw = await asyncio.open_connection(host, port)
                        break
            else:
                _state["fails"] += 1
                return
        await asyncio.gather(_pipe(reader, uw, "客户端→上游"), _pipe(ur, writer, "上游→客户端"))
    except Exception as e:  # noqa: BLE001
        _state["fails"] += 1
        logger.warning("[outbound-proxy] 隧道失败: %s: %s", type(e).__name__, str(e)[:120])
    finally:
        try:
            writer.close()
        except Exception:  # noqa: BLE001
            pass


async def start() -> bool:
    """启动本机转发器;成功返回 True(已运行/重复调用幂等)。"""
    global _server
    if not enabled() or _state["running"]:
        return _state["running"]
    port = int(getattr(config.settings, "AIFREE_PROXY_PORT", 18110))
    _server = await asyncio.start_server(_handle, "127.0.0.1", port)
    _state.update(running=True, port=port, started_at=time.time())
    uri = await acquire_upstream()
    _state["upstream"] = uri
    logger.info("[outbound-proxy] 本机出口转发器已启动: %s (上游: %s)",
                address(), uri or "暂无,首连时兜底直连")
    return True


async def stop() -> None:
    global _server
    if _server:
        _server.close()
        try:
            await _server.wait_closed()
        except Exception:  # noqa: BLE001
            pass
    _server = None
    _state["running"] = False
