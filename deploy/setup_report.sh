#!/bin/bash
# One-time setup of the scheduled Claude reviews (deploy/claude_report.sh).
# Idempotent. Needs /etc/alpha5/claude-token (claude setup-token, owner's
# account) and /etc/alpha5/report-topic-arn (the SNS topic) in place.
set -euo pipefail
U=alpha5-report
H=/var/lib/$U
id $U >/dev/null 2>&1 || sudo useradd --system --home-dir $H --create-home --shell /bin/bash $U
# Reads system logs; not in group ubuntu, so it can't write data/ or control files.
sudo usermod -aG systemd-journal $U
sudo mkdir -p /etc/alpha5
for f in claude-token report-topic-arn; do
  [ -f /etc/alpha5/$f ] || { echo "missing /etc/alpha5/$f"; exit 1; }
done
sudo chown $U:$U /etc/alpha5/claude-token && sudo chmod 600 /etc/alpha5/claude-token
[ -x $H/.local/bin/claude ] || sudo -u $U -H bash -c 'curl -fsSL https://claude.ai/install.sh | bash'
sudo cp /opt/alpha5/deploy/systemd/alpha5-report-*.service /opt/alpha5/deploy/systemd/alpha5-report-*.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now alpha5-report-daily.timer alpha5-report-hourly.timer
systemctl list-timers 'alpha5-report-*' --no-pager
