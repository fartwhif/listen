# Consolidated audio stack: DrivePod + GrabbyTube + tunester + FrogRock +
# ytcookies(vault) + bgutil + the hub platform - one image, one container.
#
# Base: node:22-bookworm-slim (Debian 12).
#   - node 22: vendored bgutil 2.0.2 requires engines.node >=22, FrogRock is
#     already node:22, and Angular 21 supports it.
#   - bookworm over trixie: Debian 13's pip package dropped the bundled wheels
#     (it declares Breaks/Replaces against python3-*), so aiohttp/yarl/multidict
#     -class dependencies compile from source at build time there; bookworm
#     installs them from wheels.
#   - bookworm python3 is 3.11 - what GrabbyTube (fastapi 0.115.*) runs on
#     today, and every vendored Python tree byte-compiles clean on it.
FROM node:22-bookworm-slim AS frontend-builder
WORKDIR /app/frontend
COPY stack/drivepod/frontend/package*.json ./
RUN npm ci --no-audit --no-fund
COPY stack/drivepod/frontend/ ./
RUN npm run build --configuration=production

# ---- DrivePod backend: deps, generated prisma client, compiled TS ----
FROM node:22-bookworm-slim AS drivepod-build
WORKDIR /app/backend
# libssl3 + the openssl CLI, BEFORE `npx prisma generate`: prisma decides its
# query-engine target from the libssl it can actually read, and
# node:22-bookworm-slim ships none, so generate picks debian-openssl-1.1.x and
# copies ONLY that into node_modules/.prisma/client. The runtime stage installs
# openssl (bookworm = 3.0.x) and asks for `debian-openssl-3.0.x`; the client
# refuses the 1.1.x engine, which is a crash-loop on every query at run time,
# not a build failure. Verified both ways in these images: with openssl present,
# generate emits the 3.0.x engine and the backend answers; without it it does not.
RUN apt-get update \
    && apt-get install -y --no-install-recommends openssl libssl3 \
    && rm -rf /var/lib/apt/lists/*
COPY stack/drivepod/backend/package*.json ./
RUN npm ci --no-audit --no-fund
COPY stack/drivepod/backend/ ./
RUN npx prisma generate && npm run build

# ---- FrogRock: compiled TS ----
FROM node:22-bookworm-slim AS frogrock-build
WORKDIR /app
COPY stack/frogrock/package*.json stack/frogrock/tsconfig.json ./
RUN npm ci --no-audit --no-fund
COPY stack/frogrock/types/ ./types/
COPY stack/frogrock/src/ ./src/
COPY stack/frogrock/server.ts ./
RUN npx tsc --skipLibCheck

# ---- bgutil PO-token provider 2.0.2 (vendored, tag-pinned) ----
# Upstream's own recipe: npm ci + npx tsc -> build/main.js, run under node.
FROM node:22-bookworm-slim AS bgutil-build
WORKDIR /app
COPY vendor/bgutil/server/package.json vendor/bgutil/server/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY vendor/bgutil/server/types/ ./types/
COPY vendor/bgutil/server/tsconfig.json ./
COPY vendor/bgutil/server/src/ ./src/
RUN npx tsc

# ---- runtime ----
FROM node:22-bookworm-slim AS runtime

ENV DEBIAN_FRONTEND=noninteractive \
    PIP_BREAK_SYSTEM_PACKAGES=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    NODE_ENV=production \
    STACK_DIR=/srv/stack \
    HUB_DATA_DIR=/data \
    HUB_CONF_DIR=/etc/stack

# The archiver/fingerprint tools are tunester's own upstream apt list, which
# the consolidated runtime stage dropped. Nothing fails loudly when they are
# absent - every one is a `shutil.which` probe, so the pipeline degrades
# quietly. That is how it stayed broken for a whole day: with no `fpcalc` the
# AcoustID CONTENT gate - the only check that catches a mislabeled file, a
# wrong cut, or a live take - never ran on a hub tune (10 `acoustid:
# unavailable - gate skipped` lines in this container's tuner log), and every
# cached delivery was served without its fingerprint re-check.
#   libchromaprint-tools  fpcalc: chromaprint for the AcoustID identity /
#                         live-take / zero-coverage gate (pipeline
#                         _fingerprint) and for the re-check `_delivery_get`
#                         runs on every store hit
#   unar                  the extract step of the RAR/ZIP/7z/tar(.gz/.bz2/.xz)
#                         sets a release is posted in (NZB_ARCHIVE_EXTS);
#                         verified in-image that unar decodes xz and bz2
#                         itself, so no external xz/bzip2/zstd is needed
#   par2                  verify (and repair) a posted set before extracting
#   p7zip-full            upstream's 7z CLI; tunester extracts 7z with unar,
#                         so this is parity with its container, not a code path
# Not carried over: `sudo`, which FrogRock's and DNAS's own entrypoints use to
# drop to appuser/scserv. Every hub program runs as root (gen-conf emits no
# `user=`), so nothing here invokes it. GrabbyTube's `nodejs` is not carried
# over either: nothing in that tree runs `node`, and the base image is node 22.
# yt-dlp is invoked as a COMMAND by both downloaders (DrivePod builds a shell
# string, GrabbyTube passes argv), so exactly one install on PATH is needed,
# pinned so the pip POT plugin and the command agree on version.
RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates curl wget unzip gnupg \
        python3 python3-pip python3-venv \
        ffmpeg vbrfix \
        libchromaprint-tools unar p7zip-full par2 \
        nginx \
        supervisor \
        wireguard iproute2 iptables procps iputils-ping \
        chromium xvfb x11vnc xauth dbus-x11 websockify novnc \
        fonts-liberation fonts-noto-core \
    && rm -rf /var/lib/apt/lists/*

# pyDes + Pillow are the vault's VNC keyboard driver (vault/vncdriver/
# vncdriver.py): it answers x11vnc's DES challenge and turns framebuffer
# tiles into PNG for its screenshot endpoint. Its own container installed
# exactly these two plus websocket-client.
#
# pytest is for platform/tests/, which run INSIDE this image: the host has
# none of the stack's dependencies, and the nginx checks need the generated
# config plus the snippets at /etc/nginx/snippets. The suite imports only
# stdlib and pytest, so no runtime dependency is added that the apps did not
# already have.
RUN pip3 install --no-cache-dir \
        "yt-dlp==2026.11.23" \
        "bgutil-ytdlp-pot-provider==2.0.2" \
        "fastapi==0.115.*" "uvicorn[standard]==0.34.*" "pydantic-settings==2.*" \
        "python-multipart==0.0.*" "mutagen" "aiohttp" \
        "websocket-client" "pyDes" "pillow" \
        "pytest"

# Deno runs yt-dlp's remote EJS challenge solvers (`--remote-components
# ejs:github`), which both downloaders ask for. DENO_INSTALL=/usr/local makes
# the installer put the binary at /usr/local/bin/deno (already on PATH); a
# separate `ln` from the customary /usr/local/deno/bin would point at a
# directory the installer never created and shadow the real binary.
RUN curl -fsSL https://deno.land/install.sh | DENO_INSTALL=/usr/local sh \
    && deno --version

# ---- service trees: vendored source + build outputs ----
WORKDIR /srv/stack

# node_modules carries the generated prisma client and its engine binaries, so
# `prisma db push` can run per partition at container start.
COPY --from=drivepod-build /app/backend/node_modules ./drivepod/backend/node_modules
COPY --from=drivepod-build /app/backend/dist ./drivepod/backend/dist
COPY --from=drivepod-build /app/backend/prisma ./drivepod/backend/prisma
COPY --from=drivepod-build /app/backend/package.json ./drivepod/backend/package.json
COPY stack/drivepod/backend/src ./drivepod/backend/src

# The built SPA plus its pristine index.html, kept as the shell template that
# gen-conf rewrites per partition (<base href> + identity placeholder).
COPY --from=frontend-builder /app/frontend/dist/frontend/browser /var/www/drivepod
RUN cp /var/www/drivepod/index.html /var/www/drivepod/index.html.dist

COPY --from=frogrock-build /app/dist ./frogrock/dist
COPY stack/frogrock/package.json stack/frogrock/package-lock.json ./frogrock/
COPY stack/frogrock/public ./frogrock/public
RUN cd frogrock && npm ci --omit=dev --no-audit --no-fund

COPY --from=bgutil-build /app/build ./bgutil/build
COPY --from=bgutil-build /app/node_modules ./bgutil/node_modules
COPY vendor/bgutil/server/package.json vendor/bgutil/server/deno.json \
     vendor/bgutil/server/deno.lock ./bgutil/

COPY stack/grabbytube ./grabbytube
COPY stack/tunester ./tunester
COPY stack/streamfix ./streamfix
COPY stack/vault ./vault
COPY platform ./platform
COPY hub ./hub
COPY nginx ./nginx

# The picker, the login/pair pages and every partition's SPA shell are
# GENERATED from partitions.json at container start (platform/gen-conf.py).
# hub/ holds the static portal files the generated pages link - the stylesheet,
# the form script, and the robots.txt the gateway's `= /robots.txt` serves.
# Nothing here is generated: gen-conf writes the picker and the three auth pages
# into the same directory at start.
#
# The snippets go into the EXISTING /etc/nginx/snippets (the base image ships
# fastcgi-php.conf and snakeoil.conf there): `cp -r nginx/snippets
# /etc/nginx/snippets` nests ours one level deep, and every generated
# `include /etc/nginx/snippets/proxy-svc.conf` then fails `nginx -t`.
RUN mkdir -p /var/www/hub /etc/stack /etc/supervisor/conf.d \
             /etc/nginx/stack-locations /etc/nginx/conf.d /etc/nginx/snippets \
             /data/users /data/vaults /data/pool/out /data/pool/cache \
             /data/pool/nzb-cache /data/pool/jobs /data/pool/blobs /var/log/sup \
    && cp platform/supervisord.conf /etc/supervisor/supervisord.conf \
    && cp nginx/platform.conf /etc/nginx/conf.d/10-platform.conf \
    && cp nginx/gateway.conf /etc/nginx/conf.d/20-gateway.conf \
    && cp -r nginx/snippets/. /etc/nginx/snippets/ \
    && cp hub/hub.css hub/hub.js hub/dock.css hub/dock.js hub/robots.txt /var/www/hub/ \
    && rm -f /etc/nginx/sites-enabled/default \
    && chmod +x platform/entrypoint.sh platform/hub.py platform/vault-run.py \
               platform/ytdlgate.sh platform/ytdlgated \
    && ln -sf /srv/stack/platform/hub.py /usr/local/bin/hub \
    && (id stack >/dev/null 2>&1 || useradd -r -m -d /srv/stack -s /usr/sbin/nologin stack) \
    && chown -R stack:stack /srv/stack /data \
    && chmod -R 0755 /data \
    && chmod -R a+rX /var/www/drivepod /usr/share/novnc /srv/stack \
    && chmod 0700 /data/vaults

COPY wireguard/wg0.conf.template /etc/wireguard/wg0.conf.template
COPY platform/entrypoint.sh /entrypoint.sh
ENTRYPOINT ["/entrypoint.sh"]

# 80 gateway (the target of every VPS server_name) | 8090 front broadcaster
# (the VPS proxy's target and DNAS's relay source) | 8091 stream-fix shim
# (DNAS's relay target) | 8101 front library API (grabbytube.example.com) | 8102
# shared pool tuner (tunester.example.com). Extra partitions are gateway-only.
EXPOSE 80 8090 8091 8101 8102
