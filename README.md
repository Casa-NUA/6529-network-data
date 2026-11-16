# 6529 network data

Public data about the 6529 validator network, collected about every 5 minutes from
one 6529 Validator node run by Casa NUA and published here as JSON. The mobile app
6529 Validator Monitor reads these files.

## What this is, and what it is not

- It is **one node's view** of the network, republished. It is not an official 6529
  publication.
- Most values are things every validator node receives from the network: cycle
  headers, roots, certificate summaries, the validator list. A few are this node's
  **own observations** and will differ slightly between nodes: `strong_unix`,
  `finalized_unix`, `compare`, `dual_run`, and everything in `chain.json`,
  `blocks.json` and `awards.json`.
- The files do not include the validators' own signatures, so a certificate cannot be
  verified from this data alone. To check it, compare with your own node (below).
- The collector publishes only what a strict schema in `collector/collect.py` allows.
  It does not publish this node's keys, peers, servers or location.

### About the collecting node's own account

Until 14 November 2026 the cycle files included two lists, `certificate.signer_weights`
and `certificate.unsigned_weights`. A node leaves its own account out of those lists,
so the missing weight could be worked out by subtraction and matched to one account in
`validators.json`. An earlier version of this README said nothing here identified the
collecting machine; because of those lists, that was not true. The lists are no longer
published, but they remain in this repository's history. The collector runs on Casa
NUA's own validator. This was found by the transparency audit of 14 November 2026.

## Files

| File | What it is |
|---|---|
| `docs/data/network.json` | `generated_unix` (collection time), `validators_unix` and `cycle_unix` (when `validators.json` and today's cycle file were last written: normally the same moment, older if the node could not supply them on the last run; `null` when unknown, and `cycle_unix` is `null` on a new day until that day's file exists), `rule_version`, `chain_id`, `network_id`, `today` (the cycle day), `first_day`, `latest_certified_day`, `latest_final_day`, `next_keys` and `next_accounts` (sizes of tomorrow's roster). |
| `docs/data/validators.json` | `cycle_day`, `w` (total weight), `accounts`: every validating account (`wallet`, `tdh`, `weight`) and its `machines` (`code`: 8-character key code, `weight`, `today`: today's state, `signed_unix`: when it signed today's result, in unix seconds, or null (validator rc.17 and later), `first_signed_day`, `last_signed_day`, `signed_7`, `signed_30`, and `report`: what the operator chose to declare: `infrastructure` (home or rack), `continent`, `country`, `software` version, `source_class`). Each account also has `handle`: its public 6529 profile name, from 6529's public API (`api.6529.io`), or `null` when it has none or it has not been looked up yet. `next_day` and `next_members` (per wallet: number of keys and TDH announced for tomorrow). `continents` and `places`: totals. |
| `docs/data/chain.json` | The 6529 chain (from validator 0.1.0-rc.16, test currency $fake6529), as the collecting node sees it: `state`, `reason`, `head`, `finalized`, timing, `currency`, `rules` and `fees`. Updated every run, published every 5 minutes. |
| `docs/data/blocks.json` | Who made (proposed) the 6529 chain's blocks, on the collecting node's current chain, for up to 7 chain days: `day` (the UTC day of the head block), `head_height`, `days` (day numbers, oldest first; a day starts at `day * 86400` UTC), `totals` (blocks per day, all makers) and `accounts`: per wallet, `counts` (its blocks per day, same order), `today_unix` (the slot time of each block it made today) and `last_unix` (the slot time of its latest block in those days). Only accounts that made a block in those days are listed; a maker that is not a validating account today is counted in `totals` but not listed. Counts are blocks proposed, not votes, and include blocks not yet final, so a count can drop after a reorganisation. Read from the node's chain store (see "Run it yourself"). |
| `docs/data/days/<day>.json` | One past day's validator list, as last collected on that day (each machine's state at the day's end): `day`, `final` (always `true`), `w`, and `accounts` (weight first): `wallet`, `handle`, `tdh`, `weight`, `machines` (`code`, `weight`, `today`, `report` as in `validators.json`). Written once, when the day changes, and never rewritten. Days before 15 November 2026 were rebuilt from this repository's own history (the last `validators.json` committed for that day, put through the same schema); their handles are those known when they were rebuilt, so some are `null`. A day's verdict and signed share are in `cycles/<day>.json`. |
| `docs/data/days/index.json` | `{"days": [...]}`: the days that have a file, oldest first. Today is never listed (it is `validators.json`). |
| `docs/data/history.json` | Network growth, one row per day, oldest first, the last row being today so far: `day`, `validators` and `signed` (machines listed, and machines that signed, at the day's end), `accounts` and `accounts_signed`, `w`, `continents` (machines per self-declared continent, `UNKNOWN` when none) and `software` (machines per self-declared version, `UNKNOWN` when none). Built from the day files and today's list; `generated_unix` is the collection time. |
| `docs/data/awards.json` | The 6529 chain's awards (test currency), from the collecting node's `awards/network` routes (validator rc.18 and later): `currency`, `decimals`, `test`, and `days`, the last up to 7 chain days, newest last: `day`, `state` (`open` or `settled`), `final`, `slots`, `blocks` (the day's blocks), `award_total` (what the chain paid for the day; `null` until it settles, the next morning), `recorded` and `accounts`. Per account (`wallet`; an account with several keys is summed over them): `blocks` and `votes` (blocks it proposed and votes it cast that day, `null` when unknown) and `award` (`total`, `blocks_part`, `votes_part`, `null` for an open day). Amounts are decimal strings in the smallest unit (18 decimals). **`blocks` and `votes` count only what the collecting node recorded**: `recorded.blocks` / `recorded.votes` say `all`, `part` (it saw only part of the day, for example because it started during it) or `none` (then they are `null`). Awards are the chain's own figures, whatever the node recorded. An account with no block, no vote and nothing paid that day is left out. `live`: `as_of_unix`, `window_s` (600) and `keys`: every machine key code (as in `validators.json`) that proposed or voted in a block in the last `window_s` seconds before `as_of_unix`, with the time of its newest such block; `null` when the node recorded no blocks then. Per-account `blocks` and `votes` are published only for days the collecting node recorded in full (`recorded` = `all`); otherwise null. `live` is null when the routes cannot be read, when the node recorded no blocks in the window, or when the chain's newest block is more than 2 minutes old. `balances` (validator rc.19 and later, from the node's `chain/account/<wallet>` route, which answers the same for any account): `as_of_unix` and `accounts`, one row per validating account in `validators.json`: `wallet`, `balance` (what the account can send now), `vesting` (awarded but still vesting), `earned` (everything the chain has awarded it, all days) and `blocks` (blocks it proposed, all days), amounts as decimal strings in the smallest unit (18 decimals), like the awards. An account whose four figures are all zero is left out. The accounts are read in rotation, up to 80 per run (each run reads first the ones read longest ago), so a row can be some minutes older than the file: `as_of_unix` is the time of the OLDEST read among the rows shown. An account that leaves `validators.json` is dropped, and a row that could not be read again for an hour is no longer shown. Balances change every morning at 00:04 UTC, when the day's vesting part is released (from 18 Nov 2026: what is vesting then, and each new award, is released in equal daily parts over 90 days), and whenever an account sends or receives a transfer. `balances` is null when the node has no such route (older node). |
| `docs/data/cycles/<day>.json` | One cycle (day number = days since 1970-01-01 UTC): `state`, `window`, `header`, `cycle_root`, `history_root`, `history_count`, `certificate` (summary: level, weights, counts, roots), `compare`, `dual_run`, `other_roots` (a count), `finalized_unix`. |
| `docs/data/cycles/index.json` | One summary line per cycle. |
| `docs/data/signed/<same names>` | The same files, each wrapped with a signature (see "Signatures"). |

Raw URLs: `https://raw.githubusercontent.com/Casa-NUA/6529-network-data/main/docs/data/<file>`

Timing: the collector runs every 5 minutes. A commit is made when something other
than the collection time changed, and otherwise on every run too, since the chain's
head (`chain.json`, `blocks.json`, `awards.json`) moves all the time: a file here is at most about 5
minutes old. This makes many small commits; old history may be trimmed from time to
time, as readers only need the current files.

Why not more often than 5 minutes: GitHub serves these raw files through a cache that
keeps each file for 5 minutes (`cache-control: max-age=300`). A reader can therefore be
handed a copy up to 5 minutes old whatever the collector does. Running the collector
more often would shave little off the delay, because the cache dominates, while adding
commits. A change in the network usually reaches a reader within about 10 minutes (up
to 5 for the collector's next run and up to 5 for the cache), and sometimes a little
more, since the cache can hand out a copy that was already a few minutes old. If the node or
the collector stops, the last files stay as they are: check `generated_unix`, and
`validators_unix` / `cycle_unix` for the age of those two files.

## Personal data

The validator list is public on the network. For each wallet it shows TDH, weight,
signing status, software version and, only where the operator chose to declare it, a
continent, a country and whether the machine is at home or in a rack. Nobody verifies
those declarations. Because this is a git repository, every past publication stays in
its history: an operator who later removes a declared location cannot remove it from
past snapshots here. `blocks.json` and `awards.json` add, for each wallet, how many blocks it made and
votes it cast on the 6529 chain, when, and what the chain paid it: public facts of that
chain, which also show when an operator's validator was running (`awards.json`'s `live`
shows which machines voted in the last 10 minutes). State and metro, which the network
also carries, are not published here. There are no names beyond each account's public 6529 handle (which can be a real name, as its owner chose it on 6529), and no IP addresses.

Handles: `validators.json` and `days/` show each validating wallet's **6529 handle**,
the public profile name its owner chose on 6529 (as `https://api.6529.io/api/identities/<wallet>`
returns it to anyone). They link a wallet to that public profile name, nothing more. The
collector looks up at most 25 wallets per run and each wallet at most once every 7 days,
so a changed or removed handle shows here within about a week; past day files keep the
handle known when they were written. A wallet with no profile shows `null`.

## Signatures

Each file in `docs/data/signed/` is a JSON envelope:

```json
{"format": 2, "alg": "ECDSA-P256-SHA256", "key": "9fc874c0f34c3d51",
 "name": "network.json",
 "payload": "<the data file's exact text>",
 "sig": "<base64 of the DER signature>"}
```

The signature is over the bytes of `name + "\n" + payload`. The name is signed too, so
a signed file cannot be passed off as another one. `key` is the first 16 hex digits of
the SHA-256 of the public key (DER). Public keys:

- `collector/signing-key.pub.pem`, key id `9fc874c0f34c3d51`: the key in use.
- `collector/reserve-key.pub.pem`, key id `04f3b06b89f126a7`: a reserve key, to replace
  the first if it is lost or exposed.

These two files sit in the same repository as the data, so do not take the key from
here alone: the key ids above are also built into the 6529 Validator Monitor app and
listed in its public audit reports.

To check one file:

```sh
python3 -c '
import base64, json
e = json.load(open("docs/data/signed/network.json"))
open("/tmp/message", "wb").write((e["name"] + "\n" + e["payload"]).encode())
open("/tmp/sig", "wb").write(base64.b64decode(e["sig"]))'
openssl dgst -sha256 -verify collector/signing-key.pub.pem -signature /tmp/sig /tmp/message
```

What a valid signature means: the holder of the key published this file under this
name. It does not prove the data is true (it is one node's view), and it does not
prove a file is recent: for that, read `generated_unix` in `network.json` and the
`day` inside a cycle file. An old signed file stays validly signed, so someone able to
change files on GitHub could still put back an older one; they could not write a new
one. The signing happens on the collecting machine, in a folder the repository's
content never enters, so the signature does not protect against that machine being
taken over. Commits in this repository are not signed.

## Check it yourself

If you run a 6529 Validator, run the same collector against your own node and compare.

```sh
git clone https://github.com/Casa-NUA/6529-network-data && cd 6529-network-data
python3 collector/collect.py --out /tmp/mine
python3 - <<'EOF'
import json
day = "20771"   # a certified day present in both
a = json.load(open("/tmp/mine/cycles/%s.json" % day))
b = json.load(open("docs/data/cycles/%s.json" % day))
for k in ("cycle_root", "history_root", "header"):
    print(k, "same" if a.get(k) == b.get(k) else "DIFFERENT")
print("roster_root", "same" if a["certificate"]["roster_root"] == b["certificate"]["roster_root"] else "DIFFERENT")
EOF
```

`cycle_root`, `history_root`, `header` and `roster_root` must be identical. Timestamps
such as `strong_unix` and `finalized_unix`, and the counts of a day still in progress,
differ between nodes. Validator lists differ by what changed between the two
collection times.

The collector finds the node's loopback API by itself on macOS (desktop app or
service install). Elsewhere, or if it finds nothing, pass it:
`--api http://127.0.0.1:<port>/api/v1`. It uses only the Python standard library and
reads the node's loopback API (`status`, `network`, `cycles/<day>`, `chain`, and from
rc.18 `awards/network/days`, `awards/network/day/<day>` and
`awards/network/day/<day>/blocks`). The one request that leaves the machine is the
handle lookup at `api.6529.io`. For
`blocks.json` it also reads one file of the node's chain store on disk, the index of
blocks (`chain/<id>/index/blocks.log`: public chain facts), and the node's own block
count from the API (`chain/me`, `awards`, `awards/days`), which it uses only to check
that it read that file right and never publishes as such. The index's format is not
documented by 6529 and may change with a release: when the check fails, `blocks.json`
is simply not refreshed, and its `generated_unix` shows its age.

Another independent collector of the same network data is
[6529scan](https://github.com/nothingbugger/6529scan); its cycle roots can be compared
with these.

## Running it as a publisher

Copy `collector/collect.py`, `collector/sign.py` and `collector/publish.sh` to a folder
**outside** the publishing clone and run them from there. `publish.sh` refuses to run
from inside the clone or any git work tree. The clone is reset to `origin/main` on
every run, so its content is whatever was last pushed: the script therefore collects
and signs in its own `state/data` folder, which nothing from the repository enters,
and only then copies the result into the clone. The collector's own state (the handle
cache, the day's last validator list, settled award days) is `state/collector-state.json`,
next to `state/data` and never published. Once, after installing this version, run
`python3 -I collector/backfill_days.py --repo <clone> --out <collector>/state/data` to
rebuild the past days from this repository's history. It also refuses a repository tree
that contains links, submodules or git control files. Keep the signing key in the
collector folder, never in the repository. See the comments in `collector/publish.sh`
and `collector/casa.nua.6529-network-data.plist`.

## Contact

Questions, corrections and security reports: bugs@nua.casa

## Licence and credits

Code: MIT (see `LICENSE`). The data is public network data, republished as is. The collector is derived from
[6529scan](https://github.com/nothingbugger/6529scan)'s (MIT) and keeps its output
format, minus the fields described above.

Notes for the publisher: run `collector/backfill_days.py` only after the handle cache has filled
(about 9 runs), since a day file is never rewritten. Losing the state file across midnight loses that
day's file (re-run the backfill). Handle lookups go to api.6529.io from the collector's own
connection, so 6529 sees that address; only the public wallet being looked up is sent.

## Live chain feed (16 Nov 2026)

Besides the files in this repository, the collecting machine runs `collector/live.py` every 30 s
(launchd agent `collector/casa.nua.6529-live.plist`). It reads the last 60 slots of the 6529 chain
from the same node, signs them like every file here (name `live.json`, same keys and envelope) and
posts them to a Cloudflare Worker (`live-worker/`), which keeps only the latest envelope and serves
it at https://monitor6529-live.casanua.workers.dev/v1/live. The apps read it only while their Chain
page is open, and check the signature themselves; the Worker accepts a post only with a valid
signature from the pinned keys, never an older one, within a short clock window.

Privacy rules: a block is published only once later blocks by other accounts confirm it (with votes
cast at or after its slot from at least 3 keys of other accounts, in a block whose slot started at
least 30 s before), and each run starts after a random pause, so what appears and when depends on
the public chain, not on what this node saw first. The page therefore runs about 1.5 to 3 minutes
behind the chain.

Known limit: the feed comes from one node. When that node or its machine stops (sleep, network,
restart, update), the feed goes stale at the same moment that node's own key stops voting on the
chain, so an observer comparing the two over several outages could work out which account runs the
collecting node. The files above have the same property at a coarser 5-minute grain. The fix is to
feed the collector from a separate, non-voting validator; planned.
