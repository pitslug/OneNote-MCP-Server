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

- **Read:** `list_notebooks`, `list_sections`, `list_section_groups`, `list_pages`,
  `find_pages`, `get_page_content`, `get_page_ink`, **`render_page_ink`** (the
  ink→PNG read path).
- **Create:** `create_notebook`, `create_section`, `create_section_group`,
  `create_section_in_group` (nested structures like *Clients > Harmony*),
  `create_page`, `create_sidekick_page` (safe write-back), `update_page_content`.
- **Page management:** `copy_page` (safe for ink pages — original untouched),
  `move_page`, `delete_page`, `check_onenote_operation` (poll async Graph copies).
  Moves and deletes are guarded: `delete_page` requires `confirm=true`, and both
  **refuse to touch any page containing ink** — the hard rule above holds.
- Plus auth helpers.

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

Or pull the published image: `ghcr.io/pitslug/onenote-mcp-server:1`.

## Auth model (two independent layers)

- **Gateway** — a bearer token gates the MCP endpoint (for machine clients like Claude
  Code). Optionally, set the `ONENOTE_OIDC_*` vars to also serve spec OAuth via an OIDC
  proxy in front of your identity provider (e.g. Pocket-ID) — this is what claude.ai
  custom connectors require, since they can't send static headers. Both credentials work
  side-by-side; see [DEPLOY.md](./DEPLOY.md) for the connector walkthrough.
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
