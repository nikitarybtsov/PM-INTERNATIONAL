"""Codex через локальный CLI вместо HTTP API.

Выбор оператора: расход идёт по подписке ChatGPT, а не по API-ключу.
Вся механика запуска — в `cli_base.py`, общая с Claude CLI: так участники
гарантированно работают в одинаковых условиях.

Требуется установленный и заранее авторизованный `codex` (`codex login`
выполняется один раз руками). Набор флагов зависит от версии CLI и задаётся
через CODEX_CLI_COMMAND.
"""

from __future__ import annotations

from app.adapters.participants.cli_base import CliParticipantAdapter, CliParticipantError
from app.constants import ParticipantKey


class CodexCliError(CliParticipantError):
    """Codex CLI не запустился, вернул ошибку или не уложился в таймаут."""


class CodexCliAdapter(CliParticipantAdapter):
    """Участник Codex, работающий через локальный бинарь `codex`."""

    key = ParticipantKey.CODEX.value
    provider = "codex-cli"
    cli_display_name = "Codex CLI"
    error_class = CodexCliError
    command_setting_name = "CODEX_CLI_COMMAND"

    @property
    def model_name(self) -> str:
        return self._settings.codex_cli_model_label

    @property
    def _command(self) -> str:
        return self._settings.codex_cli_command

    @property
    def _timeout(self) -> float:
        return self._settings.codex_cli_timeout_seconds

    @classmethod
    def _probe_command(cls) -> str:
        from app.config import get_settings

        return get_settings().codex_cli_command
