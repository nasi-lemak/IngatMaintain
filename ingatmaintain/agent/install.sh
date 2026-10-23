#!/bin/sh
# Installs the IngatMaintain monitoring agent on Linux (Ubuntu, Debian, Raspberry Pi OS).
#
#   curl -fsSLO https://ingatmaintain.example.com/api/agent/install.sh
#   sudo sh install.sh --server https://ingatmaintain.example.com --key <enrollment key> [--with-smartmontools]
#
#   sudo sh install.sh --uninstall
#
# 1. Enrolls this computer using a one-time enrollment key.
# 2. Saves the device token in /etc/ingatmaintain/agent.json (root only).
# 3. Runs the agent every 5 minutes with a systemd timer (or cron where there's no systemd).
#
# --with-smartmontools installs smartmontools (apt) so SSD / NVMe health can be read.
# Raspberry Pis that only have an SD card don't need it.
# agent.py is downloaded from the server unless it sits next to this script.
set -eu

SERVER=""
KEY=""
UNINSTALL=0
SMART=0
APP_DIR=/opt/ingatmaintain
CONFIG_DIR=/etc/ingatmaintain
CONFIG=$CONFIG_DIR/agent.json

while [ $# -gt 0 ]; do
    case "$1" in
        --server) SERVER="${2%/}"; shift 2 ;;
        --key) KEY="$2"; shift 2 ;;
        --with-smartmontools) SMART=1; shift ;;
        --uninstall) UNINSTALL=1; shift ;;
        *) echo "Unknown option: $1" >&2; exit 2 ;;
    esac
done

if [ "$(id -u)" -ne 0 ]; then
    echo "Run with sudo." >&2
    exit 1
fi

if [ "$UNINSTALL" -eq 1 ]; then
    if command -v systemctl >/dev/null 2>&1; then
        systemctl disable --now ingatmaintain-agent.timer 2>/dev/null || true
        rm -f /etc/systemd/system/ingatmaintain-agent.service /etc/systemd/system/ingatmaintain-agent.timer
        systemctl daemon-reload 2>/dev/null || true
    fi
    rm -f /etc/cron.d/ingatmaintain-agent
    rm -rf "$APP_DIR" "$CONFIG_DIR"
    echo "IngatMaintain agent removed. Also click 'Remove agent' for this computer in IngatMaintain."
    exit 0
fi

if [ -z "$SERVER" ] || [ -z "$KEY" ]; then
    echo "Usage: sudo sh install.sh --server <https://...> --key <enrollment key> [--with-smartmontools]" >&2
    exit 2
fi
case "$SERVER" in
    https://*|http://localhost*|http://127.0.0.1*) ;;
    *) echo "The server address must start with https:// so the device token is never sent unencrypted." >&2; exit 1 ;;
esac
PYTHON=$(command -v python3 || true)
if [ -z "$PYTHON" ]; then
    echo "python3 is required (it's included in Raspberry Pi OS, Ubuntu and Debian)." >&2
    exit 1
fi

mkdir -p "$APP_DIR"
HERE=$(cd "$(dirname "$0")" && pwd)
if [ -f "$HERE/agent.py" ] && [ "$HERE" != "$APP_DIR" ]; then
    cp "$HERE/agent.py" "$APP_DIR/agent.py"
elif [ ! -f "$HERE/agent.py" ]; then
    if command -v curl >/dev/null 2>&1; then
        curl -fsSL "$SERVER/api/agent/agent.py" -o "$APP_DIR/agent.py"
    else
        wget -q "$SERVER/api/agent/agent.py" -O "$APP_DIR/agent.py"
    fi
fi
chmod 755 "$APP_DIR/agent.py"

if [ "$SMART" -eq 1 ] && ! command -v smartctl >/dev/null 2>&1; then
    if command -v apt-get >/dev/null 2>&1; then
        apt-get install -y smartmontools >/dev/null && echo "Installed smartmontools."
    else
        echo "Couldn't install smartmontools automatically - install it with your package manager." >&2
    fi
fi

"$PYTHON" "$APP_DIR/agent.py" --enroll --server "$SERVER" --key "$KEY" --config "$CONFIG"
chmod 700 "$CONFIG_DIR"
MINUTES=$("$PYTHON" -c "import json; print(int(json.load(open('$CONFIG')).get('interval_minutes') or 5))")

if command -v systemctl >/dev/null 2>&1 && [ -d /run/systemd/system ]; then
    cat > /etc/systemd/system/ingatmaintain-agent.service <<EOF
[Unit]
Description=IngatMaintain monitoring agent check-in
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
ExecStart=$PYTHON $APP_DIR/agent.py --config $CONFIG
EOF
    cat > /etc/systemd/system/ingatmaintain-agent.timer <<EOF
[Unit]
Description=Run the IngatMaintain agent every $MINUTES minutes

[Timer]
OnBootSec=1min
OnUnitActiveSec=${MINUTES}min
RandomizedDelaySec=30

[Install]
WantedBy=timers.target
EOF
    systemctl daemon-reload
    systemctl enable --now ingatmaintain-agent.timer >/dev/null
    systemctl start ingatmaintain-agent.service || true
    echo "Scheduled with systemd (ingatmaintain-agent.timer) every $MINUTES minutes."
else
    echo "*/$MINUTES * * * * root $PYTHON $APP_DIR/agent.py --config $CONFIG >/dev/null 2>&1" > /etc/cron.d/ingatmaintain-agent
    chmod 644 /etc/cron.d/ingatmaintain-agent
    "$PYTHON" "$APP_DIR/agent.py" --config "$CONFIG" || true
    echo "Scheduled with cron (/etc/cron.d/ingatmaintain-agent) every $MINUTES minutes."
fi
echo "Done - check the Fleet page in IngatMaintain."
