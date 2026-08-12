"""Общая механика участников, работающих через локальный CLI.

Оператор может запускать Codex и Claude не по API-ключу, а через уже
залогиненные CLI с подписками. Транспорт у них одинаковый: промпт уходит в
stdin, ответ читается из stdout — различаются только команда, таймаут и метка
модели.

Общий базовый класс здесь не ради экономии строк: требование эксперимента —
чтобы участники получали ровно одинаковые условия. Пока код запуска общий,
разойтись они не могут.

Ограничения, о которых надо знать:
  * CLI должен быть установлен и заранее авторизован на машине
    (`codex login` / `claude login` выполняются один раз руками, интерактивно);
  * CLI не гарантирует чистый JSON — вокруг ответа бывает служебный вывод,
    поэтому JSON извлекается из текста, а не парсится целиком;
  * точный набор флагов зависит от версии CLI и задаётся переменной окружения;
  * ретраи и таймаут работают так же, как у HTTP-адаптеров.
"""

from __future__ import annotations

import logging
import shlex
import subprocess

from app.adapters.participants.llm_base import LLMParticipantAdapter

logger = logging.getLogger(__name__)

PROBE_TIMEOUT_SECONDS = 20


class CliParticipantError(RuntimeError):
    """CLI не запустился, вернул ошибку или не уложился в таймаут."""


class CliParticipantAdapter(LLMParticipantAdapter):
    """Участник, чьи решения приходят от локального бинаря."""

    #: как называть инструмент в сообщениях об ошибках
    cli_display_name: str = "CLI"
    #: исключение, которым сообщать о проблемах запуска
    error_class: type[CliParticipantError] = CliParticipantError
    #: имя переменной окружения с командой — для текста ошибки
    command_setting_name: str = "CLI_COMMAND"

    # ---- переопределяется в наследниках ------------------------------------
    @property
    def _command(self) -> str:
        raise NotImplementedError

    @property
    def _timeout(self) -> float:
        raise NotImplementedError

    # ---- общая механика ----------------------------------------------------
    def _resolve_mock(self, mock: bool | None) -> bool:
        if mock is not None:
            return mock
        # CLI не требует ключа: mock включается только явно.
        return False

    def _build_argv(self) -> list[str]:
        argv = shlex.split(self._command)
        if not argv:
            raise self.error_class(f"{self.command_setting_name} пуст")
        return argv

    def _call_model(self, system_prompt: str, user_prompt: str) -> tuple[str, str]:
        argv = self._build_argv()
        payload = f"{system_prompt}\n\n{user_prompt}\n"
        timeout = self._timeout

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
            raise self.error_class(
                f"бинарь не найден: {argv[0]}. Установите {self.cli_display_name} "
                "и проверьте PATH"
            ) from exc
        except subprocess.TimeoutExpired as exc:
            raise self.error_class(
                f"{self.cli_display_name} не ответил за {timeout:.0f}с"
            ) from exc

        if completed.returncode != 0:
            stderr = (completed.stderr or "").strip()[:500]
            raise self.error_class(
                f"{self.cli_display_name} вернул код {completed.returncode}: {stderr}"
            )

        output = (completed.stdout or "").strip()
        if not output:
            stderr = (completed.stderr or "").strip()[:500]
            raise self.error_class(
                f"{self.cli_display_name} вернул пустой stdout (stderr: {stderr})"
            )

        # Версию модели CLI не сообщает — фиксируем метку из конфигурации.
        return output, self.model_name

    @classmethod
    def _probe_command(cls) -> str:
        raise NotImplementedError

    @classmethod
    def probe(cls) -> dict:
        """Проверка готовности CLI. Вызывается из /health и команды doctor."""
        argv = shlex.split(cls._probe_command())
        binary = argv[0] if argv else ""
        try:
            completed = subprocess.run(  # noqa: S603
                [binary, "--version"],
                capture_output=True,
                text=True,
                timeout=PROBE_TIMEOUT_SECONDS,
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
