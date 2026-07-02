# OneNote MCP Server (Sidekick)

An [MCP](https://modelcontextprotocol.io) server that lets Claude act as a **sidekick
over Microsoft OneNote** — including **handwritten ink**. It reads your notes (typed
*and* ink), and can write summaries or aggregated to-do lists back into a dedicated
section. Packaged to run in **Docker** behind a reverse proxy, so you can reach it from
anywhere.

> Hard rule: **it never edits or converts your ink.** Claude reads ink and writes only
> *new* typed pages, in a separate section. Your handwriting stays pristine.

## What makes it different

- **Ink → image rasterizer.** OneNote returns handwriting as InkML stroke data. This
  server rasterizes those strokes to a PNG so Claude's vision can actually *read* your
  handwriting — the piece the other community servers don't have.
- **Safe write-back.** Summaries and to-dos are posted as brand-new typed pages in a
  dedicated `Sidekick` section, never onto your ink pages.
- **Runs as a service.** streamable-HTTP transport, so it lives behind Traefik/your
  proxy instead of only as a local stdio subprocess.
- **Bearer-token gate.** The MCP endpoint is protected by a long-lived token the client
  sends — internet-facing without an interactive login on every call.
- **Two-tier notebook cache.** In-memory + on-disk (per-page), so re-reading a page
  skips the network fetch *and* the re-render. Keyed on `lastModifiedDateTime`.

## Tools

`list_notebooks`, `list_sections`, `list_pages`, `find_pages`, `get_page_content`,
`get_page_ink`, **`render_page_ink`** (the ink→PNG read path), `create_page`,
`create_sidekick_page` (safe write-back), `update_page_content`, plus auth helpers.

## Quick start

See **[DEPLOY.md](./DEPLOY.md)** for the full walkthrough. In short:

1. Register a public-client Azure app (scopes `Notes.ReadWrite`, `offline_access`,
   `User.Read`) and note its **client ID**.
2. Create `secrets/onenote_client_id` and `secrets/onenote_api_token`
   (`openssl rand -hex 32` for the token).
3. Fill in your Traefik host in `docker-compose.yml`.
4. Build, then sign in once (device code):
   ```bash
   docker compose build
   docker compose run --rm onenote-mcp python server_entry.py --auth
   docker compose up -d
   ```
5. Add a Claude custom connector at `https://<your-host>/mcp` with header
   `Authorization: Bearer <your token>`.

Or pull the published image: `ghcr.io/pitslug/onenote-mcp-server:v1`.

## Auth model (two independent layers)

- **Gateway** — a bearer token gates the MCP endpoint (for the machine client). Keep any
  interactive SSO (e.g. Pocket-ID) for human-facing routes only.
- **Microsoft/OneNote** — MSAL device-code sign-in once; the refresh token
  (`offline_access`) is cached to a mounted volume and refreshed silently thereafter.

Both fail independently; gateway expiry never touches the OneNote token.

## Configuration

Everything is env-driven; sensitive values support the Docker `*_FILE` convention (e.g.
`AZURE_CLIENT_ID_FILE`, `ONENOTE_API_TOKEN_FILE`). See **[.env.example](./.env.example)**
for the full list (transport, ports, cache TTLs/budget, target notebook/section).

## Security notes

- The token cache and the on-disk page cache contain your data — they live on mounted
  volumes, never in the image.
- Serve over HTTPS; treat the bearer token like a password and rotate by swapping the
  secret.

## Credits & license

Forked from [purpleslurple/onenote-mcp-server](https://github.com/purpleslurple/onenote-mcp-server)
(original author Matthew Schneider). MIT licensed — see [LICENSE](./LICENSE).
