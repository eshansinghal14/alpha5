# Create the c7g.large in us-east-1: key pair, a security group that only
# admits SSH from this machine's current IP, Ubuntu 24.04 arm64, 30 GB gp3.
# Requires `aws login` first. Prints the public IP and the next commands.
param(
    [string]$Region = "us-east-1",
    [string]$Type = "c7g.large",
    [string]$Name = "alpha5-live",
    [int]$DiskGB = 30
)
$ErrorActionPreference = "Stop"
$keyName = "$Name-key"
$keyPath = Join-Path $env:USERPROFILE ".ssh\$keyName.pem"

aws sts get-caller-identity --region $Region | Out-Null

if (-not (Test-Path $keyPath)) {
    # Join the CLI's output lines with LF: piping it straight to Set-Content
    # -NoNewline dropped every line break and OpenSSH rejected the key.
    $pem = (aws ec2 create-key-pair --region $Region --key-name $keyName --key-type ed25519 `
        --query KeyMaterial --output text) -join "`n"
    [IO.File]::WriteAllText($keyPath, $pem.TrimEnd() + "`n")
    # OpenSSH refuses a key other users can read.
    icacls $keyPath /inheritance:r /grant:r "$($env:USERNAME):(R)" | Out-Null
}

$myIp = (Invoke-RestMethod -Uri "https://checkip.amazonaws.com").Trim()
$sg = aws ec2 describe-security-groups --region $Region --filters "Name=group-name,Values=$Name-ssh" `
    --query "SecurityGroups[0].GroupId" --output text
if ($sg -eq "None" -or -not $sg) {
    $sg = aws ec2 create-security-group --region $Region --group-name "$Name-ssh" `
        --description "SSH from operator IP only" --query GroupId --output text
}
aws ec2 authorize-security-group-ingress --region $Region --group-id $sg --protocol tcp --port 22 `
    --cidr "$myIp/32" 2>$null | Out-Null

# Canonical's published SSM parameter for the current Ubuntu 24.04 arm64 AMI.
$ami = aws ssm get-parameter --region $Region `
    --name /aws/service/canonical/ubuntu/server/24.04/stable/current/arm64/hvm/ebs-gp3/ami-id `
    --query Parameter.Value --output text

$id = aws ec2 run-instances --region $Region --image-id $ami --instance-type $Type --key-name $keyName `
    --security-group-ids $sg `
    --block-device-mappings "DeviceName=/dev/sda1,Ebs={VolumeSize=$DiskGB,VolumeType=gp3,DeleteOnTermination=true}" `
    --metadata-options "HttpTokens=required" `
    --tag-specifications "ResourceType=instance,Tags=[{Key=Name,Value=$Name}]" `
    --query "Instances[0].InstanceId" --output text
if ($LASTEXITCODE -ne 0 -or -not $id) { throw "run-instances failed; nothing launched" }
aws ec2 wait instance-running --region $Region --instance-ids $id
$ip = aws ec2 describe-instances --region $Region --instance-ids $id `
    --query "Reservations[0].Instances[0].PublicIpAddress" --output text

"instance $id ($Type, $Region) at $ip; SSH allowed only from $myIp"
"key: $keyPath"
"next:"
"  ssh -i `"$keyPath`" ubuntu@$ip 'curl -fsSL https://raw.githubusercontent.com/eshansinghal14/alpha5/ec2-live/deploy/bootstrap.sh | bash -s ec2-live'"
"  powershell -File deploy\push_secrets.ps1 -Target ubuntu@$ip -Key `"$keyPath`""
"  ssh -i `"$keyPath`" ubuntu@$ip 'sudo systemctl enable --now alpha5-crypto alpha5-dashboard'"
"  dashboard: ssh -i `"$keyPath`" -N -L 8502:127.0.0.1:8502 ubuntu@$ip  then open http://localhost:8502"
