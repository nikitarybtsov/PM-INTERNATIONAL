"""Участник Codex — решения через OpenAI API.

Ключ читается только из переменной окружения OPENAI_API_KEY и никуда не
сохраняется. Без ключа адаптер автоматически работает в mock-режиме.
Транспорт — httpx, чтобы проект не зависел от установки SDK.
"""

from __future__ import annotations

import httpx

from app.adapters.participants.llm_base import LLMParticipantAdapter
from app.constants import ParticipantKey


class CodexAdapter(LLMParticipantAdapter):
    key = ParticipantKey.CODEX.value
    provider = "openai"

    def _resolve_mock(self, mock: bool | None) -> bool:
        if mock is not None:
            return mock
        return not self._settings.has_openai()

    @property
    def model_name(self) -> str:
        return self._settings.openai_model

    def _call_model(self, system_prompt: str, user_prompt: str) -> tuple[str, str]:
        settings = self._settings
        url = f"{settings.openai_base_url.rstrip('/')}/chat/completions"
        payload = {
            "model": settings.openai_model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "response_format": {"type": "json_object"},
        }
        headers = {
            "Authorization": f"Bearer {settings.openai_api_key}",
            "Content-Type": "application/json",
        }
        with httpx.Client(timeout=self.timeout) as client:
            resp = client.post(url, json=payload, headers=headers)
            if resp.status_code >= 400:
                # тело ответа может содержать эхо запроса — обрезаем и не логируем ключ
                raise RuntimeError(f"OpenAI HTTP {resp.status_code}: {resp.text[:300]}")
            data = resp.json()
        try:
            content = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise RuntimeError("неожиданная структура ответа OpenAI") from exc
        model_version = str(data.get("model") or settings.openai_model)
        return content, model_version
