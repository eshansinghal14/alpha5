# Change the live instance type (e.g. c7g.large -> c7g.xlarge for S&P).
# STOPS THE BOX: every service, including real-money crypto, is down for the
# ~2-4 min the stop/modify/start takes, and resting Kalshi orders stay on the
# book unwatched. Run it between crypto events with crypto DISARMED.
# The public IP changes on stop/start unless the instance has an Elastic IP.
#   powershell -File deploy\resize.ps1 -Type c7g.xlarge
param(
    [Parameter(Mandatory = $true)][string]$Type,
    [string]$Region = "us-east-1",
    [string]$Name = "alpha5-live"
)
$ErrorActionPreference = "Stop"
$id = aws ec2 describe-instances --region $Region `
    --filters "Name=tag:Name,Values=$Name" "Name=instance-state-name,Values=running,stopped" `
    --query "Reservations[0].Instances[0].InstanceId" --output text
if (-not $id -or $id -eq "None") { throw "no instance named $Name" }
$cur = aws ec2 describe-instances --region $Region --instance-ids $id `
    --query "Reservations[0].Instances[0].InstanceType" --output text
"instance $id is $cur; resizing to $Type"
if ($cur -eq $Type) { "already $Type"; exit 0 }
$ok = Read-Host "This stops $id (crypto included). Crypto disarmed and between events? Type yes"
if ($ok -ne "yes") { "aborted"; exit 1 }

aws ec2 stop-instances --region $Region --instance-ids $id | Out-Null
aws ec2 wait instance-stopped --region $Region --instance-ids $id
aws ec2 modify-instance-attribute --region $Region --instance-id $id --instance-type "{\`"Value\`": \`"$Type\`"}"
aws ec2 start-instances --region $Region --instance-ids $id | Out-Null
aws ec2 wait instance-running --region $Region --instance-ids $id
$ip = aws ec2 describe-instances --region $Region --instance-ids $id `
    --query "Reservations[0].Instances[0].PublicIpAddress" --output text
"running as $Type at $ip (services start themselves; check with deploy/status.sh)"
"The public IP changes on every stop/start; review_agent_loop looks it up itself, update your own ssh commands."
