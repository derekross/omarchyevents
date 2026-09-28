#!/usr/bin/env bash
# Idempotent installer for the Omarchy Events Nostr bot.
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "$0")" && pwd)"
CONFIG_DIR="$HOME/.config/omarchyevents"
SYSTEMD_USER_DIR="$HOME/.config/systemd/user"
NSEC_FILE="$CONFIG_DIR/nsec"

cd "$PROJECT_DIR"

if [[ ! -d .venv ]]; then
    echo "==> creating venv"
    python3 -m venv .venv
fi
echo "==> installing python deps"
.venv/bin/pip install --upgrade pip --quiet
.venv/bin/pip install -r requirements.txt --quiet

if ! command -v nak >/dev/null 2>&1; then
    echo "ERROR: 'nak' not found in PATH. Install from https://github.com/fiatjaf/nak" >&2
    exit 1
fi

echo "==> preparing config dir"
mkdir -p "$CONFIG_DIR"
chmod 700 "$CONFIG_DIR"

if [[ ! -s "$NSEC_FILE" ]]; then
    echo "==> generating new nsec"
    nak key generate > "$NSEC_FILE"
    chmod 600 "$NSEC_FILE"
    echo "    nsec written to $NSEC_FILE (chmod 600)"
else
    echo "==> nsec already exists, leaving alone"
fi

NSEC=$(cat "$NSEC_FILE")
NPUB=$(nak encode npub "$(nak key public "$NSEC")")
echo "    npub: $NPUB"

echo "==> self-test"
.venv/bin/python omarchybot.py --self-test

echo "==> publishing kind 0 profile"
.venv/bin/python omarchybot.py publish-profile

echo "==> installing systemd user units"
mkdir -p "$SYSTEMD_USER_DIR"
cp -f systemd/omarchyevents.service "$SYSTEMD_USER_DIR/"
cp -f systemd/omarchyevents.timer "$SYSTEMD_USER_DIR/"
systemctl --user daemon-reload
systemctl --user enable --now omarchyevents.timer

echo
echo "==> done"
echo "    systemctl --user status omarchyevents.timer"
echo "    journalctl --user -u omarchyevents.service --since '1 hour ago'"
echo
echo "Inspect published events:"
echo "    nak req -k 31923 -k 31924 -a $(nak key public "$NSEC") --limit 10 wss://relay.ditto.pub"
echo
echo "View profile in a Nostr client:"
echo "    https://ditto.pub/$NPUB"
