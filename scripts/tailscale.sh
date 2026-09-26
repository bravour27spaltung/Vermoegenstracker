# Hilfsfunktionen für Tailscale (wird von start.sh, autostart.sh und check.sh eingebunden).
# Robust: jede Funktion liefert einen Rückgabewert statt das aufrufende Skript abzubrechen.

TS="$(command -v tailscale 2>/dev/null || true)"
if [[ -z "$TS" && -x /Applications/Tailscale.app/Contents/MacOS/Tailscale ]]; then
  TS=/Applications/Tailscale.app/Contents/MacOS/Tailscale
fi

_ts_json() { [[ -n "$TS" ]] && "$TS" status --json 2>/dev/null; }

_ts_field() {  # $1 = Python-Ausdruck über d (geparstes JSON)
  _ts_json | /usr/bin/python3 -c "import json,sys
try:
    d=json.load(sys.stdin); print($1)
except Exception:
    sys.exit(1)" 2>/dev/null
}

ts_state()    { _ts_field 'd.get("BackendState","")'; }
ts_ready()    { [[ "$(ts_state)" == "Running" ]]; }
ts_login()    { _ts_field 'd["User"][str(d["Self"]["UserID"])]["LoginName"]'; }
ts_hostname() { _ts_field 'd["Self"]["DNSName"].rstrip(".")'; }

ts_problem() {
  if [[ -z "$TS" ]]; then echo "nicht installiert – App Store → Tailscale, dann in der App ‚CLI installieren‘";
  else
    case "$(ts_state)" in
      "")            echo "Tailscale-App läuft nicht – bitte öffnen";;
      NeedsLogin)    echo "nicht angemeldet – Tailscale-App öffnen und anmelden";;
      Stopped)       echo "ausgeschaltet – in der Tailscale-App ‚Connect‘ klicken";;
      *)             echo "Status $(ts_state)";;
    esac
  fi
}

ts_serve() {  # Freigabe im Tailnet (bleibt gespeichert, auch nach Neustart des Macs)
  local port="$1" out
  if out="$("$TS" serve --bg "$port" 2>&1)"; then return 0; fi
  echo "⚠ Freigabe fürs Handy fehlgeschlagen:"
  echo "$out" | sed 's/^/    /'
  echo "    Häufigste Ursache: HTTPS ist im Tailnet noch nicht aktiviert – den Link oben öffnen und bestätigen,"
  echo "    oder unter login.tailscale.com → DNS → HTTPS Certificates aktivieren. Danach erneut starten."
  return 1
}

# Python der virtuellen Umgebung, in der die Pakete wirklich installiert sind (aus uvicorns Startzeile).
# Robust gegen eine gemischte .venv, in der „python“ auf ein anderes Python zeigt.
venv_python() { head -1 .venv/bin/uvicorn 2>/dev/null | sed 's/^#!//'; }
