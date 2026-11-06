"""Распознавание голосовых через AssemblyAI (REST API v2)."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import aiohttp

log = logging.getLogger(__name__)


class TranscriptionError(RuntimeError):
    pass


class AssemblyAITranscriber:
    """Загружает аудио в AssemblyAI, запускает распознавание и ждёт результат.

    Поток: POST /v2/upload -> POST /v2/transcript -> опрос GET /v2/transcript/{id}.
    Голосовые Telegram (OGG/Opus) AssemblyAI принимает как есть.
    """

    def __init__(
        self,
        api_key: str,
        base_url: str = "https://api.assemblyai.com",
        language_code: str = "ru",
        speech_model: str | None = None,
        poll_interval: float = 1.5,
        timeout: float = 300.0,
    ) -> None:
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._language_code = language_code
        self._speech_model = speech_model
        self._poll_interval = poll_interval
        self._timeout = timeout
        self._session: aiohttp.ClientSession | None = None

    def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                headers={"authorization": self._api_key},
                timeout=aiohttp.ClientTimeout(total=60),
                trust_env=True,  # учитывать HTTPS_PROXY, если он задан
            )
        return self._session

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()

    async def _request(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        session = self._get_session()
        url = f"{self._base_url}{path}"
        try:
            async with session.request(method, url, **kwargs) as resp:
                payload = await resp.json(content_type=None)
                if resp.status >= 400:
                    message = payload.get("error") if isinstance(payload, dict) else payload
                    raise TranscriptionError(f"AssemblyAI {resp.status}: {message}")
                return payload
        except aiohttp.ClientError as exc:
            raise TranscriptionError(f"Сетевая ошибка AssemblyAI: {exc}") from exc

    async def transcribe(self, audio: bytes) -> str:
        if not audio:
            raise TranscriptionError("Пустой аудиофайл")

        upload = await self._request(
            "POST",
            "/v2/upload",
            data=audio,
            headers={"content-type": "application/octet-stream"},
        )
        body: dict[str, Any] = {
            "audio_url": upload["upload_url"],
            "language_code": self._language_code,
        }
        if self._speech_model:
            body["speech_model"] = self._speech_model

        job = await self._request("POST", "/v2/transcript", json=body)
        transcript_id = job["id"]
        log.info("AssemblyAI: распознавание %s запущено", transcript_id)

        loop = asyncio.get_running_loop()
        deadline = loop.time() + self._timeout
        while True:
            result = await self._request("GET", f"/v2/transcript/{transcript_id}")
            status = result.get("status")
            if status == "completed":
                text = (result.get("text") or "").strip()
                if not text:
                    raise TranscriptionError("Не удалось разобрать речь — запись пустая или неразборчивая")
                return text
            if status == "error":
                raise TranscriptionError(f"AssemblyAI: {result.get('error') or 'ошибка распознавания'}")
            if loop.time() > deadline:
                raise TranscriptionError("AssemblyAI: превышено время ожидания распознавания")
            await asyncio.sleep(self._poll_interval)
