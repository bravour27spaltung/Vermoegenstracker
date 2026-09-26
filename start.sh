#!/usr/bin/env bash
# Startet den Vermögenstracker im Vordergrund (zum Ausprobieren; dauerhaft: ./autostart.sh install).
#   ./start.sh          → Sicherung, App auf 127.0.0.1:8000, Freigabe fürs Handy über Tailscale
#   ./start.sh --local  → nur auf diesem Mac
# Beenden: Strg+C.
# Tailscale ist optional: Fehlt es oder ist es nicht angemeldet, startet die App trotzdem lokal.
set -uo pipefail
cd "$(dirname "$0")"
PORT="${PORT:-8000}"
LABEL="de.vermoegenstracker.app"

if launchctl print "gui/$(id -u)/$LABEL" >/dev/null 2>&1; then
  echo "Die App läuft bereits als Hintergrunddienst (Autostart)."
  echo "Mac: http://127.0.0.1:$PORT   ·   Status: ./check.sh   ·   Autostart beenden: ./autostart.sh uninstall"
  exit 0
fi
if lsof -ti ":$PORT" >/dev/null 2>&1; then
  echo "Port $PORT ist belegt – eine alte Instanz läuft noch. Beenden mit:"
  echo "  kill \$(lsof -ti :$PORT)"
  exit 1
fi
if [[ ! -x .venv/bin/uvicorn ]]; then
  echo "Virtuelle Umgebung fehlt. Einmalig einrichten:"
  echo "  python3 -m venv .venv && .venv/bin/pip install -r requirements.txt"
  exit 1
fi

source ./scripts/tailscale.sh
"$(venv_python)" -m app.backup --keep 30 || echo "⚠ Sicherung fehlgeschlagen – App startet trotzdem."

# ---- Tailscale (optional)
PHONE_URL=""
if [[ "${1:-}" != "--local" ]]; then
  if ts_ready; then
    [[ -z "${VT_ALLOWED_USERS:-}" ]] && VT_ALLOWED_USERS="$(ts_login)"
    export VT_ALLOWED_USERS
    if ts_serve "$PORT"; then
      PHONE_URL="https://$(ts_hostname)"
    fi
  else
    echo "ℹ Tailscale nicht bereit ($(ts_problem)) – starte nur lokal."
  fi
fi

# ---- App starten und prüfen, ob sie antwortet
/usr/bin/caffeinate -i .venv/bin/uvicorn app.main:app --host 127.0.0.1 --port "$PORT" &
PID=$!
trap 'kill $PID 2>/dev/null; wait $PID 2>/dev/null; echo; echo "App beendet."; exit 0' INT TERM
for _ in $(seq 1 30); do
  code="$(curl -s -o /dev/null -w '%{http_code}' "http://127.0.0.1:$PORT/dashboard" || true)"
  [[ "$code" == "200" ]] && break
  kill -0 $PID 2>/dev/null || { echo "✗ Die App ist beim Start abgestürzt – Fehlermeldung siehe oben."; exit 1; }
  sleep 0.5
done
echo
echo "✓ App läuft."
echo "  Mac:   http://127.0.0.1:$PORT"
[[ -n "$PHONE_URL" ]] && echo "  Handy: $PHONE_URL   (Tailscale auf dem iPhone einschalten)"
[[ -n "${VT_ALLOWED_USERS:-}" ]] && echo "  Freigegeben für: $VT_ALLOWED_USERS"
echo "  Beenden mit Strg+C."
wait $PID
