#!/usr/bin/env python3
"""Offline tests of live.py against a fake node (python3 -I -B test_live.py)."""
import base64
import json
import os
import sys
import tempfile
import urllib.error

HERE = os.path.dirname(os.path.realpath(__file__))
sys.path.insert(0, HERE)
import collect  # noqa: E402
import live     # noqa: E402

START, SLOT_MS, PER_DAY = 1794571200000, 30000, 2880
DAY0 = START // 86400000                    # 20770 (the chain starts at 12:00 UTC, like the real one)
DAY_OF = lambda s: (START + s * SLOT_MS) // 86400000
DAY_START = lambda d: max(0, -(-(d * 86400000 - START) // SLOT_MS))
ROSTER = ["%08x" % (0x10000000 + i) for i in range(10)]
OUTSIDER = "ffffffff"                      # votes but is not in today's list (yesterday's key)
W = lambda i: "0x" + ("%040x" % (i + 1))


def make_chain(head_slot, holes=(), skip_every=4):
    """Blocks at every slot not divisible by skip_every; `holes` = blocks the node lacks.
    Block s is made by ROSTER[s % 10] (account W(s % 10)) and carries votes for the previous
    block's slot from 4 keys of 4 other accounts."""
    blocks, h, prev = {}, 100, None
    for s in range(0, head_slot + 1):
        if s % skip_every == 0:
            continue
        h += 1
        votes = []
        if prev is not None:
            votes = [{"key_code": ROSTER[(prev + j) % 10], "account": W((prev + j) % 10), "key_id": "x",
                      "slot": prev, "w": "1"} for j in range(1, 5)]
        if s % 7 == 0:
            votes.append({"key_code": OUTSIDER, "account": W(99), "key_id": "x", "slot": s - 1, "w": "1"})
        if s % 11 == 0 and votes:          # one key voting for two slots in this block
            votes.append(dict(votes[0], slot=votes[0]["slot"] - 1))
        if s not in holes:
            blocks[s] = {"slot": s, "time": START + s * SLOT_MS, "height": h, "root": "0x00",
                         "final": s < head_slot - 20,
                         "proposer": {"key_code": ROSTER[s % 10], "account": W(s % 10), "key_id": "x"},
                         "votes": votes}
        prev = s
    return blocks, h


class FakeNode(object):
    def __init__(self, head_slot, holes=(), state="live"):
        self.blocks, self.height = make_chain(head_slot, holes)
        self.head = max(self.blocks)
        self.calls = []
        self.state = state

    def get(self, api, path, timeout=20):
        self.calls.append(path)
        if path == "chain":
            hb = self.blocks[self.head]
            return {"state": self.state, "slot_ms": SLOT_MS, "epoch_slots": 8, "start_time": START, "start_day": DAY0,
                    "head": {"height": hb["height"], "slot": self.head, "time": hb["time"], "root": "0x01"},
                    "finalized": {"height": hb["height"] - 20, "slot": self.head - 27, "time": hb["time"] - 810000,
                                  "root": "0x02", "epoch": 1},
                    "own_turns": [1, 2], "proposes": True}
        if path.startswith("awards/network/day/"):
            d = int(path.split("/")[3])
            after = int(path.split("after_slot=")[1]) if "after_slot=" in path else None
            if d < DAY0:
                raise urllib.error.HTTPError(path, 404, "nf", {}, None)
            day = sorted(s for s in self.blocks if DAY_OF(s) == d)
            rows = [self.blocks[s] for s in day if after is None or s > after][:240]
            more = rows and rows[-1]["slot"] < (day[-1] if day else -1)
            return {"day": d, "slots": PER_DAY, "blocks": len(day), "recorded": "all", "rows": rows,
                    "next_after_slot": rows[-1]["slot"] if more else None}
        raise AssertionError(path)


def run(node, now_ms, vals):
    collect.get = node.get
    return live.build("http://127.0.0.1:1/api/v1", now_ms, vals)


def main():
    tmp = tempfile.mkdtemp()
    vals = os.path.join(tmp, "validators.json")
    with open(vals, "w") as f:
        json.dump({"cycle_day": DAY0 + 3, "accounts": [{"wallet": W(i), "machines": [{"code": c}]}
                                                       for i, c in enumerate(ROSTER)]}, f)
    ok = []

    # 1. a window across midnight (day DAY0+3 starts at slot 7200), head 40 s old
    #    (older than CONFIRM_S, so the head confirms the block before it)
    M = DAY_START(DAY0 + 3)
    assert M == 7200
    head = M + 10
    node = FakeNode(head)
    now = START + head * SLOT_MS + 40000
    p = run(node, now, vals)
    raw = live.compact(p)
    top = head - 1                             # the head itself has no confirming block yet
    assert p["slots"][0]["slot"] == top and p["slots"][-1]["slot"] == top - 59
    assert [s["slot"] for s in p["slots"]] == list(range(top, top - 60, -1))
    days = sorted({s["time"] // 86400000 for s in p["slots"]})
    assert days == [DAY0 + 2, DAY0 + 3], days
    blocks_routes = [c for c in node.calls if "/blocks" in c]
    assert len(blocks_routes) == 2 and "after_slot" in blocks_routes[0] and "after_slot" not in blocks_routes[1], blocks_routes
    ok.append("window across midnight: previous day's blocks from the previous day's route (%d requests)" % len(node.calls))
    for s in p["slots"]:
        if s["slot"] % 4 == 0:
            assert s == {"slot": s["slot"], "time": START + s["slot"] * SLOT_MS, "height": None}
        else:
            b = node.blocks[s["slot"]]
            assert s["height"] == b["height"] and s["maker"] == b["proposer"]["account"] and s["key"] == b["proposer"]["key_code"]
            assert s["votes"] == len(b["votes"])
            bits = int.from_bytes(base64.b64decode(s["v"]), "little")
            got = {p["keys"][i] for i in range(len(p["keys"])) if bits >> i & 1}
            assert got == {v["key_code"] for v in b["votes"]}, s["slot"]
    ok.append("empty slots are {slot,time,height:null}; every bitmap decodes to the block's voters")
    assert p["keys"] == sorted(ROSTER) + [OUTSIDER]
    ok.append("keys = today's codes sorted, then a voting code outside the list")
    assert p["day"] == DAY0 + 3 and p["day_blocks"] == len([s for s in node.blocks if M <= s <= top])
    assert p["chain"]["head"] == {"height": node.blocks[top]["height"], "slot": top, "time": START + top * SLOT_MS}
    assert p["chain"]["state"] == "live" and set(p["chain"]["finalized"]) == {"height", "slot", "time"}
    assert "own_turns" not in raw.decode() and "proposes" not in raw.decode() and '"root"' not in raw.decode()
    assert '"w"' not in raw.decode() and "key_id" not in raw.decode()
    ok.append("day/day_blocks of the shown head (unconfirmed blocks not counted); no root; nothing node-specific")

    # 2. confirmation waits until the confirming block's slot is CONFIRM_S old
    h2 = M + 9                                 # block; its child is M + 10
    n2 = FakeNode(M + 10)
    t_child = START + (M + 10) * SLOT_MS
    assert run(n2, t_child + 25000, vals)["slots"][0]["slot"] < h2
    assert run(n2, t_child + 31000, vals)["slots"][0]["slot"] == h2
    ok.append("block h appears only once a confirming block is %d s old" % live.CONFIRM_S)

    # 3. an orphaned own-style block: the newest block, followed only by a block of the SAME
    #    account (that account building on its own block nobody else had), is never shown
    n3 = FakeNode(M + 10)
    n3.blocks[M + 10]["proposer"] = dict(n3.blocks[M + 9]["proposer"])
    n3.blocks[M + 10]["votes"] = [dict(v, account=n3.blocks[M + 9]["proposer"]["account"]) for v in n3.blocks[M + 10]["votes"]]
    for dt in (31000, 40000, 85000):
        q = run(n3, t_child + dt, vals)
        assert q["slots"][0]["slot"] < h2 and all(x["slot"] < h2 for x in q["slots"]), dt
    n3b = FakeNode(M + 9)                      # the newest block with nothing after it at all
    for dt in (20000, 60000, 85000):
        assert run(n3b, START + (M + 9) * SLOT_MS + dt, vals)["slots"][0]["slot"] < h2
    ok.append("a block no other account built on is never published (checked up to 85 s)")

    # 4. a late-propagated block: its child carries no votes for it, the next block does
    n4 = FakeNode(M + 11)
    n4.blocks[M + 10]["votes"] = []
    n4.blocks[M + 11]["votes"] = [dict(v, slot=M + 9) for v in n4.blocks[M + 11]["votes"]]
    t11 = START + (M + 11) * SLOT_MS
    assert run(n4, t11 + 25000, vals)["slots"][0]["slot"] < h2
    assert run(n4, t11 + 31000, vals)["slots"][0]["slot"] >= h2
    ok.append("a block whose votes arrive late is published once they are in a block %d s old" % live.CONFIRM_S)

    # 4b. later blocks by others that carry only votes cast BEFORE h's slot do not confirm h
    n5 = FakeNode(M + 11)
    for x in (M + 10, M + 11):
        n5.blocks[x]["votes"] = [dict(v, slot=M + 7) for v in n5.blocks[x]["votes"]]
    assert run(n5, t11 + 40000, vals)["slots"][0]["slot"] < h2
    ok.append("votes cast before h's slot do not confirm h")

    # 5. a node that is not live, or whose head is old, publishes nothing
    for nd, t in ((FakeNode(head, state="catching up"), now), (FakeNode(head), START + head * SLOT_MS + 91000)):
        try:
            run(nd, t, vals)
            raise AssertionError("published")
        except live.Skip:
            pass
    ok.append("not live, or head older than %d s: nothing published" % live.HEAD_MAX_S)

    # 4. a block the node lacks, in the window or in the margin before it: nothing published
    for hole in (head - 5, head - 58, head - 75):
        if hole % 4 == 0:
            hole += 1
        try:
            run(FakeNode(head, holes=(hole,)), now, vals)
            raise AssertionError("published with a hole at %d" % hole)
        except live.Skip:
            pass
    ok.append("a missing block (window or margin) publishes nothing")

    # 5. schema: anything extra or malformed is refused
    for bad in (lambda q: q.update(extra=1), lambda q: q["slots"][0].update(note="x"),
                lambda q: q["slots"][1].update(final=True) if q["slots"][1]["height"] is None else q["slots"][1].update(v="!!"),
                lambda q: q["chain"].update(root="0x1"), lambda q: q["slots"].reverse(),
                lambda q: q["chain"].update(state="syncing"),
                lambda q: q["slots"][0].update(maker="0xnothex"), lambda q: q["slots"].pop()):
        q = json.loads(raw)
        bad(q)
        try:
            live.check(q)
            raise AssertionError("schema accepted a bad payload")
        except live.Skip:
            pass
    live.check(json.loads(raw))
    ok.append("strict schema refuses extra fields, bad forms, wrong order, wrong length")
    assert len(raw) < 64 * 1024

    # 6. the opener: no proxy whatever the environment says, and a 3xx is a failure
    import http.server
    import threading

    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path.endswith("/redir"):
                self.send_response(302)
                self.send_header("Location", "http://127.0.0.1:%d/api/v1/ok" % srv.server_port)
                self.end_headers()
                return
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b'{"ok":1}')

        def log_message(self, *a):
            pass
    srv = http.server.HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    os.environ["http_proxy"] = os.environ["HTTP_PROXY"] = "http://127.0.0.1:9"
    os.environ.pop("no_proxy", None)
    os.environ.pop("NO_PROXY", None)
    base = "http://127.0.0.1:%d/api/v1" % srv.server_port
    assert live.http_get(base, "ok", timeout=3) == {"ok": 1}
    try:
        live.http_get(base, "redir", timeout=3)
        raise AssertionError("redirect followed")
    except urllib.error.HTTPError as e:
        assert e.code == 302
    srv.shutdown()
    ok.append("HTTP: proxy settings ignored, redirects refused")

    # 7. the log cap
    logp = os.path.join(tmp, "live.log")
    with open(logp, "wb") as f:
        f.write(b"old line\n" * 130000)        # ~1.2 MB
    live.cap_log(logp)
    assert os.path.getsize(logp) <= live.LOG_KEEP and open(logp, "rb").read().startswith(b"old line")
    live.cap_log(logp)
    ok.append("log cut to its last 64 KB above 1 MB")
    print("\n".join("ok  " + x for x in ok))
    print("\n%d checks passed" % len(ok))


if __name__ == "__main__":
    main()
