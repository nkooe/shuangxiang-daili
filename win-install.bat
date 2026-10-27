@echo off
setlocal EnableExtensions

rem === Linux broker settings ===
set "SERVER_HOST=YOUR_LINUX_PUBLIC_IP"
set "AGENT_PORT=27500"
set "AGENT_TOKEN="
set "TLS_FINGERPRINT="
set "POOL_SIZE=256"
set "CONNECT_TIMEOUT=10"
set "RECONNECT_DELAY=3"
set "RECONNECT_JITTER=5"
set "RELAY_IDLE_TIMEOUT=300"
set "KEEPALIVE_IDLE=60"
set "KEEPALIVE_INTERVAL=15"
set "KEEPALIVE_COUNT=4"

rem Optional machine-specific configuration is excluded from Git.
if exist "%~dp0win-config.bat" call "%~dp0win-config.bat"

rem Windows runs only the reverse agent. It does not listen as a local proxy.
set "TASK_NAME=PairProxyAgent"
set "OLD_TASK_NAME=PairProxyClient"
set "BASE=%ProgramData%\PairProxy"
set "SOURCE=%~dp0reverse_pair_proxy.py"

if "%SERVER_HOST%"=="YOUR_LINUX_PUBLIC_IP" (
  echo Edit SERVER_HOST in this file first.
  exit /b 1
)
if "%AGENT_TOKEN%"=="" (
  echo AGENT_TOKEN is empty. Copy Agent token from Linux install output.
  exit /b 1
)
if "%TLS_FINGERPRINT%"=="" (
  echo TLS_FINGERPRINT is empty. Copy Agent TLS fingerprint from Linux output.
  exit /b 1
)
if not exist "%SOURCE%" (
  echo reverse_pair_proxy.py not found: "%SOURCE%"
  exit /b 1
)

mkdir "%BASE%" >nul 2>nul
copy /Y "%SOURCE%" "%BASE%\reverse_pair_proxy.py" >nul

rem Remove the previous local-proxy version if it was installed earlier.
schtasks /End /TN "%OLD_TASK_NAME%" >nul 2>nul
schtasks /Delete /TN "%OLD_TASK_NAME%" /F >nul 2>nul
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -like '*pair_proxy.py*client*' -or $_.CommandLine -like '*run_pairproxy.cmd*' } | ForEach-Object { try { Stop-Process -Id $_.ProcessId -Force -ErrorAction Stop } catch {} }" >nul 2>nul
reg add "HKCU\Software\Microsoft\Windows\CurrentVersion\Internet Settings" /v ProxyEnable /t REG_DWORD /d 0 /f >nul 2>nul
reg delete "HKCU\Software\Microsoft\Windows\CurrentVersion\Internet Settings" /v ProxyServer /f >nul 2>nul
netsh winhttp reset proxy >nul 2>nul

set "PYTHONW_PATH="
for /f "delims=" %%p in ('where pythonw 2^>nul') do if not defined PYTHONW_PATH set "PYTHONW_PATH=%%p"
if not defined PYTHONW_PATH (
  for /f "delims=" %%p in ('py -3 -c "import sys; print(sys.executable)" 2^>nul') do if not defined PYTHON_EXE set "PYTHON_EXE=%%p"
  if defined PYTHON_EXE (
    for %%p in ("%PYTHON_EXE%") do set "PYTHONW_PATH=%%~dppythonw.exe"
  )
)
if not defined PYTHONW_PATH (
  echo Python 3 pythonw.exe not found. Install Python 3, then run this BAT again.
  exit /b 1
)
if not exist "%PYTHONW_PATH%" (
  echo pythonw.exe not found: "%PYTHONW_PATH%"
  exit /b 1
)

(
  echo $ErrorActionPreference = 'Stop'
  echo $pythonw = '%PYTHONW_PATH%'
  echo $arguments = @(
  echo   'reverse_pair_proxy.py'
  echo   'agent'
  echo   '--server'
  echo   '%SERVER_HOST%'
  echo   '--agent-port'
  echo   '%AGENT_PORT%'
  echo   '--agent-token'
  echo   '%AGENT_TOKEN%'
  echo   '--pool-size'
  echo   '%POOL_SIZE%'
  echo   '--connect-timeout'
  echo   '%CONNECT_TIMEOUT%'
  echo   '--reconnect-delay'
  echo   '%RECONNECT_DELAY%'
  echo   '--reconnect-jitter'
  echo   '%RECONNECT_JITTER%'
  echo   '--relay-idle-timeout'
  echo   '%RELAY_IDLE_TIMEOUT%'
  echo   '--keepalive-idle'
  echo   '%KEEPALIVE_IDLE%'
  echo   '--keepalive-interval'
  echo   '%KEEPALIVE_INTERVAL%'
  echo   '--keepalive-count'
  echo   '%KEEPALIVE_COUNT%'
  echo   '--agent-tls'
  echo   '--tls-fingerprint'
  echo   '%TLS_FINGERPRINT%'
  echo   '--log-level'
  echo   'INFO'
  echo   '--log-file'
  echo   '%BASE%\pairproxy-agent.log'
  echo ^)
  echo while ($true^) {
  echo   $process = Start-Process -FilePath $pythonw -ArgumentList $arguments -WorkingDirectory '%BASE%' -WindowStyle Hidden -Wait -PassThru
  echo   Start-Sleep -Seconds 3
  echo }
) > "%BASE%\run_pairproxy-agent.ps1"

schtasks /End /TN "%TASK_NAME%" >nul 2>nul
schtasks /Delete /TN "%TASK_NAME%" /F >nul 2>nul
schtasks /Create /TN "%TASK_NAME%" /SC ONLOGON /TR "powershell.exe -NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File %BASE%\run_pairproxy-agent.ps1" /F
if errorlevel 1 (
  echo Failed to create scheduled task.
  exit /b 1
)

schtasks /Run /TN "%TASK_NAME%" >nul
echo PairProxy Windows reverse agent installed and started.
echo Linux broker control: %SERVER_HOST%:%AGENT_PORT%
echo Hidden scheduled task enabled; no CMD window or local proxy is opened.
echo Log: %BASE%\pairproxy-agent.log
endlocal
