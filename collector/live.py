#!/usr/bin/env python3
"""The 6529 chain block by block, for the apps' Chain page: live.json, signed, POSTed.

Every 30 s (launchd agent casa.nua.6529-live) this reads the local validator node and sends
one signed envelope to the relay Worker (live-worker/, "monitor6529-live"), which keeps the
latest one and hands it to the apps while their Chain page is on screen. The Worker only
relays: the apps check the signature themselves, with the same pinned keys as every other
file (contract: handoffs/specs/2026-11-16-live-chain-contract.md).

live.json (compact JSON):
    {"generated_unix": <unix s>,
     "chain": {"state", "slot_ms", "epoch_slots", "head": {height, slot, time},
               "finalized": {height, slot, time}},
     "day": <chain day of the newest slot>, "day_blocks": <blocks that day>,
     "keys": [<8-hex key code>, ...],
     "slots": [60 entries, NEWEST FIRST, one per slot, empty slots included:
        {"slot", "time", "height", "final", "maker", "key", "votes", "v"}   a block
        {"slot", "time", "height": null}                                     no block]}
`v` is a bitmap over `keys` (base64; bit i set = keys[i] voted in this block; byte 0 bit 0 =
keys[0]); `votes` is the number of votes the block carries (a key can vote for more than one
slot in one block, so set bits <= votes). `keys` are today's machine codes (as in
validators.json), sorted, then any other code that voted in the window (for example
yesterday's roster just after midnight), sorted, so every vote has a bit.

What it does NOT publish, and why (see the README of live-worker/):
  - nothing from the node that describes this machine (/chain's own_turns, proposes, sync,
    /network's `own` flag, the per-vote weight): only block contents, which every node has;
  - no block until the chain has CONFIRMED it, by one rule that is the same for every
    account and never needs to know which account runs this node: block h is published only
    when the node has a later block (its slot at least CONFIRM_S old) made by a DIFFERENT
    account, and the later blocks made by accounts other than h's maker carry votes cast at
    h's slot or after it from at least VOTE_MIN keys not of h's maker. Later blocks descend
    from h (heights are checked to be consecutive), so this means other accounts built on h
    and included others' votes on h's chain. A block that reached nobody else (for example
    this node's own block that never propagated) is never shown, and when a block appears
    here depends on other nodes, not on how soon this node saw it. The head shown is the
    newest block whose whole 60-slot window is confirmed, and empty slots are shown only up
    to it. Measured on day 20773: every block confirms, median 60 s after its slot starts,
    90th percentile 105 s.
    Why not the review's "votes for h's slot from half of `keys`, in block h+1": at most ~22
    keys vote in any one slot (of ~240), and a vote's `slot` is the slot it was CAST in, not
    the block it is for: about 1 block in 10 never gets a vote carrying its own slot number,
    so that rule would never be met.
  - nothing while the chain is not "live" or the node's head is more than HEAD_MAX_S old
    (a lagging node);
  - no window with a hole: blocks have consecutive heights, so a gap (the node did not
    record a block) is detected and the run publishes nothing, rather than showing a
    missing block as an empty slot.

It writes nothing inside state/data (everything there is published to GitHub) and keeps no
state: the node's API is found with collect.detect_api() on every run, which checks the port
belongs to the validator process. Each run starts with a random 0-10 s pause, so its phase
is not tied to the slot. HTTP goes through one opener with no proxy and no redirects (a 3xx
is a failure). If $LIVE_LOG names its log and it passes 1 MB, the log is cut to its last
64 KB.

Signing: exactly like sign.py (sign.sign_envelope), name "live.json", with the key at
$SIGNING_KEY or signing-key.pem next to this file. The key id must be one of the two
pinned ids, or nothing is sent.

    python3 -I -B live.py                      # sign and POST to $LIVE_URL (or --url)
    python3 -I -B live.py --dry-run OUT.json   # read only: write the unsigned payload, no key, no POST
    (--no-jitter skips the opening pause)

Output: nothing when all is well; one line on stderr when a run publishes nothing.
Python 3.9+, standard library and `openssl`.
"""
import argparse
import base64
import http.client
import json
import os
import random
import sys
import tempfile
import time
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.realpath(__file__))
sys.path.insert(0, HERE)        # python -I leaves the script's folder out of sys.path
import collect                  # noqa: E402  (detect_api, get, clean, the schema helpers)
import sign                     # noqa: E402  (sign_envelope: the one signing function)

NAME = "live.json"
WINDOW = 60                     # slots published
MARGIN = 30                     # slots read before the window, to check its first block
AHEAD = 20                      # slots read after the window (confirmations, unconfirmed blocks)
CONFIRM_S = 30                  # a confirming block's slot must have started this long ago
                                # (30, not 15: past almost every Tor delivery delay; review 16 Nov 2026)
VOTE_MIN = 3                    # keys (not of h's maker) voting at h's slot or later, see the docstring
HEAD_MAX_S = 90                 # the node's head may be at most this old
JITTER_S = 10
LOG_MAX = 1024 * 1024
LOG_KEEP = 64 * 1024
TIMEOUT = 3                     # each node request
POST_TIMEOUT = 8
MAX_ENVELOPE = 64 * 1024        # the Worker refuses more
PINNED_IDS = ("9fc874c0f34c3d51", "04f3b06b89f126a7")   # active, reserve (as the apps pin)
VALIDATORS = os.path.join(HERE, "state", "data", "validators.json")
UA = "6529-live-collector/1"

_BAD = collect._BAD
INT, BOOL, STR, CODE, WALLET = collect.INT, collect.BOOL, collect.STR, collect.CODE, collect.WALLET
B64 = collect.RE(r"^(?:[A-Za-z0-9+/]{4})*(?:[A-Za-z0-9+/]{2}==|[A-Za-z0-9+/]{3}=)?$")
POINT = {"height": INT, "slot": INT, "time": INT}
BLOCK_SLOT = {"slot": INT, "time": INT, "height": INT, "final": BOOL, "maker": WALLET,
              "key": CODE, "votes": INT, "v": B64}
LIVE = {"generated_unix": INT,
        "chain": {"state": collect.RE(r"^live$"), "slot_ms": INT, "epoch_slots": INT, "head": POINT, "finalized": POINT},
        "day": INT, "day_blocks": INT, "keys": collect.LIST(CODE, 4096),
        "slots": collect.LIST(lambda v: v, WINDOW)}


class Skip(Exception):
    """This run publishes nothing (one line on stderr says why)."""


def _int(v):
    return isinstance(v, int) and not isinstance(v, bool) and v >= 0


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise urllib.error.HTTPError(req.full_url, code, "redirect refused", headers, fp)


# One opener for everything: no proxy (whatever the environment says), no redirects.
OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())


def http_get(api, path, timeout=20):
    req = urllib.request.Request(api.rstrip("/") + "/" + path.lstrip("/"),
                                 headers={"Accept": "application/json"})
    with OPENER.open(req, timeout=timeout) as r:
        return json.loads(r.read(4 * 1024 * 1024).decode("utf-8"))


collect.get = http_get          # detect_api() and _is_validator() use it too, in this process


def node(api, path):
    return collect.get(api, path, timeout=TIMEOUT)


def find_api():
    """The node's API, found on every run (detect_api checks the port is the validator's)."""
    return collect.detect_api()


def roster_codes(api, path=VALIDATORS):
    """Today's machine codes: from the collector's own validators.json, else from /network
    through the collector's schema (which drops the node's `own` flag)."""
    vals = None
    try:
        with open(path) as f:
            vals = json.load(f)
    except (OSError, ValueError):
        vals = None
    if not isinstance(vals, dict) or not isinstance(vals.get("accounts"), list):
        vals = collect.clean_validators(collect.get(api, "network", timeout=TIMEOUT))
    if not vals:
        raise Skip("no validator list")
    codes = set()
    for a in vals["accounts"]:
        for m in (a.get("machines") or []) if isinstance(a, dict) else []:
            if isinstance(m, dict) and CODE(m.get("code")) is not _BAD:
                codes.add(m["code"])
    if not codes:
        raise Skip("empty validator list")
    return sorted(codes)


def day_rows(api, day, after_slot, slot_ms, start):
    """Rows of one chain day with slot > after_slot (None: from the day's start), oldest first,
    and the day's block count from the same answer."""
    rows, a, blocks = [], after_slot, None
    for _ in range(4):                 # one page is normal (240 rows > the 90 slots read)
        try:
            page = node(api, "awards/network/day/%d/blocks?limit=240%s" % (
                day, "" if a is None else "&after_slot=%d" % a))
        except urllib.error.HTTPError as e:
            if e.code == 404:          # a day the chain does not have
                return [], None
            raise
        if not isinstance(page, dict) or page.get("day") != day or not isinstance(page.get("rows"), list):
            raise Skip("unexpected blocks page")
        if blocks is None and _int(page.get("blocks")):
            blocks = page["blocks"]
        for r in page["rows"]:
            if not isinstance(r, dict) or not _int(r.get("slot")) or not _int(r.get("height")) \
                    or r.get("time") != start + r["slot"] * slot_ms or not isinstance(r.get("final"), bool) \
                    or not isinstance(r.get("proposer"), dict) or not isinstance(r.get("votes"), list):
                raise Skip("unexpected block row")
            rows.append(r)
        nxt = page.get("next_after_slot")
        if nxt is None or not page["rows"]:
            return rows, blocks
        if not _int(nxt) or (a is not None and nxt <= a):
            raise Skip("bad paging")
        a = nxt
    raise Skip("too many pages")


def bitmap(indexes, n):
    b = bytearray((n + 7) // 8)
    for i in indexes:
        b[i // 8] |= 1 << (i % 8)
    return base64.b64encode(bytes(b)).decode("ascii")


def build(api, now_ms, validators=VALIDATORS):
    chain = node(api, "chain")
    if not isinstance(chain, dict):
        raise Skip("no /chain")
    start, slot_ms, epoch = chain.get("start_time"), chain.get("slot_ms"), chain.get("epoch_slots")
    head, fin = chain.get("head"), chain.get("finalized")
    if not (_int(start) and _int(slot_ms) and slot_ms > 0 and _int(epoch) and isinstance(head, dict)
            and isinstance(fin, dict) and _int(head.get("slot")) and _int(head.get("height"))):
        raise Skip("chain not ready")
    if chain.get("state") != "live":
        raise Skip("chain not live")
    if not _int(head.get("time")) or now_ms - head["time"] > HEAD_MAX_S * 1000:
        raise Skip("the node's head is old")
    first = head["slot"] - (WINDOW + MARGIN + AHEAD) + 1     # read from here up to the head
    if first < 0:
        raise Skip("chain too young")
    day_ms = 86400000
    day_of = lambda s: (start + s * slot_ms) // day_ms
    day_start = lambda d: max(0, -(-(d * day_ms - start) // slot_ms))   # first slot of day d

    def after(d, slot):                # `after_slot` for day d, or None for the day's start
        return slot if slot >= day_start(d) else None
    rows, blocks_of = [], {}
    for d in range(day_of(first), day_of(head["slot"]) + 1):
        r, n = day_rows(api, d, after(d, first - 1), slot_ms, start)
        rows += r
        blocks_of[d] = n
    rows = sorted((r for r in rows if first <= r["slot"] <= head["slot"]), key=lambda r: r["slot"])
    # Complete or nothing: heights consecutive up to /chain's head (rows newer than the
    # /chain answer, read just before, are left for the next run).
    if not rows or rows[-1]["slot"] != head["slot"] or rows[-1]["height"] != head["height"]:
        raise Skip("blocks do not end at the head")
    for x, y in zip(rows, rows[1:]):
        if y["height"] != x["height"] + 1 or y["slot"] <= x["slot"]:
            raise Skip("a block is missing")
    acct = lambda x: str((x.get("account") if isinstance(x, dict) else None) or "").lower()
    for r in rows:
        if WALLET(r["proposer"].get("account")) is _BAD:
            raise Skip("a block without a maker")

    # Confirmation (the same rule for every block and every account; see the docstring).
    cutoff = now_ms - CONFIRM_S * 1000
    evidence = [r for r in rows if r["time"] <= cutoff]

    def confirmed(i):
        h = evidence[i]
        maker = acct(h["proposer"])
        later = [x for x in evidence[i + 1:] if acct(x["proposer"]) != maker]
        if not later:
            return False
        voters = {v.get("key_code") for x in later for v in x["votes"]
                  if isinstance(v, dict) and _int(v.get("slot")) and v["slot"] >= h["slot"]
                  and acct(v) != maker and v.get("key_code")}
        return len(voters) >= VOTE_MIN
    ok = [confirmed(i) for i in range(len(evidence))]
    # The head shown: the newest block such that every block of its 60-slot window is confirmed.
    top_row = None
    for i in range(len(evidence) - 1, -1, -1):
        if ok[i] and all(ok[j] for j in range(len(evidence))
                         if evidence[i]["slot"] - WINDOW < evidence[j]["slot"] <= evidence[i]["slot"]):
            top_row = evidence[i]
            break
    if top_row is None:
        raise Skip("no confirmed block")
    top = top_row["slot"]
    lo = top - WINDOW + 1
    if lo - MARGIN < first:
        raise Skip("no confirmed block recently")
    if rows[0]["slot"] >= lo and first > 0:
        # No block in the margin to check the window's first block against (the chain was
        # silent 15 minutes): look up to 240 slots further back, on that day, once.
        d = day_of(first - 1)
        prev, _ = day_rows(api, d, after(d, first - 241), slot_ms, start)
        prev = [r for r in prev if r["slot"] < first]
        if prev and max(prev, key=lambda r: r["slot"])["height"] != rows[0]["height"] - 1:
            raise Skip("a block is missing before the window")
    shown = [r for r in rows if lo <= r["slot"] <= top]
    later = [r for r in rows if r["slot"] > top]        # not confirmed yet: next runs
    if not (_int(fin.get("slot")) and fin["slot"] <= top):
        raise Skip("finalized after the shown head")

    keys = roster_codes(api, validators)
    seen = set(keys)
    extra = sorted({v.get("key_code") for r in shown for v in r["votes"]
                    if isinstance(v, dict) and CODE(v.get("key_code")) is not _BAD} - seen)
    keys += extra
    index = {k: i for i, k in enumerate(keys)}
    by_slot = {r["slot"]: r for r in shown}
    slots = []
    for s in range(top, lo - 1, -1):
        t = start + s * slot_ms
        r = by_slot.get(s)
        if r is None:
            slots.append({"slot": s, "time": t, "height": None})
            continue
        p = r["proposer"]
        voters = []
        for v in r["votes"]:
            c = v.get("key_code") if isinstance(v, dict) else None
            if c not in index:
                raise Skip("a vote without a key code")
            voters.append(index[c])
        slots.append({"slot": s, "time": t, "height": r["height"], "final": r["final"],
                      "maker": p.get("account"), "key": p.get("key_code"),
                      "votes": len(r["votes"]), "v": bitmap(voters, len(keys))})
    top_day = day_of(top)
    day_blocks = blocks_of.get(top_day)
    if not _int(day_blocks):
        raise Skip("no day block count")
    # The day's count as of the same answer, minus the blocks not confirmed yet.
    day_blocks -= sum(1 for r in later if day_of(r["slot"]) == top_day)
    point = lambda b: {"height": b.get("height"), "slot": b.get("slot"), "time": b.get("time")}
    payload = {
        "generated_unix": now_ms // 1000,
        "chain": {"state": "live", "slot_ms": slot_ms, "epoch_slots": epoch,
                  "head": point(top_row), "finalized": point(fin)},
        "day": top_day, "day_blocks": max(0, day_blocks), "keys": keys, "slots": slots,
    }
    check(payload)
    return payload


def check(p):
    """The payload must be exactly what the schema allows, field for field."""
    c = collect.clean(p, LIVE, "live")
    if c is _BAD or c != {k: p[k] for k in LIVE}:
        raise Skip("payload outside the schema")
    if set(p) != set(LIVE) or set(p["chain"]) != set(LIVE["chain"]):
        raise Skip("payload outside the schema")
    if len(p["slots"]) != WINDOW or len(set(p["keys"])) != len(p["keys"]):
        raise Skip("payload outside the schema")
    nbytes = (len(p["keys"]) + 7) // 8
    for i, s in enumerate(p["slots"]):
        if not isinstance(s, dict) or s.get("slot") != p["slots"][0]["slot"] - i:
            raise Skip("slots not newest first")
        if s.get("height") is None:          # an empty slot: exactly slot, time, height null
            ok = set(s) == {"slot", "time", "height"} and INT(s["slot"]) is not _BAD \
                and INT(s["time"]) is not _BAD
        else:
            cs = collect.clean(s, BLOCK_SLOT, "live.slot")
            ok = cs is not _BAD and cs == s and set(s) == set(BLOCK_SLOT) \
                and len(base64.b64decode(s["v"])) == nbytes \
                and bin(int.from_bytes(base64.b64decode(s["v"]), "little")).count("1") <= s["votes"]
        if not ok:
            raise Skip("bad slot %s" % s.get("slot"))


def compact(p):
    return json.dumps(p, separators=(",", ":"), ensure_ascii=True).encode("utf-8")


def envelope(raw, key):
    key_id, pub = sign.public_key(key)
    if key_id not in PINNED_IDS:
        raise Skip("signing key %s is not one of the pinned keys" % key_id)
    fd, pub_pem = tempfile.mkstemp(suffix=".pem")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(pub)
        return sign.sign_envelope(key, key_id, pub_pem, NAME, raw)
    finally:
        os.unlink(pub_pem)


def post(url, text):
    body = text.encode("utf-8")
    if len(body) > MAX_ENVELOPE:
        raise Skip("envelope too large (%d bytes)" % len(body))
    req = urllib.request.Request(url, data=body, method="POST",
                                 headers={"Content-Type": "application/json", "User-Agent": UA})
    try:
        with OPENER.open(req, timeout=POST_TIMEOUT) as r:
            r.read(1024)
    except urllib.error.HTTPError as e:
        raise Skip("worker answered %d" % e.code)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--api", default=None, help="node API base (default: last known, else detect)")
    ap.add_argument("--url", default=os.environ.get("LIVE_URL"), help="Worker URL (default $LIVE_URL)")
    ap.add_argument("--key", default=os.environ.get("SIGNING_KEY") or os.path.join(HERE, "signing-key.pem"))
    ap.add_argument("--validators", default=VALIDATORS)
    ap.add_argument("--dry-run", metavar="OUT", help="write the unsigned payload to OUT; no key, no POST")
    ap.add_argument("--envelope-out", metavar="OUT", help="also write the signed envelope to OUT (tests)")
    ap.add_argument("--no-post", action="store_true", help="sign but do not POST")
    ap.add_argument("--no-jitter", action="store_true", help="skip the random 0-10 s opening pause")
    a = ap.parse_args()
    cap_log(os.environ.get("LIVE_LOG"))
    tmp = os.environ.get("TMPDIR")
    if tmp and not os.path.isdir(tmp):
        os.makedirs(tmp, mode=0o700, exist_ok=True)
    if not a.dry_run and (os.path.realpath(a.key).startswith(os.path.realpath(os.path.join(HERE, "state")) + os.sep)):
        raise Skip("the key must not sit inside state/")
    for d in (HERE,) + tuple(_parents(HERE)):
        if os.path.exists(os.path.join(d, ".git")):
            raise Skip("refusing to run from inside a git work tree (%s)" % HERE)
    if not a.no_jitter:
        time.sleep(random.SystemRandom().uniform(0, JITTER_S))
    api = a.api or find_api()
    if not api:
        raise Skip("no validator API found")
    raw = compact(build(api, int(time.time() * 1000), a.validators))
    if a.dry_run:
        _write(a.dry_run, raw)
        return 0
    if not os.path.isfile(a.key):
        raise Skip("no signing key")
    text = envelope(raw, a.key)
    if a.envelope_out:
        _write(a.envelope_out, text.encode("utf-8"))
    if a.no_post:
        return 0
    if not a.url or not a.url.startswith("https://"):
        raise Skip("no https Worker URL (LIVE_URL)")
    post(a.url, text)
    return 0


def cap_log(path):
    """Cut the log to its last LOG_KEEP bytes once it passes LOG_MAX (launchd appends to it)."""
    try:
        if not path or os.path.getsize(path) <= LOG_MAX:
            return
        with open(path, "r+b") as f:
            f.seek(-LOG_KEEP, os.SEEK_END)
            tail = f.read()
            tail = tail[tail.find(b"\n") + 1:]
            f.seek(0)
            f.truncate()
            f.write(tail)
    except OSError:
        pass


def _parents(p):
    while True:
        q = os.path.dirname(p)
        if q == p:
            return
        yield q
        p = q


def _write(path, data):
    path = os.path.abspath(path)
    if os.path.realpath(path).startswith(os.path.realpath(os.path.join(HERE, "state", "data")) + os.sep):
        raise Skip("refusing to write inside state/data (it is published)")
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), suffix=".tmp")
    with os.fdopen(fd, "wb") as f:
        f.write(data)
    os.replace(tmp, path)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Skip as e:
        print("live: %s" % e, file=sys.stderr)
    except (urllib.error.URLError, http.client.HTTPException, OSError, ValueError, sign.Refuse) as e:
        print("live: %s" % (type(e).__name__ if not isinstance(e, sign.Refuse) else e), file=sys.stderr)
    except Exception as e:             # never a traceback in the log
        print("live: unexpected %s" % type(e).__name__, file=sys.stderr)
    sys.exit(1)
