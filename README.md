# 6529 network data

Public data about the 6529 validator network, collected about every 10 minutes from
one 6529 Validator node run by Casa NUA and published here as JSON. The mobile app
6529 Validator Monitor reads these files.

## What this is, and what it is not

- It is **one node's view** of the network, republished. It is not an official 6529
  publication.
- Most values are things every validator node receives from the network: cycle
  headers, roots, certificate summaries, the validator list. A few are this node's
  **own observations** and will differ slightly between nodes: `strong_unix`,
  `finalized_unix`, `compare`, `dual_run`, and everything in `chain.json`.
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
| `docs/data/validators.json` | `cycle_day`, `w` (total weight), `accounts`: every validating account (`wallet`, `tdh`, `weight`) and its `machines` (`code`: 8-character key code, `weight`, `today`: today's state, `first_signed_day`, `last_signed_day`, `signed_7`, `signed_30`, and `report`: what the operator chose to declare: `infrastructure` (home or rack), `continent`, `country`, `software` version, `source_class`). `next_day` and `next_members` (per wallet: number of keys and TDH announced for tomorrow). `continents` and `places`: totals. |
| `docs/data/chain.json` | The 6529 chain (from validator 0.1.0-rc.16, test currency $fake6529), as the collecting node sees it: `state`, `reason`, `head`, `finalized`, timing, `currency`, `rules` and `fees`. Updated every run, published at least hourly. |
| `docs/data/cycles/<day>.json` | One cycle (day number = days since 1970-01-01 UTC): `state`, `window`, `header`, `cycle_root`, `history_root`, `history_count`, `certificate` (summary: level, weights, counts, roots), `compare`, `dual_run`, `other_roots` (a count), `finalized_unix`. |
| `docs/data/cycles/index.json` | One summary line per cycle. |
| `docs/data/signed/<same names>` | The same files, each wrapped with a signature (see "Signatures"). |

Raw URLs: `https://raw.githubusercontent.com/Casa-NUA/6529-network-data/main/docs/data/<file>`

Timing: the collector runs every 10 minutes. A commit is made when something other
than the collection time changed, and at least once an hour, so a file here can be up
to about an hour behind the node, plus a few minutes of GitHub caching. If the node or
the collector stops, the last files stay as they are: check `generated_unix`, and
`validators_unix` / `cycle_unix` for the age of those two files.

## Personal data

The validator list is public on the network. For each wallet it shows TDH, weight,
signing status, software version and, only where the operator chose to declare it, a
continent, a country and whether the machine is at home or in a rack. Nobody verifies
those declarations. Because this is a git repository, every past publication stays in
its history: an operator who later removes a declared location cannot remove it from
past snapshots here. State and metro, which the network also carries, are not
published here. There are no names, handles or IP addresses.

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
reads only the node's loopback API (`status`, `network`, `cycles/<day>`, `chain`).

Another independent collector of the same network data is
[6529scan](https://github.com/nothingbugger/6529scan); its cycle roots can be compared
with these.

## Running it as a publisher

Copy `collector/collect.py`, `collector/sign.py` and `collector/publish.sh` to a folder
**outside** the publishing clone and run them from there. `publish.sh` refuses to run
from inside the clone or any git work tree. The clone is reset to `origin/main` on
every run, so its content is whatever was last pushed: the script therefore collects
and signs in its own `state/data` folder, which nothing from the repository enters,
and only then copies the result into the clone. It also refuses a repository tree
that contains links, submodules or git control files. Keep the signing key in the
collector folder, never in the repository. See the comments in `collector/publish.sh`
and `collector/casa.nua.6529-network-data.plist`.

## Contact

Questions, corrections and security reports: bugs@nua.casa

## Licence and credits

Code: MIT (see `LICENSE`). The data is public network data, republished as is. The collector is derived from
[6529scan](https://github.com/nothingbugger/6529scan)'s (MIT) and keeps its output
format, minus the fields described above.
