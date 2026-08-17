# Listen Shell Website - listen.example.com

A static landing page that serves as a hub/portal for the three deployed audio projects.

## Existing Infrastructure

| Project | Domain | WG IP | Tech |
|---------|--------|-------|------|
| DrivePod | drivepod.example.com | 10.99.0.229 | Angular + Express, YouTube podcast player |
| FrogRock | frogrock.example.com | 10.99.0.230:8090 | Node, live MP3 radio stream |
| GrabbyTube | grabbytube.example.com | 10.99.0.234:8101 | FastAPI, YouTube-to-MP3 |

All three live on vps VPS (<vps-ip>) behind nginx + WireGuard tunnels.

## What "Listen" Is

A simple static HTML/CSS page. No backend, no Docker, no WireGuard needed.
Served directly from the VPS as static files.

## Tasks

### 1. Create the page (local)

- `/home/user/projects/listen/index.html` - single-page static HTML
- Dark theme (match the existing project aesthetics - dark zinc/slate)
- Header: "LISTEN" with tagline (e.g., "Audio projects")
- Three cards linking to the subdomains:
  - **DrivePod** - YouTube podcast player & queue
  - **FrogRock** - Live radio stream
  - **GrabbyTube** - YouTube to MP3 converter
- Each card: icon/emoji, title, one-line description, link button
- Footer: example.com branding
- Responsive (mobile-first, works on phone and desktop)
- No JS dependencies - pure HTML + inline CSS (or single `<style>` block)
- Self-contained: no external CDN calls

### 2. SSL cert on vps

```bash
# SSH to vps
ssh -i ~/.ssh/<key-file> -p <ssh-port> root@<vps-ip>

# Get cert (chicken-and-egg: HTTP-only config first, then full config)
certbot certonly --webroot -w /var/www/certbot -d listen.example.com
```

### 3. Nginx config on vps

Write `/etc/nginx/conf.d/listen.example.com.conf`:

- HTTP (port 80): ACME challenge + redirect to HTTPS
- HTTPS (port 443): serve static files from `/var/www/listen.example.com/html`
- Standard SSL headers (match existing configs)

```nginx
server {
    listen 80;
    server_name listen.example.com;
    location /.well-known/acme-challenge/ { root /var/www/certbot; }
    location / { return 301 https://$host$request_uri; }
}

server {
    listen 443 ssl;
    server_name listen.example.com;
    ssl_certificate     /etc/letsencrypt/live/listen.example.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/listen.example.com/privkey.pem;
    # ... SSL settings ...
    root /var/www/listen.example.com/html;
    index index.html;
    location / { try_files $uri $uri/ =404; }
}
```

### 4. Deploy files to vps

```bash
# Create directory on vps
ssh ... "mkdir -p /var/www/listen.example.com/html"

# Copy index.html
scp -P <ssh-port> -i ~/.ssh/<key-file> index.html root@<vps-ip>:/var/www/listen.example.com/html/

# Reload nginx
ssh ... "nginx -t && systemctl reload nginx"
```

### 5. Verify

- `curl -sI https://listen.example.com` - should return 200, HTTPS
- Open in browser - dark page, three cards, links work
- Mobile check - responsive layout

## Design Direction

Minimal, dark, consistent with the existing project aesthetic. Think:
- Dark background (#0a0a0a or similar)
- Three equal cards in a row (stacks on mobile)
- Subtle hover effects
- Monospace or clean sans-serif font
- No animations, no frameworks, no dependencies

## Estimated effort

~30 minutes total. No backend, no Docker, no WireGuard. Just HTML + nginx + cert.
