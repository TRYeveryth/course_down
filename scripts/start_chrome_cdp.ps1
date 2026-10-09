$ErrorActionPreference = 'Stop'
$projectDirectory = Split-Path -Parent $PSScriptRoot
$taskPython = Join-Path $projectDirectory '.venv/Scripts/python.exe'
if (-not (Test-Path -LiteralPath $taskPython)) { $taskPython = 'python' }
Push-Location $projectDirectory
try {
    $configuration = & $taskPython -c 'import json,common; print(json.dumps({"cdp":common.CDP_URL,"url":common.CATALOG_URL or common.COURSE_URL}))'
    if ($LASTEXITCODE -ne 0) { throw '读取配置失败，请先安装 Python 并检查 config.py。' }
    $configuration = $configuration | ConvertFrom-Json
} finally { Pop-Location }
$endpoint = [uri]$configuration.cdp
if ($endpoint.Scheme -notin @('http','https') -or $endpoint.Host -notin @('localhost','127.0.0.1') -or $endpoint.Port -le 0) {
    throw '专用 Chrome 启动脚本要求 CDP_URL 为本机 HTTP 地址并包含端口。'
}
$chromeCandidates = @(
    (Join-Path $env:ProgramFiles 'Google/Chrome/Application/chrome.exe'),
    (Join-Path ${env:ProgramFiles(x86)} 'Google/Chrome/Application/chrome.exe'),
    (Join-Path $env:LOCALAPPDATA 'Google/Chrome/Application/chrome.exe')
)
$chromePath = $chromeCandidates | Where-Object { Test-Path -LiteralPath $_ } | Select-Object -First 1
if (-not $chromePath) { throw '未找到 Google Chrome，请安装 Chrome 后重试。' }
$targetUrl = $configuration.url
if (-not $targetUrl) { $targetUrl = 'about:blank' }
if ($targetUrl.Contains('"') -or $targetUrl.Contains("`r") -or $targetUrl.Contains("`n")) {
    throw '配置中的网址包含无效引号或换行。'
}
$profilePath = Join-Path $projectDirectory 'chrome-profile'
$chromeArguments = @(
    "--remote-debugging-port=$($endpoint.Port)", '--remote-debugging-address=127.0.0.1',
    "--user-data-dir=`"$profilePath`"", "`"$targetUrl`""
)
# This window is interactive: the user must log in to their learning platform.
Start-Process -FilePath $chromePath -ArgumentList $chromeArguments -WindowStyle Normal
