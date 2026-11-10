# listen

Static hub page linking the self-hosted audio projects (DrivePod, FrogRock,
GrabbyTube). One nginx container; no application code.

## How it works

- `index.html` carries a `__DOMAIN__` placeholder for every service link and
  for the footer, so **no public hostname is baked into the image or the repo**.
- `entrypoint.sh` fills it at container start from `SITE_DOMAIN` (default
  `example.com`), then hands off to `exec nginx -g "daemon off;"`.
- The container also runs a WireGuard tunnel to the VPS so the reverse proxy can
  reach it (`NET_ADMIN`, `src_valid_mark` sysctl, `/lib/modules` mounted read-only
  by the compose file's host needs). `wg-quick`'s full-tunnel route table would
  otherwise swallow replies to the docker bridge gateway, so the entrypoint pins
  the bridge subnet onto `eth0` in table 51820.

## Configure

Copy `.env.example` to `.env` and fill it in:

- `LISTEN_PORT` — host port the container publishes on (default `8200`).
- `SITE_DOMAIN` — public domain the hub links to, e.g. `example.com`. The page
  links become `https://drivepod.<SITE_DOMAIN>` and so on.
- `WG_*` — tunnel keys/address/endpoint; `WG_PEER_ALLOWEDIPS` should be the VPS
  peer `/32` unless a full tunnel is genuinely wanted (the default `0.0.0.0/0`
  diverts the LAN and the compose bridge into the tunnel).

## Run

    docker compose up -d --build
    docker logs listen          # "Site links point at …"
    curl -s localhost:8200/health   # ok
    curl -s localhost:8200/ | grep -o 'https://[^"]*'   # the rendered links
