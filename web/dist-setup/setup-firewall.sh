#!/bin/bash
# Gobby Firewall Setup for macOS
# Allows access only from localhost and Tailscale
#
# Usage: setup-firewall.sh [HTTP_PORT] [WS_PORT] [UI_PORT]

set -e

HTTP_PORT="${1:-60887}"
WS_PORT="${2:-60888}"
UI_PORT="${3:-60889}"

echo "Setting up Gobby firewall rules for ports $HTTP_PORT, $WS_PORT, $UI_PORT..."

# Create rules file
sudo mkdir -p /etc/pf.anchors
sudo tee /etc/pf.anchors/gobby > /dev/null << EOF
# Gobby Firewall Rules
# Ports: $HTTP_PORT (HTTP API), $WS_PORT (WebSocket), $UI_PORT (Web UI)

# Allow localhost access
pass in quick on lo0 proto tcp from any to any port {$HTTP_PORT, $WS_PORT, $UI_PORT}

# Allow Tailscale subnet (100.64.0.0/10 covers all Tailscale IPs)
pass in quick proto tcp from 100.64.0.0/10 to any port {$HTTP_PORT, $WS_PORT, $UI_PORT}

# Block all other access to Gobby ports
block in quick proto tcp from any to any port {$HTTP_PORT, $WS_PORT, $UI_PORT}
EOF

echo "Created /etc/pf.anchors/gobby"

# Add anchor to pf.conf if not already present
if ! grep -q 'anchor "gobby"' /etc/pf.conf 2>/dev/null; then
    echo "" | sudo tee -a /etc/pf.conf
    echo '# Gobby firewall rules' | sudo tee -a /etc/pf.conf
    echo 'anchor "gobby"' | sudo tee -a /etc/pf.conf
    echo 'load anchor "gobby" from "/etc/pf.anchors/gobby"' | sudo tee -a /etc/pf.conf
    echo "Added anchor to /etc/pf.conf"
else
    echo "Anchor already in /etc/pf.conf"
fi

# Load the rules now
if ! sudo pfctl -ef /etc/pf.conf 2>&1; then
    echo "pfctl -ef failed, trying without -e flag..." >&2
    if ! sudo pfctl -f /etc/pf.conf 2>&1; then
        echo "Error: Failed to load firewall rules" >&2
        exit 1
    fi
fi

echo ""
echo "Firewall configured! Rules will persist across reboots."
echo ""
echo "To verify:"
echo "  sudo pfctl -sr | grep -A5 'anchor \"gobby\"'"
echo ""
echo "To disable temporarily:"
echo "  sudo pfctl -d"
echo ""
echo "To re-enable:"
echo "  sudo pfctl -ef /etc/pf.conf"
