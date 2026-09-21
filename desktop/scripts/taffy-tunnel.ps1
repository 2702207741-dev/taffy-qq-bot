# Foreground SSH tunnel. Verify and trust the server host key independently first.
# Stop with Ctrl+C; unrelated SSH processes are never terminated.
param(
    [Parameter(Mandatory=$true)][ValidatePattern('^[a-zA-Z0-9][a-zA-Z0-9.-]*$')][string]$RemoteHost,
    [Parameter(Mandatory=$true)][ValidatePattern('^[a-zA-Z0-9_][a-zA-Z0-9_.-]*$')][string]$RemoteUser,
    [Parameter(Mandatory=$true)][string]$KeyFile,
    [ValidateRange(1,65535)][int]$LocalPort = 18080,
    [ValidateRange(1,65535)][int]$RemotePort = 8080
)
$ErrorActionPreference = 'Stop'
$resolvedKey = (Resolve-Path -LiteralPath $KeyFile).Path
& ssh -i $resolvedKey -N -o StrictHostKeyChecking=yes -o BatchMode=yes `
    -o ExitOnForwardFailure=yes -o ServerAliveInterval=30 -o ServerAliveCountMax=3 `
    -L "127.0.0.1:${LocalPort}:127.0.0.1:${RemotePort}" "${RemoteUser}@${RemoteHost}"
exit $LASTEXITCODE
