"""Проверка ключа Supabase: что за ключ, от какого проекта, не перепутан ли он с другим."""

from __future__ import annotations

import base64
import json
from dataclasses import dataclass
from urllib.parse import urlsplit

WHERE = (
    "Project Settings → API Keys: вкладка «Legacy API Keys» → service_role → Reveal → Copy "
    "(начинается с eyJ…) или вкладка «API Keys» → Secret keys (начинается с sb_secret_…)"
)


class SupabaseKeyError(ValueError):
    pass


@dataclass(frozen=True)
class KeyInfo:
    kind: str  # «service_role» или «secret»
    ref: str | None  # проект, для которого выдан ключ (есть только у ключей eyJ…)
    fingerprint: str  # начало и конец ключа — чтобы сверить с тем, что в Supabase

    def describe(self) -> str:
        project = f", проект {self.ref}" if self.ref else ""
        return f"{self.kind}{project}, {self.fingerprint}"


def clean_key(raw: str) -> str:
    """Убирает кавычки, пробелы и переносы строк, которые часто попадают при копировании."""
    return "".join(raw.split()).strip("\"'«»")


def project_ref(url: str) -> str | None:
    host = urlsplit(url.strip()).hostname or ""
    return host.split(".")[0] if host.endswith(".supabase.co") else None


def fingerprint(key: str) -> str:
    if len(key) <= 16:
        return f"{len(key)} символов"
    return f"{key[:8]}…{key[-6:]} ({len(key)} символов)"


def _jwt_claims(key: str) -> dict | None:
    try:
        payload = key.split(".")[1]
        claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    except (IndexError, ValueError):
        return None
    return claims if isinstance(claims, dict) else None


def inspect_key(key: str, url: str) -> KeyInfo:
    """Проверяет SUPABASE_KEY. При явной ошибке — SupabaseKeyError с понятным текстом."""
    fp = fingerprint(key)
    if any(ch in key for ch in "•●*·…") or not key.isascii():
        raise SupabaseKeyError(
            "SUPABASE_KEY скопирован в скрытом виде (с точками/звёздочками) или с лишними символами. "
            f"В Supabase нажмите Reveal или значок глаза и кнопку Copy: {WHERE}"
        )
    public = "SUPABASE_KEY: это публичный ключ ({kind}). Боту нужен секретный ключ: " + WHERE

    if key.startswith("sb_publishable_"):
        raise SupabaseKeyError(public.format(kind="publishable"))
    if key.startswith("sb_secret_"):
        return KeyInfo("secret", None, fp)

    if key.startswith("eyJ"):
        claims = _jwt_claims(key) if key.count(".") == 2 else None
        if claims is None:
            raise SupabaseKeyError(
                f"SUPABASE_KEY повреждён ({fp}) — похоже, скопирован не целиком. "
                f"Скопируйте ключ заново кнопкой Copy: {WHERE}"
            )
        role = claims.get("role")
        if role == "anon":
            raise SupabaseKeyError(public.format(kind="anon"))
        if role != "service_role":
            raise SupabaseKeyError(f"SUPABASE_KEY: в ключе роль «{role}», а нужен service_role. {WHERE}")
        key_ref = claims.get("ref")
        url_ref = project_ref(url)
        if key_ref and url_ref and key_ref != url_ref:
            raise SupabaseKeyError(
                f"SUPABASE_KEY от другого проекта: ключ выдан для проекта {key_ref}, "
                f"а SUPABASE_URL указывает на проект {url_ref}. Возьмите URL и ключ в одном проекте"
            )
        return KeyInfo("service_role", key_ref, fp)

    raise SupabaseKeyError(
        f"SUPABASE_KEY не похож на ключ Supabase ({fp}). Секретный ключ начинается с eyJ… "
        "или sb_secret_…; возможно, скопирован JWT Secret, пароль базы или Project ID. " + WHERE
    )
