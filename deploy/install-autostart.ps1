<#
.SYNOPSIS
    Ставит туннель к панели в автозагрузку Windows.

    После этого панель на http://localhost:8000/ доступна сразу после входа
    в систему — никаких команд запускать не нужно.

.EXAMPLE
    .\deploy\install-autostart.ps1            # установить и сразу поднять
    .\deploy\install-autostart.ps1 -Remove    # убрать из автозагрузки

.NOTES
    Туннель нужен потому, что панель на сервере слушает только localhost:
    без PANEL_PASSWORD выставлять её наружу нельзя.
#>
[CmdletBinding()]
param([switch]$Remove)

$ErrorActionPreference = 'Stop'

$Root = Split-Path -Parent $PSScriptRoot
$PythonW = Join-Path $Root '.venv\Scripts\pythonw.exe'
$Tunnel = Join-Path $Root 'deploy\tunnel.py'
# Имя латиницей: кириллица в путях автозагрузки иногда ломается кодировкой.
$LinkPath = Join-Path ([Environment]::GetFolderPath('Startup')) 'pm-international-tunnel.lnk'

if ($Remove) {
    if (Test-Path $LinkPath) {
        Remove-Item $LinkPath -Force
        Write-Host "Автозапуск отключён. Работающий туннель не тронут."
    } else {
        Write-Host "Автозапуска и не было."
    }
    return
}

if (-not (Test-Path $PythonW)) {
    throw "Не найден $PythonW. Сначала выполните: .\run.ps1 install"
}
if (-not (Test-Path (Join-Path $Root '.env'))) {
    throw "Нет .env с DEPLOY_HOST и SERVER_PASSWORD — туннелю неоткуда взять доступ."
}

$shell = New-Object -ComObject WScript.Shell
$link = $shell.CreateShortcut($LinkPath)
# pythonw вместо python — иначе при каждом входе мелькает окно консоли
$link.TargetPath = $PythonW
$link.Arguments = "`"$Tunnel`""
$link.WorkingDirectory = $Root
$link.Description = 'Туннель к панели PM-INTERNATIONAL'
$link.WindowStyle = 7   # свёрнуто
$link.Save()

Write-Host "Ярлык создан: $LinkPath"

# Поднимаем сразу, чтобы не ждать перезагрузки
$running = Get-CimInstance Win32_Process -Filter "Name='pythonw.exe'" -ErrorAction SilentlyContinue |
    Where-Object { $_.CommandLine -like '*tunnel.py*' }
if ($running) {
    Write-Host "Туннель уже работает (PID $($running.ProcessId))."
} else {
    Start-Process -FilePath $PythonW -ArgumentList "`"$Tunnel`"" -WorkingDirectory $Root -WindowStyle Hidden
    Start-Sleep -Seconds 4
    try {
        $code = (Invoke-WebRequest 'http://localhost:8000/health' -TimeoutSec 10 -UseBasicParsing).StatusCode
        Write-Host "Туннель поднят, панель отвечает (HTTP $code)."
    } catch {
        Write-Warning "Туннель запущен, но панель пока не ответила. Проверьте через минуту."
    }
}

Write-Host ""
Write-Host "Панель: http://localhost:8000/"
Write-Host "Отключить автозапуск: .\deploy\install-autostart.ps1 -Remove"
