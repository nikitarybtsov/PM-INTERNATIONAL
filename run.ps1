<#
.SYNOPSIS
    Запуск проекта на Windows — замена Makefile, который написан под POSIX
    (.venv/bin, rm, find) и в PowerShell не работает.

.EXAMPLE
    .\run.ps1 install    # создать venv и поставить зависимости
    .\run.ps1 demo       # полный демо-раунд без единого ключа
    .\run.ps1 run        # веб-панель на http://localhost:8000

.NOTES
    Скрипт не запускает и не может запустить реальные сделки: live execution
    в проекте физически отсутствует.
#>
[CmdletBinding()]
param(
    [Parameter(Position = 0)]
    [ValidateSet('help', 'install', 'init', 'seed', 'demo', 'run', 'test',
                 'lint', 'migrate', 'export', 'status', 'doctor', 'clean')]
    [string]$Command = 'help',

    # Порт веб-панели (только для команды run)
    [int]$Port = 8000,

    # Слушать на всех интерфейсах. По умолчанию только localhost: панель без
    # PANEL_PASSWORD открыта всем, кто дотянется до порта.
    [switch]$Public
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$Root = $PSScriptRoot
$VenvPython = Join-Path $Root '.venv\Scripts\python.exe'

# Кириллица в выводе CLI не должна превращаться в мусор.
$env:PYTHONIOENCODING = 'utf-8'
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

function Find-BasePython {
    foreach ($candidate in @('py -3.12', 'python3.12', 'python')) {
        $parts = $candidate.Split(' ')
        $exe = Get-Command $parts[0] -ErrorAction SilentlyContinue
        if (-not $exe) { continue }
        try {
            $version = & $parts[0] $parts[1..($parts.Length - 1)] --version 2>&1
        } catch { continue }
        if ($version -match '3\.1[2-9]') { return $candidate }
    }
    throw "Не найден Python 3.12+. Установите с https://python.org и повторите."
}

function Assert-Venv {
    if (-not (Test-Path $VenvPython)) {
        throw "Окружение не создано. Сначала выполните: .\run.ps1 install"
    }
}

function Invoke-Venv {
    param([string[]]$Arguments)
    Assert-Venv
    Push-Location $Root
    try {
        & $VenvPython @Arguments
        if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
    } finally { Pop-Location }
}

switch ($Command) {
    'help' {
        @'
Команды (Windows):

  .\run.ps1 install    создать venv и поставить зависимости
  .\run.ps1 init       создать БД и трёх участников по $1000
  .\run.ps1 seed       загрузить рынки из активного провайдера
  .\run.ps1 demo       полный демо-раунд без API-ключей
  .\run.ps1 run        веб-панель на http://localhost:8000
  .\run.ps1 test       полный test suite
  .\run.ps1 lint       ruff
  .\run.ps1 migrate    применить миграции Alembic
  .\run.ps1 export     сохранить экспорт для монтажа
  .\run.ps1 status     краткий scoreboard
  .\run.ps1 doctor     проверить конфигурацию
  .\run.ps1 clean      удалить БД, экспорты и кэши

Первый запуск с нуля:
  .\run.ps1 install
  Copy-Item .env.example .env
  .\run.ps1 demo

Ключи не нужны: без них Codex и Claude работают заглушками, а рынки берутся
из mock-набора. Живые рынки Polymarket — MARKET_DATA_PROVIDER=polymarket в .env
(ключи для чтения тоже не нужны).
'@ | Write-Host
    }

    'install' {
        if (-not (Test-Path $VenvPython)) {
            $base = Find-BasePython
            Write-Host "Создаю venv с помощью: $base"
            $parts = $base.Split(' ')
            & $parts[0] $parts[1..($parts.Length - 1)] -m venv (Join-Path $Root '.venv')
        }
        & $VenvPython -m pip install --quiet --upgrade pip
        & $VenvPython -m pip install --quiet -r (Join-Path $Root 'requirements-dev.txt')
        Write-Host "Готово. Дальше: Copy-Item .env.example .env; .\run.ps1 demo"
    }

    'init'    { Invoke-Venv @('-m', 'app.cli', 'init') }
    'seed'    { Invoke-Venv @('-m', 'app.cli', 'seed') }
    'demo'    { Invoke-Venv @('-m', 'app.cli', 'demo') }
    'export'  { Invoke-Venv @('-m', 'app.cli', 'export') }
    'status'  { Invoke-Venv @('-m', 'app.cli', 'status') }
    'test'    { Invoke-Venv @('-m', 'pytest') }
    'lint'    { Invoke-Venv @('-m', 'ruff', 'check', 'app', 'tests') }
    'migrate' { Invoke-Venv @('-m', 'alembic', 'upgrade', 'head') }

    'run' {
        $bindHost = if ($Public) { '0.0.0.0' } else { '127.0.0.1' }
        if ($Public) {
            Write-Warning ("Панель слушает на всех интерфейсах. Без PANEL_PASSWORD " +
                           "её откроет любой, кто дотянется до порта $Port.")
        }
        Write-Host "Панель: http://localhost:$Port/  (Ctrl+C — остановить)"
        Invoke-Venv @('-m', 'uvicorn', 'app.main:app', '--host', $bindHost,
                      '--port', "$Port", '--reload')
    }

    'doctor' {
        Assert-Venv
        Push-Location $Root
        try {
            & $VenvPython -c @"
import json, sys
sys.path.insert(0, '.')
from app.config import get_settings
s = get_settings()
print(json.dumps({
    'market_data_provider': s.market_data_provider,
    'codex_transport': s.codex_transport,
    'openai_key': bool(s.has_openai()),
    'anthropic_key': bool(s.has_anthropic()),
    'telegram': bool(s.has_telegram()),
    'panel_auth': bool(s.panel_auth_enabled()),
    'live_trading_enabled': s.live_trading_enabled,
}, indent=2, ensure_ascii=False))
"@
        } finally { Pop-Location }
        Write-Host "`nПодробнее — GET /api/doctor при запущенной панели."
    }

    'clean' {
        foreach ($pattern in @('data\*.db', 'exports\*')) {
            Get-ChildItem (Join-Path $Root $pattern) -ErrorAction SilentlyContinue |
                Where-Object { $_.Name -ne '.gitkeep' } |
                Remove-Item -Recurse -Force
        }
        foreach ($dir in @('.pytest_cache', '.ruff_cache')) {
            Remove-Item (Join-Path $Root $dir) -Recurse -Force -ErrorAction SilentlyContinue
        }
        Get-ChildItem $Root -Recurse -Directory -Filter '__pycache__' -ErrorAction SilentlyContinue |
            Remove-Item -Recurse -Force -ErrorAction SilentlyContinue
        Write-Host "Очищено: БД, экспорты, кэши."
    }
}
