"""Claude через локальный CLI вместо Anthropic API.

Выбор оператора: расход идёт по подписке, а не по API-ключу. Вся механика
запуска — в `cli_base.py`, общая с Codex CLI: одинаковый промпт, одинаковые
ретраи, одинаковый таймаут. Никаких дополнительных данных Claude не получает.

Требуется установленный и заранее авторизованный `claude` (вход выполняется
один раз руками, интерактивно). Набор флагов зависит от версии CLI и задаётся
через CLAUDE_CLI_COMMAND; по умолчанию используется неинтерактивный режим
`claude -p`, который читает запрос из stdin и печатает ответ в stdout.
"""

from __future__ import annotations

from app.adapters.participants.cli_base import CliParticipantAdapter, CliParticipantError
from app.constants import ParticipantKey


class ClaudeCliError(CliParticipantError):
    """Claude CLI не запустился, вернул ошибку или не уложился в таймаут."""


class ClaudeCliAdapter(CliParticipantAdapter):
    """Участник Claude, работающий через локальный бинарь `claude`."""

    key = ParticipantKey.CLAUDE.value
    provider = "claude-cli"
    cli_display_name = "Claude CLI"
    error_class = ClaudeCliError
    command_setting_name = "CLAUDE_CLI_COMMAND"

    @property
    def model_name(self) -> str:
        return self._settings.claude_cli_model_label

    @property
    def _command(self) -> str:
        return self._settings.claude_cli_command

    @property
    def _timeout(self) -> float:
        return self._settings.claude_cli_timeout_seconds

    @classmethod
    def _probe_command(cls) -> str:
        from app.config import get_settings

        return get_settings().claude_cli_command
