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
rotation from chain/account/<wallet>), health.json (from 20 Nov 2026: per machine, the
minutes of the last 24 h in which it voted, from the same blocks route), and
adds each account's public 6529 handle (from api.6529.io) to validators.json. From
18 Nov 2026 it also republishes 6529's signed release feed as releases.json once its
signature verifies, and adds network.json `crosscheck` ("6529 agrees", against
api.6529.io's TDH history and the feed's signed checkpoints). Those three are the only
requests that leave this machine, all public GETs (see their sections). From 28 Nov 2026
network.json also has `chain_roster` (five counts of the chain's award roster), read from
the local node's own chain/roster route, which never leaves the machine. It keeps a small state file NEXT TO the data
directory (never inside it): the handle cache, the day's last validator list and the
award days that are settled, and the last balances read.

Python 3.9+, standard library only.

    python3 collect.py --out /path/to/site/data [--api http://127.0.0.1:6529/api/v1]
                       [--state /path/outside/data/collector-state.json]
"""
import argparse
import base64
import calendar
import glob
import hashlib
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


# network.json `chain_roster` (from 28 Nov 2026): the node's chain/roster, the list the 6529
# chain uses for awards (fixed at 22:00 UTC for the next day), as five counts:
#   {"day": int, "accounts": int, "keys": int, "active_keys": int, "active_accounts": int}
# keys = the route's `validators`, active_keys = its `active`. Only those five numbers are read
# (the route's per-key list, weight and times are not published). Optional and all or nothing:
# an older node without the route, an error, or any of the five missing or not a whole number
# >= 0 (or a day out of range) leaves `chain_roster` out of network.json; nothing else changes.
ROSTER_TIMEOUT_S = 10
ROSTER_FIELDS = (("day", "day"), ("accounts", "accounts"), ("keys", "validators"),
                 ("active_keys", "active"), ("active_accounts", "active_accounts"))


def parse_roster(r):
    if not isinstance(r, dict):
        return None
    out = {}
    for mine, theirs in ROSTER_FIELDS:
        v = INT(r.get(theirs))
        if v is _BAD:
            return None
        out[mine] = v
    if not 19000 < out["day"] < 40000:
        return None
    return out


def chain_roster(api, fetch=None):
    try:
        r = (fetch or get)(api, "chain/roster", timeout=ROSTER_TIMEOUT_S)
    except (urllib.error.URLError, http.client.HTTPException, OSError, ValueError):
        return None
    return parse_roster(r)


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
# Block heights the apps mark as milestones (blocks.json `notable`). A block is listed
# only while it is inside the walked window (BLOCK_DAYS), so the apps must remember one
# they have seen: after a week it is no longer in the file.
NOTABLE_HEIGHTS = frozenset((6529, 10000, 42069, 65290, 69420, 100000, 420000, 1000000))
NOTABLE = {"height": INT, "wallet": WALLET, "unix": INT}
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
            final_height = (chain.get("finalized") or {}).get("height")
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
    # Blocks of a notable height in the walk, FINAL blocks only (height <= the chain's
    # finalized height): the apps keep a notable block for good, so one that a
    # reorganisation could still replace must never be listed. The maker is named by the
    # same rule as `accounts`: only a listed validating account; any other maker is null.
    # Fail-closed: without a usable finalized height the field is left out this run
    # (absent, not empty: a reader can tell "unknown" from "none").
    notable = None
    if _req_int(final_height) and final_height <= height:
        listed_set = set(listed)
        notable = [{"height": c[2], "wallet": c[5] if c[5] in listed_set else None,
                    "unix": c[4] // 1000}
                   for c in reversed(canon) if c[2] in NOTABLE_HEIGHTS and c[2] <= final_height]
        cleaned = clean(notable, LIST(NOTABLE, len(NOTABLE_HEIGHTS)), "blocks.notable")
        if cleaned is _BAD or cleaned != notable:
            return no("a notable block has an unexpected form")
    else:
        print("blocks.json: no notable list (the finalized height is not usable)", file=sys.stderr)
    out = {
        "generated_unix": now, "day": day, "head_height": height,
        "days": days, "totals": [sum(per_day[d].values()) for d in days],
        "accounts": [{"wallet": w, "counts": [per_day[d].get(w, 0) for d in days],
                      "today_unix": sorted(times.get(w, []))[-2880:],
                      "last_unix": last.get(w)} for w in listed],
    }
    if notable is not None:
        out["notable"] = notable
    return out


# ---- The collector's own state (never published) -----------------------------------
# One JSON file NEXT TO the data directory, never inside it (everything inside the data
# directory is published). It holds: the handle cache, the last validator list seen for
# the current day (to write that day's file when the day changes), and the awards days
# that are settled and final (they no longer change); also each settled day's award
# total ("award_totals", for distributed_total) and each account's signed-day counters
# ("sign_counts"). Losing it costs only a few runs for most of it; those two then fall
# back to lower bounds (or null), see their sections.
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
            # Days skipped entirely (no run saw them): the node gives no past day's list
            # (see build_history), so they are recorded as missing, never made up.
            skipped = range(last["cycle_day"] + 1, vals["cycle_day"])
            if 0 < len(skipped) <= 4000:
                kept = state.get("missing_days") if isinstance(state.get("missing_days"), list) else []
                state["missing_days"] = sorted(set(d for d in kept if _req_int(d)) | set(skipped))[-4000:]
        elif vals["cycle_day"] < last["cycle_day"]:
            return None                # the node went back a day (resync): keep what we had
    if isinstance(vals.get("cycle_day"), int):
        state["last_vals"] = vals
    return wrote


# ---- Signed days and streak per ACCOUNT (validators.json accounts[]) ---------------
# `signed_days`: the days the account signed (a day counts when ANY of its machines
# signed it), all days since the network's first day, today included once signed.
# `streak`: consecutive signed days ending today when the account signed today, else
# ending yesterday (today, still open, never breaks it; a finished day unsigned resets it).
#
# The node gives per MACHINE only `signed_30` (finished days signed in the last 30, NOT
# today: a machine that signed every day since its first_signed_day shows exactly
# today - first_signed_day) and first/last_signed_day; the days/ archive starts only on
# day 20770 while the network started on day 20765. So the collector keeps, in the state
# file ("sign_counts"), per account: {days, streak, through}, the signed days and the
# streak counted over the FINISHED days up to and including `through`. It is
#   - started once from a lower bound read from today's list (sign_lower_bounds): days =
#     the largest signed_30 of its machines; streak = today - first_signed_day for a
#     machine "proven" to have signed every day since its first (signed_30 equals that
#     number; possible only while it is within 30 days), else 1 when a machine's
#     last_signed_day is yesterday. The network is younger than 30 days (Nov 2026), so
#     today this bound is the exact figure for an account whose machines cover its days;
#   - moved on by one day when the day changes, from the previous day's last list: that
#     day was signed when a machine showed SIGNED with last_signed_day that day, or, in
#     the new list, a machine's last_signed_day is that day or (the very next day only,
#     last_signed_day on or after it, both counters plausible) its signed_30 grew (a late
#     signature after the last run of the day; signed_30 can only grow by a newly signed
#     day). Today counts only for a machine SIGNED with last_signed_day today, so a
#     `today` left over from the day before is never counted twice;
#   - never below the lower bound of the current list (both are lower bounds, so is
#     their maximum). After a gap (days the collector did not see) the streak restarts
#     from the lower bound and the days keep the larger of the two.
# Everything here is a LOWER bound: a missed signature only makes a figure small (a
# milestone late), never large. Null when the list has no day.
SIGN_KEEP_DAYS = 60                    # entries of accounts gone this long are forgotten


def _machines(a):
    return [m for m in (a.get("machines") if isinstance(a.get("machines"), list) else [])
            if isinstance(m, dict)]


def _day_signed(a, day):
    """True when a machine of `a` shows SIGNED for `day` (its last_signed_day must be that
    day too, so a `today` left over from the day before is never counted twice)."""
    return any(m.get("today") == "SIGNED" and m.get("last_signed_day") == day
               for m in _machines(a))


def _s30_ok(m, day):
    """True when m's signed_30 fits the counter this was written for, in a list of `day`:
    finished days signed since first_signed_day, at most 30 and at most day - first."""
    s30, fs = m.get("signed_30"), m.get("first_signed_day")
    return _req_int(s30) and _req_int(fs) and fs <= day and s30 <= min(30, day - fs)


def sign_lower_bounds(a, today):
    """(days, streak) the account surely signed among the finished days before `today`."""
    days = streak = 0
    for m in _machines(a):
        s30, fs, ls = m.get("signed_30"), m.get("first_signed_day"), m.get("last_signed_day")
        if _req_int(ls) and ls == today - 1:
            streak = max(streak, 1)
        if not _s30_ok(m, today):
            continue                   # not the counter this was written for: not used
        days = max(days, s30)
        # proven: signed every finished day since its first (then it also signed yesterday)
        if fs < today and s30 == today - fs and _req_int(ls) and ls >= today - 1:
            streak = max(streak, today - fs)
    return max(days, streak), streak


def account_signing(state, vals):
    """Set `signed_days` and `streak` on each account of `vals` (see above)."""
    today = vals.get("cycle_day")
    accounts = vals.get("accounts") or []
    if not _req_int(today):
        for a in accounts:
            a["signed_days"] = a["streak"] = None
        return
    raw = state.get("sign_counts")
    counts = {}
    for w, e in (raw.items() if isinstance(raw, dict) else []):
        if clean(w, WALLET) is not _BAD and w == w.lower() and isinstance(e, dict) \
                and all(_req_int(e.get(k)) for k in ("days", "streak", "through")) \
                and e["streak"] <= e["days"] and e["through"] >= today - SIGN_KEEP_DAYS:
            counts[w] = {k: e[k] for k in ("days", "streak", "through")}
    # The day changed: count the previous day (final now) once, for each account.
    prev = state.get("last_vals")
    if isinstance(prev, dict) and _req_int(prev.get("cycle_day")) and prev["cycle_day"] < today \
            and isinstance(prev.get("accounts"), list):
        day = prev["cycle_day"]
        cur = {a["wallet"].lower(): {m["code"]: m for m in _machines(a) if CODE(m.get("code")) is not _BAD}
               for a in accounts}
        for a in prev["accounts"]:
            if not isinstance(a, dict) or clean(a.get("wallet"), WALLET) in (_BAD, None):
                continue
            w = a["wallet"].lower()
            e = counts.get(w)
            if e is None or e["through"] != day - 1:
                continue               # not started, or a gap: the lower bound below decides
            now_m = cur.get(w, {})
            signed = _day_signed(a, day)
            for m in _machines(a):
                if CODE(m.get("code")) is _BAD:
                    continue
                n = now_m.get(m["code"]) or {}
                # signed_30 growing tells only when no day was skipped in between, the
                # machine signed on or after that day, and both counters are plausible
                if n.get("last_signed_day") == day or (
                        today == day + 1 and _req_int(n.get("last_signed_day"))
                        and n["last_signed_day"] >= day and _s30_ok(m, day) and _s30_ok(n, today)
                        and n["signed_30"] > m["signed_30"]):
                    signed = True
            counts[w] = {"days": e["days"] + (1 if signed else 0),
                         "streak": e["streak"] + 1 if signed else 0, "through": day}
    for a in accounts:
        w = a["wallet"].lower()
        lb_days, lb_streak = sign_lower_bounds(a, today)
        e = counts.get(w)
        if e is not None and e["through"] > today - 1:
            pub = (lb_days, lb_streak)  # the node went back a day (resync): kept, not used
        else:
            if e is None:
                e = {"days": lb_days, "streak": lb_streak}
            elif e["through"] == today - 1:
                e = {"days": max(e["days"], lb_days), "streak": max(e["streak"], lb_streak)}
            else:                      # days not seen: the streak is not known past them
                e = {"days": max(e["days"], lb_days), "streak": lb_streak}
            e["through"] = today - 1
            counts[w] = e
            pub = (e["days"], e["streak"])
        signed_today = 1 if _day_signed(a, today) else 0
        days, streak = INT(pub[0] + signed_today), INT(pub[1] + signed_today)
        a["signed_days"] = None if days is _BAD else days
        a["streak"] = None if streak is _BAD else streak
    state["sign_counts"] = counts


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
    return {"generated_unix": now, "days": cleaned,
            "missing_days": missing_days(out, state, today)}


# history.json `missing_days`: past days with no days/<day>.json, so the apps can show
# "no data" instead of silently joining the days on each side. The node has no route for
# a past day's validator list (network gives today's only; cycles/<day> is the day's
# certificate: totals, not machines), so such a day cannot be rebuilt. A day is missing
# when the collector saw the day jump over it (roll_day, kept in the state file), or when
# it lies between the archive's first day and today without a file. A missing day that
# later gets a file (a manual backfill) is no longer listed.
def missing_days(out, state, today):
    have = day_files(out)
    kept = state.get("missing_days") if isinstance(state.get("missing_days"), list) else []
    miss = set(d for d in kept if _req_int(d))
    if have and _req_int(today):
        miss |= set(range(min(have), today))
    miss = sorted(d for d in miss - have if today is None or d < today)[-4000:]
    ms = set(miss)
    state["missing_days"] = [d for d in kept if _req_int(d) and d in ms]
    return miss


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


# ---- awards.json "distributed_total" (all days since the chain started) -------------
# The sum of `award_total` over every settled day since the chain's first day, each day
# counted once. The node's days list covers only the last AWARD_DAYS days, so each
# settled day's total is kept in the state file ("award_totals", {day: wei string}): a
# day read again replaces its own value (a settled day that is not yet final may still
# be corrected), and a day that left the list keeps the value last read for it. The open
# day pays nothing yet and is not counted. Fail-closed: null unless EVERY day from the
# chain's start_day to the newest settled day has a value (a gap, for example after the
# state file was lost for more than AWARD_DAYS days, would make the sum too small), and
# null when the chain's start_day is unknown.
def distributed_total(state, days, start_day):
    kept = state.get("award_totals")
    kept = {k: v for k, v in kept.items() if isinstance(k, str) and k.isascii() and k.isdigit()
            and len(k) <= 9 and AMOUNT(v) is not _BAD} if isinstance(kept, dict) else {}
    if _req_int(start_day):
        # another chain start (a new chain): the stored days are not this chain's
        if state.get("award_totals_start") != start_day:
            kept = {}
        state["award_totals_start"] = start_day
    for d in days:
        if d.get("state") == "settled" and AMOUNT(d.get("award_total")) is not _BAD \
                and _req_int(d.get("day")):
            kept[str(d["day"])] = d["award_total"]
    state["award_totals"] = kept
    if not _req_int(start_day):
        return None
    settled = [int(k) for k in kept if int(k) >= start_day]
    if not settled:
        return None
    if any(str(x) not in kept for x in range(start_day, max(settled) + 1)):
        return None
    total = str(sum(int(kept[str(x)]) for x in range(start_day, max(settled) + 1)))
    return total if AMOUNT(total) is not _BAD else None


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
    total = distributed_total(state, days, chain.get("start_day"))
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
        "distributed_total": total,
        "live": None if (live is None or not _head_recent(chain, now)) else {"as_of_unix": now, "window_s": LIVE_WINDOW_S,
                                           "keys": dict(sorted(live.items()))},
        "balances": balances,
    }


# ---- health.json (each machine's vote "pulse", minute by minute, the last 24 hours) ---
# For every validating machine: in which minutes of the last 24 h its key cast at least one
# chain vote. From the same node route as awards.json `live`:
#   awards/network/day/{d}/blocks   every block of day d the node recorded, each with the
#                                   votes it carries: {slot, key_id, key_code, account, w}
# A vote counts in the minute of the slot it votes FOR (start_time + slot * slot_ms), not
# of the block that carries it, whatever its weight. A block carries votes for earlier
# slots only (1 to ~15 slots back, measured 20 Nov 2026), so the blocks after slot S hold
# every vote for a slot >= S: reading the blocks from the first minute not yet kept
# recounts that minute and every later one from scratch.
#
# File: {"generated_unix", "minute_s": 60, "from_unix", "minutes": 1440, "known": <b64>,
#        "machines": {"<key code>": <b64>}}. Each <b64> is a 1440-bit bitmap (180 bytes,
# standard base64 with padding): bit i is minute [from_unix + 60*i, from_unix + 60*(i+1)),
# bit i is in byte i // 8, most significant bit first (bit i set <=> byte[i >> 3] &
# (0x80 >> (i & 7))). `known` bit 1 = that minute is covered by fully recorded data; 0 =
# unknown (before the chain started, or a minute that ends after the node's newest block).
# A machine's bit 1 = at least one vote of its key for a slot in that minute; 0 in a known
# minute = no vote; in an unknown minute always 0 (read it as "no data", not "no vote").
# The last minute is the last whole minute before generated_unix.
#
# Kept in the state file ("health"): the minutes that ended at least HEALTH_SEAL_S before
# the chain's FINALIZED block (no reorganisation can change their blocks, and votes for them
# arriving that late were never seen), as one bitmap per key code over [base, upto). So a
# normal run reads the last ~45 minutes of blocks (one page); a first run, or one after the
# state was lost, reads up to 24 h (about 13 pages, a few seconds).
# Fail-closed: when this node did not record part of a chain day in the window (the node's
# `recorded` is "part" or "none") the file is not written at all: unknown minutes would show
# when this node started recording, a hint about which machine it is (same reason as
# awards.json per-account counts). Any error leaves health.json and the kept state as they were.
HEALTH_MINUTE_S = 60
HEALTH_MINUTES = 1440
HEALTH_SEAL_S = 1800
HEALTH_PAGES = 40                      # 24 h is ~13 pages of 240 blocks: far beyond any run
# Votes for a slot reach blocks up to ~14 slots later (measured 20 Nov 2026: 20% of a
# minute's votes are not in any block yet when the head passes the minute's end, none
# after 7 minutes). A minute stays unknown until the head is this many slots past it,
# so the newest minutes never show a false 0.
HEALTH_SETTLE_SLOTS = 15


def _bits_b64(n, ones):
    """Base64 of an n-bit bitmap, MSB first, with bit i set for each i in `ones`."""
    buf = bytearray((n + 7) // 8)
    for i in ones:
        buf[i >> 3] |= 0x80 >> (i & 7)
    return base64.b64encode(bytes(buf)).decode("ascii")


def _b64_bits(s, n):
    """The set bit indexes of an n-bit bitmap from _bits_b64, or None if malformed."""
    if not isinstance(s, str) or len(s) > 4 * ((n + 7) // 8 + 2) // 3 + 4:
        return None
    try:
        buf = base64.b64decode(s.encode("ascii"), validate=True)
    except (ValueError, UnicodeEncodeError):
        return None
    if len(buf) != (n + 7) // 8:
        return None
    return set(i for i in range(n) if buf[i >> 3] & (0x80 >> (i & 7)))


def build_health(api, chain, codes, state, now, live=None):
    """health.json for the roster's machine `codes`, or None (and why on stderr)."""
    def no(why):
        print("health.json not written: %s" % why, file=sys.stderr)
        return None
    start, slot_ms = chain.get("start_time"), chain.get("slot_ms")
    head = chain.get("head") or {}
    if not _req_int(start) or not _req_int(slot_ms) or not slot_ms or not _req_int(head.get("time")):
        return no("the chain's start, slot length or head is not usable")
    codes = sorted(set(c for c in codes if CODE(c) is not _BAD))
    if not codes:
        return no("no validating machines")
    M = HEALTH_MINUTE_S
    end = now // M * M                 # the last minute is the last whole minute
    frm = end - HEALTH_MINUTES * M
    # The kept minutes: [frm, upto), with the minutes in it that had a vote, per code, and
    # the unknown ones. Anything malformed, or not covering frm, is dropped (read again).
    old = state.get("health")
    old = old if isinstance(old, dict) and old.get("start_time") == start \
        and old.get("minute_s") == M else {}
    base, upto = old.get("base"), old.get("upto")
    voted, unknown_kept, upto_ok = {}, set(), frm
    if _req_int(base) and _req_int(upto) and base % M == 0 and upto % M == 0 \
            and base <= frm < upto <= end and (upto - base) // M <= 2 * HEALTH_MINUTES \
            and isinstance(old.get("codes"), dict) and len(old["codes"]) <= 20000:
        n = (upto - base) // M
        unk = _b64_bits(old.get("unknown"), n)
        per = {}
        for c, s in old["codes"].items():
            got = _b64_bits(s, n) if CODE(c) is not _BAD else None
            if got is None:
                per = None
                break
            per[c] = got
        if unk is not None and per is not None:
            skip = (frm - base) // M
            for c, got in per.items():
                voted[c] = set(base + i * M for i in got if i >= skip)
            unknown_kept = set(base + i * M for i in unk if i >= skip)
            upto_ok = upto
    todo = list(range(upto_ok, end, M))
    hit = {}                           # code -> minutes (unix) with a vote, for todo minutes
    unknown = set()
    rows_seen, recent = 0, set()
    if todo:
        lo_ms = todo[0] * 1000
        s0 = max(0, -(-(lo_ms - start) // slot_ms))      # first slot at or after lo_ms
        after = s0 - 1
        pages, seen = 0, set()
        prev_h = prev_s = None         # heights follow each other; where this node lacks
        holes = []                     # blocks, the votes they carried are not seen: those
                                       # minutes become unknown (a gap), never a 0
        for d in range((start + s0 * slot_ms) // 86400000, now // 86400 + 1):
            a = after
            while True:
                pages += 1
                if pages > HEALTH_PAGES:
                    return no("more than %d pages of blocks" % HEALTH_PAGES)
                try:
                    page = get(api, "awards/network/day/%d/blocks?limit=240%s" % (
                        d, "" if a < 0 else "&after_slot=%d" % a))
                except urllib.error.HTTPError as e:
                    if e.code == 404:  # a day the chain does not have (before its start)
                        if d * 86400000 >= head["time"]:
                            break      # after the head: covered by the head rule below
                        unknown.update(t for t in todo if t * 1000 < (d + 1) * 86400000
                                       and (t + M) * 1000 > d * 86400000)
                        break
                    raise
                if not isinstance(page, dict) or page.get("day") != d \
                        or not isinstance(page.get("rows"), list):
                    return no("the node's blocks answer has an unexpected shape")
                if page.get("recorded") != "all":
                    return no("this node did not record all of day %d (%r)" % (d, page.get("recorded")))
                for r in page["rows"]:
                    if not isinstance(r, dict) or not _req_int(r.get("slot")) \
                            or not _req_int(r.get("time")) or not isinstance(r.get("votes"), list):
                        return no("a block row has an unexpected shape")
                    h = r.get("height")
                    if _req_int(h):
                        if prev_h is not None and h != prev_h + 1:
                            if h <= prev_h:
                                return no("the node's blocks go back from height %d to %d" % (prev_h, h))
                            # The missing blocks sat between the two slots and carried votes
                            # for slots up to HEALTH_SETTLE_SLOTS before them.
                            holes.append((prev_s - HEALTH_SETTLE_SLOTS, r["slot"]))
                            print("health.json: this node lacks blocks %d to %d (a gap)" % (prev_h + 1, h - 1),
                                  file=sys.stderr)
                        prev_h, prev_s = h, r["slot"]
                    rows_seen += 1
                    if r["time"] >= (now - LIVE_WINDOW_S) * 1000:
                        for x in [r.get("proposer")] + r["votes"]:
                            c = x.get("key_code") if isinstance(x, dict) else None
                            if CODE(c) is not _BAD:
                                recent.add(c)
                    for v in r["votes"]:
                        c = v.get("key_code") if isinstance(v, dict) else None
                        vs = v.get("slot") if isinstance(v, dict) else None
                        if CODE(c) is _BAD or not _req_int(vs) or vs >= r["slot"]:
                            return no("a vote has an unexpected shape")
                        if (c, vs) in seen:
                            continue
                        seen.add((c, vs))
                        t = (start + vs * slot_ms) // 1000 // M * M
                        if upto_ok <= t < end:
                            hit.setdefault(c, set()).add(t)
                nxt = page.get("next_after_slot")
                if nxt is None or not page["rows"]:
                    break
                if not _req_int(nxt) or nxt <= a:
                    return no("the node's blocks paging went backwards")
                a = nxt
    # Unknown: before the chain's first slot, and any minute whose votes may still be on
    # their way (it ends less than HEALTH_SETTLE_SLOTS before the node's newest block: a
    # lagging node, or the newest minutes, whose votes later blocks still carry).
    for t in todo:
        if (t + M) * 1000 <= start or (t + M) * 1000 + HEALTH_SETTLE_SLOTS * slot_ms > head["time"]:
            unknown.add(t)
    for lo, hi in holes:
        a_ms, b_ms = start + lo * slot_ms, start + (hi + 1) * slot_ms
        unknown.update(t for t in todo if t * 1000 < b_ms and (t + M) * 1000 > a_ms)
    for c in hit:
        hit[c] -= unknown
    if isinstance(live, dict) and isinstance(live.get("keys"), dict) and rows_seen:
        lk = set(live["keys"])
        if lk != recent:
            print("health.json: the last %d s differ from awards.json live.keys (%d only there, %d only here)"
                  % (LIVE_WINDOW_S, len(lk - recent), len(recent - lk)), file=sys.stderr)

    all_unknown = unknown_kept | unknown

    def index(ts, lo, n):
        return [(t - lo) // M for t in ts if lo <= t < lo + n * M]
    machines = {}
    for c in codes:
        machines[c] = _bits_b64(HEALTH_MINUTES, index(voted.get(c, set()) | hit.get(c, set()), frm, HEALTH_MINUTES))
    known = _bits_b64(HEALTH_MINUTES, [i for i in range(HEALTH_MINUTES) if frm + i * M not in all_unknown])
    # Keep what is now final enough: [frm, new_upto).
    final = (chain.get("finalized") or {}).get("time")
    new_upto = upto_ok
    if _req_int(final):
        lim = min(final // 1000 - HEALTH_SEAL_S, now - HEALTH_SEAL_S) // M * M
        new_upto = max(upto_ok, min(lim, end))
    n = (new_upto - frm) // M
    kept_codes = {}
    for c in set(voted) | set(hit):
        ts = index(voted.get(c, set()) | hit.get(c, set()), frm, n)
        if ts:
            kept_codes[c] = _bits_b64(n, ts)
    state["health"] = {"start_time": start, "minute_s": M, "base": frm, "upto": new_upto,
                       "unknown": _bits_b64(n, index(all_unknown, frm, n)), "codes": kept_codes}
    return {"generated_unix": now, "minute_s": M, "from_unix": frm, "minutes": HEALTH_MINUTES,
            "known": known, "machines": machines}


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


# ---- 6529's signed release feed (releases.json) --------------------------------------
# 6529 publishes https://6529.io/validator/releases.json (releases, deadlines and signed
# checkpoints of the network's history) with a detached signature, releases.json.sig:
#     {"format": 1, "algorithm": "ecdsa-secp256k1-sha256", "key": "0xf005…B832",
#      "signature": "0x<r 32 bytes><s 32 bytes>"}
# The message is SHA-256("6529 Validator releases v1" || 0x00 || the feed's exact bytes),
# signed with secp256k1 as r||s (no recovery byte). The key is pinned below as its Ethereum
# address (keccak-256 of the public key, last 20 bytes), so the check is: recover the public
# key from (r, s) with recovery id 0 or 1, and accept only if one of them has that address.
# This is the scheme of 6529's own code (frontend lib/validator-page/feedSigningKeys.ts,
# KMS alias 6529-validator-releases; audit/2026-11-18-data-sources.md), and the real feed
# of 18 Nov 2026 verifies with it (test fixture). The .sig's own "key" field is only checked
# to name the pinned key; the key itself is never taken from the network.
#
# Only a feed that verifies is republished, as our own releases.json (signed with our
# envelope like every other file): the source, the signer, when we verified it, the feed
# as parsed and as its exact text (so anyone can check 6529's signature from our file), the
# SHA-256 of those bytes and 6529's signature. A failed fetch or check,
# or an older feed (lower `sequence`) than the newest one ever published, leaves the previous
# file as it is and logs one line. Fetched at most every RELEASES_EVERY_S.
RELEASES_URL = "https://6529.io/validator/releases.json"
RELEASES_SIG_URL = RELEASES_URL + ".sig"
RELEASES_SIGNER = "0xf005F32cb7Ecf25566B8Cf6685394e0cC694B832"
RELEASES_DOMAIN = b"6529 Validator releases v1\x00"
RELEASES_ALG = "ecdsa-secp256k1-sha256"
RELEASES_MAX = 64 * 1024            # 6529 caps the feed at 56 KiB
RELEASES_SIG_MAX = 4096
RELEASES_EVERY_S = 900
RELEASES_TIMEOUT_S = 8
PUBLIC_UA = HANDLE_UA

# Keccak-256 (Ethereum's, the original padding 0x01, not SHA3-256's 0x06).
_KRC = (0x0000000000000001, 0x0000000000008082, 0x800000000000808A, 0x8000000080008000,
        0x000000000000808B, 0x0000000080000001, 0x8000000080008081, 0x8000000000008009,
        0x000000000000008A, 0x0000000000000088, 0x0000000080008009, 0x000000008000000A,
        0x000000008000808B, 0x800000000000008B, 0x8000000000008089, 0x8000000000008003,
        0x8000000000008002, 0x8000000000000080, 0x000000000000800A, 0x800000008000000A,
        0x8000000080008081, 0x8000000000008080, 0x0000000080000001, 0x8000000080008008)
_KROT = ((0, 36, 3, 41, 18), (1, 44, 10, 45, 2), (62, 6, 43, 15, 61), (28, 55, 25, 21, 56),
         (27, 20, 39, 8, 14))
_M64 = (1 << 64) - 1


def _keccak_f(a):
    for rc in _KRC:
        c = [a[x][0] ^ a[x][1] ^ a[x][2] ^ a[x][3] ^ a[x][4] for x in range(5)]
        d = [c[(x - 1) % 5] ^ (((c[(x + 1) % 5] << 1) | (c[(x + 1) % 5] >> 63)) & _M64)
             for x in range(5)]
        a = [[a[x][y] ^ d[x] for y in range(5)] for x in range(5)]
        b = [[0] * 5 for _ in range(5)]
        for x in range(5):
            for y in range(5):
                r, v = _KROT[x][y], a[x][y]
                b[y][(2 * x + 3 * y) % 5] = (((v << r) | (v >> (64 - r))) & _M64) if r else v
        a = [[b[x][y] ^ ((~b[(x + 1) % 5][y]) & b[(x + 2) % 5][y]) for y in range(5)]
             for x in range(5)]
        a[0][0] ^= rc
    return a


def keccak256(data):
    rate = 136
    msg = bytearray(data) + b"\x01"
    msg += b"\x00" * (-len(msg) % rate)
    msg[-1] |= 0x80
    a = [[0] * 5 for _ in range(5)]
    for off in range(0, len(msg), rate):
        for i in range(rate // 8):
            a[i % 5][i // 5] ^= int.from_bytes(msg[off + 8 * i:off + 8 * i + 8], "little")
        a = _keccak_f(a)
    return b"".join(a[i % 5][i // 5].to_bytes(8, "little") for i in range(4))


# secp256k1, Jacobian coordinates (verification only: nothing secret, so no constant time).
K1_P = 2 ** 256 - 2 ** 32 - 977
K1_N = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEBAAEDCE6AF48A03BBFD25E8CD0364141
K1_G = (0x79BE667EF9DCBBAC55A06295CE870B07029BFCDB2DCE28D959F2815B16F81798,
        0x483ADA7726A3C4655DA4FBFC0E1108A8FD17B448A68554199C47D08FFB10D4B8)


def _k1_dbl(p):
    if p is None or p[1] == 0:
        return None
    x, y, z = p
    P = K1_P
    s = 4 * x * y * y % P
    m = 3 * x * x % P
    x3 = (m * m - 2 * s) % P
    return (x3, (m * (s - x3) - 8 * pow(y, 4, P)) % P, 2 * y * z % P)


def _k1_add(p, q):
    if p is None:
        return q
    if q is None:
        return p
    P = K1_P
    x1, y1, z1 = p
    x2, y2, z2 = q
    z1s, z2s = z1 * z1 % P, z2 * z2 % P
    u1, u2 = x1 * z2s % P, x2 * z1s % P
    s1, s2 = y1 * z2s * z2 % P, y2 * z1s * z1 % P
    if u1 == u2:
        return _k1_dbl(p) if s1 == s2 else None
    h, r = (u2 - u1) % P, (s2 - s1) % P
    h2 = h * h % P
    h3 = h * h2 % P
    x3 = (r * r - h3 - 2 * u1 * h2) % P
    return (x3, (r * (u1 * h2 - x3) - s1 * h3) % P, h * z1 * z2 % P)


def k1_mul(k, point):
    """k * point (affine (x, y) in, affine out; None is the point at infinity)."""
    acc, add = None, (point[0], point[1], 1)
    k %= K1_N
    while k:
        if k & 1:
            acc = _k1_add(acc, add)
        add = _k1_dbl(add)
        k >>= 1
    return _k1_affine(acc)


def _k1_affine(p):
    if p is None:
        return None
    x, y, z = p
    zi = pow(z, K1_P - 2, K1_P)
    return (x * zi * zi % K1_P, y * zi * zi * zi % K1_P)


def k1_recover(z, r, s, recid):
    """The public key (x, y) whose ECDSA signature (r, s) over the hash z (an int) has recovery
    id `recid` (0 or 1: the parity of R's y), or None. r >= n (recovery ids 2, 3) never occurs
    in practice and is not supported."""
    P, N = K1_P, K1_N
    if not (1 <= r < N and 1 <= s < N) or recid not in (0, 1):
        return None
    alpha = (r * r * r + 7) % P
    y = pow(alpha, (P + 1) // 4, P)
    if y * y % P != alpha:
        return None
    if y & 1 != recid:
        y = P - y
    ri = pow(r, N - 2, N)
    a = k1_mul(s * ri % N, (r, y))
    b = k1_mul((-z * ri) % N, K1_G)
    q = _k1_add(None if a is None else (a[0], a[1], 1), None if b is None else (b[0], b[1], 1))
    return _k1_affine(q)


def eth_address(pub):
    """The lower-case Ethereum address of a public key (x, y)."""
    return "0x" + keccak256(pub[0].to_bytes(32, "big") + pub[1].to_bytes(32, "big"))[12:].hex()


def k1_signed_by(digest, sig64, address):
    """True when the 64-byte r||s signature over the 32-byte digest is by `address`."""
    if len(digest) != 32 or len(sig64) != 64:
        return False
    z = int.from_bytes(digest, "big")
    r, s = int.from_bytes(sig64[:32], "big"), int.from_bytes(sig64[32:], "big")
    for recid in (0, 1):
        q = k1_recover(z, r, s, recid)
        if q is not None and eth_address(q) == address.lower():
            return True
    return False


_SIG_HEX = re.compile(r"^0x[0-9a-fA-F]{128}$")


def _is_one(v):
    return type(v) is int and v == 1          # not True, not 1.0


def verify_release_feed(feed_raw, sig_raw):
    """(the parsed feed, its exact text, the signature as 0x + 128 lower-case hex) when sig_raw is 6529's valid
    signature over the exact bytes feed_raw by the pinned key; raises ValueError (why) if not."""
    if not isinstance(feed_raw, bytes) or not 0 < len(feed_raw) <= RELEASES_MAX:
        raise ValueError("feed size")
    try:
        sig = json.loads(sig_raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError, RecursionError):
        raise ValueError("signature file is not JSON")
    if not isinstance(sig, dict) or not _is_one(sig.get("format")) or sig.get("algorithm") != RELEASES_ALG:
        raise ValueError("unsupported signature format")
    if not isinstance(sig.get("key"), str) or sig["key"].lower() != RELEASES_SIGNER.lower():
        raise ValueError("signature names another key")
    hx = sig.get("signature")
    if not isinstance(hx, str) or not _SIG_HEX.match(hx):
        raise ValueError("signature is not 64 bytes of hex")
    digest = hashlib.sha256(RELEASES_DOMAIN + feed_raw).digest()
    if not k1_signed_by(digest, bytes.fromhex(hx[2:]), RELEASES_SIGNER):
        raise ValueError("signature does not verify")
    try:
        text = feed_raw.decode("utf-8")
        feed = json.loads(text)
    except (ValueError, UnicodeDecodeError, RecursionError):
        raise ValueError("signed feed is not JSON")
    if FEED(feed) is _BAD:
        raise ValueError("signed feed of an unknown format")
    return feed, text, hx.lower()


def fetch_public(url, most, timeout):
    """The body of a public GET (no proxy, no redirect, at most `most` bytes); raises on any
    failure (network, HTTP status other than 200, too large, or more than `timeout` seconds in
    all: the socket timeout bounds each wait, and the body is read in pieces checked against
    the same deadline, so a server that trickles bytes cannot hold the run)."""
    end = time.monotonic() + timeout
    req = urllib.request.Request(url, headers={"Accept": "application/json", "User-Agent": PUBLIC_UA})
    with OPENER.open(req, timeout=timeout) as r:
        if r.status != 200:
            raise ValueError("HTTP %s" % r.status)
        parts, size = [], 0
        while size <= most:
            if time.monotonic() > end:
                raise TimeoutError("too slow")
            chunk = r.read(min(8192, most + 1 - size))
            if not chunk:
                break
            parts.append(chunk)
            size += len(chunk)
    if size > most:
        raise ValueError("answer too large")
    return b"".join(parts)


# The releases fetch and the cross-check together get at most PUBLIC_BUDGET_S of wall clock per
# run (checked before each request; each request's timeout is at most what is left), so a slow
# 6529 can delay the run, and the signing and upload after it, by that much at most.
PUBLIC_BUDGET_S = 20
PUBLIC_MIN_S = 1.0                     # less than this left: the request is not made


def _left(deadline, cap):
    """The timeout for the next request: `cap`, cut to what is left before `deadline`
    (time.monotonic(); None = no budget); None when too little is left to try."""
    if deadline is None:
        return cap
    left = deadline - time.monotonic()
    return min(cap, left) if left >= PUBLIC_MIN_S else None


def FEED(v):  # 6529's own signed feed, republished as parsed once its signature verified
    return v if isinstance(v, dict) and _is_one(v.get("format")) and INT(v.get("sequence")) is not _BAD else _BAD


def FEED_TEXT(v):  # the exact text 6529 signed (UTF-8), so anyone can check feed_sha256 and sig
    return v if isinstance(v, str) and 0 < len(v.encode("utf-8")) <= RELEASES_MAX else _BAD


RELEASES_FILE = {
    "source": RE(r"^https://6529\.io/validator/releases\.json$"),
    "signer": WALLET, "verified_unix": INT, "generated_unix": INT, "feed": FEED, "feed_text": FEED_TEXT,
    "feed_sha256": RE(r"^[0-9a-f]{64}$"), "sig": RE(r"^0x[0-9a-f]{128}$"),
}


def _read_own(path):
    try:
        with open(path) as f:
            v = json.load(f)
        return v if isinstance(v, dict) else None
    except (OSError, ValueError, RecursionError):
        return None


def update_releases(out, state, now, fetch=fetch_public, deadline=None):
    """Fetch, verify and republish 6529's release feed when due (see above), within the time
    budget `deadline` (time.monotonic(), None = none). Returns a short log line, or None when
    nothing was due or the file was written."""
    path = os.path.join(out, "releases.json")
    st = state.get("releases")
    st = st if isinstance(st, dict) else {}
    last = INT(st.get("t"))
    if last is not _BAD and 0 <= now - last < RELEASES_EVERY_S:
        return None
    if _left(deadline, RELEASES_TIMEOUT_S) is None:
        return "releases.json: no time left this run, previous file kept"
    state["releases"] = st
    st["t"] = now
    try:
        t = _left(deadline, RELEASES_TIMEOUT_S)
        feed_raw = fetch(RELEASES_URL, RELEASES_MAX, t)
        t = _left(deadline, RELEASES_TIMEOUT_S)
        if t is None:
            raise TimeoutError("time budget")
        sig_raw = fetch(RELEASES_SIG_URL, RELEASES_SIG_MAX, t)
    except Exception as e:             # network, HTTP, too large, too slow: keep the previous file
        return "releases.json: fetch failed (%s), previous file kept" % type(e).__name__
    try:
        feed, feed_text, sig = verify_release_feed(feed_raw, sig_raw)
    except ValueError as e:
        return "releases.json: NOT published, %s; previous file kept" % e
    # Never back: not below the newest sequence ever published here (state) nor the one in the
    # file now, whichever is higher.
    old_feed = (_read_own(path) or {}).get("feed")
    seqs = [x for x in (INT(st.get("sequence")),
                        INT(old_feed.get("sequence")) if isinstance(old_feed, dict) else _BAD)
            if x is not _BAD]
    if seqs and feed["sequence"] < max(seqs):
        return "releases.json: verified feed is older (sequence %d < %d), previous file kept" % (
            feed["sequence"], max(seqs))
    rec = clean({"source": RELEASES_URL, "signer": RELEASES_SIGNER.lower(), "verified_unix": now,
                 "generated_unix": now, "feed": feed, "feed_text": feed_text,
                 "feed_sha256": hashlib.sha256(feed_raw).hexdigest(), "sig": sig},
                RELEASES_FILE, "releases")
    if rec is _BAD or len(rec) != len(RELEASES_FILE):
        return "releases.json: does not fit the schema, previous file kept"
    write_json(path, rec)
    st["sequence"] = feed["sequence"]
    return None


# ---- "6529 agrees": our cycle against 6529's own public numbers -------------------------
# Two independent checks of what this node publishes, shown in network.json `crosscheck`:
#  - cutoff / tdh: today's cycle header (cutoff_number, total_tdh) against api.6529.io's TDH
#    history row for the same UTC date (`block`, `total_boosted_tdh`): 6529's TDH run that the
#    validators' cycle is built on. One small request (CROSSCHECK_URL), at most
#    CROSSCHECK_TRIES per cycle day and CROSSCHECK_RETRY_S apart, and none once the day has an
#    answer; remembered in the collector's state.
#  - checkpoint: every checkpoint in 6529's signed feed (our verified releases.json),
#    "day:cutoff_number:history_root:history_count:cycle_root:roster_root", against our
#    cycles/<day>.json. Local, every run.
# Each is "ok", "differs" or "unknown". "unknown" whenever 6529's side could not be read or
# has nothing for that day yet (or our side is missing): a failed read is never "differs".
CROSSCHECK_URL = "https://api.6529.io/api/tdh_global_history?page_size=3"
CROSSCHECK_TRIES = 3
CROSSCHECK_RETRY_S = 3600
CROSSCHECK_MAX = 64 * 1024
VERDICT = RE(r"^(ok|differs|unknown)$")
CROSSCHECK = {"day": INT, "cutoff": VERDICT, "tdh": VERDICT, "checkpoint": VERDICT,
              "checked_unix": INT}
_CHECKPOINT = re.compile(r"^([0-9]{1,6}):([0-9]{1,12}):(0x[0-9a-fA-F]{64}):([0-9]{1,12}):"
                         r"(0x[0-9a-fA-F]{64}):(0x[0-9a-fA-F]{64})$")


def _verdict(ours, theirs):
    if INT(ours) is _BAD or INT(theirs) is _BAD:
        return "unknown"
    return "ok" if ours == theirs else "differs"


def compare_tdh(header, rows, day):
    """(cutoff, tdh) verdicts for cycle `day` from 6529's TDH history rows."""
    date = time.strftime("%Y-%m-%d", time.gmtime(day * 86400))
    row = None
    if isinstance(rows, list):
        for r in rows:
            if isinstance(r, dict) and r.get("date") == date:
                row = r
                break
    if not isinstance(header, dict) or row is None:
        return "unknown", "unknown"
    return (_verdict(header.get("cutoff_number"), row.get("block")),
            _verdict(header.get("total_tdh"), row.get("total_boosted_tdh")))


def fetch_tdh_rows(timeout=HANDLE_TIMEOUT_S, fetch=fetch_public):
    d = json.loads(fetch(CROSSCHECK_URL, CROSSCHECK_MAX, timeout).decode("utf-8"))
    rows = d.get("data") if isinstance(d, dict) else None
    if not isinstance(rows, list):
        raise ValueError("unexpected answer")
    return rows


def check_checkpoints(out):
    """Our cycle files against the checkpoints in our verified releases.json."""
    rel = _read_own(os.path.join(out, "releases.json"))
    feed = rel.get("feed") if rel else None
    cps = feed.get("checkpoints") if isinstance(feed, dict) else None
    if not isinstance(cps, list):
        return "unknown"
    seen_ok = False
    for cp in cps:
        m = _CHECKPOINT.match(cp) if isinstance(cp, str) else None
        if not m:
            continue
        day = int(m.group(1))
        c = _read_own(os.path.join(out, "cycles", "%d.json" % day))
        if not c:
            continue
        ours = ((c.get("header") or {}).get("cutoff_number"), c.get("history_root"),
                c.get("history_count"), c.get("cycle_root"),
                (c.get("certificate") or {}).get("roster_root"))
        if any(x is None for x in ours):
            continue
        theirs = (int(m.group(2)), m.group(3).lower(), int(m.group(4)), m.group(5).lower(),
                  m.group(6).lower())
        ours = tuple(x.lower() if isinstance(x, str) else x for x in ours)
        if ours != theirs:
            return "differs"
        seen_ok = True
    return "ok" if seen_ok else "unknown"


def update_crosscheck(out, state, today, now, fetch_rows=fetch_tdh_rows, deadline=None):
    """network.json's `crosscheck` for cycle day `today` (see above), calling 6529 only when
    due and within the time budget `deadline` (as update_releases). Never raises for a failed
    read; no time left means no request (and no try counted) this run."""
    st = state.get("crosscheck")
    if not isinstance(st, dict) or st.get("day") != today:
        st = {"day": today, "cutoff": "unknown", "tdh": "unknown", "tries": 0, "t": None,
              "checked_unix": None}
    for k in ("cutoff", "tdh"):
        if VERDICT(st.get(k)) is _BAD:
            st[k] = "unknown"
    tries = INT(st.get("tries"))
    tries = 0 if tries is _BAD else tries
    last = INT(st.get("t"))
    header = (_read_own(os.path.join(out, "cycles", "%d.json" % today)) or {}).get("header")
    answered = st["cutoff"] != "unknown" and st["tdh"] != "unknown"
    due = (not answered and tries < CROSSCHECK_TRIES
           and (last is _BAD or not 0 <= now - last < CROSSCHECK_RETRY_S)
           and isinstance(header, dict) and INT(header.get("cutoff_number")) is not _BAD)
    timeout = _left(deadline, HANDLE_TIMEOUT_S) if due else None
    if due and timeout is not None:
        st["tries"], st["t"] = tries + 1, now
        try:
            rows = fetch_rows(timeout)
        except Exception:              # network, HTTP, JSON: unknown, try again later
            rows = None
        cutoff, tdh = compare_tdh(header, rows, today)
        st["cutoff"], st["tdh"], st["checked_unix"] = cutoff, tdh, now
    state["crosscheck"] = st
    res = clean({"day": today, "cutoff": st["cutoff"], "tdh": st["tdh"],
                 "checkpoint": check_checkpoints(out),
                 "checked_unix": st.get("checked_unix")}, CROSSCHECK, "crosscheck")
    return None if res is _BAD else res


# ---- The Monitor app's current iOS version --------------------------------------------
# network.json `app` = {"ios": {"version", "released_unix"}}: the version of our own iOS app
# on the App Store, from Apple's public lookup (APP_LOOKUP_URL, no user data), so the app can
# tell its users an update exists. At most once an hour (state "app"), inside the same
# PUBLIC_BUDGET_S as the release feed and the cross-check. Strictly checked; on any failure
# (network, HTTP, JSON, another app, odd version or date) the previous value stays. Android
# is not included: Google Play has no public lookup, the Android app uses Play's own check.
APP_STORE_ID = 6834147132
APP_LOOKUP_URL = "https://itunes.apple.com/lookup?id=%d&country=us" % APP_STORE_ID
APP_EVERY_S = 3600
APP_TIMEOUT_S = 8
APP_MAX = 256 * 1024
APP_EARLIEST_UNIX = 1704067200         # 2024-01-01: a release date before this is not ours
APP_VERSION = RE(r"^\d{1,3}(\.\d{1,3}){1,2}\Z")   # \Z: no trailing newline
APP = {"ios": {"version": APP_VERSION, "released_unix": INT}}


def _app_ok(v):
    """`v` when it is a complete, valid `app` value, else None."""
    r = clean(v, APP, "app") if isinstance(v, dict) else _BAD
    if r is _BAD or not r or not isinstance(r.get("ios"), dict) or len(r["ios"]) != 2:
        return None
    return r


def parse_app_lookup(raw, now):
    """{"version", "released_unix"} from Apple's lookup answer; raises ValueError otherwise."""
    try:
        doc = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError, RecursionError):
        raise ValueError("not JSON")
    res = doc.get("results") if isinstance(doc, dict) else None
    if not isinstance(res, list) or not res or not isinstance(res[0], dict):
        raise ValueError("no result")
    r = res[0]
    if r.get("trackId") != APP_STORE_ID:
        raise ValueError("another app")
    version = r.get("version")
    if APP_VERSION(version) is _BAD:
        raise ValueError("odd version")
    date = r.get("currentVersionReleaseDate")
    if not isinstance(date, str) or not re.match(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ\Z", date):
        raise ValueError("odd date")
    try:
        t = int(calendar.timegm(time.strptime(date, "%Y-%m-%dT%H:%M:%SZ")))
    except (ValueError, OverflowError):
        raise ValueError("odd date")
    if not APP_EARLIEST_UNIX <= t <= now + 86400:
        raise ValueError("date out of range")
    return {"version": version, "released_unix": t}


def app_known(state, previous=None):
    """The last good `app` value: the collector's state, else `previous` (network.json's)."""
    st = state.get("app")
    return (_app_ok({"ios": st.get("ios")}) if isinstance(st, dict) else None) or _app_ok(previous)


def update_app(state, now, previous=None, fetch=fetch_public, deadline=None):
    """network.json's `app` (see above): Apple asked when due and within the time budget
    `deadline` (as update_releases); otherwise, and on any failure, the last good value
    (state, else `previous`, the value in the network.json now). Returns (app or None, note)."""
    st = state.get("app")
    st = st if isinstance(st, dict) else {}
    good = app_known(state, previous)
    last = INT(st.get("t"))
    note = None
    if last is _BAD or not 0 <= now - last < APP_EVERY_S:
        t = _left(deadline, APP_TIMEOUT_S)
        if t is None:
            note = "app version: no time left this run, previous value kept"
        else:
            st["t"] = now
            try:
                ios = parse_app_lookup(fetch(APP_LOOKUP_URL, APP_MAX, t), now)
                good = _app_ok({"ios": ios}) or good
            except ValueError as e:
                note = "app version: NOT published, %s; previous value kept" % e
            except Exception as e:     # network, HTTP, too large, too slow
                note = "app version: fetch failed (%s), previous value kept" % type(e).__name__
    if good:
        st["ios"] = good["ios"]
    state["app"] = st
    return good, note


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
        try:
            # before roll_day: it reads the previous run's list (state "last_vals")
            account_signing(state, vals)
        except Exception as e:         # never let these two figures stop the run
            print("signed_days/streak not computed: %s" % type(e).__name__, file=sys.stderr)
            for x in vals["accounts"]:
                x["signed_days"] = x["streak"] = None
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
    # The "6529 agrees" check as last computed. It is refreshed at the end of the run, after
    # every file is written, so a slow 6529 does not delay these files; it can delay the end of
    # the run (and so the signing and upload that follow) by at most PUBLIC_BUDGET_S.
    cc = clean(previous.get("crosscheck"), CROSSCHECK, "network.crosscheck")
    net["crosscheck"] = cc if (cc is not _BAD and cc and cc.get("day") == today) else None
    # The iOS app's App Store version as last known (refreshed at the end of the run too).
    net["app"] = app_known(state, previous.get("app"))
    # The chain's award roster (see chain_roster). Optional: left out when not read.
    try:
        roster = chain_roster(a.api)
    except Exception as e:             # never let this optional field stop the run
        print("chain_roster not read: %s" % type(e).__name__, file=sys.stderr)
        roster = None
    if roster:
        net["chain_roster"] = roster
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
    # The minutes each machine voted in, last 24 h (see build_health). Optional, like blocks.json.
    health = None
    try:
        health = build_health(a.api, chain, [m.get("code") for x in vals["accounts"]
                                             for m in x.get("machines") or []
                                             if isinstance(m, dict)],
                              state, int(time.time()),
                              live=(awards or {}).get("live")) if (chain and vals) else None
    except Exception as e:             # never let this optional file stop the others
        print("health.json not written: %s" % type(e).__name__, file=sys.stderr)
    if health:
        write_json(os.path.join(out, "health.json"), health)
    if vals:
        try:
            update_handles(state, [x["wallet"].lower() for x in vals["accounts"]], int(time.time()))
        except Exception as e:         # never let the handles stop the run
            print("handles not updated: %s" % type(e).__name__, file=sys.stderr)
    # 6529's signed release feed, then the "6529 agrees" check (see their sections). Each only
    # ever leaves its own output as it was when something fails. Then the iOS app's version. All
    # three share one time budget.
    deadline = time.monotonic() + PUBLIC_BUDGET_S
    try:
        note = update_releases(out, state, int(time.time()), deadline=deadline)
        if note:
            print(note, file=sys.stderr)
    except Exception as e:
        print("releases.json not updated: %s" % type(e).__name__, file=sys.stderr)
    try:
        cc = update_crosscheck(out, state, today, int(time.time()), deadline=deadline)
        if cc != net.get("crosscheck"):
            net["crosscheck"] = cc
            write_json(os.path.join(out, "network.json"), net)
    except Exception as e:
        print("crosscheck not updated: %s" % type(e).__name__, file=sys.stderr)
    try:
        app, note = update_app(state, int(time.time()), previous.get("app"), deadline=deadline)
        if note:
            print(note, file=sys.stderr)
        if app != net.get("app"):
            net["app"] = app
            write_json(os.path.join(out, "network.json"), net)
    except Exception as e:
        print("app version not updated: %s" % type(e).__name__, file=sys.stderr)
    save_state(state_path, state)

    for what in sorted(_dropped - KNOWN_UNPUBLISHED):
        print("not published: %s" % what, file=sys.stderr)
    print("ok: %d cycles, latest certified %s, %s validator accounts, chain %s%s%s%s%s%s" % (
        len(index), network.get("latest_certified_day"), len(vals["accounts"]) if vals else 0,
        "%s at height %s" % (chain.get("state"), (chain.get("head") or {}).get("height")) if chain else "-",
        ", wrote day %d" % wrote_day if wrote_day else "",
        ", history %d days" % len(history["days"]) if history else "",
        ", awards %d days" % len(awards["days"]) if awards else "",
        ", health %d machines" % len(health["machines"]) if health else "",
        ", balances %d accounts" % len(awards["balances"]["accounts"])
        if awards and awards.get("balances") else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
