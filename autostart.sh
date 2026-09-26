#!/usr/bin/env bash
# Vermögenstracker als Hintergrunddienst (macOS LaunchAgent):
# startet beim Anmelden, startet nach Absturz neu, hält den Mac am Netzteil wach.
#   ./autostart.sh install     einrichten und starten
#   ./autostart.sh restart     neu starten (z. B. nach einem Update)
#   ./autostart.sh uninstall   entfernen
#   ./autostart.sh logs        letzte Log-Zeilen
set -uo pipefail
cd "$(dirname "$0")"
DIR="$(pwd)"
PORT="${PORT:-8000}"
LABEL="de.vermoegenstracker.app"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
LOG="$DIR/data/logs/app.log"
DOMAIN="gui/$(id -u)"
source ./scripts/tailscale.sh

xml() { sed -e 's/&/\&amp;/g' -e 's/</\&lt;/g' -e 's/>/\&gt;/g' <<<"$1"; }

install() {
  [[ -x .venv/bin/uvicorn ]] || { echo "Virtuelle Umgebung fehlt: python3 -m venv .venv && .venv/bin/pip install -r requirements.txt"; exit 1; }
  if lsof -ti ":$PORT" >/dev/null 2>&1 && ! launchctl print "$DOMAIN/$LABEL" >/dev/null 2>&1; then
    echo "Port $PORT ist belegt (läuft ./start.sh noch?). Erst beenden: kill \$(lsof -ti :$PORT)"; exit 1
  fi
  mkdir -p "$(dirname "$LOG")" "$HOME/Library/LaunchAgents"
  local users="${VT_ALLOWED_USERS:-}"
  if [[ -z "$users" ]] && ts_ready; then users="$(ts_login)"; fi
  # Beim Start: Sicherung, dann App. caffeinate -s verhindert den Ruhezustand nur am Netzteil.
  local py; py="$(venv_python)"
  local cmd="cd '$DIR' && '$py' -m app.backup --keep 30; exec /usr/bin/caffeinate -s '$py' -m uvicorn app.main:app --host 127.0.0.1 --port $PORT"
  cat > "$PLIST" <<PL
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key>
  <array><string>/bin/bash</string><string>-c</string><string>$(xml "$cmd")</string></array>
  <key>WorkingDirectory</key><string>$(xml "$DIR")</string>
  <key>EnvironmentVariables</key>
  <dict>
    <key>VT_ALLOWED_USERS</key><string>$(xml "$users")</string>
    <key>PATH</key><string>/usr/bin:/bin:/usr/sbin:/sbin</string>
    <key>PYTHONUNBUFFERED</key><string>1</string>
  </dict>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>ThrottleInterval</key><integer>10</integer>
  <key>ProcessType</key><string>Background</string>
  <key>StandardOutPath</key><string>$(xml "$LOG")</string>
  <key>StandardErrorPath</key><string>$(xml "$LOG")</string>
</dict>
</plist>
PL
  plutil -lint "$PLIST" >/dev/null || { echo "Fehler in $PLIST"; exit 1; }
  launchctl bootout "$DOMAIN/$LABEL" 2>/dev/null
  launchctl bootstrap "$DOMAIN" "$PLIST" || { echo "Konnte den Dienst nicht laden."; exit 1; }
  echo "✓ Autostart eingerichtet."
  if [[ -n "$users" ]]; then
    ts_serve "$PORT" && echo "✓ Handy-Freigabe: https://$(ts_hostname)   (nur für $users)"
  else
    echo "ℹ Tailscale nicht bereit ($(ts_problem)) – App läuft nur lokal. Später: ./autostart.sh install"
  fi
  sleep 3
  ./check.sh
}

case "${1:-}" in
  install)   install ;;
  restart)   launchctl kickstart -k "$DOMAIN/$LABEL" && sleep 3 && ./check.sh ;;
  uninstall) launchctl bootout "$DOMAIN/$LABEL" 2>/dev/null; rm -f "$PLIST"
             [[ -n "$TS" ]] && "$TS" serve reset >/dev/null 2>&1
             echo "✓ Autostart und Handy-Freigabe entfernt." ;;
  logs)      tail -n 60 "$LOG" ;;
  *)         sed -n '2,8p' "$0" | sed 's/^# \{0,1\}//' ;;
esac
