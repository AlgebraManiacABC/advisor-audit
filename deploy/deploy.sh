#!/usr/bin/env bash
# Deploy to an Akamai (Linode) Ubuntu 24.04 host: ./deploy/deploy.sh root@HOST
# Ships `git archive HEAD`, so uncommitted work never goes out. .env is copied separately.
set -euo pipefail
HOST=${1:?usage: deploy.sh user@host}
cd "$(dirname "$0")/.."
[ -f .env ] || { echo "Missing .env (see .env.example)"; exit 1; }
DOMAIN=$(grep -E '^DOMAIN=' .env | cut -d= -f2)
[ -n "$DOMAIN" ] || { echo "Set DOMAIN in .env"; exit 1; }

ssh "$HOST" 'command -v docker >/dev/null || (apt-get update && apt-get install -y docker.io docker-compose-v2)'
ssh "$HOST" 'mkdir -p /opt/advisor-audit'
git archive --format=tar HEAD | ssh "$HOST" 'tar -x -C /opt/advisor-audit'
scp -q .env "$HOST:/opt/advisor-audit/.env"
ssh "$HOST" 'chmod 600 /opt/advisor-audit/.env && cd /opt/advisor-audit/deploy && docker compose --env-file ../.env up -d --build'

for i in $(seq 1 30); do
  if curl -fsS "https://$DOMAIN/healthz" >/dev/null 2>&1; then echo "Up: https://$DOMAIN"; exit 0; fi
  sleep 4
done
echo "Health check failed; see: ssh $HOST 'cd /opt/advisor-audit/deploy && docker compose logs'"; exit 1
