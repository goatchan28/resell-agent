# The private beta

Five family members, their phones, one Mac Studio, one eBay **Sandbox** seller
account, one SQLite file.

```
phone → HTTPS → Cloudflare Access (email allowlist)
      → Cloudflare Tunnel
      → cloudflared on the Mac Studio
      → 127.0.0.1:5000  waitress (1 process, 12 threads)
      → resell → SQLite (WAL) + data/uploads
```

## What this is, and what it is not

Everyone shares one database and one seller account. `item.owner_email` decides
whose consumer screens an item appears on — it is a label on a shelf, not a wall
around the data. `/ops` ignores it and sees everything, on purpose: there is one
inventory and one person answerable for what goes up on eBay.

There are no accounts, no passwords, no sessions and no per-user eBay
credentials. Identity is whatever address Cloudflare Access authenticated.

## Setup

**1. Environment.** In `.env` on the beta host:

```
RESELL_SECRET_KEY=<python -c "import secrets; print(secrets.token_hex(32))">
RESELL_ADMIN_EMAILS=you@example.com
```

Do **not** set `RESELL_DEV_EMAIL` here. It stands in for the Access header when
there is no tunnel; with it set, anyone who reaches the port is that person.

**2. Dependencies.** `uv sync --extra beta` (adds waitress).

**3. Tunnel.** See `cloudflared-config.yml`. Use a *named* tunnel — a quick
tunnel gets a new hostname every restart and the invitations go stale nightly.

**4. Access policies.** Two applications on the same hostname, most specific
first:

| Path | Policy |
|---|---|
| `sell.example.com/ops*` | your address only |
| `sell.example.com` | the five invited addresses |

The app enforces the same split itself (`@access.admin_only`), so a path rule
that silently stops matching after a rename does not open the operator screens.

**5. Services.** Edit `USERNAME` in both plists, then:

```
cp deploy/com.resell.*.plist ~/Library/LaunchAgents/
launchctl load -w ~/Library/LaunchAgents/com.resell.app.plist
launchctl load -w ~/Library/LaunchAgents/com.resell.backup.plist
```

**6. Verify before inviting.** `uv run pytest tests/test_beta_access.py` — five
simulated Access emails, five SKUs, simultaneous runs, cross-shelf reads and
writes, and restart recovery, all through the real routes.

## Decisions worth remembering

**Single process.** Agent runs are threads inside the server process, and
`recover_interrupted_runs` assumes at startup that a `running` row means a
process that has died. A second worker would make that assumption false and
would double every budget.

**Loopback origin.** The Access header is trusted because nothing but
cloudflared can reach the port. Binding to `0.0.0.0` breaks that, silently.

**`caffeinate`, not `pmset`.** The wake assertion is scoped to the server process
and reverts by unloading the job. `pmset -a disablesleep 1` would outlive the
beta until somebody remembered a second command.

**Backups cover photographs too.** The browser resizes to 2048px before
uploading and the original stays on the phone, so `data/uploads` holds the only
copy of what a listing shows. A database restored without it is a shelf of
items whose pictures 404.

## Required before production eBay or a wider invite list

**Verify the Access JWT.** Today the app trusts
`Cf-Access-Authenticated-User-Email` as a header. That is sound only while
loopback is the sole route in. Before real money or a wider audience, validate
`Cf-Access-Jwt-Assertion` against Cloudflare's public keys — signature, audience
and expiry — so identity survives the origin being reachable another way. This
is a precondition, not an improvement.

Also still deferred, deliberately: individual eBay OAuth · PostgreSQL ·
Redis/job queues · billing · CSRF tokens and real sessions · rate limiting ·
`ProxyFix` · backgrounding `publish`.

## Operating notes

- Logs: `~/Library/Logs/resell-app.log`, `~/Library/Logs/resell-backup.log`.
- A restart marks in-flight runs `interrupted` and frees their items. Expected,
  visible in `/ops`, and the seller is told *"We stopped partway"* rather than
  that the work failed.
- `publish` is synchronous and takes ~10s. That occupies one of twelve threads.
- Uploads are capped at 64 MB per request; over that the seller is asked to send
  a few at a time.
