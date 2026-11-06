import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from bot.services.transcriber import AssemblyAITranscriber, TranscriptionError


def make_app(final_status="completed", text="Ивану подготовить отчёт"):
    state = {"polls": 0, "upload": None, "job": None, "auth": []}
    app = web.Application()

    async def upload(request):
        state["auth"].append(request.headers.get("authorization"))
        state["upload"] = await request.read()
        return web.json_response({"upload_url": "https://cdn.example/audio"})

    async def submit(request):
        state["auth"].append(request.headers.get("authorization"))
        state["job"] = await request.json()
        return web.json_response({"id": "tr_1", "status": "queued"})

    async def poll(request):
        state["polls"] += 1
        if state["polls"] < 2:
            return web.json_response({"id": "tr_1", "status": "processing"})
        if final_status == "error":
            return web.json_response({"id": "tr_1", "status": "error", "error": "bad audio"})
        return web.json_response({"id": "tr_1", "status": "completed", "text": text})

    app.router.add_post("/v2/upload", upload)
    app.router.add_post("/v2/transcript", submit)
    app.router.add_get("/v2/transcript/{id}", poll)
    return app, state


async def run(app, **kwargs):
    server = TestServer(app)
    await server.start_server()
    transcriber = AssemblyAITranscriber(
        "KEY", base_url=str(server.make_url("")).rstrip("/"), poll_interval=0.01, **kwargs
    )
    try:
        return await transcriber.transcribe(b"OGG")
    finally:
        await transcriber.close()
        await server.close()


async def test_transcribe_success():
    app, state = make_app()
    text = await run(app, speech_model="universal")
    assert text == "Ивану подготовить отчёт"
    assert state["upload"] == b"OGG"
    assert state["job"] == {
        "audio_url": "https://cdn.example/audio",
        "language_code": "ru",
        "speech_model": "universal",
    }
    assert state["auth"] == ["KEY", "KEY"]
    assert state["polls"] == 2


async def test_transcribe_error_status():
    app, _ = make_app(final_status="error")
    with pytest.raises(TranscriptionError, match="bad audio"):
        await run(app)


async def test_transcribe_empty_text():
    app, _ = make_app(text="")
    with pytest.raises(TranscriptionError):
        await run(app)


async def test_http_error():
    app = web.Application()

    async def upload(request):
        return web.json_response({"error": "Invalid API key"}, status=401)

    app.router.add_post("/v2/upload", upload)
    with pytest.raises(TranscriptionError, match="401"):
        await run(app)
