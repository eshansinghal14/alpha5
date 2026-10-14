# S&P on the EC2 box

How to run the S&P strategy (KXINX + KXINXU + KXINXDUD + pricing hub) on
`alpha5-live` (54.81.109.56, us-east-1) next to KXCRYPTOLEAD15M, with IB
Gateway on the same box and one dashboard for both.

## Shape

```
alpha5-ibgateway  docker: IB Gateway + IBC + Xvfb   127.0.0.1:4001 (API), :5900 (VNC)
alpha5-spx        run --series KXINX   -> hub (only IB client) + one process for the 3 series
alpha5-crypto     unchanged (nice -5, real money)
alpha5-dashboard  one Streamlit for every series (sidebar), 127.0.0.1:8502
alpha5-watchdog   unchanged; already covers every series under data/live
```

The dashboard needs no change: its series list comes from the registry,
so the S&P series appear in the same sidebar as crypto as soon as their
`data/live/<SERIES>/` folders exist on the box.

## What has to be true first

1. **Instance size.** Today: c7g.large, 2 vCPU / 3.8 GB, ~2.1 GB available
   with crypto running (crypto + venue process + dashboard ~1.5 GB RSS).
   S&P measured locally on 2026-10-14: hub ~25 MB, launcher ~120 MB, three
   series ~420 MB together, plus IB Gateway ~220-500 MB (Java, capped at
   1.2 GB in the compose file). That fits in 2.1 GB, but with no swap and
   the review agent's 1 GB cap on top it leaves no margin, and two vCPUs
   would be shared by two latency-sensitive strategies plus Java.
   **Recommended: c7g.xlarge (4 vCPU / 8 GB)** via `deploy/resize.ps1`.
   That stops the box for 2-4 minutes. Disarm crypto first and do it between
   events. Without an Elastic IP the public IP changes; update `EC2_HOST`
   in `kalshi_infra/runtime/review_agent_loop.py` and your SSH commands.
2. **IB login for the server.** IBKR allows one session per username. If
   the gateway on EC2 and your desktop Gateway/TWS/mobile app use the same
   login, each one knocks the other off. Create an IBKR **secondary user**
   (Client Portal > Settings > Users & Access Rights) with market-data
   permission and the OPRA subscription. Market data is billed per user, so
   OPRA is charged again for that user. Or stop using IB anywhere else
   while the server runs.
3. **2FA.** The first login on the server needs one IBKR Mobile approval.
   The compose file restarts the gateway nightly at 23:45 ET
   (`AUTO_RESTART_TIME`). A restart reuses the session token and normally
   needs no new 2FA, but IBKR still forces a full re-login with 2FA about
   once a week. If nobody approves it, the hub reports "not connected" and
   S&P stops quoting (crypto is unaffected). Watch for it on Sunday/Monday.
4. **Commit and push** this branch. The server deploys from git
   (`bootstrap.sh ec2-live`). The S&P code is uncommitted today.

## Steps

From this machine, with crypto disarmed if resizing:

```powershell
# 0. (optional, recommended) resize
powershell -File deploy\resize.ps1 -Type c7g.xlarge

# 1. IB credentials for the server (gitignored)
copy deploy\ibgateway\ib.env.example deploy\ibgateway\ib.env   # then fill it in

# 2. code + deps + docker + units (idempotent; crypto keeps running)
ssh -i $env:USERPROFILE\.ssh\alpha5-live-key.pem ubuntu@54.81.109.56 'cd /opt/alpha5 && bash deploy/bootstrap.sh ec2-live'

# 3. secrets: keys.py, Kalshi key, ib.env
powershell -File deploy\push_secrets.ps1 -Target ubuntu@54.81.109.56 -Key $env:USERPROFILE\.ssh\alpha5-live-key.pem

# 4. start the gateway and approve the 2FA prompt on your phone
ssh ... 'sudo systemctl enable --now alpha5-ibgateway; sleep 60; timeout 2 bash -c "</dev/tcp/127.0.0.1/4001" && echo API up'

# 5. start S&P (shadow) and restart the dashboard so it picks up the new code
ssh ... 'sudo systemctl enable --now alpha5-spx && sudo systemctl restart alpha5-dashboard && bash deploy/status.sh'
```

Step 2 re-runs `pip install` and copies the units. It does not restart
`alpha5-crypto`. Crypto picks up new code only when you restart it, so
restart it deliberately, between events and disarmed, if you want the newer
shared runtime (session/order-manager changes) on crypto too.

Then stop the local S&P run. Two launchers would both trade the same
series and write budget from two machines.

## Operating

- Dashboard: `ssh -i ... -N -L 8502:127.0.0.1:8502 ubuntu@54.81.109.56`,
  then http://localhost:8502. Crypto and the three S&P series are in one sidebar.
- Gateway screen (2FA, stuck dialog):
  `ssh -i ... -N -L 5900:127.0.0.1:5900 ubuntu@54.81.109.56`, then any VNC
  client on localhost:5900 with `VNC_SERVER_PASSWORD`.
- Gateway logs: `ssh ... 'cd /opt/alpha5/deploy/ibgateway && sudo docker compose logs --tail 100'`.
- `deploy/status.sh` shows every unit, whether port 4001 is open, memory,
  and each series' signal age / feed problem.
- `keys.py` on the server keeps `IB_HOST=127.0.0.1`, `IB_PORT=4001`. The
  container maps its socat port 4003 to 127.0.0.1:4001.
- `READ_ONLY_API=yes`: the gateway refuses order API calls. The strategy
  only reads market data, so leave it on.

## Limits / risks

- One IB session per user (see 2). The weekly 2FA needs a human.
- Crypto and S&P share the box and Kalshi account. Their write budgets
  don't collide: crypto is on shard 2, S&P on shard 0. The balance watchdog
  and STOP.json stop both.
- A gateway outage only affects S&P: the hub goes stale and quotes are
  pulled. A box outage affects both.
