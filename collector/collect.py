#!/usr/bin/env python3
"""Collect PUBLIC network data from a local 6529 validator node into static JSON.

Derived from 6529scan's collector (MIT, Copyright (c) 2026 nothingbugger,
https://github.com/nothingbugger/6529scan); same output shape, minus the node's own
`release` in network.json and the certificate's per-signer weight lists, so anything
that reads 6529scan's data without those can read this. Changes: finds the validator's loopback port
whatever user runs it (the desktop app runs it as the logged-in user), checks the
port really is the validator, and publishes only continent and country from each
machine's self-reported location (not state or metro).

Runs next to the node and reads only its loopback API.
Writes only what a strict schema allows (see "What may be published" below): cycle
headers, roots, certificate summaries and verification outcomes, the validator list
and public chain facts. It does not write this node's keys, peers, servers or
location, and it leaves out the certificate lists from which the node's own account
could be worked out. Some values are this node's own observations rather than
network facts (strong_unix, finalized_unix, compare, dual_run, chain.json).

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

def get(api, path, timeout=20):
    req = urllib.request.Request(api.rstrip("/") + "/" + path.lstrip("/"),
                                 headers={"Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


# ---- What may be published -------------------------------------------------------
# A strict schema, checked at every level. A field is published only if it is named
# here AND its value has the expected type and form. Anything else the node returns,
# including sub-fields a future node version adds inside a known object, is dropped
# (and named on stderr, so the schema can be extended on purpose).
#
# Left out on purpose:
#   certificate.signer_weights / unsigned_weights: the node leaves its OWN account
#     out of these lists, so publishing them lets anyone work out, by subtraction,
#     which account the collecting node belongs to. (They were published until
#     14 Nov 2026 and remain in this repository's history.)
#   chain.switch: its shape is not known yet (always null so far).

import re

_SAFE = re.compile(r"^[A-Za-z0-9 _.$:/+\-]{1,64}$")
_HEX = re.compile(r"^0x[0-9a-fA-F]{1,128}$")
_NUM = re.compile(r"^[0-9]{1,80}$")
_MAXINT = 2 ** 63 - 1


def INT(v):
    return v if isinstance(v, int) and not isinstance(v, bool) and 0 <= v <= _MAXINT else _BAD


def BOOL(v):
    return v if isinstance(v, bool) else _BAD


def STR(v):
    return v if isinstance(v, str) and _SAFE.match(v) else _BAD


def HEX(v):
    return v if isinstance(v, str) and _HEX.match(v) else _BAD


_TEXT = re.compile(r"^[\x20-\x7e]{1,240}$")


def TEXT(v):  # a short printable-ASCII sentence
    return v if isinstance(v, str) and _TEXT.match(v) else _BAD


def NUMSTR(v):  # big integers the node sends as decimal strings
    return v if isinstance(v, str) and _NUM.match(v) else _BAD


def INT_OR_STR(v):
    return v if INT(v) is not _BAD else STR(v)


def RE(pattern):
    rx = re.compile(pattern)
    return lambda v: v if isinstance(v, str) and rx.match(v) else _BAD


class _Bad(object):
    pass


_BAD = _Bad()
_dropped = set()


def LIST(spec, most=5000):
    return ("list", spec, most)


def TUPLE(*specs):
    return ("tuple", specs)


def clean(v, spec, path=""):
    """`v` reduced to what `spec` allows; _BAD when it does not fit. None stays None."""
    if v is None:
        return None
    if callable(spec):
        return spec(v)
    if isinstance(spec, dict):
        if not isinstance(v, dict):
            return _BAD
        out = {}
        for k in v:
            if k not in spec:
                _dropped.add("%s.%s (not in schema)" % (path, k))
        for k, sub in spec.items():
            if k in v:
                r = clean(v[k], sub, path + "." + k)
                if r is _BAD:
                    _dropped.add("%s.%s (unexpected form)" % (path, k))
                else:
                    out[k] = r
        return out
    kind = spec[0]
    if kind == "list":
        if not isinstance(v, list) or len(v) > spec[2]:
            return _BAD
        out = []
        for x in v:
            r = clean(x, spec[1], path + "[]")
            if r is _BAD:
                _dropped.add("%s[] (unexpected form)" % path)
            else:
                out.append(r)
        return out
    if kind == "tuple":
        if not isinstance(v, list) or len(v) != len(spec[1]):
            return _BAD
        out = [clean(x, sp, path + "[]") for x, sp in zip(v, spec[1])]
        return _BAD if any(r is _BAD for r in out) else out
    return _BAD


CERT = {
    "cycle_day": INT, "cycle_root": HEX, "level": STR, "signed_weight": INT, "w": INT,
    "raw_signed_tdh": INT, "raw_registered_tdh": INT, "network_tdh": INT,
    "signing_accounts": INT, "roster_count": INT, "roster_root": HEX,
    "weight_basis": HEX, "flags": LIST(STR, 32), "excluded_accounts": INT,
    "key_count": INT, "signed_keys": INT, "roster_accounts": INT, "strong_unix": INT,
}
HEADER = {
    "cycle_day": INT, "boundary": INT, "tdh_rule_id": HEX, "cutoff_number": INT,
    "cutoff_hash": HEX, "cutoff_timestamp": INT, "wallet_root": HEX, "wallet_count": INT,
    "identity_count": INT, "total_tdh": INT, "token_root": HEX, "token_count": INT,
}
WINDOW = {"opens": INT, "closes": INT, "final_at": INT, "frozen_at": INT}
COMPARE = {
    "status": STR, "identities_compared": INT, "identities_differing": INT,
    "wallets_compared": INT, "wallets_differing": INT, "totals": LIST(INT, 8),
}


def _count(v):
    return len(v) if isinstance(v, list) else 0


def clean_cycle(rec):
    """The public parts of one cycle record, checked against the schema."""
    def part(key, spec):
        r = clean(rec.get(key), spec, "cycle." + key)
        if r is _BAD:
            _dropped.add("cycle.%s (unexpected form)" % key)
            return None
        return r
    out = {
        "day": part("day", INT),
        "state": part("state", STR),
        "window": part("window", WINDOW),
        "finalized_unix": part("finalized_unix", INT),
        "header": part("header", HEADER),
        "cycle_root": part("cycle_root", HEX),
        "history_root": part("history_root", HEX),
        "history_count": part("history_count", INT),
        "certificate": part("certificate", CERT),
        "other_roots": _count(rec.get("other_roots")),
    }
    dr = rec.get("dual_run")
    if isinstance(dr, dict):
        impl = clean(dr.get("implementations"), INT)
        outcome = clean(dr.get("outcome"), STR)
        out["dual_run"] = {
            "implementations": None if impl is _BAD else impl,
            "outcome": None if outcome is _BAD else outcome,
            "differences": _count(dr.get("differences")),
        }
    if isinstance(rec.get("compare"), dict):
        out["compare"] = part("compare", COMPARE)
    return out


# State and metro are left out on purpose: continent and country are enough to place a
# validator on a map, and finer self-reported location is not ours to republish.
REPORT = {
    "infrastructure": STR, "continent": STR, "country": RE(r"^[A-Z]{2}$"),
    "software": RE(r"^[0-9A-Za-z.+\-]{1,32}$"), "source_class": STR,
}
MACHINE = {
    "code": RE(r"^[0-9a-f]{8}$"), "weight": INT, "today": STR, "first_signed_day": INT,
    "last_signed_day": INT, "signed_7": INT, "signed_30": INT,
}
WALLET = RE(r"^0x[0-9a-fA-F]{40}$")
NEXT_MEMBER = {"wallet": WALLET, "keys": INT, "tdh": INT}
LABEL_COUNT = TUPLE(STR, INT)


def clean_validators(network):
    """Who validates: accounts and machines as the network broadcasts them.

    Drops the `own` flag (it would single out this node), links, and the key ids
    of current members. `next_members` keeps, per wallet, the NUMBER of keys the
    network announces for tomorrow's roster. Location is only what each operator
    chose to publish in its daily report, reduced to continent and country.
    """
    members = network.get("members")
    if not isinstance(members, dict) or not isinstance(members.get("accounts"), list) \
            or not members["accounts"]:
        return None  # a node that is restarting or syncing reports no members yet
    accounts = []
    for a in members["accounts"]:
        if not isinstance(a, dict) or clean(a.get("wallet"), WALLET) in (_BAD, None):
            _dropped.add("validators.accounts[] (no valid wallet)")
            continue
        machines = []
        for m in (a.get("machines") if isinstance(a.get("machines"), list) else []):
            if not isinstance(m, dict):
                continue
            mm = {k: m.get(k) for k in MACHINE if k in m}
            mm = clean(mm, MACHINE, "validators.machine")
            rep = clean(m.get("report"), REPORT, "validators.report") \
                if isinstance(m.get("report"), dict) else None
            mm["report"] = None if rep is _BAD else rep
            machines.append(mm)
        tdh, weight = clean(a.get("tdh"), INT), clean(a.get("weight"), INT)
        accounts.append({"wallet": a["wallet"], "tdh": None if tdh is _BAD else tdh,
                         "weight": None if weight is _BAD else weight, "machines": machines})
    if not accounts:
        return None
    accounts.sort(key=lambda x: x.get("weight") or 0, reverse=True)

    def lst(key, spec, most):
        r = clean(network.get(key) or [], LIST(spec, most), "validators." + key)
        return [] if r is _BAD else r
    day = clean(members.get("cycle_day"), INT)
    w = clean(members.get("w"), INT)
    day = None if day is _BAD else day
    return {
        "cycle_day": day,
        "w": None if w is _BAD else w,
        "accounts": accounts,
        "next_day": (day or 0) + 1,
        "next_members": lst("next_members", NEXT_MEMBER, 5000),
        "continents": lst("continents", LABEL_COUNT, 16),
        "places": lst("places", LABEL_COUNT, 16),
    }


BLOCK = {"height": INT, "root": HEX, "slot": INT, "time": INT}
CHAIN = {
    "state": STR, "reason": TEXT, "rule_version": INT_OR_STR, "network_id": HEX,
    "registry_domain": HEX, "start_day": INT,
    "start_date": RE(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$"), "start_time": INT,
    "head": BLOCK, "finalized": BLOCK,
    "slot_ms": INT, "epoch_slots": INT, "final_after_ms": INT,
    "currency": {"asset_id": HEX, "decimals": INT, "symbol": STR, "test": BOOL},
    "rules": {"vest_divisor": INT, "vest_dust": NUMSTR},
    "fees": {
        "base": LIST({"kind": STR, "per_unit": NUMSTR, "resource": INT}, 32),
        "transfer_estimate": NUMSTR, "transfer_max_work": INT, "transfer_most": NUMSTR,
    },
}
# Top-level keys of /chain that are known and deliberately NOT published: they describe
# this machine, not the chain (whether it proposes blocks and why not, catch-up, counts).
CHAIN_PRIVATE = ("proposes", "proposes_why", "catch_up", "records", "state_records", "switch")


def clean_chain(chain, now):
    """The 6529 chain (rc.16+) as this node sees it: public chain facts only."""
    if not isinstance(chain, dict) or "state" not in chain:
        return None
    for k in chain:
        if k not in CHAIN and k not in CHAIN_PRIVATE:
            _dropped.add("chain.%s (not in schema)" % k)
    c = clean({k: v for k, v in chain.items() if k in CHAIN}, CHAIN, "chain")
    if c is _BAD or "state" not in c:
        return None
    c["generated_unix"] = now
    return c


# Fields the node is known to return and that are left out on purpose. Anything dropped
# that is NOT listed here is reported on stderr: it is new, and someone should decide.
KNOWN_UNPUBLISHED = set("%s (not in schema)" % k for k in (
    # this node's own account and view (would identify the collecting node):
    "cycle.certificate.ours", "cycle.certificate.own_signed", "cycle.certificate.own_weight",
    "cycle.certificate.reported_by", "cycle.certificate.verified_here",
    "cycle.certificate.weight_own_node", "cycle.certificate.weight_verified",
    "cycle.certificate.signer_weights", "cycle.certificate.unsigned_weights",
    # local bookkeeping:
    "cycle.compare.by_class", "cycle.compare.compared_unix", "cycle.window.late",
    # finer self-reported location than we republish, and the report's own day:
    "validators.report.state", "validators.report.metro", "validators.report.cycle_day",
))

NETWORK = {
    "rule_version": STR, "chain_id": STR, "network_id": HEX, "today": INT,
    "first_day": INT, "latest_certified_day": INT, "latest_final_day": INT,
    "next_keys": INT, "next_accounts": INT,
}


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

    if not isinstance(status, dict) or not isinstance(network, dict):
        print("node API returned something unexpected", file=sys.stderr)
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
        except (urllib.error.URLError, http.client.HTTPException, OSError, ValueError, AttributeError):
            continue
        if not isinstance(rec, dict) or not rec:
            continue
        c = clean_cycle(rec)
        if c.get("day") != day or (not c.get("header") and not c.get("certificate")):
            continue  # nothing public to show yet (or not the day that was asked for)
        write_json(os.path.join(cycles_dir, "%d.json" % day), c)
        days.add(day)

    index = []
    for day in sorted(days, reverse=True):
        try:
            with open(os.path.join(cycles_dir, "%d.json" % day)) as f:
                old = json.load(f)
            if isinstance(old, dict) and old.get("day") == day:
                index.append(summary(old))
        except (OSError, ValueError, AttributeError, KeyError, TypeError):
            pass
    write_json(os.path.join(cycles_dir, "index.json"), index)

    netinfo = status.get("network") or {}
    # No `release`: that is this node's own software version, not a network fact.
    net = clean({
        "rule_version": status.get("rule_version"),
        "chain_id": netinfo.get("chain_id"),
        "network_id": netinfo.get("network_id"),
        "today": today,
        "first_day": network.get("first_day"),
        "latest_certified_day": network.get("latest_certified_day"),
        "latest_final_day": network.get("latest_final_day"),
        "next_keys": network.get("next_keys"),
        "next_accounts": network.get("next_accounts"),
    }, NETWORK, "network")
    net["generated_unix"] = int(time.time())
    write_json(os.path.join(out, "network.json"), net)

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
    for what in sorted(_dropped - KNOWN_UNPUBLISHED):
        print("not published: %s" % what, file=sys.stderr)
    print("ok: %d cycles, latest certified %s, %s validator accounts, chain %s" % (
        len(index), network.get("latest_certified_day"), len(vals["accounts"]) if vals else 0,
        "%s at height %s" % (chain.get("state"), (chain.get("head") or {}).get("height")) if chain else "-"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
