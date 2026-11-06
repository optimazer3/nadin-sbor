"""Мини-прокси для тестов: SOCKS5 и HTTP (CONNECT + обычная пересылка). Запоминают, куда подключались."""

from __future__ import annotations

import asyncio
import contextlib
import struct
from urllib.parse import urlsplit


async def _pipe(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        while data := await reader.read(65536):
            writer.write(data)
            await writer.drain()
    except (ConnectionError, asyncio.CancelledError):
        pass
    finally:
        with contextlib.suppress(Exception):
            writer.close()


async def _tunnel(cr, cw, tr, tw) -> None:
    await asyncio.gather(_pipe(cr, tw), _pipe(tr, cw))


class _BaseProxy:
    def __init__(self) -> None:
        self.targets: list[tuple[str, int]] = []
        self.server: asyncio.base_events.Server | None = None
        self.port = 0

    async def start(self) -> _BaseProxy:
        self.server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
        self.port = self.server.sockets[0].getsockname()[1]
        return self

    async def close(self) -> None:
        if self.server:
            self.server.close()
            await self.server.wait_closed()

    async def _handle(self, reader, writer) -> None:  # pragma: no cover - переопределяется
        raise NotImplementedError


class Socks5Proxy(_BaseProxy):
    @property
    def url(self) -> str:
        return f"socks5://127.0.0.1:{self.port}"

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            ver, nmethods = await reader.readexactly(2)
            await reader.readexactly(nmethods)
            writer.write(b"\x05\x00")  # без авторизации
            _, cmd, _, atyp = await reader.readexactly(4)
            if atyp == 1:
                host = ".".join(str(b) for b in await reader.readexactly(4))
            elif atyp == 3:
                (length,) = await reader.readexactly(1)
                host = (await reader.readexactly(length)).decode()
            else:
                writer.close()
                return
            (port,) = struct.unpack("!H", await reader.readexactly(2))
            self.targets.append((host, port))
            tr, tw = await asyncio.open_connection("127.0.0.1" if host == "localhost" else host, port)
            writer.write(b"\x05\x00\x00\x01" + bytes(4) + b"\x00\x00")
            await writer.drain()
            await _tunnel(reader, writer, tr, tw)
        except (asyncio.IncompleteReadError, ConnectionError):
            writer.close()


class HttpProxy(_BaseProxy):
    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            head = await reader.readuntil(b"\r\n\r\n")
        except (asyncio.IncompleteReadError, asyncio.LimitOverrunError):
            writer.close()
            return
        request_line, _, rest = head.partition(b"\r\n")
        method, target, version = request_line.decode().split(" ")
        if method == "CONNECT":
            host, _, port = target.rpartition(":")
            self.targets.append((host, int(port)))
            tr, tw = await asyncio.open_connection("127.0.0.1" if host == "localhost" else host, int(port))
            writer.write(b"HTTP/1.1 200 Connection established\r\n\r\n")
            await writer.drain()
            await _tunnel(reader, writer, tr, tw)
            return
        # Обычная пересылка: «GET http://host:port/path HTTP/1.1»
        url = urlsplit(target)
        port = url.port or 80
        self.targets.append((url.hostname, port))
        path = url.path + (f"?{url.query}" if url.query else "")
        tr, tw = await asyncio.open_connection(
            "127.0.0.1" if url.hostname == "localhost" else url.hostname, port
        )
        tw.write(f"{method} {path or '/'} {version}\r\n".encode() + rest)
        await tw.drain()
        await _tunnel(reader, writer, tr, tw)
