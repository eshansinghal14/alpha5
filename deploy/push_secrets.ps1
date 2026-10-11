# Copy the two gitignored secrets to the server over SSH, and the last few
# hours of crypto data the live process warms up from (vol forecaster: 4 h of
# composite bars; burst calibration: 2 h of prints). Never through git.
#   powershell -File deploy\push_secrets.ps1 -Target ubuntu@<ip> -Key <path-to.pem>
param(
    [Parameter(Mandatory = $true)][string]$Target,
    [Parameter(Mandatory = $true)][string]$Key,
    [int]$Hours = 5
)
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
# Native ssh/scp failures don't trip ErrorActionPreference; check every exit code.
function Check($what) { if ($LASTEXITCODE -ne 0) { throw "$what failed (exit $LASTEXITCODE)" } }
$app = "/opt/alpha5"
$ssh = @("-i", $Key, "-o", "StrictHostKeyChecking=accept-new")

& ssh @ssh $Target "mkdir -p $app/kalshi_btc_hedge $app/data/kalshi_crypto/composite $app/data/kalshi_crypto/prints"
& scp @ssh "$root\kalshi_infra\keys.py" "${Target}:$app/kalshi_infra/keys.py"; Check 'copy keys.py'
& scp @ssh "$root\kalshi_btc_hedge\eshan-ws-key.txt" "${Target}:$app/kalshi_btc_hedge/eshan-ws-key.txt"; Check 'copy private key'
& ssh @ssh $Target "chmod 600 $app/kalshi_infra/keys.py $app/kalshi_btc_hedge/eshan-ws-key.txt"; Check 'chmod secrets'

$cut = (Get-Date).ToUniversalTime().AddHours(-$Hours)
foreach ($sub in "composite", "prints") {
    $files = Get-ChildItem "$root\data\kalshi_crypto\$sub\*.parquet" |
        Where-Object { $_.LastWriteTimeUtc -ge $cut }
    if ($files) {
        & scp @ssh @($files.FullName) "${Target}:$app/data/kalshi_crypto/$sub/"
    }
    "{0}: {1} file(s)" -f $sub, @($files).Count
}
& scp @ssh "$root\data\kalshi_crypto\rti_truth.parquet" "${Target}:$app/data/kalshi_crypto/" 2>$null
"secrets and warmup data copied"
