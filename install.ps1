# LVCT user installer. Does not change execution policy or start trading.
[CmdletBinding()]
param(
    [string]$Ref = 'main',
    [string]$HomeDir,
    [string]$Source,
    [switch]$NoPath,
    [switch]$InstallPython
)
$ErrorActionPreference = 'Stop'
function Find-LvctPython {
    foreach ($name in @('py', 'python', 'python3')) {
        $candidate = Get-Command $name -ErrorAction SilentlyContinue
        if ($candidate -and $candidate.Source -notmatch '\\Microsoft\\WindowsApps\\python(3)?\.exe$') {
            try {
                $path = & $candidate.Source -c 'import sys,json; assert sys.version_info >= (3,10); print(json.dumps(sys.executable))' 2>$null
                if ($LASTEXITCODE -eq 0 -and $path) { return [string](($path | Select-Object -Last 1) | ConvertFrom-Json) }
            } catch { }
        }
    }
    return $null
}
$python = Find-LvctPython
if (-not $python -and $InstallPython) {
    if (-not (Get-Command winget -ErrorAction SilentlyContinue)) { throw 'Install Python 3.10+ from python.org, then retry.' }
    & winget install --exact --id Python.Python.3.12 --scope user
    if ($LASTEXITCODE -ne 0) { throw 'Python installation did not complete.' }
    $python = Find-LvctPython
    if (-not $python) {
        $standard = Join-Path $env:LOCALAPPDATA 'Programs\Python\Python312\python.exe'
        if (Test-Path $standard) { $python = $standard }
    }
}
if (-not $python) { throw 'Python 3.10+ is required. Retry with -InstallPython, or install Python first.' }
$arguments = @('--ref', $Ref)
if ($HomeDir) { $arguments += @('--home', $HomeDir) }
if ($Source) { $arguments += @('--source', $Source) }
if ($NoPath) { $arguments += '--no-path' }
$temporary = Join-Path ([IO.Path]::GetTempPath()) ('lvct-' + [guid]::NewGuid().ToString('N'))
try {
    New-Item -ItemType Directory -Path $temporary | Out-Null
    if ($Source) {
        $bootstrap = Join-Path $Source 'cli\bootstrap.py'
    } else {
        $bootstrap = Join-Path $temporary 'bootstrap.py'
        Invoke-WebRequest -UseBasicParsing -Uri 'https://raw.githubusercontent.com/abdulloh5007/lv-copytrader-public/main/cli/bootstrap.py' -OutFile $bootstrap
    }
    & $python $bootstrap @arguments
    if ($LASTEXITCODE -ne 0) { throw 'LVCT installation failed.' }
} finally {
    if (Test-Path $temporary) { Remove-Item -Recurse -Force $temporary }
}
