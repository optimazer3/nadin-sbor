"""Прокси для внешних сервисов (Telegram из России без прокси/VPN недоступен).

Поддерживаются SOCKS5 и HTTP-прокси — их даёт Happ и другие клиенты (v2rayN, Hiddify, Clash).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, TypeVar
from urllib.parse import quote, unquote, urlsplit

# Сервисы, которые можно пустить через прокси. Polza AI — российский сервис,
# поэтому по умолчанию к нему ходим напрямую.
SERVICES = ("telegram", "assemblyai", "supabase", "llm")
DEFAULT_SERVICES = ("telegram", "assemblyai", "supabase")

T = TypeVar("T")

_SCHEMES = {"socks5": "socks5", "socks5h": "socks5", "socks": "socks5", "http": "http"}


class ProxyError(ValueError):
    pass


@dataclass(frozen=True)
class Proxy:
    scheme: str  # socks5 | http
    host: str
    port: int
    username: str | None = None
    password: str | None = None

    @classmethod
    def parse(cls, raw: str) -> Proxy:
        """Понимает «socks5://127.0.0.1:10808», «http://user:pass@host:port» и просто «127.0.0.1:10808»."""
        value = raw.strip()
        if "://" not in value:
            value = "socks5://" + value  # без схемы — считаем, что это SOCKS5 (так чаще всего в Happ)
        parts = urlsplit(value)
        scheme = _SCHEMES.get(parts.scheme.lower())
        if scheme is None:
            raise ProxyError(
                f"PROXY_URL: тип «{parts.scheme}» не поддерживается — укажите socks5://… или http://…"
            )
        try:
            port = parts.port
        except ValueError as exc:
            raise ProxyError("PROXY_URL: неверный порт") from exc
        if not parts.hostname or not port:
            raise ProxyError("PROXY_URL: укажите адрес и порт, например socks5://127.0.0.1:10808")
        username = unquote(parts.username) if parts.username else None
        password = unquote(parts.password) if parts.password else None
        return cls(scheme, parts.hostname, port, username, password)

    def _url(self, scheme: str) -> str:
        auth = ""
        if self.username:
            auth = quote(self.username, safe="")
            if self.password:
                auth += ":" + quote(self.password, safe="")
            auth += "@"
        host = f"[{self.host}]" if ":" in self.host else self.host
        return f"{scheme}://{auth}{host}:{self.port}"

    @property
    def aiohttp_url(self) -> str:
        """Для aiogram и aiohttp-socks (DNS-имена резолвит сам прокси)."""
        return self._url(self.scheme)

    @property
    def httpx_url(self) -> str:
        """Для httpx (Supabase, нейросеть): socks5h — DNS-имена резолвит прокси."""
        return self._url("socks5h" if self.scheme == "socks5" else "http")

    @property
    def httpx_url_fallback(self) -> str:
        """Для старых httpx (< 0.28), которые не знают socks5h: имена резолвит компьютер."""
        return self._url(self.scheme)

    def httpx_client(self, factory: Callable[..., T], **kwargs: Any) -> T:
        """Создаёт httpx-клиент (httpx.AsyncClient и т.п.) с этим прокси.

        Сначала пробуем socks5h; если установленная версия httpx его не знает
        («Unknown scheme for proxy URL»), берём обычный socks5.
        """
        try:
            return factory(proxy=self.httpx_url, **kwargs)
        except ValueError:
            if self.httpx_url == self.httpx_url_fallback:
                raise
            return factory(proxy=self.httpx_url_fallback, **kwargs)

    def __str__(self) -> str:  # без пароля — для логов
        return f"{self.scheme}://{self.host}:{self.port}"


def parse_services(raw: str | None) -> frozenset[str]:
    if raw is None or not raw.strip():
        return frozenset(DEFAULT_SERVICES)
    names = {part.strip().lower() for part in raw.replace(";", ",").split(",") if part.strip()}
    unknown = names - set(SERVICES)
    if unknown:
        raise ProxyError(
            f"PROXY_SERVICES: неизвестно {', '.join(sorted(unknown))}; можно: {', '.join(SERVICES)}"
        )
    return frozenset(names)
