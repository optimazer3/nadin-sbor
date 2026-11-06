"""Прокси: разбор настроек и реальные запросы всех клиентов через локальные SOCKS5/HTTP-прокси."""

from __future__ import annotations

import socket
from datetime import date

import httpx
import openai
import pytest
from aiogram import Bot
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.client.telegram import TelegramAPIServer
from aiohttp import web
from aiohttp.test_utils import TestServer
from supabase import AsyncClientOptions, acreate_client

from bot.config import ConfigError, load_settings
from bot.main import StartupError, _check_telegram
from bot.proxy import Proxy, ProxyError, parse_services
from bot.repository import SupabaseTaskRepository
from bot.services.task_parser import TaskParser
from bot.services.transcriber import AssemblyAITranscriber, TranscriptionError
from tests.proxy_servers import HttpProxy, Socks5Proxy
from tests.test_config_scheduler import BASE_ENV
from tests.test_transcriber import make_app as make_assemblyai_app

# ---------- разбор PROXY_URL ----------


@pytest.mark.parametrize(
    ("raw", "aiohttp_url", "httpx_url"),
    [
        ("socks5://127.0.0.1:10808", "socks5://127.0.0.1:10808", "socks5h://127.0.0.1:10808"),
        ("127.0.0.1:10808", "socks5://127.0.0.1:10808", "socks5h://127.0.0.1:10808"),
        ("socks5h://127.0.0.1:2080", "socks5://127.0.0.1:2080", "socks5h://127.0.0.1:2080"),
        ("http://127.0.0.1:10809", "http://127.0.0.1:10809", "http://127.0.0.1:10809"),
        (
            "  SOCKS5://user:p%40ss@host:1080 ",
            "socks5://user:p%40ss@host:1080",
            "socks5h://user:p%40ss@host:1080",
        ),
    ],
)
def test_proxy_parse(raw, aiohttp_url, httpx_url):
    proxy = Proxy.parse(raw)
    assert proxy.aiohttp_url == aiohttp_url
    assert proxy.httpx_url == httpx_url
    assert "p%40ss" not in str(proxy) and "@" not in str(proxy)  # пароль не попадает в логи


@pytest.mark.parametrize(
    "raw", ["https://127.0.0.1:443", "vless://abc@host:443", "socks5://127.0.0.1", "socks5://:1080"]
)
def test_proxy_parse_errors(raw):
    with pytest.raises(ProxyError):
        Proxy.parse(raw)


def test_parse_services():
    assert parse_services(None) == {"telegram", "assemblyai", "supabase"}
    assert parse_services("telegram, LLM") == {"telegram", "llm"}
    with pytest.raises(ProxyError):
        parse_services("telegram,youtube")


@pytest.fixture
def env(monkeypatch):
    for key in ["PROXY_URL", "PROXY_SERVICES", "TIMEZONE", "REMINDER_DAY", "REMINDER_TIME"]:
        monkeypatch.delenv(key, raising=False)
    for key, value in BASE_ENV.items():
        monkeypatch.setenv(key, value)
    return monkeypatch


def test_settings_without_proxy(env):
    s = load_settings(env_file=None)
    assert s.proxy is None and s.proxy_for("telegram") is None


def test_settings_with_proxy(env):
    env.setenv("PROXY_URL", "socks5://127.0.0.1:10808")
    s = load_settings(env_file=None)
    assert str(s.proxy_for("telegram")) == "socks5://127.0.0.1:10808"
    assert s.proxy_for("supabase") is not None and s.proxy_for("assemblyai") is not None
    assert s.proxy_for("llm") is None  # Polza AI по умолчанию напрямую
    env.setenv("PROXY_SERVICES", "telegram")
    s = load_settings(env_file=None)
    assert s.proxy_for("telegram") is not None and s.proxy_for("supabase") is None


def test_settings_bad_proxy(env):
    env.setenv("PROXY_URL", "vless://whatever")
    with pytest.raises(ConfigError, match="socks5"):
        load_settings(env_file=None)


# ---------- реальные запросы через прокси ----------


@pytest.fixture(params=[Socks5Proxy, HttpProxy], ids=["socks5", "http"])
async def proxy(request):
    p = await request.param().start()
    yield p
    await p.close()


@pytest.fixture
async def serve():
    servers = []

    async def start(app: web.Application) -> int:
        server = TestServer(app)
        await server.start_server()
        servers.append(server)
        return server.port

    yield start
    for server in servers:
        await server.close()


def fake_telegram() -> web.Application:
    app = web.Application()

    async def get_me(request):
        return web.json_response(
            {"ok": True, "result": {"id": 1, "is_bot": True, "first_name": "T", "username": "test_bot"}}
        )

    app.router.add_post("/bot42:TEST/getMe", get_me)
    return app


async def test_telegram_through_proxy(proxy, serve, env):
    port = await serve(fake_telegram())
    env.setenv("PROXY_URL", proxy.url)
    settings = load_settings(env_file=None)
    session = AiohttpSession(
        proxy=settings.proxy_for("telegram").aiohttp_url,
        api=TelegramAPIServer.from_base(f"http://localhost:{port}"),
    )
    bot = Bot("42:TEST", session=session)
    try:
        await _check_telegram(bot, settings)
        me = await bot.get_me()
    finally:
        await bot.session.close()
    assert me.username == "test_bot"
    # Имя хоста передано прокси как есть — DNS резолвит прокси, а не компьютер.
    assert ("localhost", port) in proxy.targets


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


async def test_telegram_dead_proxy_gives_happ_hint(env):
    env.setenv("PROXY_URL", f"socks5://127.0.0.1:{_free_port()}")
    settings = load_settings(env_file=None)
    bot = Bot("42:TEST", session=AiohttpSession(proxy=settings.proxy.aiohttp_url))
    try:
        with pytest.raises(StartupError, match="Happ"):
            await _check_telegram(bot, settings)
    finally:
        await bot.session.close()


async def test_assemblyai_through_proxy(proxy, serve):
    app, state = make_assemblyai_app()
    port = await serve(app)
    transcriber = AssemblyAITranscriber(
        "KEY",
        base_url=f"http://localhost:{port}",
        poll_interval=0.01,
        proxy=Proxy.parse(proxy.url).aiohttp_url,
    )
    try:
        assert await transcriber.transcribe(b"OGG") == "Ивану подготовить отчёт"
    finally:
        await transcriber.close()
    assert ("localhost", port) in proxy.targets
    assert state["upload"] == b"OGG"


async def test_assemblyai_dead_proxy():
    transcriber = AssemblyAITranscriber(
        "KEY", base_url="https://api.assemblyai.com", proxy=f"socks5://127.0.0.1:{_free_port()}"
    )
    try:
        with pytest.raises(TranscriptionError, match="прокси"):
            await transcriber.check()
    finally:
        await transcriber.close()


async def test_supabase_through_proxy(proxy, serve):
    seen = {}
    app = web.Application()

    async def tasks(request):
        seen["query"] = dict(request.query)
        seen["apikey"] = request.headers.get("apikey")
        return web.json_response([])

    app.router.add_get("/rest/v1/tasks", tasks)
    port = await serve(app)
    http = httpx.AsyncClient(proxy=Proxy.parse(proxy.url).httpx_url)
    try:
        client = await acreate_client(
            f"http://localhost:{port}", "sb_secret_test", options=AsyncClientOptions(httpx_client=http)
        )
        await SupabaseTaskRepository(client).check()
    finally:
        await http.aclose()
    assert seen == {"query": {"select": "id", "limit": "1"}, "apikey": "sb_secret_test"}
    assert ("localhost", port) in proxy.targets


async def test_llm_through_proxy(proxy, serve):
    app = web.Application()

    async def completions(request):
        return web.json_response(
            {
                "id": "x",
                "object": "chat.completion",
                "created": 0,
                "model": "m",
                "choices": [
                    {
                        "index": 0,
                        "finish_reason": "stop",
                        "message": {
                            "role": "assistant",
                            "content": '{"tasks": [{"title": "A", "assignee": null, "deadline": null}]}',
                        },
                    }
                ],
            }
        )

    app.router.add_post("/api/v1/chat/completions", completions)
    port = await serve(app)
    http = openai.DefaultAsyncHttpxClient(proxy=Proxy.parse(proxy.url).httpx_url)
    client = openai.AsyncOpenAI(
        api_key="k", base_url=f"http://localhost:{port}/api/v1", http_client=http, max_retries=0
    )
    try:
        [draft] = await TaskParser(client, model="m").parse_tasks("x", date(2026, 11, 6))
    finally:
        await http.aclose()
    assert draft.title == "A"
    assert ("localhost", port) in proxy.targets


def test_httpx_client_falls_back_for_old_httpx():
    """httpx < 0.28 падает на socks5h с ValueError — тогда берём socks5."""
    calls = []

    def old_httpx_client(proxy, **kwargs):
        calls.append(proxy)
        if proxy.startswith("socks5h://"):
            raise ValueError(f"Unknown scheme for proxy URL URL('{proxy}')")
        return ("client", proxy, kwargs)

    client = Proxy.parse("socks5://127.0.0.1:10808").httpx_client(old_httpx_client, timeout=30.0)
    assert client == ("client", "socks5://127.0.0.1:10808", {"timeout": 30.0})
    assert calls == ["socks5h://127.0.0.1:10808", "socks5://127.0.0.1:10808"]


def test_httpx_client_http_proxy_error_is_not_swallowed():
    def broken(proxy, **kwargs):
        raise ValueError("boom")

    with pytest.raises(ValueError, match="boom"):
        Proxy.parse("http://127.0.0.1:10809").httpx_client(broken)


async def test_supabase_through_socks5_without_socks5h(serve):
    """Настоящий httpx-клиент через обычный socks5:// (как на старых версиях httpx)."""
    p = await Socks5Proxy().start()
    app = web.Application()
    async def tasks(request):
        return web.json_response([])

    app.router.add_get("/rest/v1/tasks", tasks)
    port = await serve(app)
    http = httpx.AsyncClient(proxy=Proxy.parse(p.url).httpx_url_fallback)
    try:
        client = await acreate_client(
            f"http://localhost:{port}", "sb_secret_test", options=AsyncClientOptions(httpx_client=http)
        )
        await SupabaseTaskRepository(client).check()
    finally:
        await http.aclose()
        await p.close()
    assert ("127.0.0.1", port) in p.targets or ("localhost", port) in p.targets
