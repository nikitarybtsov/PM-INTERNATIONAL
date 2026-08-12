"""Codex через локальный CLI вместо HTTP API.

Выбор оператора: расход идёт по подписке ChatGPT, а не по API-ключу.
Промпт передаётся в stdin, ответ читается из stdout.

Ограничения, о которых надо знать:
  * требуется установленный и заранее авторизованный `codex` на сервере
    (`codex login` выполняется один раз руками, интерактивно);
  * CLI не гарантирует чистый JSON — вокруг ответа бывает служебный вывод,
    поэтому JSON извлекается из текста, а не парсится целиком;
  * точный набор флагов зависит от версии CLI и задаётся через
    CODEX_CLI_COMMAND — проверьте его на сервере командой из README;
  * ретраи и таймаут работают так же, как у HTTP-адаптера.
"""

from __future__ import annotations

import logging
import shlex
import subprocess

from app.adapters.participants.llm_base import LLMParticipantAdapter
from app.constants import ParticipantKey

logger = logging.getLogger(__name__)


class CodexCliError(RuntimeError):
    """CLI не запустился, вернул ошибку или не уложился в таймаут."""


class CodexCliAdapter(LLMParticipantAdapter):
    """Участник Codex, работающий через локальный бинарь `codex`."""

    key = ParticipantKey.CODEX.value
    provider = "codex-cli"

    def _resolve_mock(self, mock: bool | None) -> bool:
        if mock is not None:
            return mock
        # CLI не требует ключа: mock включается только явно.
        return False

    @property
    def model_name(self) -> str:
        return self._settings.codex_cli_model_label

    def _build_argv(self) -> list[str]:
        argv = shlex.split(self._settings.codex_cli_command)
        if not argv:
            raise CodexCliError("CODEX_CLI_COMMAND пуст")
        return argv

    def _call_model(self, system_prompt: str, user_prompt: str) -> tuple[str, str]:
        argv = self._build_argv()
        payload = f"{system_prompt}\n\n{user_prompt}\n"
        timeout = self._settings.codex_cli_timeout_seconds

        try:
            completed = subprocess.run(  # noqa: S603 — команда задаётся оператором в .env
                argv,
                input=payload,
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
            )
        except FileNotFoundError as exc:
            raise CodexCliError(
                f"бинарь не найден: {argv[0]}. Установите Codex CLI и проверьте PATH"
            ) from exc
        except subprocess.TimeoutExpired as exc:
            raise CodexCliError(f"Codex CLI не ответил за {timeout:.0f}с") from exc

        if completed.returncode != 0:
            stderr = (completed.stderr or "").strip()[:500]
            raise CodexCliError(f"Codex CLI вернул код {completed.returncode}: {stderr}")

        output = (completed.stdout or "").strip()
        if not output:
            stderr = (completed.stderr or "").strip()[:500]
            raise CodexCliError(f"Codex CLI вернул пустой stdout (stderr: {stderr})")

        # Версию модели CLI не сообщает — фиксируем метку из конфигурации.
        return output, self._settings.codex_cli_model_label

    @staticmethod
    def probe() -> dict:
        """Проверка готовности CLI. Вызывается из /health и CLI-команды doctor."""
        from app.config import get_settings

        settings = get_settings()
        argv = shlex.split(settings.codex_cli_command)
        binary = argv[0] if argv else ""
        try:
            completed = subprocess.run(  # noqa: S603
                [binary, "--version"],
                capture_output=True,
                text=True,
                timeout=20,
                check=False,
            )
        except FileNotFoundError:
            return {"available": False, "error": f"бинарь {binary!r} не найден в PATH"}
        except subprocess.TimeoutExpired:
            return {"available": False, "error": f"{binary} --version завис"}
        if completed.returncode != 0:
            return {
                "available": False,
                "error": (completed.stderr or completed.stdout or "").strip()[:300],
            }
        return {"available": True, "version": (completed.stdout or "").strip()[:120]}
