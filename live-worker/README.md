# Live chain relay (Cloudflare Worker "monitor6529-live")

The apps' Chain page shows the 6529 chain block by block, seconds behind. The collector machine
(`collector/live.py`, launchd agent `casa.nua.6529-live`, every 30 s) POSTs one signed envelope
here; the Worker keeps the latest in a Durable Object and hands it, unchanged, to the apps (GET
every ~10 s, only while the Chain page is on screen). Contract:
`handoffs/specs/2026-11-16-live-chain-contract.md`.

**The Worker is only a relay.** The apps verify the signature themselves, with the same pinned
keys and rules as every other signed file. The Worker checks it too, so nobody else can fill it.

## Endpoints

`GET /v1/live` → `200` the envelope, byte for byte as POSTed (`content-type: application/json`,
`cache-control: no-store`, no CORS) · `404` before the first publish · `503 {"stale":true}` when
the stored one's `generated_unix` is more than 5 minutes old · `503` (empty) if storage fails.

`POST /v1/live` (collector only) → `204` stored. Refused, with nothing stored:

| status | when |
|---|---|
| 413 | body over 64 KB (declared length, or while reading) |
| 400 | not JSON / not UTF-8, fields other than exactly `alg format key name payload sig`, not format 2 / ECDSA-P256-SHA256, malformed base64 or DER, payload not a JSON object, `generated_unix` not a positive integer |
| 403 | key id not pinned, name not `live.json`, signature does not verify |
| 422 | `generated_unix` more than 120 s behind or more than 30 s ahead of the Worker's clock |
| 409 | `generated_unix` not greater than the stored one (first against the newest one this isolate knows, without touching the Durable Object; then inside the Durable Object, so two POSTs cannot race) |
| 429 | rate limit (binding `RL`, 10 POSTs a minute per address) |
| 405 / 404 | other method / other path |

Pinned keys (in `live.js`, as in the apps and `push/worker.js`): `9fc874c0f34c3d51` (active),
`04f3b06b89f126a7` (reserve). The id is recomputed from the stored SPKI (first 16 hex of SHA-256
of the DER) before each use. There is no setting, var or secret that adds a key: `worker.js`
always calls `handle()` with the production pins; only `test.mjs` passes an extra test key.

GETs: each isolate reuses the latest envelope it read or stored for 3 s, so a burst of GETs
costs one Durable Object request; the 5-minute staleness is still checked on every GET.
Abuse of GET: a WAF rate limiting rule cannot be set on a `workers.dev` address (it is not a
zone of this account). Two options: (a) the optional `RL_GET` binding (commented out in
`wrangler.toml`, 60 GETs a minute per address; the app makes 6), or (b) serve the Worker on a
custom domain of a Cloudflare zone and add a rate limiting rule there, e.g. *URI path equals
`/v1/live` and method GET: 120 requests per 60 s per IP → block for 60 s*, then point the apps
at that domain. `preview_urls = false`: no per-version preview addresses.

## What it stores and logs

One Durable Object entry: the latest envelope and its `generated_unix`. Nothing else. No
`console` calls, `observability` off. The caller's address (`cf-connecting-ip`) is used only for
the rate limit check and is not stored. Cloudflare itself still sees the collector's address on
each POST (it is the network in between); it is never part of anything the apps receive.

## Files

- `live.js`: all logic (`handle`, `verifyEnvelope`, the `LiveStore` Durable Object). The
  signature check is `push/worker.js`'s `verifySigned` / `derToRaw` / `b64` / `readCapped`,
  plus an exact field set.
- `worker.js`: wiring only (default export + the `LiveStore` class export).
- `wrangler.toml`: Durable Object binding `LIVE` → `LiveStore`, migration `v1`
  (`new_sqlite_classes`), rate limit `RL` (namespace 6530; the push relay uses 6529).
- `test.mjs`, `fixtures/`: `node test.mjs` (54 checks). `live-envelope.json` is a real
  `live.json` from `live.py`'s dry run against the node, signed through `sign.py`'s
  `sign_envelope` with a THROWAWAY key (public part `test-key.spki.b64`; the private part was
  never kept in this folder). `real-signed-network.json` is the published, production-signed
  `network.json`, to show the production pins verify a real file.

## Deploy (account on Workers Paid)

```sh
cd live-worker
node test.mjs                      # must end "54 checks passed"
npx wrangler whoami                # the Casa NUA account
npx wrangler deploy --dry-run      # bundles, checks wrangler.toml, uploads nothing
npx wrangler deploy                # first deploy: applies migration v1 (creates LiveStore)
```

The first `deploy` creates the Worker, the `LiveStore` class (migration `v1`) and the
`workers.dev` route; it prints the URL `https://monitor6529-live.<subdomain>.workers.dev`.
Nothing has to be created beforehand: a Durable Object needs no namespace id, and the rate
limit namespace is just the number in `wrangler.toml`. Later deploys of changed code reuse the
class; a change to the class itself (rename, delete) needs a NEW `[[migrations]]` entry with a
new tag, never an edit of `v1`.

Check: `curl -i https://monitor6529-live.<subdomain>.workers.dev/v1/live` → `404` until the
collector's first POST, then `200` with the envelope.

Remove: `npx wrangler delete` (this also deletes the stored envelope).

## Privacy: what live.json could say about the collecting node

The collector runs on Casa NUA's own validator; nothing published may single out which account
that is. What was checked and done:

1. **Its own votes.** The votes in a block are the votes that block's proposer included
   (each vote carries an earlier slot: 1 to 4 slots, rarely more), so every node has the same
   lists; they are not this node's own records. Checked on one day's blocks without printing
   the key: the own key's votes are not in every block, and arrive with the same slot offsets
   as other keys' (no figures for that key are written down anywhere, since a per-key count
   could be matched against awards.json). The vote weight `w`, `key_id` and the node's `own`
   flag are not published.
2. **Recorded-part effects.** Nothing about what this node recorded is published (no
   `recorded`, no `not_recorded`). A block the node lacks would otherwise look like an empty
   slot exactly while it was down; instead `live.py` checks that block heights are consecutive
   from 30 slots before the window up to the head, and publishes nothing if one is missing.
3. **Seeing its own blocks first / blocks only this node has.** A node has its own block the
   instant it makes it, and a block of its own that never propagated would exist only here.
   `live.py` publishes a block only once the chain has confirmed it, by one rule that is the
   same for every account and never uses which account runs the node: a later block (slot at
   least 15 s old) made by a different account, and at least 3 keys of other accounts whose
   votes, cast at the block's slot or later, are carried in blocks made by other accounts.
   The head shown is the newest confirmed block; empty slots only up to it; `day_blocks`
   leaves unconfirmed blocks out. Cost: in 12 dry runs the head shown started 53 to 163 s
   before the run (median 93 s), 1 to 3 blocks behind the node's head (median 2). Each run also starts after a random
   0 to 10 s pause, so its phase is not tied to the slots.
4. **Node-only fields** of `/chain` (`own_turns`, `proposes`, `sync`, `catch_up`, …) and block
   roots are not published; `head` and `finalized` are `{height, slot, time}` only. `state` is
   always `"live"`: nothing is published while the node's chain is in any other state, or while
   its head is more than 90 s old (a lagging node).
5. **POST timing.** Every 30 s at whatever phase launchd starts it; `generated_unix` shows that
   cadence, which says nothing about an account. The address is seen by Cloudflare, not by
   readers.

Not solved (open): **outages line up.** When the collecting machine stops (sleep, reboot,
network), live.json goes stale at the same moment its own key stops voting and proposing on
the chain. Repeated a few times, that points to the account. The GitHub files have the same
property (`generated_unix` gaps), so this adds precision (30 s instead of 5 min), not a new
kind of leak.
