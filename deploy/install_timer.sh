#!/usr/bin/env bash
# Installa il timer systemd utente. Da lanciare una volta, quando i login sono fatti.
set -euo pipefail
dest="$HOME/.config/systemd/user"
mkdir -p "$dest"
cp "$(dirname "$0")/spotify-ytm-sync.service" "$(dirname "$0")/spotify-ytm-sync.timer" "$dest/"
systemctl --user daemon-reload
systemctl --user enable --now spotify-ytm-sync.timer
# Fa girare il timer anche senza aver fatto login nella sessione grafica
loginctl enable-linger "$USER"
systemctl --user list-timers spotify-ytm-sync.timer
