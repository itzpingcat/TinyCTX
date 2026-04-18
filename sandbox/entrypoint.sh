#!/bin/sh
# sandbox/entrypoint.sh
# Runs as root. Applies firewall rules then starts the server.
# The server runs as root inside this locked-down container:
#   - cap_drop: ALL (NET_ADMIN only during this script, dropped after exec)
#   - read_only filesystem
#   - no host network access
#   - network-isolated from agent and LAN
set -e

iptables -I OUTPUT -d 10.0.0.0/8     -j DROP
iptables -I OUTPUT -d 172.16.0.0/12  -j DROP
iptables -I OUTPUT -d 192.168.0.0/16 -j DROP
iptables -I OUTPUT -d 100.64.0.0/10  -j DROP
iptables -I OUTPUT -d 169.254.0.0/16 -j DROP
# Note: do NOT block 127.0.0.0/8 — loopback is needed for healthcheck and IPC
iptables -I OUTPUT -o lo -j ACCEPT
echo "[firewall] egress rules installed"

exec python -m sandbox
