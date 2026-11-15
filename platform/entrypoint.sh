#!/bin/bash
# PID 1 bootstrap for the consolidated audio stack.
#
# Order matters: the WireGuard tunnel must exist before anything that talks to
# the VPS or the LAN, the partition tree must exist before any app starts (they
# all resolve their paths from it), and nginx must pass `nginx -t` before
# supervisord brings the apps up - a broken generated file should fail loudly
# here, not silently at the first request.
set -e

STACK=${STACK_DIR:-/srv/stack}
DATA=${HUB_DATA_DIR:-/data}
CONF=${HUB_CONF_DIR:-/etc/stack}
LOGS=/var/log/sup
mkdir -p "$DATA" "$DATA/users" "$DATA/vaults" "$DATA/pool/out" "$DATA/pool/cache" \
         "$DATA/pool/nzb-cache" "$DATA/pool/jobs" "$DATA/pool/blobs" "$CONF" "$LOGS" \
         /etc/supervisor/conf.d /etc/nginx/stack-locations /etc/nginx/conf.d \
         /var/www/hub /var/www/drivepod
# $DATA is a bind mount of a host directory (see docker-compose.yml: one mount,
# shared with the pre-hub trees so the media import can hardlink). The image's
# `chmod 0700 /data/vaults` therefore says nothing about what is here - the host's
# umask does, and a 0755 vaults tree on a multi-user host exports every cookie jar
# and chromium profile to the other users of that host. Re-assert it on every
# start: gen-conf creates each partition's vault directory with the ambient umask
# and migrate.py chmods it once, so this is the belt to those braces.
chmod 700 "$DATA/vaults" 2>/dev/null || echo "entrypoint: chmod $DATA/vaults failed (host mount permissions?)"

# ---- WireGuard ------------------------------------------------------
if [ -n "$WG_PRIVATE_KEY" ] && [ -n "$WG_PEER_PUBLIC_KEY" ] && [ -n "$WG_PEER_ENDPOINT" ]; then
  echo "wireguard: bringing up wg0"
  sed -e "s|__WG_PRIVATE_KEY__|${WG_PRIVATE_KEY}|g" \
      -e "s|__WG_ADDRESS__|${WG_ADDRESS:-10.99.0.235/32}|g" \
      -e "s|__WG_PEER_PUBLIC_KEY__|${WG_PEER_PUBLIC_KEY}|g" \
      -e "s|__WG_PEER_ALLOWEDIPS__|${WG_PEER_ALLOWEDIPS:-10.99.0.42/32}|g" \
      -e "s|__WG_PEER_ENDPOINT__|${WG_PEER_ENDPOINT}|g" \
      -e "s|__WG_PEER_PERSISTENT_KEEPALIVE__|${WG_PEER_PERSISTENT_KEEPALIVE:-25}|g" \
      /etc/wireguard/wg0.conf.template > /etc/wireguard/wg0.conf
  chmod 600 /etc/wireguard/wg0.conf
  wg-quick up wg0
  # Keep every locally-connected subnet on eth0: a full-tunnel route in table
  # 51820 would divert traffic to sibling containers and the bridge gateways
  # into the tunnel and drop it (this bit ace1's sibling database once).
  ip -4 route show table main | awk '/scope link/ {print $1, $3}' | \
    while read -r net dev; do
      ip route replace "${net}" dev "${dev}" table 51820 2>/dev/null || true
    done
  wg show
else
  echo "wireguard: WG_* env unset, running without the tunnel"
fi

# ---- registry -------------------------------------------------------
# partitions.json is DATA, not config: `hub partition add` writes it at
# runtime and a container recreate must not lose partitions, so it lives
# on the volume. The generated supervisor/nginx files under $CONF are
# throwaway - they are regenerated from the registry at every start.
export HUB_REGISTRY="$DATA/partitions.json"
# Hub identity's anonymous broadcast allowance. HUB_RADIO_PASS is the canonical
# name (that is what hubauth reads and what .env.example advertises); RADIO_PASS
# is the legacy name the pre-hub listen container used, accepted here so an old
# env file still works. Blindly assigning from RADIO_PASS - as this line used to
# do - clobbered a HUB_RADIO_PASS the compose env_file had already supplied with
# an unset RADIO_PASS, so every ?pass= stream request 401'd behind a gateway
# that looked correctly configured.
if [ -z "${HUB_RADIO_PASS:-}" ] && [ -n "${RADIO_PASS:-}" ]; then
  export HUB_RADIO_PASS="$RADIO_PASS"
fi
if [ ! -f "$HUB_REGISTRY" ]; then
  echo "registry: seeding default partition '${HUB_DEFAULT_SLUG:-owner}'"
  python3 "$STACK/platform/seed-registry.py" "${HUB_DEFAULT_SLUG:-owner}"
fi

# ---- SPA shells: keep the pristine built index.html as the template ----
if [ ! -f /var/www/drivepod/index.html.dist ] && [ -f /var/www/drivepod/index.html ]; then
  cp /var/www/drivepod/index.html /var/www/drivepod/index.html.dist
fi

# ---- one-shot migration from the pre-hub layout ---------------------
if [ ! -f "$DATA/.migrated" ]; then
  python3 "$STACK/platform/migrate.py" || echo "migrate: reported problems (continuing)"
  : > "$DATA/.migrated"
fi

# ---- generated config ----------------------------------------------
python3 "$STACK/platform/gen-conf.py"
nginx -t

# ---- identity store + first owner ----------------------------------
# prisma db push per partition is the apps' own schema step, done by the
# one-shot launcher (platform/run-service.py) so a failing schema for one
# partition cannot stop the others.
python3 "$STACK/platform/hub.py" bootstrap

echo "entrypoint: handing over to supervisord"
exec supervisord -c /etc/supervisor/supervisord.conf
