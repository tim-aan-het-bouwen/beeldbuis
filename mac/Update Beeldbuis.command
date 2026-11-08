#!/bin/bash
# Beeldbuis update: place the new iptv.py + assets, restart the background service,
# check that the NEW version is running, and keep this window open with a clear result.
HERE="$(cd "$(dirname "$0")" && pwd)"
APPDIR="$HOME/Library/Application Support/IPTVPlayer"
LOG="$APPDIR/update.log"
PORT=8765

finish() {  # $1 = 0 (ok) / 1 (fout)
  echo
  if [ "$1" = "0" ]; then
    printf '\033[1;32m  KLAAR: de update is gelukt.\033[0m\n'
  else
    printf '\033[1;31m  FOUT: de update is niet gelukt.\033[0m\n'
    echo "  Maak een screenshot van dit venster en stuur het naar Claude."
  fi
  echo
  echo "  Druk op een toets om dit venster te sluiten."
  read -n 1 -s
  osascript -e 'tell application "Terminal" to close (every window whose name contains "Update Beeldbuis")' >/dev/null 2>&1 &
  exit "$1"
}

running_version() {
  curl -s -m 1 "http://127.0.0.1:$PORT/api/ping" | sed -n 's/.*"v": *\([0-9][0-9]*\).*/\1/p'
}
stamp() { date '+%Y-%m-%d %H:%M:%S'; }

clear
echo "  Beeldbuis wordt bijgewerkt..."
echo

if [ ! -f "$APPDIR/iptv.py" ]; then
  echo "  Beeldbuis is nog niet geinstalleerd op deze Mac."
  finish 1
fi

NEW=$(sed -n 's/^VERSION *= *\([0-9][0-9]*\).*/\1/p' "$HERE/iptv.py" | head -1)
OLD=$(sed -n 's/^VERSION *= *\([0-9][0-9]*\).*/\1/p' "$APPDIR/iptv.py" | head -1)
echo "  Versie nu: ${OLD:-?}   nieuwe versie: ${NEW:-?}"
echo "$(stamp) start update v${OLD:-?} -> v${NEW:-?} from $HERE" >> "$LOG"

# Keep the previous version so we can roll back if the new one doesn't start.
cp "$APPDIR/iptv.py" "$APPDIR/iptv.py.vorige" 2>/dev/null

if ! cp "$HERE/iptv.py" "$APPDIR/iptv.py"; then
  echo "  Kon iptv.py niet kopieren."
  echo "$(stamp) FAIL copy iptv.py" >> "$LOG"
  finish 1
fi
mkdir -p "$APPDIR/assets"
cp "$HERE/assets/"* "$APPDIR/assets/" 2>/dev/null
xattr -dr com.apple.quarantine "$APPDIR/iptv.py" "$APPDIR/assets" 2>/dev/null
echo "  [ok] Bestanden geplaatst"

# Personal files from the work folder (the folder that holds the "Beeldbuis update N" folders):
# sleutels.txt (API keys), smaak.json (taste profiles, shipped in this package) and the newest Letterboxd export.
WORK="$(dirname "$HERE")"
CONF="$APPDIR"
mkdir -p "$CONF"
if [ -f "$WORK/sleutels.txt" ]; then
  cp "$WORK/sleutels.txt" "$CONF/sleutels.txt" && chmod 600 "$CONF/sleutels.txt" && echo "  [ok] sleutels.txt overgenomen"
else
  echo "  [ -] geen sleutels.txt in de werkmap (posters en aanbevelingen blijven dan beperkt)"
fi
if [ -f "$HERE/smaak.json" ]; then
  cp "$HERE/smaak.json" "$CONF/smaak.json" && echo "  [ok] smaakprofielen geplaatst"
fi
LBZIP="$(ls -t "$WORK"/letterboxd*.zip 2>/dev/null | head -1)"
if [ -n "$LBZIP" ]; then
  cp "$LBZIP" "$CONF/letterboxd.zip" && echo "  [ok] Letterboxd-export overgenomen ($(basename "$LBZIP"))"
fi

restart() {
  launchctl kickstart -k "gui/$(id -u)/nl.tim.iptvplayer" 2>/dev/null || { pkill -f "iptv.py"; sleep 2; }
  for i in $(seq 1 60); do
    [ "$(running_version)" = "$1" ] && return 0
    sleep 0.25
  done
  return 1
}

echo "  ... service herstarten (even geduld)"
if restart "$NEW"; then
  echo "  [ok] Beeldbuis draait nu met versie $NEW"
  echo "$(stamp) OK running v$NEW" >> "$LOG"
  echo
  echo "  Sluit de Beeldbuis-app en open hem opnieuw om de nieuwe versie te zien."
  finish 0
fi

# New version did not come up: put the previous one back.
echo "  [x] De nieuwe versie start niet."
echo "$(stamp) FAIL v$NEW did not start" >> "$LOG"
if [ -f "$APPDIR/iptv.py.vorige" ] && [ -n "$OLD" ]; then
  cp "$APPDIR/iptv.py.vorige" "$APPDIR/iptv.py"
  if restart "$OLD"; then
    echo "  Vorige versie ($OLD) teruggezet: Beeldbuis blijft gewoon werken."
    echo "$(stamp) rolled back to v$OLD" >> "$LOG"
  fi
fi
echo "  Details staan in: $APPDIR/server.log"
finish 1
