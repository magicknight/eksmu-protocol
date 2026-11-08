#!/usr/bin/env bash
# Run in the existing VM terminal. Read-only; do not paste private key contents.
set -u
printf '\n== Current account / system ==\n'
id -un
uname -sr
printf '\n== SSH / prospective app listeners ==\n'
ss -ltn '( sport = :22 or sport = :8765 )' 2>/dev/null || true
printf '\n== SSH service state ==\n'
systemctl is-active ssh.service 2>/dev/null || systemctl is-active sshd.service 2>/dev/null || true
printf '\n== Python / renderer ==\n'
for p in python3.13 python3.12 python3.11 python3; do
  if command -v "$p" >/dev/null 2>&1; then "$p" --version; fi
done
if command -v pandoc >/dev/null 2>&1; then pandoc --version | head -1; fi
printf '\n== User-service support ==\n'
systemctl --user is-system-running 2>/dev/null || true
loginctl show-user "$(id -un)" -p Linger 2>/dev/null || true
printf '\n== Home filesystem space / RAM ==\n'
df -h "$HOME"
free -h
