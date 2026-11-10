#!/bin/bash
set -e

# ── WireGuard tunnel ───────────────────────────────────────────────
if [ -n "$WG_PRIVATE_KEY" ] && [ -n "$WG_PEER_PUBLIC_KEY" ] && [ -n "$WG_PEER_ENDPOINT" ]; then
  echo "Configuring WireGuard tunnel..."

  sed \
    -e "s|__WG_PRIVATE_KEY__|${WG_PRIVATE_KEY}|g" \
    -e "s|__WG_ADDRESS__|${WG_ADDRESS:-10.99.0.235/32}|g" \
    -e "s|__WG_PEER_PUBLIC_KEY__|${WG_PEER_PUBLIC_KEY}|g" \
    -e "s|__WG_PEER_ALLOWEDIPS__|${WG_PEER_ALLOWEDIPS:-10.99.0.0/20}|g" \
    -e "s|__WG_PEER_ENDPOINT__|${WG_PEER_ENDPOINT}|g" \
    -e "s|__WG_PEER_PERSISTENT_KEEPALIVE__|${WG_PEER_PERSISTENT_KEEPALIVE:-25}|g" \
    /etc/wireguard/wg0.conf.template > /etc/wireguard/wg0.conf

  wg-quick up wg0
  # Keep the docker bridge subnet on eth0: full-tunnel WireGuard (table
  # 51820) would otherwise route replies to the bridge gateway
  # (docker-proxy's local end for host-mapped ports) via wg0 and drop them.
  BRIDGE_NET=$(ip -4 route show table main | awk '/scope link/ {print $1; exit}')
  [ -n "$BRIDGE_NET" ] && ip route replace ${BRIDGE_NET} dev eth0 table 51820 2>/dev/null || true

  echo "WireGuard tunnel established"
  wg show
else
  echo "WireGuard: skipping (missing env vars)"
fi

# ── Publish the app links ──────────────────────────────────────────
# index.html ships with a __DOMAIN__ placeholder so no public domain is baked
# into the image or the repo. SITE_DOMAIN (from .env) fills it at start.
sed -i "s|__DOMAIN__|${SITE_DOMAIN:-example.com}|g" /usr/share/nginx/html/index.html
echo "Site links point at ${SITE_DOMAIN:-example.com}"

# ── Start nginx ────────────────────────────────────────────────────
echo "Starting nginx..."
exec nginx -g "daemon off;"
