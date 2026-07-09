#!/usr/bin/env bash
# Подключить /obzvon в существующий nginx-конфиг parsercompressor.online.
# Безопасно: бэкап конфига, вставка deploy/nginx-obzvon.conf рядом с уже
# настроенным /stat, `nginx -t`, автоматический откат при ошибке.
# Идемпотентно: если /obzvon уже подключён — ничего не делает.
set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
SNIPPET="$REPO/deploy/nginx-obzvon.conf"

# конфиг ищем по уже вставленному блоку /stat (его добавлял wire_nginx.sh)
FILE="$(grep -RlE 'location /stat/' /etc/nginx/ 2>/dev/null | head -1 || true)"
if [ -z "$FILE" ]; then
  echo "ERROR: не найден nginx-конфиг с 'location /stat/' — сначала подключите /stat"
  echo "(bash deploy/wire_nginx.sh), либо пришлите вывод:  nginx -T | grep -n server_name"
  exit 1
fi
echo "Целевой nginx-конфиг: $FILE"

if grep -q 'location /obzvon/' "$FILE"; then
  echo "/obzvon уже подключён в $FILE — делать нечего."
  exit 0
fi

BACKUP="/root/nginx-site.bak.$(date +%s)"
cp "$FILE" "$BACKUP"
echo "Бэкап: $BACKUP"

# вставляем сниппет после строки-редиректа /stat (тот же server{}, тот же уровень)
sed -i "/location = \/stat {/r $SNIPPET" "$FILE"

if nginx -t; then
  systemctl reload nginx
  echo "DONE: /obzvon подключён."
  echo -n "Проверка (401 = всё верно, сервис просит пароль): HTTP "
  curl -s -o /dev/null -w '%{http_code}' https://parsercompressor.online/obzvon/kc || true
  echo
else
  echo "nginx -t НЕ прошёл — откатываю, сайт не тронут."
  cp "$BACKUP" "$FILE"
  nginx -t
  exit 1
fi
