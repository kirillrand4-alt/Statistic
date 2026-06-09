#!/usr/bin/env bash
# Install a systemd service for the parser app (/opt/parser, python3 webui.py)
# and switch the currently-manual process over to it. Run once:
#   bash deploy/install_parser_service.sh
# This briefly (~1-2s) restarts the parser; it does NOT touch seostat or nginx.
set -euo pipefail

PARSER_DIR=/opt/parser
PY="$(command -v python3)"

if [ ! -f "${PARSER_DIR}/webui.py" ]; then
  echo "ERROR: ${PARSER_DIR}/webui.py not found."
  exit 1
fi

echo "Writing /etc/systemd/system/parser.service (python3 = ${PY})"
cat > /etc/systemd/system/parser.service <<EOF
[Unit]
Description=Parser Compressor web UI
After=network.target

[Service]
Type=simple
WorkingDirectory=${PARSER_DIR}
ExecStart=${PY} ${PARSER_DIR}/webui.py
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload

echo "Stopping the manually-started parser (python3 webui.py), if any..."
pkill -f webui.py 2>/dev/null || true
sleep 1

echo "Starting + enabling parser.service..."
systemctl enable --now parser
sleep 2

echo "----------------------------------------------"
echo -n "service active: "; systemctl is-active parser || true
echo "port 5000:"; ss -ltnp sport = :5000 || true
echo -n "local HTTP check: "; curl -m 5 -s -o /dev/null -w "%{http_code}\n" http://127.0.0.1:5000/ || true
echo "----------------------------------------------"
echo "If active and HTTP code is not 000 -> parser is now under systemd."
echo "Restart later:  systemctl restart parser"
echo "Logs:           journalctl -u parser -n 50 --no-pager"
echo "If something is wrong, see logs; you can always run it the old way again:"
echo "  cd /opt/parser && nohup python3 webui.py >> webui.log 2>&1 &"
