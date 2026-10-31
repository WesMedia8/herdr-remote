# Windows PowerShell 5+. Override $env:REPO_URL before running if needed.
$ErrorActionPreference = 'Stop'
$RepoUrl = if ($env:REPO_URL) { $env:REPO_URL } else { 'https://github.com/WesMedia8/herdr-remote' }
function Invoke-Checked {
    param([string]$File, [string[]]$Arguments)
    & $File @Arguments
    if ($LASTEXITCODE -ne 0) { throw "$File failed (exit $LASTEXITCODE)." }
}
if (-not (Get-Command py -ErrorAction SilentlyContinue)) {
    throw 'Install Python with: winget install Python.Python.3.12 (include the Python launcher), then reopen PowerShell.'
}
& py -3 -c 'import sys; sys.exit(sys.version_info < (3,10))'
if ($LASTEXITCODE -ne 0) { throw 'Python >=3.10 required. Run: winget install Python.Python.3.12' }
if (-not (Get-Command git -ErrorAction SilentlyContinue)) { throw 'Install Git: winget install Git.Git; then reopen PowerShell.' }
Invoke-Checked py @('-3', '-m', 'pip', 'install', '--user', 'pipx')
Invoke-Checked py @('-3', '-m', 'pipx', 'ensurepath')
$PipxBin = (& py -3 -m pipx environment --value PIPX_BIN_DIR).Trim()
if ($LASTEXITCODE -ne 0) { throw 'Cannot locate pipx executable directory.' }
$env:PATH = "$PipxBin;$env:PATH"
Invoke-Checked py @('-3', '-m', 'pipx', 'install', '--force', "git+$RepoUrl#subdirectory=bridge")
& herdr-remote-bridge doctor
if ($LASTEXITCODE -ne 0) {
    Write-Host 'Install Tailscale: winget install Tailscale.Tailscale (or https://tailscale.com/download). Open the Tailscale app and sign in.'
    Read-Host 'Review doctor output for missing herdr tools. Press Enter when ready to pair'
}
# Start the bridge first, so the phone can connect the moment it scans the code.
Invoke-Checked herdr-remote-bridge @('service', 'install')
Invoke-Checked herdr-remote-bridge @('pair')
