"""Участник Claude — решения через Anthropic API.

Правила ровно те же, что у Codex: тот же snapshot, тот же промпт, та же схема
TradeDecision, те же ретраи и таймауты. Никаких дополнительных данных.
"""

from __future__ import annotations

import httpx

from app.adapters.participants.llm_base import LLMParticipantAdapter
from app.constants import ParticipantKey

ANTHROPIC_VERSION = "2023-06-01"


class ClaudeAdapter(LLMParticipantAdapter):
    key = ParticipantKey.CLAUDE.value
    provider = "anthropic"

    def _resolve_mock(self, mock: bool | None) -> bool:
        if mock is not None:
            return mock
        return not self._settings.has_anthropic()

    @property
    def model_name(self) -> str:
        return self._settings.anthropic_model

    def _call_model(self, system_prompt: str, user_prompt: str) -> tuple[str, str]:
        settings = self._settings
        url = f"{settings.anthropic_base_url.rstrip('/')}/v1/messages"
        payload = {
            "model": settings.anthropic_model,
            "max_tokens": 2000,
            "system": system_prompt,
            "messages": [
                {"role": "user", "content": user_prompt},
                # префилл повышает шанс чистого JSON без обёртки
                {"role": "assistant", "content": "{"},
            ],
        }
        headers = {
            "x-api-key": settings.anthropic_api_key or "",
            "anthropic-version": ANTHROPIC_VERSION,
            "Content-Type": "application/json",
        }
        with httpx.Client(timeout=self.timeout) as client:
            resp = client.post(url, json=payload, headers=headers)
            if resp.status_code >= 400:
                raise RuntimeError(f"Anthropic HTTP {resp.status_code}: {resp.text[:300]}")
            data = resp.json()
        blocks = data.get("content") or []
        text = "".join(b.get("text", "") for b in blocks if isinstance(b, dict))
        if not text.strip():
            raise RuntimeError("пустой ответ Anthropic")
        if not text.lstrip().startswith("{"):
            text = "{" + text  # компенсируем префилл
        model_version = str(data.get("model") or settings.anthropic_model)
        return text, model_version
