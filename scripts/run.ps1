param(
    [ValidateSet('catalog','official','retry','probe','verify')]
    [string]$Task = 'catalog',
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$ScriptArgs
)
$ErrorActionPreference = 'Stop'
$projectDirectory = Split-Path -Parent $PSScriptRoot
$taskPython = Join-Path $projectDirectory '.venv/Scripts/python.exe'
if (-not (Test-Path -LiteralPath $taskPython)) {
    python -m venv (Join-Path $projectDirectory '.venv')
    if ($LASTEXITCODE -ne 0) { throw '创建环境失败，请先安装 Python 3.11 或更新版本。' }
}
& $taskPython -m pip install -r (Join-Path $projectDirectory 'requirements.txt')
if ($LASTEXITCODE -ne 0) { throw '依赖安装失败。' }
if ($Task -eq 'verify') {
    & $taskPython -m pip install -r (Join-Path $projectDirectory 'requirements-verify.txt')
    if ($LASTEXITCODE -ne 0) { throw '核验依赖安装失败。' }
}
$entryFiles = @{
    catalog = 'download_catalog.py'; official = 'download_official.py'
    retry = 'retry_failed.py'; probe = 'probe.py'; verify = 'verify_downloads.py'
}
& $taskPython -u (Join-Path $projectDirectory $entryFiles[$Task]) @ScriptArgs
exit $LASTEXITCODE
