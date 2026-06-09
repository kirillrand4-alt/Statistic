#!/usr/bin/env bash
# Password-protect /stat with nginx HTTP Basic auth.
# Usage:  bash deploy/protect_stat.sh [user] [password]
# If password is omitted, a random alphanumeric one is generated and printed.
set -euo pipefail

USER="${1:-admin}"
PASS="${2:-$(tr -dc 'A-Za-z0-9' </dev/urandom | head -c 14)}"

command -v htpasswd >/dev/null 2>&1 || apt-get install -y apache2-utils >/dev/null 2>&1 || true

htpasswd -bc /etc/nginx/.htpasswd "$USER" "$PASS" >/dev/null 2>&1

FILE="$(grep -RlE 'location /stat/' /etc/nginx/ 2>/dev/null | head -1 || true)"
if [ -z "$FILE" ]; then
  echo "ERROR: 'location /stat/' not found. Run deploy/wire_nginx.sh first."
  exit 1
fi

cp "$FILE" "/root/nginx-site.bak.$(date +%s)"

if ! grep -q 'auth_basic_user_file /etc/nginx/.htpasswd' "$FILE"; then
  sed -i '/location \/stat\/ {/a\    auth_basic "Restricted"; auth_basic_user_file /etc/nginx/.htpasswd;' "$FILE"
fi

if nginx -t; then
  systemctl reload nginx
  echo "----------------------------------------------"
  echo "/stat is now password-protected."
  echo "  user: $USER"
  echo "  pass: $PASS"
  echo "Save these — the browser will ask for them at /stat."
else
  echo "nginx test FAILED — check config."
  exit 1
fi
