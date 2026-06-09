#!/usr/bin/env bash
# One-shot setup: venv, deps, .env, systemd service. Run from the repo root:
#   bash deploy/setup.sh
set -euo pipefail
cd "$(dirname "$0")/.."
ROOT="$(pwd)"

echo "[1/5] Python venv + dependencies"
python3 -m venv .venv
.venv/bin/pip install --upgrade pip >/dev/null
.venv/bin/pip install -r requirements.txt >/dev/null
echo "      ok"

echo "[2/5] .env"
if [ ! -f .env ]; then
  cp .env.example .env
  SK="$(.venv/bin/python -c 'import secrets; print(secrets.token_hex(32))')"
  sed -i "s|^SECRET_KEY=.*|SECRET_KEY=${SK}|" .env
  sed -i "s|^ROOT_PATH=.*|ROOT_PATH=/stat|" .env
  echo "      created .env (ROOT_PATH=/stat, random SECRET_KEY)"
else
  echo "      .env already exists, left as-is"
fi
mkdir -p secrets data

echo "[3/5] systemd unit -> /etc/systemd/system/seostat.service"
sed "s|/opt/seostat|${ROOT}|g" deploy/seostat.service > /etc/systemd/system/seostat.service
systemctl daemon-reload
systemctl enable --now seostat || true

echo "[4/5] service status"
sleep 2
systemctl is-active seostat || true

echo "[5/5] health check"
curl -s http://127.0.0.1:8011/stat/health || true
echo
echo "----------------------------------------------------------------"
echo "Service installed. If health returned {\"status\":\"ok\"} you are good."
echo "Next:"
echo "  1) put your GSC service-account key at: ${ROOT}/secrets/gsc-sa.json"
echo "  2) set GSC_SITE_URL in ${ROOT}/.env"
echo "  3) systemctl restart seostat"
echo "Logs: journalctl -u seostat -n 50 --no-pager"
