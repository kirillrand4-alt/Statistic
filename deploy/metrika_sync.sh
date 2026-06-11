#!/usr/bin/env bash
# Run the Metrica Logs API sync in the background so it keeps running after you
# close the console / log out of SSH (the process is reparented to init and
# nohup ignores SIGHUP). Writes a log + PID file; manage it with the same script.
#
#   bash deploy/metrika_sync.sh start [extra args...]   # launch detached
#   bash deploy/metrika_sync.sh status                  # running? PID? log path
#   bash deploy/metrika_sync.sh log                     # tail -f the log
#   bash deploy/metrika_sync.sh stop                    # stop it
#
# "start" with no extra args runs:  metrika_logs.py --sync-all --force
# (visits + hits, last 365 days). Widen the period by passing flags through:
#   bash deploy/metrika_sync.sh start --force --from 2025-06-01 --to 2026-06-10
set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
PY="${METRIKA_PY:-$REPO/.venv/bin/python}"
[ -x "$PY" ] || PY="$(command -v python3 || echo python3)"
SCRIPT="${METRIKA_SCRIPT:-$REPO/scripts/metrika_logs.py}"
LOG="${METRIKA_LOG:-/tmp/metrika_sync.log}"
PIDFILE="${METRIKA_PID:-/tmp/metrika_sync.pid}"

cmd="${1:-start}"
[ $# -gt 0 ] && shift || true

_running() { [ -f "$PIDFILE" ] && kill -0 "$(cat "$PIDFILE" 2>/dev/null)" 2>/dev/null; }

case "$cmd" in
  start)
    if _running; then
      echo "Уже выполняется (PID $(cat "$PIDFILE")). Лог: $LOG"
      exit 0
    fi
    args=("$@")
    [ ${#args[@]} -eq 0 ] && args=(--force)
    echo "Запускаю: $PY $SCRIPT --sync-all ${args[*]}"
    # nohup + background + the launcher shell exits => process is reparented to
    # init and survives the console closing. Output goes to $LOG (not a tty).
    nohup "$PY" "$SCRIPT" --sync-all "${args[@]}" >"$LOG" 2>&1 &
    echo $! > "$PIDFILE"
    disown 2>/dev/null || true
    sleep 1
    if _running; then
      echo "OK. В фоне, PID $(cat "$PIDFILE"). Лог: $LOG"
      echo "  следить:  bash deploy/metrika_sync.sh log"
      echo "  стоп:     bash deploy/metrika_sync.sh stop"
    else
      echo "Процесс сразу завершился — смотрите $LOG:"
      tail -n 20 "$LOG" 2>/dev/null || true
      exit 1
    fi
    ;;
  status)
    if _running; then
      echo "Выполняется (PID $(cat "$PIDFILE")). Лог: $LOG"
    else
      echo "Не запущено. Последний лог: $LOG"
    fi
    ;;
  log)
    exec tail -n 200 -f "$LOG"
    ;;
  stop)
    if _running; then
      pid="$(cat "$PIDFILE")"
      kill "$pid" 2>/dev/null || true
      echo "Остановлено (PID $pid)."
    else
      echo "Не запущено."
    fi
    rm -f "$PIDFILE"
    ;;
  *)
    echo "Использование: $0 {start [args]|status|log|stop}"
    exit 1
    ;;
esac
