#!/bin/sh
# sandbox/entrypoint.sh
# Runs as root. Applies firewall rules then starts the server.
# The server runs as root inside this locked-down container:
#   - cap_drop: ALL (NET_ADMIN only during this script, dropped after exec)
#   - read_only filesystem
#   - no host network access
#   - network-isolated from agent and LAN
set -e

# 1. Always allow loopback and established/related (replies to inbound connections).
iptables -A OUTPUT -o lo                                        -j ACCEPT
iptables -A OUTPUT -m conntrack --ctstate ESTABLISHED,RELATED   -j ACCEPT
# 2. Block LAN, Tailscale CGNAT, and link-local egress.
iptables -A OUTPUT -d 10.0.0.0/8     -j DROP
iptables -A OUTPUT -d 172.16.0.0/12  -j DROP
iptables -A OUTPUT -d 192.168.0.0/16 -j DROP
iptables -A OUTPUT -d 100.64.0.0/10  -j DROP
iptables -A OUTPUT -d 169.254.0.0/16 -j DROP
echo "[firewall] egress rules installed"

exec python -m sandbox
