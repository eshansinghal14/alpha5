# Copy the two gitignored secrets to the server over SSH, and the last few
# hours of crypto data the live process warms up from (vol forecaster: 4 h of
# composite bars; burst calibration: 2 h of prints). Never through git.
#   powershell -File deploy\push_secrets.ps1 -Host ubuntu@<ip> -Key <path-to.pem>
param(
    [Parameter(Mandatory = $true)][string]$Host,
    [Parameter(Mandatory = $true)][string]$Key,
    [int]$Hours = 5
)
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$app = "/opt/alpha5"
$ssh = @("-i", $Key, "-o", "StrictHostKeyChecking=accept-new")

& ssh @ssh $Host "mkdir -p $app/kalshi_btc_hedge $app/data/kalshi_crypto/composite $app/data/kalshi_crypto/prints"
& scp @ssh "$root\kalshi_infra\keys.py" "${Host}:$app/kalshi_infra/keys.py"
& scp @ssh "$root\kalshi_btc_hedge\eshan-ws-key.txt" "${Host}:$app/kalshi_btc_hedge/eshan-ws-key.txt"
& ssh @ssh $Host "chmod 600 $app/kalshi_infra/keys.py $app/kalshi_btc_hedge/eshan-ws-key.txt"

$cut = (Get-Date).ToUniversalTime().AddHours(-$Hours)
foreach ($sub in "composite", "prints") {
    $files = Get-ChildItem "$root\data\kalshi_crypto\$sub\*.parquet" |
        Where-Object { $_.LastWriteTimeUtc -ge $cut }
    if ($files) {
        & scp @ssh @($files.FullName) "${Host}:$app/data/kalshi_crypto/$sub/"
    }
    "{0}: {1} file(s)" -f $sub, @($files).Count
}
& scp @ssh "$root\data\kalshi_crypto\rti_truth.parquet" "${Host}:$app/data/kalshi_crypto/" 2>$null
"secrets and warmup data copied"
