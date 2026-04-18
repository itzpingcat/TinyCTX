#!/bin/sh
# sandbox/firewall-init.sh
#
# Runs as an init container (restartPolicy: no) with NET_ADMIN + NET_RAW.
# Installs iptables rules on the sandbox_egress network bridge that prevent
# the sandbox from reaching:
#   - RFC-1918 private ranges (your LAN)
#   - Tailscale CGNAT range (100.64.0.0/10)
#   - Link-local (169.254.0.0/16)
#   - The Docker daemon socket / host (172.17.0.1 etc. are covered by 172.16/12)
#
# Only internet-routable IPs are allowed out.
# The agent_sandbox IPC network (internal: true) is untouched — it has no
# internet egress by design, so these rules don't apply to it.
#
# This script exits 0 on success. Docker restartPolicy: no means it runs
# once at stack startup and never again.

set -e

echo "[firewall-init] installing egress rules..."

# Block RFC-1918
iptables -I FORWARD -s 0.0.0.0/0 -d 10.0.0.0/8      -j DROP
iptables -I FORWARD -s 0.0.0.0/0 -d 172.16.0.0/12   -j DROP
iptables -I FORWARD -s 0.0.0.0/0 -d 192.168.0.0/16  -j DROP

# Block Tailscale CGNAT range
iptables -I FORWARD -s 0.0.0.0/0 -d 100.64.0.0/10   -j DROP

# Block link-local
iptables -I FORWARD -s 0.0.0.0/0 -d 169.254.0.0/16  -j DROP

# Block loopback (shouldn't be routed anyway, but belt-and-suspenders)
iptables -I FORWARD -s 0.0.0.0/0 -d 127.0.0.0/8     -j DROP

# Same rules in the OUTPUT chain so local processes on this host can't
# route to those ranges via the bridge either
iptables -I OUTPUT -d 10.0.0.0/8      -j DROP
iptables -I OUTPUT -d 172.16.0.0/12   -j DROP
iptables -I OUTPUT -d 192.168.0.0/16  -j DROP
iptables -I OUTPUT -d 100.64.0.0/10   -j DROP
iptables -I OUTPUT -d 169.254.0.0/16  -j DROP

echo "[firewall-init] done."
