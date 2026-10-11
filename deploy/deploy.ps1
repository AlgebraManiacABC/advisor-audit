# Deploy to an Akamai (Linode) Ubuntu 24.04 host from Windows PowerShell:
#   .\deploy\deploy.ps1 root@HOST
# Ships `git archive HEAD`, so uncommitted work never goes out. .env is copied separately.
# Needs the OpenSSH client (built into Windows 10/11) and git.
param([Parameter(Mandatory = $true)][string]$TargetHost)
$ErrorActionPreference = "Stop"
Set-Location (Join-Path $PSScriptRoot "..")

function Run([string]$exe, [string[]]$argv) {
    & $exe @argv
    if ($LASTEXITCODE -ne 0) { throw "$exe failed (exit $LASTEXITCODE)" }
}

if (-not (Test-Path .env)) { throw "Missing .env (see .env.example)" }
$domainLine = Select-String -Path .env -Pattern '^DOMAIN=(.+)$' | Select-Object -First 1
if (-not $domainLine) { throw "Set DOMAIN in .env" }
$Domain = $domainLine.Matches[0].Groups[1].Value.Trim()

# Write the archive to a file: piping binary data through PowerShell would corrupt it.
$tar = Join-Path $env:TEMP "advisor-audit.tar"
Run git @("archive", "--format=tar", "-o", $tar, "HEAD")

Run ssh @($TargetHost, "command -v docker >/dev/null || (apt-get update && apt-get install -y docker.io docker-compose-v2)")
Run ssh @($TargetHost, "mkdir -p /opt/advisor-audit")
Run scp @("-q", $tar, "${TargetHost}:/tmp/advisor-audit.tar")
Run scp @("-q", ".env", "${TargetHost}:/opt/advisor-audit/.env")
Remove-Item $tar
Run ssh @($TargetHost, "tar -x -C /opt/advisor-audit -f /tmp/advisor-audit.tar && rm /tmp/advisor-audit.tar && sed -i 's/\r$//' /opt/advisor-audit/.env && chmod 600 /opt/advisor-audit/.env && cd /opt/advisor-audit/deploy && docker compose --env-file ../.env up -d --build")

for ($i = 0; $i -lt 30; $i++) {
    try {
        Invoke-WebRequest -UseBasicParsing -TimeoutSec 5 "https://$Domain/healthz" | Out-Null
        Write-Host "Up: https://$Domain"
        exit 0
    } catch { Start-Sleep -Seconds 4 }
}
Write-Host "Health check failed; see: ssh $TargetHost 'cd /opt/advisor-audit/deploy && docker compose logs'"
exit 1
