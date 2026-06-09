#!/usr/bin/env bash
# Wire /stat into the existing nginx server block for parsercompressor.online.
# Safe: backs up the config, inserts deploy/nginx-subpath.conf inside the 443
# server block, runs `nginx -t`, and auto-rolls back if the test fails.
# Idempotent: does nothing if /stat is already present.
set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
SNIPPET="$REPO/deploy/nginx-subpath.conf"

FILE="$(grep -RlE 'client_max_body_size[[:space:]]+100M' /etc/nginx/ 2>/dev/null | head -1 || true)"
if [ -z "$FILE" ]; then
  echo "ERROR: could not locate the nginx config (anchor 'client_max_body_size 100M' not found)."
  echo "Run:  nginx -T | grep -n 'server_name'   and send the output to the assistant."
  exit 1
fi
echo "Target nginx config: $FILE"

if grep -q 'location /stat/' "$FILE"; then
  echo "/stat is already configured in $FILE — nothing to do."
  exit 0
fi

BACKUP="/root/nginx-site.bak.$(date +%s)"
cp "$FILE" "$BACKUP"
echo "Backup saved: $BACKUP"

sed -i "/client_max_body_size 100M;/r $SNIPPET" "$FILE"

if nginx -t; then
  systemctl reload nginx
  echo "DONE: /stat is live."
  echo -n "Health via public URL: "
  curl -s https://parsercompressor.online/stat/health || true
  echo
else
  echo "nginx test FAILED — rolling back, your site is untouched."
  cp "$BACKUP" "$FILE"
  nginx -t
  exit 1
fi
