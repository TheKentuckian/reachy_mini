#!/bin/bash
# Enable the central signaling relay. Run this ON THE ROBOT.
#
# Writes REACHY_CENTRAL_RELAY=1 to /etc/reachy-mini/relay.env (read by
# launcher.sh) and restarts the daemon. This keeps the git checkout clean —
# never edit launcher.sh in place on the robot.
set -euo pipefail

RELAY_ENV_FILE="/etc/reachy-mini/relay.env"

sudo mkdir -p "$(dirname "$RELAY_ENV_FILE")"
printf 'REACHY_CENTRAL_RELAY=1\n' | sudo tee "$RELAY_ENV_FILE" > /dev/null
echo "Wrote REACHY_CENTRAL_RELAY=1 to $RELAY_ENV_FILE"

echo "Restarting reachy-mini-daemon..."
sudo systemctl restart reachy-mini-daemon

cat <<'EOF'
Central signaling relay is now ON.

REMINDERS:
  * A daemon restart does NOT cascade-restart the app. If an app is
    running (e.g. reachy-app-autostart), restart it too or its SDK
    websocket stays dead:
        sudo systemctl restart reachy-app-autostart
  * Relay ON exposes the camera and bidirectional audio to the internet
    via the HuggingFace signaling relay whenever an HF token is cached.
    Turn it back OFF after testing:
        scripts/relay_off.sh
EOF
