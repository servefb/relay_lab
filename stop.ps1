$ErrorActionPreference = 'Stop'
$address = 'http://127.0.0.1:9011'
try {
    $runs = Invoke-RestMethod -Uri "$address/api/runs" -TimeoutSec 5
} catch {
    throw 'Relay Lab is not answering on port 9011; nothing to stop.'
}
foreach ($run in $runs.runs) {
    if ($run.status -in @('running', 'stopping')) {
        Invoke-RestMethod -Method Post -Uri "$address/api/runs/$($run.run_id)/stop" -ContentType 'application/json' -Body '{}' -TimeoutSec 5 | Out-Null
    }
}
$active = $null
for ($i = 0; $i -lt 30; $i++) {
    $active = (Invoke-RestMethod -Uri "$address/api/runs" -TimeoutSec 5).runs | Where-Object { $_.status -in @('running', 'stopping') }
    if (-not $active) { break }
    Start-Sleep -Seconds 1
}
if ($active) { throw 'Experiment still stopping; refusing to kill its parent service.' }

$listener = Get-NetTCPConnection -LocalPort 9011 -State Listen -ErrorAction Stop
$owner = Get-CimInstance Win32_Process -Filter "ProcessId = $($listener[0].OwningProcess)"
if ($owner.CommandLine -notlike '*-m relay_lab*') {
    throw 'Unexpected process identity; refusing to stop another service.'
}
Stop-Process -Id $owner.ProcessId -ErrorAction Stop
Write-Output 'Stopped the local Relay Lab only. Nothing else on this machine was touched.'
