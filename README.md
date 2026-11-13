# 6529 network data

Public data about the 6529 validator network, collected every 10 minutes from a
6529 Validator node and published here as plain JSON.

Everything in `docs/data/` is something every validator node already receives from
the network. Nothing here identifies the collecting machine: no keys, peers, servers,
addresses or account flags.

## Files

| File | What it is |
|---|---|
| `docs/data/network.json` | Collection time, rule version, chain, today's cycle day, latest certified and final days. |
| `docs/data/validators.json` | Every validating account (wallet, TDH, weight) and its machines (8-character key code, today's state, signed days in the last 7 and 30, software version, and the continent and country the operator chose to report). |
| `docs/data/cycles/<day>.json` | One cycle (day number = days since 1970-01-01 UTC): window, header, roots and certificate. |
| `docs/data/cycles/index.json` | One summary line per cycle. |

Raw URLs: `https://raw.githubusercontent.com/Casa-NUA/6529-network-data/main/docs/data/<file>`

## Check it yourself

If you run a 6529 Validator, run the same collector against your own node and
compare. Cycle roots and certificates must match exactly; validator lists can
differ only by what changed between the two collection times.

```sh
git clone https://github.com/Casa-NUA/6529-network-data && cd 6529-network-data
python3 collector/collect.py --out /tmp/mine          # finds your node's loopback API
diff <(python3 -m json.tool /tmp/mine/cycles/20769.json) \
     <(python3 -m json.tool docs/data/cycles/20769.json)
```

`collector/collect.py` uses only the Python standard library and reads only the
node's loopback API (`status`, `network`, `cycles/<day>`). The git history is the
record of what was published and when.

## Credits

The collector is derived from [6529scan](https://github.com/nothingbugger/6529scan)'s
(MIT), and keeps its output format.
