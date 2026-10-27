param(
  [int]$ProxyPort = 27499,
  [int]$AgentPort = 27500,
  [int]$TargetPort = 28080
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$Python = (Get-Command python -ErrorAction Stop).Source
$Processes = @()
$User = "smoke_user"
$Pass = "smoke_pass"
$Token = "smoke_agent_token"

function Start-HiddenPython {
  param(
    [string[]]$Arguments,
    [string]$Stdout,
    [string]$Stderr
  )
  $p = Start-Process -FilePath $Python `
    -ArgumentList $Arguments `
    -WorkingDirectory $Root `
    -WindowStyle Hidden `
    -PassThru `
    -RedirectStandardOutput (Join-Path $Root $Stdout) `
    -RedirectStandardError (Join-Path $Root $Stderr)
  $script:Processes += $p
  Start-Sleep -Milliseconds 700
  return $p
}

try {
  Remove-Item -Force `
    (Join-Path $Root "smoke-http.out.log"), `
    (Join-Path $Root "smoke-http.err.log"), `
    (Join-Path $Root "smoke-broker.out.log"), `
    (Join-Path $Root "smoke-broker.err.log"), `
    (Join-Path $Root "smoke-agent.out.log"), `
    (Join-Path $Root "smoke-agent.err.log") `
    -ErrorAction SilentlyContinue

  Start-HiddenPython @("-m", "http.server", "$TargetPort", "--bind", "127.0.0.1", "--directory", ".") "smoke-http.out.log" "smoke-http.err.log" | Out-Null
  Start-HiddenPython @("reverse_pair_proxy.py", "broker", "--proxy-bind", "127.0.0.1", "--proxy-port", "$ProxyPort", "--proxy-user", $User, "--proxy-pass", $Pass, "--agent-bind", "127.0.0.1", "--agent-port", "$AgentPort", "--agent-token", $Token, "--log-level", "DEBUG") "smoke-broker.out.log" "smoke-broker.err.log" | Out-Null
  Start-HiddenPython @("reverse_pair_proxy.py", "agent", "--server", "127.0.0.1", "--agent-port", "$AgentPort", "--agent-token", $Token, "--pool-size", "4", "--log-level", "DEBUG") "smoke-agent.out.log" "smoke-agent.err.log" | Out-Null

  Start-Sleep -Seconds 1
  $HttpResult = & curl.exe --fail -sS --noproxy "proxy-smoke.invalid" --connect-timeout 5 --max-time 15 --proxy "http://${User}:${Pass}@127.0.0.1:${ProxyPort}" "http://127.0.0.1:${TargetPort}/README.md"
  $HttpExit = $LASTEXITCODE
  $SocksResult = & curl.exe --fail -sS --noproxy "proxy-smoke.invalid" --connect-timeout 5 --max-time 15 --proxy "socks5h://${User}:${Pass}@127.0.0.1:${ProxyPort}" "http://127.0.0.1:${TargetPort}/README.md"
  $SocksExit = $LASTEXITCODE
  $HttpText = $HttpResult -join "`n"
  $SocksText = $SocksResult -join "`n"

  if ($HttpExit -ne 0 -or $HttpText -notmatch "Win-Linux Reverse PairProxy") {
    throw "Reverse HTTP proxy smoke test failed"
  }
  if ($SocksExit -ne 0 -or $SocksText -notmatch "Win-Linux Reverse PairProxy") {
    throw "Reverse SOCKS5 proxy smoke test failed"
  }

  Write-Output "SMOKE_OK reverse_http_and_socks"
}
finally {
  foreach ($p in $Processes) {
    Stop-Process -Id $p.Id -Force -ErrorAction SilentlyContinue
  }
}
