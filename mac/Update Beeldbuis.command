#!/bin/bash
# Beeldbuis update: place the new iptv.py + assets and restart the background service.
HERE="$(cd "$(dirname "$0")" && pwd)"
APPDIR="$HOME/Library/Application Support/IPTVPlayer"
if [ ! -f "$APPDIR/iptv.py" ]; then echo "Beeldbuis (IPTV Player) is nog niet geïnstalleerd; gebruik eerst 'Installeer IPTV Player.command'."; sleep 8; exit 1; fi
cp "$HERE/iptv.py" "$APPDIR/iptv.py"
mkdir -p "$APPDIR/assets"
cp "$HERE/assets/"* "$APPDIR/assets/"
xattr -dr com.apple.quarantine "$APPDIR/iptv.py" "$APPDIR/assets" 2>/dev/null
launchctl kickstart -k "gui/$(id -u)/nl.tim.iptvplayer" 2>/dev/null || { pkill -f "iptv.py"; sleep 2; }
for i in $(seq 1 40); do curl -s -m 1 http://127.0.0.1:8765/api/ping >/dev/null && break; sleep 0.25; done
if curl -s -m 1 http://127.0.0.1:8765/api/ping >/dev/null; then echo "Update geïnstalleerd. Open Beeldbuis (IPTV Player) opnieuw."; else echo "De service start niet; zie $APPDIR/server.log"; fi
sleep 6
osascript -e 'tell application "Terminal" to close (every window whose name contains "Update Beeldbuis")' >/dev/null 2>&1 &
exit 0
