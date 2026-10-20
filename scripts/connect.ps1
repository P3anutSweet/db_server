[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [ValidatePattern('^[a-zA-Z0-9][a-zA-Z0-9.-]*$')]
    [string]$Server,
    [ValidatePattern('^[a-z_][a-z0-9_-]*[$]?$')]
    [string]$User,
    [ValidateSet('Terminal', 'Desktop', 'Check')]
    [string]$Mode = 'Terminal'
)
$ErrorActionPreference = 'Stop'
if ($Mode -eq 'Check') {
    foreach ($port in @(22, 3389)) {
        Test-NetConnection -ComputerName $Server -Port $port -InformationLevel Detailed
    }
} elseif ($Mode -eq 'Desktop') {
    # Interactive application requested by the user; credentials stay in the RDP client.
    Start-Process -FilePath 'mstsc.exe' -ArgumentList "/v:${Server}:3389"
} else {
    if (-not $User) { throw '-User is required for terminal access.' }
    & ssh.exe -o ConnectTimeout=10 -o ServerAliveInterval=30 -o ServerAliveCountMax=3 "${User}@${Server}"
    if ($LASTEXITCODE -ne 0) { throw "SSH exited with code $LASTEXITCODE" }
}
