# Cloudflare Worker + D1 deployment

This deployment uses the dashboard source already tracked in `../dist/`. It builds the dashboard into the Worker and reads current published data from the D1 binding `DB`. No local SQLite file, source workbook, or initial order snapshot is included.

## Required Cloudflare settings

- Repository: `sora745653354-maker/b2b-logistics-tower`
- Root directory: `/`
- Build command: `node cloudflare-worker/scripts/build.mjs && node cloudflare-worker/scripts/validate-artifact.mjs && node cloudflare-worker/scripts/check-worker.mjs`
- Deploy command: `cd cloudflare-worker && npx wrangler d1 migrations apply b2b-logistics-tower --remote && npx wrangler deploy`
- Production branch: `main`
- D1 binding: `DB` (configured in `wrangler.toml`)
- Secret: `SYNC_TOKEN`, at least 32 characters. Set it in Workers & Pages > Settings > Variables and Secrets before enabling snapshot updates. Never put it in this repository.

Without `SYNC_TOKEN`, `POST /api/snapshot` deliberately returns 503. With the secret configured, it accepts only `Authorization: Bearer <SYNC_TOKEN>`; missing or wrong tokens receive 401. Read endpoints remain available according to the existing site's public access policy.

## Local push credential

The local snapshot publisher reads the same token from macOS Keychain service `b2b-logistics-tower-cloudflare-token`, account `tower-uploader`. Install the token there only after setting the matching Cloudflare secret. The publisher sends only the sanitized snapshot produced by the local API.

## Data and schema

`migrations/0001_create_dashboard_snapshot_store.sql` creates the two D1 tables. The deployment command applies the migration idempotently before publishing the Worker. D1 starts empty; deployment does not import local orders. The first authorized snapshot upload is a separate operation.
