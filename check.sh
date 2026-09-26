#!/usr/bin/env bash
# Prüft Schritt für Schritt, warum die App (nicht) erreichbar ist.
set -uo pipefail
cd "$(dirname "$0")"
PORT="${PORT:-8000}"
LABEL="de.vermoegenstracker.app"
source ./scripts/tailscale.sh
ok()   { echo "  ✓ $*"; }
bad()  { echo "  ✗ $*"; }
info() { echo "  · $*"; }

echo "Vermögenstracker – Status"
PY="$(venv_python)"
if [[ -n "$PY" ]] && "$PY" -c "import fastapi, sqlalchemy, uvicorn" 2>/dev/null; then ok "Python-Umgebung: $("$PY" --version 2>&1)"
else bad "Python-Umgebung defekt → rm -rf .venv && python3.12 -m venv .venv && .venv/bin/pip install -r requirements.txt"; fi
if [[ "$(readlink .venv/bin/python 2>/dev/null)" != "$(basename "${PY:-x}")" && -n "$PY" ]] && \
   ! .venv/bin/python -c "import fastapi" 2>/dev/null; then
  info ".venv/bin/python zeigt auf ein anderes Python als die Pakete – für Befehle ./.venv/bin/$(basename "$PY") nutzen"
fi

if launchctl print "gui/$(id -u)/$LABEL" >/dev/null 2>&1; then
  st="$(launchctl print "gui/$(id -u)/$LABEL" | awk -F'= ' '/^\tstate/{print $2} /last exit code/{print "Exit " $2}' | paste -sd ' ' -)"
  ok "Autostart-Dienst eingerichtet ($st)"
else info "kein Autostart (manuell mit ./start.sh oder dauerhaft mit ./autostart.sh install)"; fi

if lsof -ti ":$PORT" >/dev/null 2>&1; then ok "Programm lauscht auf Port $PORT"
else bad "nichts läuft auf Port $PORT → ./start.sh oder ./autostart.sh install"; fi

code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 "http://127.0.0.1:$PORT/dashboard" || true)"
case "$code" in
  200) ok "Mac: http://127.0.0.1:$PORT antwortet" ;;
  000) bad "Mac: keine Antwort von http://127.0.0.1:$PORT" ;;
  *)   bad "Mac: Fehler $code – Log ansehen: ./autostart.sh logs" ;;
esac
if [[ "$code" != "200" && -f data/logs/app.log ]]; then
  echo "    Letzte Log-Zeilen:"; tail -n 8 data/logs/app.log | sed 's/^/      /'
fi

if ts_ready; then
  ok "Tailscale verbunden als $(ts_login)"
  if "$TS" serve status 2>/dev/null | grep -q "$PORT"; then ok "Handy-Freigabe aktiv: https://$(ts_hostname)"
  else bad "Handy-Freigabe fehlt → $TS serve --bg $PORT"; fi
else
  bad "Tailscale: $(ts_problem)"
fi

if pmset -g 2>/dev/null | grep -qE '^ *sleep +0'; then ok "Mac schläft am Netzteil nicht ein"
else info "Mac kann einschlafen – dann ist die App fürs Handy weg (siehe HANDY.md → Dauerbetrieb)"; fi
