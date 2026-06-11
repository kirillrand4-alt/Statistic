#!/usr/bin/env bash
# Raise nginx's upload limit so large visit archives (.tsv/.gz/.zip) don't 413.
#
# Inserts `client_max_body_size <SIZE>;` into the app's location (the block that
# proxies to 127.0.0.1:8011); a location-level cap overrides any smaller
# server-level one, so this works for both the subdomain and the /stat subpath
# setup. Backs up the file, tests with `nginx -t`, rolls back on failure.
# Idempotent. Run as root.
#
# Usage:  bash deploy/raise_upload_limit.sh [SIZE]      # default 1024M
set -euo pipefail

SIZE="${1:-1024M}"
NGINX_DIR="${NGINX_DIR:-/etc/nginx}"

FILE="$(grep -RlE 'proxy_pass[[:space:]]+http://127\.0\.0\.1:8011' "$NGINX_DIR" 2>/dev/null | head -1 || true)"
if [ -z "$FILE" ]; then
  echo "ERROR: в $NGINX_DIR не найден конфиг с proxy_pass на 127.0.0.1:8011."
  echo "Проверьте вручную:  grep -Rl 8011 $NGINX_DIR"
  exit 1
fi
echo "nginx config: $FILE"

if grep -qE "client_max_body_size[[:space:]]+${SIZE};" "$FILE"; then
  echo "Лимит ${SIZE} уже задан — нечего менять."
  exit 0
fi

BACKUP="/root/nginx-upload-limit.bak.$(date +%s)"
cp "$FILE" "$BACKUP"
echo "backup: $BACKUP"

sed -i -E "/proxy_pass[[:space:]]+http:\/\/127\.0\.0\.1:8011/i\\    client_max_body_size ${SIZE};" "$FILE"
echo "вставил client_max_body_size ${SIZE}"

if nginx -t; then
  systemctl reload nginx
  echo "DONE: client_max_body_size ${SIZE}, nginx перезагружен."
else
  echo "nginx -t FAILED — откатываю, конфиг возвращён как был ($BACKUP)."
  cp "$BACKUP" "$FILE"
  nginx -t || true
  exit 1
fi
