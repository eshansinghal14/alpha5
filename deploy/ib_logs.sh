#!/bin/bash
# IB Gateway container status + recent log, with anything credential-like removed.
cd /opt/alpha5/deploy/ibgateway
sudo docker compose ps --format 'table {{.Name}}\t{{.Status}}'
echo "--- container log (filtered)"
sudo docker compose logs --no-color --tail "${1:-80}" 2>&1 | grep -v -i -E 'password|passwd|userid|user id|token' | cut -c1-220
echo "--- IBC log (filtered)"
sudo docker compose exec -T ib-gateway bash -lc 'ls -t ~/ibc/logs/*.txt 2>/dev/null | head -1 | xargs -r tail -n 40' 2>/dev/null \
  | grep -v -i -E 'password|passwd|userid|user id|token' | cut -c1-220
echo "--- api port"
(timeout 2 bash -c '</dev/tcp/127.0.0.1/4001') 2>/dev/null && echo "4001 OPEN" || echo "4001 closed"
