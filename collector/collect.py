#!/usr/bin/env python3
"""Collect PUBLIC network data from a local 6529 validator node into static JSON.

Derived from 6529scan's collector (MIT, Copyright (c) 2026 nothingbugger,
https://github.com/nothingbugger/6529scan); same output shape (minus the node's own
`release` in network.json), so anything that reads 6529scan's data can read this. Changes: finds the validator's loopback port
whatever user runs it (the desktop app runs it as the logged-in user), checks the
port really is the validator, and publishes only continent and country from each
machine's self-reported location (not state or metro).

Runs next to the node and reads only its loopback API.
Writes only network-wide facts: cycle headers, roots, certificates and
verification outcomes. It never writes this node's keys, peers, servers,
location, account or anything else that identifies the machine.

Python 3.9+, standard library only.

    python3 collect.py --out /path/to/site/data [--api http://127.0.0.1:6529/api/v1]
"""
import argparse
import http.client
import json
import os
import sys
import tempfile
import time
import urllib.error
import urllib.request

DEFAULT_API = "http://127.0.0.1:6529/api/v1"
VALIDATOR_PATTERNS = (
    "6529 Validator.app/Contents/Resources/validator",   # the desktop app (macOS)
)


def _is_validator(api):
    """True when `api` answers like a validator node (6529 Desktop can own :6529)."""
    try:
        s = get(api, "status", timeout=5)
        return isinstance(s, dict) and "release" in s and "today" in s
    except (urllib.error.URLError, http.client.HTTPException, OSError, ValueError):
        return False


def detect_api():
    """Find the validator's loopback API (rc.8+ picks a random port).

    Finds the validator process (the desktop app's, or the `_validator6529`
    service user's), reads its 127.0.0.1 LISTEN ports, and returns the first one
    that answers like a validator. lsof covers a process of the same user;
    `netstat -anv` lists the owning pid without root, so it covers the service
    user. Falls back to :6529 only if that port is a validator too.
    """
    import subprocess
    pids = []
    try:
        for pat in VALIDATOR_PATTERNS:
            pids += subprocess.run(["/usr/bin/pgrep", "-f", pat],
                                   capture_output=True, text=True, timeout=10).stdout.split()
        pids += subprocess.run(["/usr/bin/pgrep", "-u", "_validator6529", "-x", "validator"],
                               capture_output=True, text=True, timeout=10).stdout.split()
    except (OSError, subprocess.SubprocessError):
        pass
    pids = list(dict.fromkeys(pids))
    ports = []
    for pid in pids:
        try:
            out = subprocess.run(["/usr/sbin/lsof", "-nP", "-a", "-p", pid, "-iTCP", "-sTCP:LISTEN"],
                                 capture_output=True, text=True, timeout=10).stdout
        except (OSError, subprocess.SubprocessError):
            continue
        for line in out.splitlines():
            for tok in line.split():
                if tok.startswith("127.0.0.1:") and tok.rsplit(":", 1)[1].isdigit():
                    ports.append(tok.rsplit(":", 1)[1])
    if pids:
        # netstat -anv: "tcp4 0 0 127.0.0.1.34004 *.* LISTEN ... <pid> ..." (no root needed)
        try:
            out = subprocess.run(["/usr/sbin/netstat", "-anv", "-p", "tcp"],
                                 capture_output=True, text=True, timeout=10).stdout
        except (OSError, subprocess.SubprocessError):
            out = ""
        for line in out.splitlines():
            f = line.split()
            if "LISTEN" in f and len(f) > 4 and f[3].startswith("127.0.0.1.") \
                    and any(t == p or t.endswith(":" + p) for t in f[4:] for p in pids):
                port = f[3].rsplit(".", 1)[1]
                if port.isdigit():
                    ports.append(port)
    for port in dict.fromkeys(ports):
        api = "http://127.0.0.1:%s/api/v1" % port
        if _is_validator(api):
            return api
    return DEFAULT_API if _is_validator(DEFAULT_API) else None

CERT_FIELDS = (
    "cycle_day", "cycle_root", "level", "signed_weight", "w", "raw_signed_tdh",
    "raw_registered_tdh", "network_tdh", "signing_accounts", "roster_count",
    "roster_root", "weight_basis", "flags", "excluded_accounts", "key_count",
    "signed_keys", "roster_accounts", "signer_weights", "unsigned_weights",
    "strong_unix",
)
HEADER_FIELDS = (
    "cycle_day", "boundary", "tdh_rule_id", "cutoff_number", "cutoff_hash",
    "cutoff_timestamp", "wallet_root", "wallet_count", "identity_count",
    "total_tdh", "token_root", "token_count",
)


def get(api, path, timeout=20):
    req = urllib.request.Request(api.rstrip("/") + "/" + path.lstrip("/"),
                                 headers={"Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def pick(d, fields):
    if not isinstance(d, dict):
        return None
    return {k: d.get(k) for k in fields if k in d}


def clean_cert(c):
    return pick(c, CERT_FIELDS)


def clean_cycle(rec):
    """Whitelist the public parts of one cycle record."""
    out = {
        "day": rec.get("day"),
        "state": rec.get("state"),
        "window": pick(rec.get("window"), ("opens", "closes", "final_at", "frozen_at")),
        "finalized_unix": rec.get("finalized_unix"),
        "header": pick(rec.get("header"), HEADER_FIELDS),
        "cycle_root": rec.get("cycle_root"),
        "history_root": rec.get("history_root"),
        "history_count": rec.get("history_count"),
        "certificate": clean_cert(rec.get("certificate")),
        "other_roots": len(rec.get("other_roots") or []),
    }
    dr = rec.get("dual_run")
    if isinstance(dr, dict):
        out["dual_run"] = {
            "implementations": dr.get("implementations"),
            "outcome": dr.get("outcome"),
            "differences": len(dr.get("differences") or []),
        }
    cmp_ = rec.get("compare")
    if isinstance(cmp_, dict):
        out["compare"] = pick(cmp_, ("status", "identities_compared", "identities_differing",
                                     "wallets_compared", "wallets_differing", "totals"))
    return out


# State and metro are left out on purpose: continent and country are enough to place a
# validator on a map, and finer self-reported location is not ours to republish.
REPORT_FIELDS = ("infrastructure", "continent", "country", "software", "source_class")
MACHINE_FIELDS = ("code", "weight", "today", "first_signed_day", "last_signed_day", "signed_7", "signed_30")


def clean_validators(network):
    """Who validates: accounts and machines as the network broadcasts them.

    Drops the `own` flag (it would single out this node), links, and the key ids
    of current members. `next_members` keeps the public keys the network announces
    for tomorrow's roster, as 6529scan does. Location is only what each operator
    chose to publish in its daily report, reduced to continent and country.
    """
    members = network.get("members")
    if not isinstance(members, dict) or not isinstance(members.get("accounts"), list) \
            or not members["accounts"]:
        return None  # a node that is restarting or syncing reports no members yet
    accounts = []
    for a in members["accounts"]:
        machines = []
        for m in a.get("machines") or []:
            mm = pick(m, MACHINE_FIELDS)
            rep = m.get("report")
            mm["report"] = pick(rep, REPORT_FIELDS) if isinstance(rep, dict) else None
            machines.append(mm)
        accounts.append({"wallet": a.get("wallet"), "tdh": a.get("tdh"),
                         "weight": a.get("weight"), "machines": machines})
    accounts.sort(key=lambda x: x.get("weight") or 0, reverse=True)
    nxt = [pick(m, ("wallet", "keys", "tdh")) for m in network.get("next_members") or []]
    return {
        "cycle_day": members.get("cycle_day"),
        "w": members.get("w"),
        "accounts": accounts,
        "next_day": (members.get("cycle_day") or 0) + 1,
        "next_members": nxt,
        "continents": network.get("continents") or [],
        "places": network.get("places") or [],
    }


CHAIN_FIELDS = (
    "state", "reason", "rule_version", "network_id", "registry_domain",
    "start_day", "start_date", "start_time", "head", "finalized",
    "slot_ms", "epoch_slots", "final_after_ms", "currency", "switch", "rules", "fees",
)


def clean_chain(chain, now):
    """The 6529 chain (rc.16+) as this node sees it: public chain facts only.

    Drops what describes this machine rather than the chain: whether it proposes
    blocks and why not, its catch-up progress and its local record count.
    """
    if not isinstance(chain, dict) or "state" not in chain:
        return None
    c = pick(chain, CHAIN_FIELDS)
    c["generated_unix"] = now
    return c


def summary(c):
    cert = c.get("certificate") or {}
    hdr = c.get("header") or {}
    net = cert.get("network_tdh") or hdr.get("total_tdh")
    signed = cert.get("raw_signed_tdh")
    return {
        "day": c["day"],
        "state": c.get("state"),
        "level": cert.get("level"),
        "cycle_root": c.get("cycle_root") or cert.get("cycle_root"),
        "signing_accounts": cert.get("signing_accounts"),
        "roster_accounts": cert.get("roster_accounts"),
        "signed_keys": cert.get("signed_keys"),
        "key_count": cert.get("key_count"),
        "signed_tdh": signed,
        "registered_tdh": cert.get("raw_registered_tdh"),
        "network_tdh": net,
        "signed_share": (signed / net) if (signed and net) else None,
        "identities": hdr.get("identity_count"),
        "wallets": hdr.get("wallet_count"),
        "tokens": hdr.get("token_count"),
        "cutoff_number": hdr.get("cutoff_number"),
        "boundary": hdr.get("boundary"),
    }


def write_json(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), suffix=".tmp")
    with os.fdopen(fd, "w") as f:
        json.dump(obj, f, separators=(",", ":"), sort_keys=True)
    os.replace(tmp, path)
    os.chmod(path, 0o644)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True, help="site data directory")
    ap.add_argument("--api", default=None, help="node API base (default: auto-detect)")
    a = ap.parse_args()
    a.api = a.api or detect_api()
    if not a.api:
        print("no validator API found on this machine", file=sys.stderr)
        return 1
    out = os.path.abspath(a.out)

    try:
        status = get(a.api, "status")
        network = get(a.api, "network")
    except (urllib.error.URLError, http.client.HTTPException, OSError, ValueError) as e:
        print("node API not reachable: %s" % e, file=sys.stderr)
        return 1

    today = status.get("today")
    if not isinstance(today, int) or not network.get("latest_certified_day"):
        # Restarting or still syncing: keep the files already published.
        print("node not ready (today=%r, latest certified=%r)" % (
            today, network.get("latest_certified_day")), file=sys.stderr)
        return 1
    first = network.get("first_day") or today
    cycles_dir = os.path.join(out, "cycles")
    os.makedirs(cycles_dir, exist_ok=True)

    days = set()
    for name in os.listdir(cycles_dir):
        if name.endswith(".json") and name[:-5].isdigit():
            days.add(int(name[:-5]))

    for day in range(int(first), int(today) + 1):
        try:
            rec = get(a.api, "cycles/%d" % day).get("record")
        except (urllib.error.URLError, http.client.HTTPException, OSError, ValueError):
            continue
        if not rec:
            continue
        c = clean_cycle(rec)
        if not c.get("header") and not c.get("certificate"):
            continue  # nothing public to show yet
        write_json(os.path.join(cycles_dir, "%d.json" % day), c)
        days.add(day)

    index = []
    for day in sorted(days, reverse=True):
        try:
            with open(os.path.join(cycles_dir, "%d.json" % day)) as f:
                index.append(summary(json.load(f)))
        except (OSError, ValueError):
            pass
    write_json(os.path.join(cycles_dir, "index.json"), index)

    netinfo = status.get("network") or {}
    # No `release`: that is this node's own software version, not a network fact.
    write_json(os.path.join(out, "network.json"), {
        "generated_unix": int(time.time()),
        "rule_version": status.get("rule_version"),
        "chain_id": netinfo.get("chain_id"),
        "network_id": netinfo.get("network_id"),
        "today": today,
        "first_day": network.get("first_day"),
        "latest_certified_day": network.get("latest_certified_day"),
        "latest_final_day": network.get("latest_final_day"),
        "next_keys": network.get("next_keys"),
        "next_accounts": network.get("next_accounts"),
    })

    # The 6529 chain (rc.16+). Optional: older nodes have no /chain.
    try:
        chain = clean_chain(get(a.api, "chain"), int(time.time()))
    except (urllib.error.URLError, http.client.HTTPException, OSError, ValueError):
        chain = None
    if chain:
        write_json(os.path.join(out, "chain.json"), chain)

    vals = clean_validators(network)
    if vals:
        write_json(os.path.join(out, "validators.json"), vals)
    print("ok: %d cycles, latest certified %s, %s validator accounts, chain %s" % (
        len(index), network.get("latest_certified_day"), len(vals["accounts"]) if vals else 0,
        "%s at height %s" % (chain.get("state"), (chain.get("head") or {}).get("height")) if chain else "-"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
