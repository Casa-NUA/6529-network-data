#!/usr/bin/env python3
"""Collect PUBLIC network data from a local 6529 validator node into static JSON.

Derived from 6529scan's collector (MIT, Copyright (c) 2026 nothingbugger,
https://github.com/nothingbugger/6529scan); same output shape, minus the node's own
`release` in network.json and the certificate's per-signer weight lists, so anything
that reads 6529scan's data without those can read this. Changes: finds the validator's loopback port
whatever user runs it (the desktop app runs it as the logged-in user), checks the
port really is the validator, and publishes only continent and country from each
machine's self-reported location (not state or metro).

Runs next to the node and reads its loopback API. For blocks.json it also reads one
file of the node's chain store on disk (the index of blocks: public chain facts) and
the node's own block count from the API, used only to check that file was read right
(see "Blocks per account" below).
Writes only what a strict schema allows (see "What may be published" below): cycle
headers, roots, certificate summaries and verification outcomes, the validator list
and public chain facts. It does not write this node's keys, peers, servers or
location, and it leaves out the certificate lists from which the node's own account
could be worked out. Some values are this node's own observations rather than
network facts (strong_unix, finalized_unix, compare, dual_run, chain.json, blocks.json).

From validator rc.18 it also writes days/<day>.json and days/index.json (each past
day's validator list), history.json (network size and software per day) and
awards.json (the chain's per-account awards over the last days, from the node's
awards/network routes, and which keys proposed or voted in the last 10 minutes; from
rc.19 also each validating account's balance, vesting and earned total, read in
rotation from chain/account/<wallet>), and
adds each account's public 6529 handle (from api.6529.io, the only request that leaves
this machine) to validators.json. It keeps a small state file NEXT TO the data
directory (never inside it): the handle cache, the day's last validator list and the
award days that are settled, and the last balances read.

Python 3.9+, standard library only.

    python3 collect.py --out /path/to/site/data [--api http://127.0.0.1:6529/api/v1]
                       [--state /path/outside/data/collector-state.json]
"""
import argparse
import glob
import http.client
import json
import os
import struct
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

class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """A 3xx is a failure: no request is ever sent anywhere a server points to."""
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise urllib.error.HTTPError(req.full_url, code, "redirect refused", headers, fp)


# One opener for every request: no proxy (whatever the environment says), no redirects.
OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())


def get(api, path, timeout=20):
    req = urllib.request.Request(api.rstrip("/") + "/" + path.lstrip("/"),
                                 headers={"Accept": "application/json"})
    with OPENER.open(req, timeout=timeout) as r:
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
    # When this machine signed today's result (unix seconds; null until it has). rc.17+.
    "signed_unix": INT,
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
CHAIN_PRIVATE = ("proposes", "proposes_why", "catch_up", "records", "state_records", "switch",
                 # rc.18: this node's own block turns and upload choice, and its sync state
                 "own_turns", "hide_block_turns_upload", "pending", "sync")


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
    # rc.18 chain details not needed by the apps (epoch of the finalized block, vesting rule id):
    "chain.finalized.epoch", "chain.rules.vest_rule",
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


# ---- Blocks per account (from the node's own chain store) --------------------------
# The node's API says who made (proposed) a block only for its own account. Its chain
# store on disk records it for every block, in index/blocks.log. That file's format is
# NOT documented and may change with any release, so nothing read from it is published
# unless it passes two checks:
#   1. public facts: the newest block read must be the API's head (same slot and time),
#      and every block's day and time must follow from its slot;
#   2. a self-check: the number of blocks the store gives this node's own account must
#      equal the number the node's API reports for that account, for today and for
#      each earlier day the API has a figure for.
# The own account is used for that comparison only, and the comparison is made at the
# same block height on both sides, so whether a run is skipped never depends on who
# made a block. The published file lists every account the same way.
#
# A record, as read from rc.17:  u32 length, body, 8 bytes (a check value, not verified
# here). Body: 2 bytes form (01 01), block root (32), parent root (32), slot (u64),
# height (u64), day (u32), time in ms (u64), the maker's key id (32), 12 zero bytes and
# the maker's wallet (20), then fields this collector does not read. Big-endian.
BLOCKS_FORM = b"\x01\x01"
BLOCK_FIXED = 2 + 32 + 32 + 8 + 8 + 4 + 8 + 32 + 32
BLOCK_DAYS = 7                         # days published, today included
STORE_DIRS = (
    "~/Library/Application Support/6529-validator",       # the desktop app, macOS
    "/var/lib/6529-validator",                             # the Linux service
)


def read_block_index(path, from_day):
    """{root: (parent, slot, height, day, time_ms, wallet)} for blocks of `from_day` on.

    Read as a stream, so the file's size does not matter. None if it is not the format
    this was written for.
    """
    recs = {}
    try:
        if os.path.islink(path):
            return None
        with open(path, "rb") as f:
            while True:
                head = f.read(4)
                if len(head) < 4:
                    break
                n = struct.unpack(">I", head)[0]
                if n < BLOCK_FIXED or n > 16 * 1024 * 1024:
                    return None
                r = f.read(BLOCK_FIXED)
                if len(r) < BLOCK_FIXED:
                    break              # the node is appending: the last record is not whole yet
                f.seek(n - BLOCK_FIXED + 8, 1)
                if r[:2] != BLOCKS_FORM or r[126:138] != b"\0" * 12:
                    return None
                slot, height = struct.unpack(">QQ", r[66:82])
                day = struct.unpack(">I", r[82:86])[0]
                t = struct.unpack(">Q", r[86:94])[0]
                if day != t // 86400000:
                    return None
                if day >= from_day:
                    recs[r[2:34]] = (r[34:66], slot, height, day, t, "0x" + r[138:158].hex())
    except OSError:
        return None
    return recs or None


def chain_blocks(api, roster_wallets, now):
    """Blocks each account made over the last days, or None (and why on stderr)."""
    def no(why):
        print("blocks.json not written: %s" % why, file=sys.stderr)
        return None
    # The head and the node's own figures must describe the same block height. A block
    # can land between the two requests, so ask again a few times; if they still
    # differ, skip this run, whoever made that block.
    for _ in range(4):
        try:
            chain = get(api, "chain")
            me = get(api, "chain/me")
            settled = get(api, "awards/days")
            awards = get(api, "awards")
        except (urllib.error.URLError, http.client.HTTPException, OSError, ValueError):
            return no("the node's chain figures could not be read")
        try:
            head = chain["head"]
            root, height, hslot, htime = head["root"], head["height"], head["slot"], head["time"]
            start, slot_ms = chain["start_time"], chain["slot_ms"]
            own, today_own, as_of = me["wallet"].lower(), awards["today"], awards["as_of"]["height"]
            own_days = {d["day"]: d["blocks"] for d in settled["days"]}
        except (KeyError, TypeError, AttributeError):
            return no("the node's chain figures have an unexpected shape")
        if as_of == height:
            break
    else:
        return no("the head moved between requests every time (will try next run)")
    if clean(root, HEX) is _BAD or clean(own, WALLET) is _BAD or len(root) != 66 \
            or not all(isinstance(v, int) and not isinstance(v, bool) and v > 0
                       for v in (height, hslot, htime, start, slot_ms)):
        return no("the node's chain figures have an unexpected shape")
    day = htime // 86400000
    if today_own.get("day") != day:
        return no("the node's own figures are for another day (a new day is starting)")
    if not (now - 3 * 86400) * 1000 < htime <= (now + 600) * 1000:
        return no("the head block's time is out of range")

    paths = []
    for d in STORE_DIRS:
        paths += glob.glob(os.path.join(os.path.expanduser(d), "chain", "*", "index", "blocks.log"))
    paths.sort(key=lambda p: os.path.getmtime(p) if os.path.exists(p) else 0, reverse=True)
    canon, cur = None, bytes.fromhex(root[2:])
    for p in paths[:4]:
        recs = read_block_index(p, day - BLOCK_DAYS)
        if not recs or cur not in recs:
            continue
        canon, c = [], cur
        while c in recs and len(canon) <= len(recs):
            canon.append(recs[c])
            c = recs[c][0]
        break
    if canon is None:
        return no("no chain store in the expected format holds the head block")
    # Newest first. Public-fact checks: the head is the API's head, heights step down
    # by one, and each block's time follows from its slot.
    if canon[0][1:5] != (hslot, height, day, htime):
        return no("the store's head block is not the API's")
    for i, c in enumerate(canon):
        if c[4] != start + c[1] * slot_ms or (i and (canon[i - 1][2] != c[2] + 1 or canon[i - 1][1] <= c[1])):
            return no("the store's blocks do not follow one another as a chain does")

    def count(d):
        out = {}
        for c in canon:
            if c[3] == d:
                out[c[5]] = out.get(c[5], 0) + 1
        return out
    # Only days read from their first block: the oldest day in the walk may be cut.
    first_whole = canon[-1][3] if canon[-1][2] == 1 else canon[-1][3] + 1
    days = [d for d in range(day - BLOCK_DAYS + 1, day + 1) if d >= first_whole]
    if day not in days:
        return no("the store does not hold today from its first block")
    per_day = {d: count(d) for d in days}

    # Self-check, at the same height on both sides (see above).
    if per_day[day].get(own, 0) != today_own.get("blocks"):
        return no("SELF-CHECK FAILED for today: the store's format may have changed")
    for d in days[:-1]:
        if d in own_days and per_day[d].get(own, 0) != own_days[d]:
            return no("SELF-CHECK FAILED for day %d: the store's format may have changed" % d)

    last, times = {}, {}
    for c in canon:                    # newest first: the first seen is the latest
        if c[3] in per_day and c[5] not in last:
            last[c[5]] = c[4] // 1000
        if c[3] == day:
            times.setdefault(c[5], []).append(c[4] // 1000)
    makers = sorted(set().union(*[set(per_day[d]) for d in days]))
    # A maker that is not a validating account today (it left, or the roster changed) is
    # counted in the totals but not listed. Many of them would mean a misread file.
    known = set(roster_wallets)
    listed = [w for w in makers if w in known and clean(w, WALLET) is not _BAD]
    if len(listed) * 10 < len(makers) * 9:
        return no("too many block makers are not validating accounts")
    return {
        "generated_unix": now, "day": day, "head_height": height,
        "days": days, "totals": [sum(per_day[d].values()) for d in days],
        "accounts": [{"wallet": w, "counts": [per_day[d].get(w, 0) for d in days],
                      "today_unix": sorted(times.get(w, []))[-2880:],
                      "last_unix": last.get(w)} for w in listed],
    }


# ---- The collector's own state (never published) -----------------------------------
# One JSON file NEXT TO the data directory, never inside it (everything inside the data
# directory is published). It holds: the handle cache, the last validator list seen for
# the current day (to write that day's file when the day changes), and the awards days
# that are settled and final (they no longer change). Losing it costs only a few runs.
STATE_VERSION = 1


def load_state(path):
    try:
        with open(path) as f:
            st = json.load(f)
        if isinstance(st, dict) and st.get("version") == STATE_VERSION:
            return st
    except (OSError, ValueError, RecursionError):
        pass
    return {"version": STATE_VERSION}


def save_state(path, st):
    try:
        write_json(path, st)
        os.chmod(path, 0o600)
    except OSError as e:
        print("state not saved: %s" % type(e).__name__, file=sys.stderr)


# ---- Handles (public 6529 profile names) -------------------------------------------
# Each validating wallet's handle, as 6529's public API shows it. A wallet with no
# profile gets a placeholder ("id-0x…") there: published as null. At most 25 lookups
# per run, each wallet re-checked at most once every 7 days, short timeouts, and a
# failed lookup only means the cached value (or null) is used. Nothing here ever stops
# a run.
HANDLE_API = "https://api.6529.io/api/identities/"
HANDLE_UA = "6529-network-data-collector (+https://github.com/Casa-NUA/6529-network-data)"
HANDLE_RX = re.compile(r"^[A-Za-z0-9_.\-]{1,40}$")
HANDLE_LOOKUPS = 25
HANDLE_RECHECK_S = 7 * 86400
HANDLE_BUDGET_S = 45
HANDLE_TIMEOUT_S = 8
HANDLE_KEEP_S = 60 * 86400   # cache entries of wallets no longer validating are dropped after this


def HANDLE(v):
    if isinstance(v, str) and HANDLE_RX.match(v) and not v.lower().startswith("id-"):
        return v
    return _BAD


def fetch_handle(wallet, timeout=HANDLE_TIMEOUT_S):
    """The wallet's handle, None when it has none; raises when the lookup failed."""
    req = urllib.request.Request(HANDLE_API + wallet,
                                 headers={"Accept": "application/json", "User-Agent": HANDLE_UA})
    try:
        with OPENER.open(req, timeout=timeout) as r:
            body = r.read(1000001)
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        raise
    if len(body) > 1000000:
        raise ValueError("answer too large")
    if not body.strip():
        return None
    d = json.loads(body.decode("utf-8"))
    h = d.get("handle") if isinstance(d, dict) else None
    if not isinstance(h, str) or not h or h.lower().startswith("id-"):
        return None                    # no profile (6529 shows a placeholder)
    if HANDLE(h) is _BAD:
        _dropped.add("handles.handle (unexpected form)")
        return None
    return h


def update_handles(state, wallets, now, fetch=fetch_handle):
    """Look up the handles that are due (see above). Returns the number looked up."""
    cache = state.get("handles")
    if not isinstance(cache, dict):
        cache = {}
    # keep only well-formed entries; forget wallets gone for a long time
    current = set(wallets)
    cache = {w: e for w, e in cache.items()
             if clean(w, WALLET) is not _BAD and w == w.lower() and isinstance(e, dict)
             and INT(e.get("t")) is not _BAD and (e.get("h") is None or HANDLE(e.get("h")) is not _BAD)
             and (w in current or now - e["t"] < HANDLE_KEEP_S)}
    state["handles"] = cache
    due = [w for w in wallets if w not in cache or now - cache[w]["t"] >= HANDLE_RECHECK_S]
    due.sort(key=lambda w: cache[w]["t"] if w in cache else -1)   # never looked up first
    started, fails, done = time.time(), 0, 0
    for w in due[:HANDLE_LOOKUPS]:
        if fails >= 3 or time.time() - started > HANDLE_BUDGET_S:
            break
        try:
            h = fetch(w)
        except Exception:              # network, HTTP, JSON: try again on a later run
            fails += 1
            continue
        fails = 0
        cache[w] = {"h": h, "t": now}
        done += 1
    return done


def handle_of(state, wallet):
    e = (state.get("handles") or {}).get(wallet.lower())
    h = e.get("h") if isinstance(e, dict) else None
    return h if h is not None and HANDLE(h) is not _BAD else None


# ---- Past days (days/<day>.json, days/index.json) ----------------------------------
# The validator list as last collected on each past day. Written once, when the
# collector sees the day change, from the last list it had for that day (kept in the
# state file); days before this existed were rebuilt from this repository's own history
# by backfill_days.py. A day's file is never rewritten.
DAY_MACHINE = {"code": RE(r"^[0-9a-f]{8}$"), "weight": INT, "today": STR, "report": REPORT}


def day_record(vals, state):
    """A days/<day>.json record from a validators.json-shaped list; None if unusable."""
    if not isinstance(vals, dict):
        return None
    day, w = clean(vals.get("cycle_day"), INT), clean(vals.get("w"), INT)
    if day in (_BAD, None) or not isinstance(vals.get("accounts"), list):
        return None
    accounts = []
    for a in vals["accounts"]:
        if not isinstance(a, dict) or clean(a.get("wallet"), WALLET) in (_BAD, None):
            _dropped.add("days.accounts[] (no valid wallet)")
            continue
        wallet = a["wallet"].lower()
        machines = []
        for m in (a.get("machines") if isinstance(a.get("machines"), list) else [])[:256]:
            if not isinstance(m, dict):
                continue
            mm = clean({k: m.get(k) for k in DAY_MACHINE if k in m}, DAY_MACHINE, "days.machine")
            if mm is not _BAD and mm.get("code"):
                machines.append(mm)
        tdh, weight = clean(a.get("tdh"), INT), clean(a.get("weight"), INT)
        accounts.append({"wallet": wallet, "handle": handle_of(state, wallet),
                         "tdh": None if tdh is _BAD else tdh,
                         "weight": None if weight is _BAD else weight, "machines": machines})
    if not accounts:
        return None
    accounts.sort(key=lambda x: x.get("weight") or 0, reverse=True)
    return {"day": day, "final": True, "w": None if w is _BAD else w, "accounts": accounts}


def day_files(out):
    d = os.path.join(out, "days")
    found = set()
    if os.path.isdir(d) and not os.path.islink(d):
        for n in os.listdir(d):
            if n.endswith(".json") and n[:-5].isdigit() and len(n) <= 11:
                found.add(int(n[:-5]))
    return found


def write_day(out, rec):
    """Write days/<day>.json unless it exists (a day's file is never rewritten)."""
    p = os.path.join(out, "days", "%d.json" % rec["day"])
    if os.path.lexists(p):
        return False
    write_json(p, rec)
    return True


def write_days_index(out, today):
    days = sorted(d for d in day_files(out) if today is None or d < today)
    write_json(os.path.join(out, "days", "index.json"), {"days": days})
    return days


def roll_day(out, state, vals):
    """Keep the newest list per day; when the day changes, write the previous day."""
    wrote = None
    last = state.get("last_vals")
    if isinstance(last, dict) and isinstance(last.get("cycle_day"), int) \
            and isinstance(vals.get("cycle_day"), int):
        if vals["cycle_day"] > last["cycle_day"]:
            rec = day_record(last, state)
            if rec and write_day(out, rec):
                wrote = rec["day"]
        elif vals["cycle_day"] < last["cycle_day"]:
            return None                # the node went back a day (resync): keep what we had
    if isinstance(vals.get("cycle_day"), int):
        state["last_vals"] = vals
    return wrote


# ---- history.json (one row per day, oldest first, today last) ----------------------
def history_row(rec):
    machines = [m for a in rec["accounts"] for m in a.get("machines") or []]
    continents, software = {}, {}
    for m in machines:
        rep = m.get("report") or {}
        c = rep.get("continent") if STR(rep.get("continent")) is not _BAD else None
        s = rep.get("software") if REPORT["software"](rep.get("software")) is not _BAD else None
        continents[c or "UNKNOWN"] = continents.get(c or "UNKNOWN", 0) + 1
        software[s or "UNKNOWN"] = software.get(s or "UNKNOWN", 0) + 1
    return {
        "day": rec["day"], "validators": len(machines),
        "signed": sum(1 for m in machines if m.get("today") == "SIGNED"),
        "accounts": len(rec["accounts"]),
        "accounts_signed": sum(1 for a in rec["accounts"]
                               if any(m.get("today") == "SIGNED" for m in a.get("machines") or [])),
        "w": rec.get("w"), "continents": continents, "software": software,
    }


def build_history(out, state, today_rec, now):
    """Rows from the day files (cached by file size and time: they never change) + today."""
    cache = state.get("history_rows")
    cache = cache if isinstance(cache, dict) else {}
    rows, keep = [], {}
    today = today_rec["day"] if today_rec else None
    for d in sorted(day_files(out)):
        if today is not None and d >= today:
            continue
        p = os.path.join(out, "days", "%d.json" % d)
        try:
            st = os.lstat(p)
            sig = "%d:%d" % (st.st_size, int(st.st_mtime))
            c = cache.get(str(d))
            if isinstance(c, dict) and c.get("sig") == sig and isinstance(c.get("row"), dict):
                row = c["row"]
            else:
                with open(p) as f:
                    rec = json.load(f)
                if not isinstance(rec, dict) or rec.get("day") != d or not isinstance(rec.get("accounts"), list):
                    continue
                row = history_row(rec)
            keep[str(d)] = {"sig": sig, "row": row}
            rows.append(row)
        except (OSError, ValueError, RecursionError, TypeError, AttributeError):
            continue
    state["history_rows"] = keep
    if today_rec:
        rows.append(history_row(today_rec))
    cleaned = clean(rows, LIST(HISTORY_ROW, 4000), "history.days")
    if cleaned is _BAD or not cleaned:
        return None
    return {"generated_unix": now, "days": cleaned}


def COUNTS(v):  # {label: count}, e.g. machines per continent
    return v if isinstance(v, dict) and len(v) <= 64 and all(
        STR(k) is not _BAD and INT(n) is not _BAD for k, n in v.items()) else _BAD


HISTORY_ROW = {"day": INT, "validators": INT, "signed": INT, "accounts": INT,
               "accounts_signed": INT, "w": INT, "continents": COUNTS, "software": COUNTS}


# ---- awards.json (6529 chain: per-account awards, and who votes right now) ---------
# From the node's rc.18 routes:
#   awards/network/days?limit=N          the chain's days, newest first (limit 1..64)
#   awards/network/day/{d}?limit=1000[&after=<rank>]   per KEY, paged by rank; the
#                                        answer's next_after is the next `after`, null at the end
#   awards/network/day/{d}/blocks?limit=240[&after_slot=<slot>]   blocks with slot > after_slot,
#                                        oldest first; next_after_slot for the next page
# Per-key figures are summed per ACCOUNT. A day that is settled and final no longer
# changes and is kept in the state file; the open day is read again every run.
AWARD_DAYS = 7
LIVE_WINDOW_S = 600
AMOUNT = NUMSTR
REC = RE(r"^(all|part|none)$")
CODE = RE(r"^[0-9a-f]{8}$")
AWARD = {"total": AMOUNT, "blocks_part": AMOUNT, "votes_part": AMOUNT}
AWARD_ACCOUNT = {"wallet": WALLET, "blocks": INT, "votes": INT, "award": AWARD}
AWARD_DAY = {"day": INT, "state": STR, "final": BOOL, "slots": INT, "blocks": INT,
             "award_total": AMOUNT, "recorded": {"blocks": REC, "votes": REC},
             "accounts": LIST(AWARD_ACCOUNT, 5000)}


def _req_int(v):
    return isinstance(v, int) and not isinstance(v, bool) and v >= 0


# What the node's awards routes are known to return. These answers are read field by
# field (only the fields named in AWARD_DAY / AWARD_ACCOUNT end up in the file); a field
# not listed here is new, and is named on stderr like any other unknown field.
_AW_SUMMARY = {"day", "date", "currency", "state", "final", "slots", "blocks", "votes", "paid",
               "paid_for", "paid_with", "earners", "settle", "recorded", "keys", "total_keys",
               "next_after", "as_of"}
_AW_KEY = {"rank", "key_id", "key_code", "account", "blocks", "votes", "votes_zero_weight",
           "vote_weight", "paid", "paid_for"}
_AW_PAID = {"award", "parts", "released", "fees", "forfeited", "credit_fees", "unvested"}
_AW_AWARD = {"total", "blocks_part", "votes_part"}
_AW_RECORDED = {"awards", "blocks", "votes"}
_AW_BLOCKS = {"day", "date", "slots", "blocks", "recorded", "not_recorded", "rows",
              "next_after_slot", "as_of"}
_AW_ROW = {"slot", "time", "height", "root", "final", "proposer", "votes"}


def _note_unknown(obj, known, path):
    if isinstance(obj, dict):
        for k in obj:
            if k not in known:
                _dropped.add("%s.%s (not in schema)" % (path, k))


def award_day(api, summary_row):
    """One day of awards.json from its summary row (days list) and its per-key pages."""
    d = summary_row.get("day")
    if not _req_int(d):
        return None
    keys, after = [], None
    for _ in range(50):                # 50 pages of 1000 keys: far beyond any real day
        page = get(api, "awards/network/day/%d?limit=1000%s" % (d, "" if after is None else "&after=%d" % after))
        if not isinstance(page, dict) or page.get("day") != d or not isinstance(page.get("keys"), list):
            return None
        _note_unknown(page, _AW_SUMMARY, "awards.day")
        keys += page["keys"]
        nxt = page.get("next_after")
        if nxt is None:
            break
        if not _req_int(nxt) or (after is not None and nxt <= after):
            return None
        after = nxt
    else:
        return None
    per = {}
    for k in keys:
        if not isinstance(k, dict):
            continue
        _note_unknown(k, _AW_KEY, "awards.keys[]")
        _note_unknown(k.get("paid"), _AW_PAID, "awards.keys[].paid")
        _note_unknown((k.get("paid") or {}).get("award") if isinstance(k.get("paid"), dict) else None,
                      _AW_AWARD, "awards.keys[].paid.award")
        acct = k.get("account")
        if clean(acct, WALLET) in (_BAD, None):
            _dropped.add("awards.keys[].account (not a 0x+40 wallet)")
            continue
        a = per.setdefault(acct.lower(), {"blocks": None, "votes": None, "award": None})
        for f in ("blocks", "votes"):
            if _req_int(k.get(f)):
                a[f] = (a[f] or 0) + k[f]
        paid = k.get("paid")
        award = paid.get("award") if isinstance(paid, dict) else None
        if isinstance(award, dict):
            parts = {f: award.get(f) for f in AWARD}
            if all(isinstance(v, str) and _NUM.match(v) for v in parts.values()):
                prev = a["award"] or {f: "0" for f in AWARD}
                a["award"] = {f: str(int(prev[f]) + int(parts[f])) for f in AWARD}
            else:
                _dropped.add("awards.keys[].paid.award (unexpected form)")
    accounts = []
    for w, a in per.items():
        # an account with nothing to show (no block, no vote, nothing paid) is left out
        if not a["blocks"] and not a["votes"] and not (a["award"] and int(a["award"]["total"])):
            continue
        accounts.append(dict(a, wallet=w))
    accounts.sort(key=lambda x: (-int((x["award"] or {}).get("total") or 0),
                                 -(x["blocks"] or 0), -(x["votes"] or 0), x["wallet"]))
    _note_unknown(summary_row, _AW_SUMMARY, "awards.days[]")
    _note_unknown(summary_row.get("recorded"), _AW_RECORDED, "awards.days[].recorded")
    paid = summary_row.get("paid")
    total = ((paid or {}).get("award") or {}).get("total") if isinstance(paid, dict) else None
    rec = summary_row.get("recorded") if isinstance(summary_row.get("recorded"), dict) else {}
    out = clean({"day": d, "state": summary_row.get("state"), "final": summary_row.get("final"),
                 "slots": summary_row.get("slots"), "blocks": summary_row.get("blocks"),
                 "award_total": total,
                 "recorded": {"blocks": rec.get("blocks"), "votes": rec.get("votes")},
                 "accounts": accounts}, AWARD_DAY, "awards.day")
    if out is _BAD or "day" not in out:
        return None
    for f in ("state", "final", "slots", "blocks", "award_total"):
        out.setdefault(f, None)
    out["recorded"] = {"blocks": out.get("recorded", {}).get("blocks"),
                       "votes": out.get("recorded", {}).get("votes")}
    if out["award_total"] is None:
        for a in out["accounts"]:
            a["award"] = None          # an open day pays nothing yet (contract: award null)
    # Per-account blocks and votes only for a day this node recorded in full: on a day it
    # recorded only in part, the counts would show (against blocks.json) the moment this node
    # started recording, a hint about which account runs it (review, 15 Nov 2026).
    for f, key in (("blocks", "blocks"), ("votes", "votes")):
        if out["recorded"].get(key) != "all":
            for a in out["accounts"]:
                a[f] = None
    for a in out["accounts"]:
        for f in ("blocks", "votes", "award"):
            a.setdefault(f, None)
    return out


def _head_recent(chain, now, most_s=120):
    """True when the chain's newest block is at most `most_s` old (a lagging node would
    otherwise publish an empty, fresh-looking `live`)."""
    t = ((chain or {}).get("head") or {}).get("time")
    return _req_int(t) and now * 1000 - t <= most_s * 1000


def live_keys(api, chain, now):
    """{key_code: unix time of its newest block} for blocks in the last LIVE_WINDOW_S."""
    start, slot_ms = chain.get("start_time"), chain.get("slot_ms")
    if not _req_int(start) or not _req_int(slot_ms) or not slot_ms:
        return None
    lo_ms = (now - LIVE_WINDOW_S) * 1000
    after = max(-1, (lo_ms - start) // slot_ms - 1)
    keys, recorded_any = {}, False
    for d in range(lo_ms // 86400000, now // 86400 + 1):
        a = after
        for _ in range(20):
            try:
                page = get(api, "awards/network/day/%d/blocks?limit=240%s" % (
                    d, "" if a < 0 else "&after_slot=%d" % a))
            except urllib.error.HTTPError as e:
                if e.code == 404:      # a day the chain does not have (before its start)
                    break
                raise
            if not isinstance(page, dict) or page.get("day") != d or not isinstance(page.get("rows"), list):
                return None
            _note_unknown(page, _AW_BLOCKS, "awards.blocks")
            if page.get("recorded") in ("all", "part"):
                recorded_any = True
            for r in page["rows"]:
                _note_unknown(r, _AW_ROW, "awards.blocks.rows[]")
                if not isinstance(r, dict) or not _req_int(r.get("time")) or r["time"] < lo_ms \
                        or r["time"] > (now + 120) * 1000:
                    continue
                t = r["time"] // 1000
                who = [r.get("proposer")] + (r.get("votes") if isinstance(r.get("votes"), list) else [])
                for x in who:
                    c = x.get("key_code") if isinstance(x, dict) else None
                    if CODE(c) is not _BAD and keys.get(c, 0) < t:
                        keys[c] = t
            nxt = page.get("next_after_slot")
            if nxt is None or not page["rows"]:
                break
            if not _req_int(nxt) or nxt <= a:
                return None
            a = nxt
    return keys if recorded_any else None


def build_awards(api, chain, state, now, wallets=None):
    summary = get(api, "awards/network/days?limit=%d" % AWARD_DAYS)
    rows = summary.get("days") if isinstance(summary, dict) else None
    if not isinstance(rows, list):
        return None
    rows = [r for r in rows if isinstance(r, dict) and _req_int(r.get("day"))]
    rows = sorted(rows, key=lambda r: r["day"])[-AWARD_DAYS:]
    cache = state.get("award_days")
    cache = cache if isinstance(cache, dict) else {}
    days, keep = [], {}
    for r in rows:
        c = cache.get(str(r["day"]))
        if isinstance(c, dict) and c.get("day") == r["day"]:
            rec = c
        else:
            rec = award_day(api, r)
            if rec is None:
                continue
        if rec.get("state") == "settled" and rec.get("final") is True:
            keep[str(r["day"])] = rec  # settled and final: it no longer changes
        days.append(rec)
    state["award_days"] = keep
    if not days:
        return None
    try:
        live = live_keys(api, chain, now)
    except (urllib.error.URLError, http.client.HTTPException, OSError, ValueError):
        live = None
    if wallets is None:                # no validator list this run: refresh what is cached
        cached = state.get("balances")
        wallets = sorted(cached) if isinstance(cached, dict) else []
    try:
        balances = build_balances(api, state, [w.lower() for w in wallets])
    except Exception as e:             # never let the balances stop awards.json
        print("balances not built: %s" % type(e).__name__, file=sys.stderr)
        balances = None
    cur = chain.get("currency") or {}
    return {
        "generated_unix": now,
        "currency": cur.get("symbol"), "decimals": cur.get("decimals"), "test": cur.get("test"),
        "days": days,
        "live": None if (live is None or not _head_recent(chain, now)) else {"as_of_unix": now, "window_s": LIVE_WINDOW_S,
                                           "keys": dict(sorted(live.items()))},
        "balances": balances,
    }


# ---- awards.json "balances" (per-account chain balances, validator rc.19) ----------
# From the node's rc.19 route chain/account/<wallet>, which answers for ANY account:
#   balance = `available` (what the account can send), vesting = `vesting`,
#   earned = totals.earned (all days), blocks = totals.blocks (all days).
# Everything else in the answer is left out, in particular `own` (it would single out
# the collecting node's account) and `faults`. Every validating account is treated the
# same way: refreshed in rotation, oldest fetch first, at most BALANCE_FETCHES per run
# within BALANCE_BUDGET_S; the others come from the state file. An account that left
# validators.json is dropped. as_of_unix is the OLDEST fetch time among the accounts
# published. Null when the node has no such route (404, an older node) or when nothing
# is known yet (no validator list and no cache, or every fetch failed on a first run).
BALANCE_FETCHES = 80
BALANCE_TIMEOUT_S = 4
BALANCE_BUDGET_S = 15
# A cached row older than this is no longer published (reads have kept failing for it).
BALANCE_MAX_AGE_S = 3600
BALANCE = {"wallet": WALLET, "balance": AMOUNT, "vesting": AMOUNT, "earned": AMOUNT, "blocks": INT}
_BAL_ANSWER = {"account", "kind", "own", "currency", "available", "vesting", "validator", "keys",
               "days", "totals", "faults", "as_of"}
_BAL_TOTALS = {"earned", "blocks", "from_day", "from_date"}


class RouteMissing(Exception):
    pass


def fetch_balance(api, wallet, timeout=BALANCE_TIMEOUT_S):
    """{balance, vesting, earned, blocks} of one account; None when the answer does not fit
    the schema; raises RouteMissing on 404 and anything else on a failed request."""
    try:
        r = get(api, "chain/account/%s" % wallet, timeout=timeout)
    except urllib.error.HTTPError as e:
        if e.code == 404:
            raise RouteMissing()
        raise
    if not isinstance(r, dict):
        return None
    _note_unknown(r, _BAL_ANSWER, "awards.balances.answer")
    tot = r.get("totals") if isinstance(r.get("totals"), dict) else {}
    _note_unknown(tot, _BAL_TOTALS, "awards.balances.answer.totals")
    acct = r.get("account")
    if not isinstance(acct, str) or acct.lower() != wallet.lower():
        _dropped.add("awards.balances (answer for another account)")
        return None
    rec = {"wallet": wallet, "balance": r.get("available"), "vesting": r.get("vesting"),
           "earned": tot.get("earned"), "blocks": tot.get("blocks")}
    out = clean(rec, BALANCE, "awards.balances.accounts[]")
    if out is _BAD or set(out) != set(BALANCE) or any(out.get(k) is None for k in BALANCE):
        _dropped.add("awards.balances.accounts[] (incomplete answer)")
        return None
    return {k: out[k] for k in ("balance", "vesting", "earned", "blocks")}


def build_balances(api, state, wallets, fetch=fetch_balance, clock=time.time):
    """awards.json "balances" (see above), or None when the node has no account route."""
    now = int(clock())
    cache = state.get("balances")
    cache = cache if isinstance(cache, dict) else {}
    # keep only well-formed entries of wallets that still validate
    current = set(wallets)
    good = {}
    for w, e in cache.items():
        if w in current and isinstance(e, dict) and INT(e.get("t")) is not _BAD and e["t"] <= now:
            rec = clean(dict({k: e.get(k) for k in BALANCE if k != "wallet"}, wallet=w), BALANCE)
            if rec is not _BAD and set(rec) == set(BALANCE) and \
                    all(rec.get(k) is not None for k in BALANCE) and now - e["t"] <= BALANCE_MAX_AGE_S:
                good[w] = dict({k: rec[k] for k in ("balance", "vesting", "earned", "blocks")}, t=e["t"])
    cache = good
    due = sorted(wallets, key=lambda w: (cache[w]["t"] if w in cache else -1, w))
    started, fails = clock(), 0
    for w in due[:BALANCE_FETCHES]:
        if fails >= 3 or clock() - started > BALANCE_BUDGET_S:
            break
        try:
            b = fetch(api, w)
        except RouteMissing:
            return None                # older node: no route (the state is left as it was)
        except Exception:              # timeout, network, JSON: kept from the cache this run
            fails += 1
            continue
        fails = 0
        if b is not None:
            cache[w] = dict(b, t=int(clock()))
    state["balances"] = cache
    if not cache:
        return None                    # nothing known yet (no list, or every fetch failed)
    accounts = []
    for w in wallets:
        e = cache.get(w)
        if e is None:
            continue
        if e["blocks"] == 0 and all(int(e[k]) == 0 for k in ("balance", "vesting", "earned")):
            continue                   # all zeros: left out (contract)
        accounts.append({"wallet": w, "balance": e["balance"], "vesting": e["vesting"],
                         "earned": e["earned"], "blocks": e["blocks"]})
    accounts.sort(key=lambda x: (-int(x["earned"]), -int(x["balance"]), x["wallet"]))
    oldest = min((cache[x["wallet"]]["t"] for x in accounts), default=None)
    return {"as_of_unix": oldest if oldest is not None else min(e["t"] for e in cache.values()),
            "accounts": accounts}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True, help="site data directory")
    ap.add_argument("--api", default=None, help="node API base (default: auto-detect)")
    ap.add_argument("--state", default=None,
                    help="the collector's own state file, outside --out "
                         "(default: collector-state.json next to the data directory)")
    a = ap.parse_args()
    a.api = a.api or detect_api()
    if not a.api:
        print("no validator API found on this machine", file=sys.stderr)
        return 1
    out = os.path.abspath(a.out)
    state_path = os.path.abspath(a.state or os.path.join(os.path.dirname(out), "collector-state.json"))
    if state_path == out or state_path.startswith(out + os.sep):
        print("refusing: the state file must be outside the data directory", file=sys.stderr)
        return 1

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

    wrote_today_cycle = False
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
        if day == today:
            wrote_today_cycle = True

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
    # network.json is written fresh on every run even when another file could not be:
    # a restarting node reports no members, and a cycle fetch can fail. So it also says
    # when validators.json and today's cycle file were last written, and a reader can
    # tell fresh data from a fresh network.json sitting next to an old file.
    now = int(time.time())
    vals = clean_validators(network)
    state = load_state(state_path)
    if vals:
        # Handles come from the cache here; new lookups run at the END of the run (after every
        # file is written), so a slow api.6529.io can never delay the network data.
        for x in vals["accounts"]:
            x["handle"] = handle_of(state, x["wallet"])
        write_json(os.path.join(out, "validators.json"), vals)
    previous = {}
    try:
        with open(os.path.join(out, "network.json")) as f:
            previous = json.load(f)
        if not isinstance(previous, dict):
            previous = {}
    except (OSError, ValueError, RecursionError):
        previous = {}

    def age(key, written):
        old = clean(previous.get(key), INT)
        return now if written else (None if old is _BAD else old)
    net["generated_unix"] = now
    net["validators_unix"] = age("validators_unix", bool(vals))
    # Only meaningful for the day it was written for: a new day starts without a file.
    net["cycle_unix"] = age("cycle_unix", wrote_today_cycle) \
        if (wrote_today_cycle or previous.get("today") == today) else None
    write_json(os.path.join(out, "network.json"), net)

    # The 6529 chain (rc.16+). Optional: older nodes have no /chain.
    try:
        chain = clean_chain(get(a.api, "chain"), int(time.time()))
    except (urllib.error.URLError, http.client.HTTPException, OSError, ValueError):
        chain = None
    if chain:
        write_json(os.path.join(out, "chain.json"), chain)

    # Blocks per account, from the node's chain store (see chain_blocks). Optional: the
    # file is simply not refreshed when the store cannot be read or the self-check fails,
    # and its generated_unix then tells a reader it is old.
    try:
        blocks = chain_blocks(a.api, [x["wallet"].lower() for x in vals["accounts"]],
                              int(time.time())) if (chain and vals) else None
    except Exception as e:             # never let this optional file stop the others
        print("blocks.json not written: %s" % type(e).__name__, file=sys.stderr)
        blocks = None
    if blocks:
        write_json(os.path.join(out, "blocks.json"), blocks)

    # Past days, history and awards. Optional: each is wrapped so that a failure only
    # leaves that file as it was (its generated_unix then shows its age).
    wrote_day, history, awards = None, None, None
    try:
        if vals:
            wrote_day = roll_day(out, state, vals)
        write_days_index(out, (vals or {}).get("cycle_day") or today)
    except Exception as e:
        print("days/ not updated: %s" % type(e).__name__, file=sys.stderr)
    try:
        today_rec = day_record(vals, state) if vals else None
        history = build_history(out, state, today_rec, int(time.time()))
    except Exception as e:
        print("history.json not written: %s" % type(e).__name__, file=sys.stderr)
    if history:
        write_json(os.path.join(out, "history.json"), history)
    try:
        awards = build_awards(a.api, chain, state, int(time.time()),
                              [x["wallet"] for x in vals["accounts"]] if vals else None) \
            if chain else None
    except Exception as e:
        print("awards.json not written: %s" % type(e).__name__, file=sys.stderr)
    if awards:
        write_json(os.path.join(out, "awards.json"), awards)
    if vals:
        try:
            update_handles(state, [x["wallet"].lower() for x in vals["accounts"]], int(time.time()))
        except Exception as e:         # never let the handles stop the run
            print("handles not updated: %s" % type(e).__name__, file=sys.stderr)
    save_state(state_path, state)

    for what in sorted(_dropped - KNOWN_UNPUBLISHED):
        print("not published: %s" % what, file=sys.stderr)
    print("ok: %d cycles, latest certified %s, %s validator accounts, chain %s%s%s%s%s" % (
        len(index), network.get("latest_certified_day"), len(vals["accounts"]) if vals else 0,
        "%s at height %s" % (chain.get("state"), (chain.get("head") or {}).get("height")) if chain else "-",
        ", wrote day %d" % wrote_day if wrote_day else "",
        ", history %d days" % len(history["days"]) if history else "",
        ", awards %d days" % len(awards["days"]) if awards else "",
        ", balances %d accounts" % len(awards["balances"]["accounts"])
        if awards and awards.get("balances") else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
