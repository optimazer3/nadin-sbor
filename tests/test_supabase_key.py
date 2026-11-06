import base64
import json

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer
from supabase import acreate_client

from bot.main import StartupError, _check_supabase
from bot.repository import SupabaseTaskRepository
from bot.supabase_key import SupabaseKeyError, clean_key, inspect_key
from tests.test_bot_flow import make_settings

URL = "https://whuygtahdgqgsnusoyql.supabase.co"


def jwt(**claims) -> str:
    def part(obj):
        return base64.urlsafe_b64encode(json.dumps(obj).encode()).decode().rstrip("=")

    return f"{part({'alg': 'HS256', 'typ': 'JWT'})}.{part(claims)}.c2lnbmF0dXJlLXNpZ25hdHVyZQ"


SERVICE = jwt(iss="supabase", ref="whuygtahdgqgsnusoyql", role="service_role", iat=1, exp=2)


def test_service_role_ok():
    info = inspect_key(SERVICE, URL)
    assert info.kind == "service_role" and info.ref == "whuygtahdgqgsnusoyql"
    assert SERVICE[:8] in info.describe() and SERVICE[-6:] in info.describe()


def test_secret_ok():
    assert inspect_key("sb_secret_abcdefghijklmnop123", URL).kind == "secret"


@pytest.mark.parametrize(
    ("key", "message"),
    [
        (jwt(ref="whuygtahdgqgsnusoyql", role="anon"), "публичный ключ"),
        ("sb_publishable_abc", "публичный ключ"),
        (jwt(ref="otherprojectref00000", role="service_role"), "от другого проекта"),
        (jwt(ref="whuygtahdgqgsnusoyql", role="authenticated"), "роль «authenticated»"),
        (SERVICE[:60], "скопирован не целиком"),
        ("super-secret-jwt-secret-value-from-settings", "не похож на ключ"),
        ("MyDbPassword123", "не похож на ключ"),
        ("sb_secret_abc••••••••••••", "скрытом виде"),
        ("sb_secret_аbc123", "скрытом виде"),  # кириллическая «а»
    ],
)
def test_bad_keys(key, message):
    with pytest.raises(SupabaseKeyError, match=message):
        inspect_key(key, URL)


def test_clean_key():
    assert clean_key(f'  "{SERVICE[:50]}\n{SERVICE[50:]}"  ') == SERVICE
    assert clean_key("«sb_secret_x»") == "sb_secret_x"


@pytest.mark.parametrize(
    ("status", "body", "message"),
    [
        (401, {"message": "Invalid API key", "hint": "Double check"}, "Supabase не принял ключ"),
        (401, {"message": "Legacy API keys are disabled"}, "sb_secret_"),
        (
            404,
            {"code": "PGRST205", "message": "Could not find the table 'public.tasks' in the schema cache"},
            "schema.sql",
        ),
    ],
)
async def test_startup_check_explains_errors(status, body, message):
    app = web.Application()

    async def tasks(request):
        return web.json_response(body, status=status)

    app.router.add_get("/rest/v1/tasks", tasks)
    server = TestServer(app)
    await server.start_server()
    try:
        url = f"http://localhost:{server.port}"
        settings = make_settings(supabase_url=url, supabase_key="sb_secret_abcdefghijklmnop123")
        repo = SupabaseTaskRepository(await acreate_client(url, settings.supabase_key))
        with pytest.raises(StartupError, match=message) as err:
            await _check_supabase(repo, settings)
        if status == 401 and "Invalid" in body["message"]:
            assert "sb_secre…nop123" in str(err.value)  # показали начало и конец ключа
    finally:
        await server.close()
