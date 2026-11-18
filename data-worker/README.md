# Data relay (Cloudflare Worker "monitor6529-data")

The collector (`collector/publish.sh`, launchd agent `casa.nua.6529-network-data`, every 2 min)
signs the data files and POSTs the signed envelopes that changed here, then pushes to GitHub at
most every 4 minutes. The Worker keeps the latest envelope per file name in a Durable Object and
hands it, unchanged, to the apps, which read it first and fall back to
`raw.githubusercontent.com` (GitHub's raw cache adds up to 5 minutes). Spec:
`handoffs/specs/2026-11-18-data-worker.md`.

**The Worker is only a relay.** The apps verify every envelope themselves, with the same pinned
keys and rules as today. The Worker checks it too, so nobody else can fill it. Modelled on
`live-worker/` (same verification code, copied, and the same pins).

## Endpoints

`GET /v1/data/<name>` → `200` the envelope, byte for byte as POSTed (which is byte for byte the
file under `docs/data/signed/`), `content-type: application/json`,
`cache-control: public, max-age=30`, no CORS · `404` for a name not on the allow-list or not
stored (also a name with a query string) · `429` (`RL_GET`) · `503` if storage fails ·
`405` other method. A `/` in the name may be sent as is or as `%2F`.

Names (the allow-list): `network.json`, `validators.json`, `blocks.json`, `chain.json`,
`awards.json`, `history.json`, `cycles/index.json`, `cycles/<5 digits>.json`, `days/index.json`,
`days/<5 digits>.json`.

`POST /v1/data` (collector only), body a JSON object `{"<name>": <envelope>, ...}` (each value is
the envelope file's exact text; the Worker stores each value's exact bytes from the body) →
`204` all stored. **All or nothing**: any refusal stores none of the batch.

| status | when |
|---|---|
| 413 | body over 6 MB (declared length, or while reading), more than 100 entries, or one envelope over 2 MB |
| 400 | not JSON / not UTF-8, not a non-empty object of objects, a name twice, envelope fields other than exactly `alg format key name payload sig`, not format 2 / ECDSA-P256-SHA256, malformed base64 or DER, payload not JSON, `generated_unix` present but not a positive integer |
| 403 | name not on the allow-list, key id not pinned, envelope `name` not the map key, signature does not verify |
| 422 | a payload's `generated_unix` more than 5 minutes ahead of the Worker's clock (it would block that name until then) |
| 409 | rollback: a `generated_unix` older than the stored one for that name, a file that has one replaced by one without, or a file without one replaced by a batch whose sequence is not newer (below); checked inside the Durable Object, so two POSTs cannot race |
| 429 | rate limit (binding `RL`, 6 POSTs a minute per address) |
| 503 | storage failed (nothing stored) |
| 405 / 404 | other method / other path |

Files without `generated_unix` (`validators.json`, `cycles/*`, `days/*`, the index files)
could be replayed from an old signed copy on their own, so the batch carries the clock: its
sequence S is the highest `generated_unix` among the entries it actually changes (an entry
byte for byte equal to the stored one does not count). S is stored with every entry the batch
writes, and a file without `generated_unix` is written only by a batch whose S is greater than
the S stored with that name (0 if new). A batch that changes no file with `generated_unix`
cannot write any file without one (409). The collector's batches always carry the new
`network.json` (it changes every run). So an old `validators.json` is refused alone, with an old
`network.json` (rollback), and with the current one (same bytes, no S).

An envelope byte for byte equal to the stored one is a no-op (`204`), so the collector can
resend everything. An equal `generated_unix` with other bytes is accepted. The Durable Object
reads a name from storage only if it is in its stored list, and remembers only found entries,
so GETs of unknown names cost no read and no memory.

Kept: the newest 60 `days/<day>.json` and the newest 40 `cycles/<day>.json` (by day number;
the index files are not counted); older ones are dropped in the same write that stores a batch.

Pinned keys (in `data.js`, as in the apps, `push/worker.js` and `live-worker/live.js`):
`9fc874c0f34c3d51` (active), `04f3b06b89f126a7` (reserve). The id is recomputed from the stored
SPKI before each use. There is no setting, var or secret that adds a key: `worker.js` always
calls `handle()` with the production pins; only `test.mjs` passes an extra test key.

GETs: each isolate reuses an envelope it read or stored for 5 s, so a burst of GETs costs one
Durable Object request per name; a POST updates the isolate at once (and drops evicted names).
`RL_GET` (300 GETs a minute per address) is on; a WAF rule cannot be set on `workers.dev`
(see `live-worker/README.md`). `preview_urls = false`.

## What it stores and logs

The latest envelope per name (and its `generated_unix`) and the list of stored names, in one
Durable Object. Nothing else. No `console` calls, `observability` off. The caller's address
(`cf-connecting-ip`) is used only for the rate limits and is not stored. Everything stored is
already public on GitHub.

## Collector side

`collector/sign.py --batch BODY --posted FILE --data state/data` builds the body: the signed
files whose envelope differs from what `state/worker-posted.json` says was last accepted, and
every file once an hour (so a Worker that lost its data refills itself). Only names on the
allow-list, only the 60/40 newest day files, top-level files first, and nothing the Worker would
refuse for size (left for the next run). A file it cannot read is skipped, and a batch that
would change a file without `generated_unix` but no file with one is held whole for the next
run; both are named in the log line. `publish.sh` POSTs it with `curl` (5 s to connect,
25 s in all, https only, no proxy, no `~/.curlrc`), keeps the new record only on `204`, and on
any failure writes one log line (the HTTP status, or the reason no batch was built) and goes on: GitHub still gets the files.

## Files

- `data.js`: all logic (`handle`, `verifyEnvelope`, `topLevel`, the `DataStore` Durable Object).
- `worker.js`: wiring only (default export + the `DataStore` class export).
- `wrangler.toml`: Durable Object binding `DATA` → `DataStore`, migration `v1`
  (`new_sqlite_classes`), rate limits `RL` (namespace 6532) and `RL_GET` (6533); the push relay
  uses 6529, the live relay 6530 and 6531.
- `test.mjs`, `fixtures/`: `node test.mjs` (110 checks). `fixtures/real-*.json` are files from
  `docs/data/signed/` as published (production key), unchanged. Other envelopes are signed in the
  test with throwaway WebCrypto keys: one added to the pins only there, one never pinned.

## Deploy (account on Workers Paid)

```sh
cd data-worker
node test.mjs                      # must end "110 checks passed"
npx wrangler whoami                # the Casa NUA account
npx wrangler deploy --dry-run      # bundles, checks wrangler.toml, uploads nothing
npx wrangler deploy                # first deploy: applies migration v1 (creates DataStore)
```

Nothing has to be created beforehand. A later change to the class itself (rename, delete) needs
a NEW `[[migrations]]` entry with a new tag, never an edit of `v1`.

Check: `curl -i https://monitor6529-data.casanua.workers.dev/v1/data/network.json` → `404` until
the collector's first POST, then `200` with the envelope.

Order: deploy the Worker before installing the new `publish.sh` in the fixed copy (until then
its POSTs fail, are logged, and GitHub is unaffected).

Remove: `npx wrangler delete` (this also deletes the stored envelopes).
