@echo off
setlocal EnableExtensions

set "TASK_NAME=PairProxyAgent"
set "OLD_TASK_NAME=PairProxyClient"
set "BASE=%ProgramData%\PairProxy"

schtasks /End /TN "%TASK_NAME%" >nul 2>nul
schtasks /Delete /TN "%TASK_NAME%" /F >nul 2>nul
schtasks /End /TN "%OLD_TASK_NAME%" >nul 2>nul
schtasks /Delete /TN "%OLD_TASK_NAME%" /F >nul 2>nul

powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -like '*reverse_pair_proxy.py*agent*' -or $_.CommandLine -like '*run_pairproxy-agent.ps1*' -or $_.CommandLine -like '*run_pairproxy-agent.cmd*' -or $_.CommandLine -like '*pair_proxy.py*client*' -or $_.CommandLine -like '*run_pairproxy.cmd*' } | ForEach-Object { try { Stop-Process -Id $_.ProcessId -Force -ErrorAction Stop } catch {} }" >nul 2>nul

reg add "HKCU\Software\Microsoft\Windows\CurrentVersion\Internet Settings" /v ProxyEnable /t REG_DWORD /d 0 /f >nul 2>nul
reg delete "HKCU\Software\Microsoft\Windows\CurrentVersion\Internet Settings" /v ProxyServer /f >nul 2>nul
netsh winhttp reset proxy >nul 2>nul

echo PairProxy Windows reverse agent stopped.
echo Files/logs are kept in "%BASE%".
endlocal
