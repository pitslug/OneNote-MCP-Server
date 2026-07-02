# Deploying the OneNote MCP Sidekick

Two paths. **Path A (local build)** gets it running on your box today. **Path B (GHCR)**
is the "pull my own image + auto-update" flow once you've pushed to GitHub. Do A first,
then B when you're ready.

Fill in your own values: your Azure app **client ID** (register a public client with
scopes `Notes.ReadWrite`, `offline_access`, `User.Read`), and your target **notebook** /
**section** (this fork defaults to `Slugbook` / `Sidekick` — override via env).

---

## Prerequisites

- Docker + Docker Compose on the Unraid host.
- Your Traefik reverse proxy running, with its external Docker network (this compose
  file assumes it's called `proxy` — rename if yours differs).
- A DNS name pointing at Traefik for this service, e.g. `onenote.slugworx.net`.
- Outbound HTTPS from the host (for Microsoft sign-in + Graph).

---

## Path A — get it running locally (build on the host)

### 1. Put the code on the host
Copy this `server/` folder to the Unraid host (or `git clone` your fork). Everything
below runs from inside that folder.

### 2. Create the secrets
```bash
mkdir -p secrets
# your Azure app (client) ID
echo -n "<YOUR_AZURE_CLIENT_ID>" > secrets/onenote_client_id
# a long random bearer token Claude will send — save a copy, you'll need it in the connector
openssl rand -hex 32 > secrets/onenote_api_token
chmod 600 secrets/*
```

### 3. Fill the placeholders in `docker-compose.yml`
- `traefik.http.routers.onenote-mcp.rule` → your host, e.g. ``Host(`onenote.slugworx.net`)``
- `traefik.http.routers.onenote-mcp.tls.certresolver` → your resolver name (e.g. `letsencrypt`)
- confirm the network name (`proxy`) matches your Traefik network
- For local build, uncomment `build: .` and comment out the `image:` line (or just use
  `--build` in step 4).

### 4. Build + one-time sign-in
```bash
docker compose build
# device-code sign-in — prints a URL + code; complete it on any device.
# Writes the token cache to the onenote_tokens volume that the service reuses.
docker compose run --rm onenote-mcp python server_entry.py --auth
```

### 5. Start it
```bash
docker compose up -d
docker compose ps          # should show healthy after ~15s
docker compose logs -f     # look for "token present and refreshable - fully operational"
```

### 6. Smoke test
```bash
# from the host (health is unauthenticated):
curl -sf https://onenote.slugworx.net/healthz && echo OK
# the MCP endpoint should reject without the token (401) and negotiate with it:
curl -s -o /dev/null -w '%{http_code}\n' https://onenote.slugworx.net/mcp                  # 401
curl -s -o /dev/null -w '%{http_code}\n' -H "Authorization: Bearer <YOUR_TOKEN>" \
     https://onenote.slugworx.net/mcp                                                       # 400/406 = reached MCP
```

### 7. Connect Claude
Add a custom connector pointing at `https://onenote.slugworx.net/mcp`, with an
`Authorization: Bearer <YOUR_TOKEN>` header. Then ask Claude to list your notebooks or
render a Slugbook ink page.

---

## Path B — publish to GHCR and pull your own image

### 1. Push to GitHub
Make `server/` the root of a new repo (so the Dockerfile + `.github/workflows` sit at the
repo root) and push. In `Dockerfile`, set the `org.opencontainers.image.source` label to
your repo URL.

### 2. Let CI build it
The workflow builds multi-arch and pushes to `ghcr.io/<you>/<repo>` on every push to
`main` (`:edge`) and on version tags:
```bash
git tag v1.0.0 && git push origin v1.0.0   # -> :1.0.0, :1.0, :1, :latest
```
After the first successful run, open the package in GitHub and set its visibility to
**public** (so pulls need no auth — the "open for all" goal).

### 3. Switch compose to pull
In `docker-compose.yml` set `image: ghcr.io/<you>/onenote-mcp-server:1`, comment out
`build:`, then:
```bash
docker compose pull && docker compose up -d
```

### 4. Auto-update (optional)
Point Watchtower at it, or use Unraid's "check for updates". Floating `:1` picks up
patch/minor releases automatically; pin `:1.0.0` if you'd rather bump by hand.

---

## Re-auth / rotate later
- **New bearer token:** overwrite `secrets/onenote_api_token`, `docker compose up -d`,
  update the connector header.
- **Re-sign-in to Microsoft** (only if the refresh token is revoked): re-run the step 4
  `--auth` command.

## Troubleshooting
- **Container unhealthy:** `docker compose logs`. If it says cache dir isn't writable,
  check the `onenote_cache` volume mount.
- **Claude gets 401:** the connector's bearer token doesn't match `secrets/onenote_api_token`.
- **Claude gets 404/connection errors:** check the Traefik router rule/host and that the
  `proxy` network matches.
- **"NO token yet" in logs:** you haven't completed step 4 sign-in, or the token volume
  isn't shared between `run --auth` and `up` (it is by default via `onenote_tokens`).
