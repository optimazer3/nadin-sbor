"""Имена исполнителей: приводим к написанию из TEAM_MEMBERS, чтобы один человек не «двоился»."""

from __future__ import annotations

from collections.abc import Sequence


def _words(name: str) -> list[str]:
    return name.lower().replace("ё", "е").replace(",", " ").split()


def match_team(name: str | None, team: Sequence[str]) -> str | None:
    """«иван» → «Иван»; «Иван» или «Петров» → «Иван Петров», если в команде он такой один."""
    if not name or not name.strip():
        return None
    clean = " ".join(name.split())
    words = _words(clean)
    for member in team:
        if _words(member) == words:
            return member
    candidates = [m for m in team if words and all(w in _words(m) for w in words)]
    if len(candidates) == 1:
        return candidates[0]
    return clean


def person_key(name: str | None, team: Sequence[str]) -> str:
    """Ключ группировки: одно и то же ФИО в разном написании даёт один ключ."""
    matched = match_team(name, team)
    return " ".join(_words(matched)) if matched else ""
