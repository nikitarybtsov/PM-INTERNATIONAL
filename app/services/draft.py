"""Пики героев из живой трансляции Valve.

Источник — публичный `/live` OpenDota, который проксирует спектейт-поток Valve.
Ключ не нужен, задержка около 10 секунд: за это время рынок обычно не успевает
переставиться, поэтому данные пригодны для ставки сразу после драфта.

Матч ищется по названиям команд из рынка Polymarket. Совпадение нестрогое:
у Valve и Polymarket названия расходятся («Nigma Galaxy » с пробелом,
«TEAM VISION» капсом), поэтому сравниваются нормализованные подстроки.

У одной серии в эфире бывает несколько игр одновременно: доигрывается
предыдущая карта и уже началась следующая. Берётся самая молодая по времени —
именно её драфт только что закончился.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field

import httpx

logger = logging.getLogger(__name__)

OPENDOTA = "https://api.opendota.com/api"
_HERO_CACHE: dict[int, str] = {}


@dataclass(slots=True)
class LiveDraft:
    """Составы одной карты."""

    radiant_team: str
    dire_team: str
    radiant_picks: list[str] = field(default_factory=list)
    dire_picks: list[str] = field(default_factory=list)
    game_time: int = 0
    delay: int = 0
    league_id: int = 0

    @property
    def is_complete(self) -> bool:
        return len(self.radiant_picks) == 5 and len(self.dire_picks) == 5


def _norm(name: str | None) -> str:
    """«Nigma Galaxy » и «nigma galaxy» — одна команда."""
    return re.sub(r"[^a-z0-9]", "", (name or "").lower())


def _heroes(client: httpx.Client) -> dict[int, str]:
    global _HERO_CACHE
    if _HERO_CACHE:
        return _HERO_CACHE
    response = client.get(f"{OPENDOTA}/heroes")
    response.raise_for_status()
    _HERO_CACHE = {
        int(h["id"]): str(h.get("localized_name") or h.get("name") or h["id"])
        for h in response.json()
        if h.get("id")
    }
    return _HERO_CACHE


def fetch_draft(team_a: str, team_b: str, *, timeout: float = 20.0) -> LiveDraft | None:
    """Пики матча между двумя командами. None — если матча нет в эфире."""
    wanted = {_norm(team_a), _norm(team_b)}
    if "" in wanted or len(wanted) < 2:
        return None

    try:
        with httpx.Client(timeout=timeout, follow_redirects=True) as client:
            live = client.get(f"{OPENDOTA}/live")
            live.raise_for_status()
            rows = live.json()
            heroes = _heroes(client)
    except Exception as exc:  # noqa: BLE001 — источник необязательный
        logger.warning("пики не получены: %s", exc)
        return None

    candidates: list[LiveDraft] = []
    for row in rows if isinstance(rows, list) else []:
        radiant = row.get("team_name_radiant")
        dire = row.get("team_name_dire")
        if {_norm(radiant), _norm(dire)} != wanted:
            continue

        players = row.get("players") or []
        draft = LiveDraft(
            radiant_team=str(radiant or ""),
            dire_team=str(dire or ""),
            radiant_picks=[
                heroes.get(int(p["hero_id"]), str(p["hero_id"]))
                for p in players
                if p.get("hero_id") and p.get("team") == 0
            ],
            dire_picks=[
                heroes.get(int(p["hero_id"]), str(p["hero_id"]))
                for p in players
                if p.get("hero_id") and p.get("team") == 1
            ],
            game_time=int(row.get("game_time") or 0),
            delay=int(row.get("delay") or 0),
            league_id=int(row.get("league_id") or 0),
        )
        if draft.is_complete:
            candidates.append(draft)

    if not candidates:
        return None
    # Самая молодая игра — её драфт закончился только что.
    return min(candidates, key=lambda d: d.game_time)
