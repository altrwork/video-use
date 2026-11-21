# Deploying the cloud Studio

Two setups. Both need Node 20+ and a Cloudflare account. Run everything from `cloud/`.

## Quick: passphrase sign-in on workers.dev

```bash
npm install
npx wrangler deploy
openssl rand -base64 36 | npx wrangler secret put SYNC_KEY
openssl rand -base64 36 | npx wrangler secret put COOKIE_KEY
npx wrangler secret put TEAM_PASSPHRASE     # type the passphrase your team will use
```

People sign in at the Worker URL with the passphrase. Claude connects to `<url>/mcp` and asks for the same passphrase.

## Team: custom domain, Cloudflare Access, your own OAuth server

The site refuses every request until all four secrets below are set, so it is safe to deploy first and configure after.

1. **Config.** `cp wrangler.team.example.jsonc wrangler.local.jsonc` and fill in the placeholders. The file is gitignored.
2. **Deploy.**
   ```bash
   npm install
   npx wrangler deploy -c wrangler.local.jsonc
   openssl rand -base64 36 | npx wrangler secret put SYNC_KEY -c wrangler.local.jsonc
   openssl rand -base64 36 | npx wrangler secret put COOKIE_KEY -c wrangler.local.jsonc
   ```
3. **Cloudflare Access** (Zero Trust → Access → Applications).
   - Protect the editor hostname with your team policy.
   - Add a bypass for `<host>/mcp`, `<host>/sync/*` and `<host>/.well-known/*`. Those paths check their own credentials in the Worker.
   - Copy the application's AUD tag: `npx wrangler secret put ACCESS_AUD -c wrangler.local.jsonc`
4. **OAuth server.** Register this app (client id `editor`, resource `https://<host>/mcp`, scopes `editor:read` and `editor:write`) and issue its introspection secret: `npx wrangler secret put OAUTH_INTROSPECT_SECRET -c wrangler.local.jsonc`. The server must support RFC 7662 introspection at `<issuer>/oauth/introspect` with HTTP Basic auth.
5. **Check.**
   ```bash
   curl -s -o /dev/null -w "%{http_code}\n" https://<host>/                    # 302 to the Access sign-in
   curl -s -o /dev/null -w "%{http_code}\n" -X POST https://<host>/mcp         # 401
   curl -s https://<host>/.well-known/oauth-protected-resource/mcp             # lists your OAuth server
   ```
6. **Connect.** In Claude, add a custom connector with `https://<host>/mcp`.

## Publishing a project

On the computer that has the footage, put the URL and sync key in `~/.config/video-use/cloud.json`:

```json
{ "url": "https://<host>", "sync_key": "<the SYNC_KEY value>" }
```

Then run `python studio/cloud_sync.py <videos_dir>/edit` and leave it running. It uploads small preview copies, keeps `edl.json` in sync, and does the rendering.

## Notes

- To rotate the sync key, set a new `SYNC_KEY` secret and update `cloud.json` on each editing computer.
- Preview media is stored in the Worker's Durable Object. Files over 300 MB are skipped.
