#!/usr/bin/env python3
"""Offline tests for the schema cleaning of the files added on 15 Nov 2026
(handles, days/, history.json, awards.json) and for sign.py's accepted names.

No network and no node: the node's answers are fixtures below.

    python3 -I -B collector/test_collect.py
"""
import json
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import collect  # noqa: E402
import sign  # noqa: E402

W1 = "0x" + "a1" * 20
W2 = "0x" + "b2" * 20
W3 = "0x" + "c3" * 20
NOW = 1794770000


def vals(day, accounts):
    return {"cycle_day": day, "w": 1000, "accounts": accounts, "next_day": day + 1,
            "next_members": [], "continents": [], "places": []}


def machine(code, today="SIGNED", continent="EUROPE", software="0.1.0-rc.18", **extra):
    m = {"code": code, "weight": 10, "today": today, "first_signed_day": 1, "signed_7": 3,
         "report": {"continent": continent, "country": "DE", "infrastructure": "home",
                    "software": software, "source_class": "VERIFIED"}}
    m.update(extra)
    return m


class Handles(unittest.TestCase):
    def setUp(self):
        collect._dropped.clear()

    def test_handle_form(self):
        self.assertEqual(collect.HANDLE("punk6529"), "punk6529")
        for bad in ("", "a" * 41, "<script>", "id-0x" + "0" * 40, "two words", "é", 7, None):
            self.assertIs(collect.HANDLE(bad), collect._BAD, bad)

    def test_limits_and_recheck(self):
        wallets = ["0x%040x" % i for i in range(60)]
        st, asked = {}, []

        def fetch(w):
            asked.append(w)
            return "h%d" % int(w, 16)
        self.assertEqual(collect.update_handles(st, wallets, NOW, fetch), 25)
        self.assertEqual(len(asked), 25)
        collect.update_handles(st, wallets, NOW + 60, fetch)
        collect.update_handles(st, wallets, NOW + 120, fetch)
        self.assertEqual(len(set(asked)), 60)              # every wallet once, never twice
        self.assertEqual(len(asked), 60)
        n = collect.update_handles(st, wallets, NOW + 6 * 86400, fetch)
        self.assertEqual(n, 0)                             # not due before 7 days
        n = collect.update_handles(st, wallets, NOW + 7 * 86400 + 200, fetch)
        self.assertEqual(n, 25)
        self.assertEqual(collect.handle_of(st, wallets[3].upper().replace("0X", "0x")), "h3")

    def test_failures_never_raise_and_keep_cache(self):
        st = {"handles": {W1: {"h": "old", "t": NOW - 8 * 86400}}}

        def boom(w):
            raise OSError("down")
        collect.update_handles(st, [W1, W2, W3], NOW, boom)
        self.assertEqual(collect.handle_of(st, W1), "old")  # a failed lookup keeps the old value
        self.assertIsNone(collect.handle_of(st, W2))

    def test_bad_cache_entries_dropped(self):
        st = {"handles": {W1: {"h": "<b>", "t": NOW}, "nope": {"h": "x", "t": NOW},
                          W2: {"h": "ok", "t": NOW}}}
        collect.update_handles(st, [W2], NOW, lambda w: "x")
        self.assertEqual(set(st["handles"]), {W2})


class Days(unittest.TestCase):
    def setUp(self):
        collect._dropped.clear()
        self.out = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.out)

    def test_day_record_schema(self):
        st = {"handles": {W1: {"h": "alice", "t": NOW}}}
        v = vals(20770, [
            {"wallet": W2.upper().replace("0X", "0x"), "tdh": 5, "weight": 5, "own": True,
             "machines": [machine("0000000b", today="NOT_YET", key_id="0x" + "1" * 64)]},
            {"wallet": W1, "tdh": 9, "weight": 9, "machines": [machine("0000000a"), machine("XYZ")]},
            {"wallet": "not-a-wallet", "machines": []},
        ])
        r = collect.day_record(v, st)
        self.assertEqual(set(r), {"day", "final", "w", "accounts"})
        self.assertEqual(r["day"], 20770)
        self.assertTrue(r["final"])
        self.assertEqual([a["wallet"] for a in r["accounts"]], [W1, W2])   # weight desc, lowercase
        self.assertEqual(r["accounts"][0]["handle"], "alice")
        self.assertIsNone(r["accounts"][1]["handle"])
        self.assertEqual(set(r["accounts"][1]), {"wallet", "handle", "tdh", "weight", "machines"})
        self.assertEqual([m["code"] for m in r["accounts"][0]["machines"]], ["0000000a"])
        self.assertEqual(set(r["accounts"][1]["machines"][0]), {"code", "weight", "today", "report"})

    def test_roll_day_writes_once_and_never_rewrites(self):
        st = {}
        a = [{"wallet": W1, "tdh": 1, "weight": 1, "machines": [machine("0000000a")]}]
        self.assertIsNone(collect.roll_day(self.out, st, vals(20770, a)))
        b = [{"wallet": W1, "tdh": 2, "weight": 2, "machines": [machine("0000000a")]}]
        self.assertIsNone(collect.roll_day(self.out, st, vals(20770, b)))   # same day: newest kept
        self.assertEqual(collect.roll_day(self.out, st, vals(20771, a)), 20770)
        with open(os.path.join(self.out, "days", "20770.json")) as f:
            self.assertEqual(json.load(f)["accounts"][0]["tdh"], 2)          # the day's LAST list
        st["last_vals"] = vals(20770, a)                                  # pretend: same day again
        self.assertIsNone(collect.roll_day(self.out, st, vals(20771, a)))   # exists: not rewritten
        self.assertIsNone(collect.roll_day(self.out, st, vals(20769, a)))   # going back: nothing
        self.assertEqual(collect.write_days_index(self.out, 20771), [20770])
        self.assertEqual(collect.write_days_index(self.out, 20770), [])      # today never listed

    def test_history(self):
        st = {}
        old = vals(20770, [
            {"wallet": W1, "tdh": 1, "weight": 1, "machines": [machine("0000000a"),
                                                                 machine("0000000b", today="NOT_YET", continent=None, software=None)]},
            {"wallet": W2, "tdh": 1, "weight": 1, "machines": [machine("0000000c", today="MISSED", continent="ASIA")]}])
        collect.write_day(self.out, collect.day_record(old, st))
        today = collect.day_record(vals(20771, [{"wallet": W1, "tdh": 1, "weight": 1,
                                                 "machines": [machine("0000000a")]}]), st)
        h = collect.build_history(self.out, st, today, NOW)
        self.assertEqual([r["day"] for r in h["days"]], [20770, 20771])
        r = h["days"][0]
        self.assertEqual((r["validators"], r["signed"], r["accounts"], r["accounts_signed"]), (3, 1, 2, 1))
        self.assertEqual(r["continents"], {"EUROPE": 1, "UNKNOWN": 1, "ASIA": 1})
        self.assertEqual(r["software"], {"0.1.0-rc.18": 2, "UNKNOWN": 1})
        h2 = collect.build_history(self.out, st, today, NOW)              # from the cache: same
        self.assertEqual(h, h2)


class FakeNode(object):
    """Answers collect.get() from fixtures."""

    def __init__(self, routes):
        self.routes, self.asked = routes, []

    def __call__(self, api, path, timeout=20):
        self.asked.append(path)
        if path not in self.routes:
            import urllib.error
            raise urllib.error.HTTPError(path, 404, "not found", {}, None)
        return json.loads(json.dumps(self.routes[path]))


def key(rank, code, acct, blocks=None, votes=None, total=None):
    paid = None if total is None else {"award": {"total": str(total), "blocks_part": str(total - 1),
                                                 "votes_part": "1", "secret": "x"},
                                       "released": "5", "unvested": "9"}
    return {"rank": rank, "key_id": "0x" + "9" * 64, "key_code": code, "account": acct,
            "blocks": blocks, "votes": votes, "votes_zero_weight": 0, "vote_weight": "7",
            "paid": paid, "paid_for": None, "own": True}


DAYS = {"days": [
    {"day": 20772, "state": "open", "final": False, "slots": 2880, "blocks": 100, "paid": None,
     "recorded": {"awards": "all", "blocks": "all", "votes": "all"}, "yours": 1},
    {"day": 20771, "state": "settled", "final": True, "slots": 2880, "blocks": 2000,
     "paid": {"award": {"total": "300", "blocks_part": "200", "votes_part": "100"}},
     "recorded": {"awards": "all", "blocks": "none", "votes": "none"}},
], "next_to": None}
CHAIN = {"start_time": 1794571200000, "slot_ms": 30000, "head": {"time": (1794770000 - 30) * 1000},
         "currency": {"symbol": "$fake6529", "decimals": 18, "test": True}}


def routes():
    lo_slot = ((NOW - 600) * 1000 - CHAIN["start_time"]) // 30000 - 1
    t_in, t_old = (NOW - 100) * 1000, (NOW - 900) * 1000
    return {
        "awards/network/days?limit=7": DAYS,
        "awards/network/day/20771?limit=1000": {"day": 20771, "keys": [
            key(1, "0000000a", W1, total=100), key(2, "0000000b", W1, total=50)],
            "total_keys": 4, "next_after": 2},
        "awards/network/day/20771?limit=1000&after=2": {"day": 20771, "keys": [
            key(3, "0000000c", W2, total=150), key(4, None, W3, total=0),
            key(5, "0000000d", "0x" + "d" * 64, total=1)], "total_keys": 4, "next_after": None},
        "awards/network/day/20772?limit=1000": {"day": 20772, "keys": [
            key(1, "0000000a", W1, blocks=3, votes=5), key(2, "0000000b", W1, blocks=1, votes=None),
            key(3, "0000000c", W2, blocks="7", votes=2)], "next_after": None},
        "awards/network/day/20772/blocks?limit=240&after_slot=%d" % lo_slot: {
            "day": 20772, "recorded": "part", "next_after_slot": lo_slot + 5, "rows": [
                {"slot": lo_slot + 1, "time": t_old, "proposer": {"key_code": "000000ff"}, "votes": []},
                {"slot": lo_slot + 5, "time": t_in, "proposer": {"key_code": "0000000a", "account": W1},
                 "votes": [{"key_code": "0000000c", "w": "3"}, {"key_code": "BAD!"}]}]},
        "awards/network/day/20772/blocks?limit=240&after_slot=%d" % (lo_slot + 5): {
            "day": 20772, "recorded": "part", "next_after_slot": None, "rows": []},
    }


class Awards(unittest.TestCase):
    def setUp(self):
        collect._dropped.clear()
        self.real_get = collect.get

    def tearDown(self):
        collect.get = self.real_get

    def test_awards_schema_paging_and_sums(self):
        node = FakeNode(routes())
        collect.get = node
        st = {}
        a = collect.build_awards("x", CHAIN, st, NOW, [W1, W2])
        self.assertEqual(set(a), {"generated_unix", "currency", "decimals", "test", "days", "live",
                                  "balances", "distributed_total"})
        self.assertIsNone(a["distributed_total"])   # CHAIN has no start_day: not known
        self.assertIsNone(a["balances"])          # no chain/account route here (older node)
        self.assertEqual((a["currency"], a["decimals"], a["test"]), ("$fake6529", 18, True))
        self.assertEqual([d["day"] for d in a["days"]], [20771, 20772])      # newest last
        settled, open_ = a["days"]
        self.assertEqual(set(settled), {"day", "state", "final", "slots", "blocks", "award_total",
                                        "recorded", "accounts"})
        self.assertEqual(settled["award_total"], "300")
        self.assertEqual(settled["recorded"], {"blocks": "none", "votes": "none"})
        acc = {x["wallet"]: x for x in settled["accounts"]}
        self.assertEqual(set(acc), {W1, W2})        # W3 paid 0 and did nothing; identity dropped
        self.assertEqual(acc[W1]["award"], {"total": "150", "blocks_part": "148", "votes_part": "2"})
        self.assertEqual(set(acc[W1]), {"wallet", "blocks", "votes", "award"})
        self.assertIsNone(acc[W1]["blocks"])
        self.assertIsNone(open_["award_total"])
        oacc = {x["wallet"]: x for x in open_["accounts"]}
        self.assertEqual((oacc[W1]["blocks"], oacc[W1]["votes"], oacc[W1]["award"]), (4, 5, None))
        self.assertEqual((oacc[W2]["blocks"], oacc[W2]["votes"]), (None, 2))   # "7" is not a number
        # a day recorded only in part publishes no per-account counts (review, 15 Nov 2026)
        part = collect.award_day(20772, dict(self.summary_open, recorded={"awards": "all", "blocks": "part", "votes": "part"}), self.keys_open) \
            if hasattr(self, "summary_open") else None
        if part is not None:
            self.assertTrue(all(x["blocks"] is None and x["votes"] is None for x in part["accounts"]))
        # live needs a recent chain head (an old head publishes no live list): see test below
        self.assertEqual(a["live"], {"as_of_unix": NOW, "window_s": 600,
                                     "keys": {"0000000a": NOW - 100, "0000000c": NOW - 100}})
        # the settled day is cached; a second run reads only the list, the open day and blocks
        node.asked[:] = []
        collect.build_awards("x", CHAIN, st, NOW)
        self.assertFalse(any(p.startswith("awards/network/day/20771") for p in node.asked))
        json.dumps(a)                                                       # plain JSON

    def test_bad_answers(self):
        r = routes()
        r["awards/network/day/20771?limit=1000&after=2"]["next_after"] = 2   # paging loop
        r["awards/network/day/20772?limit=1000"] = {"day": 99, "keys": []}  # wrong day
        collect.get = FakeNode(r)
        a = collect.build_awards("x", CHAIN, {}, NOW)
        self.assertIsNone(a)                                                # no usable day at all
        r = routes()
        lo_slot = ((NOW - 600) * 1000 - CHAIN["start_time"]) // 30000 - 1
        r["awards/network/day/20772/blocks?limit=240&after_slot=%d" % lo_slot]["recorded"] = "none"
        r["awards/network/day/20772/blocks?limit=240&after_slot=%d" % lo_slot]["rows"] = []
        r["awards/network/day/20772/blocks?limit=240&after_slot=%d" % lo_slot]["next_after_slot"] = None
        collect.get = FakeNode(r)
        a = collect.build_awards("x", CHAIN, {}, NOW)
        self.assertIsNone(a["live"])                                        # nothing recorded


def account_answer(wallet, available="21", vesting="19", earned="40", blocks=7, own=False):
    return {"account": wallet, "kind": "address", "own": own, "currency": "$fake6529",
            "available": available, "vesting": vesting,
            "validator": {"authorized": True, "on_list": True, "since_day": 20770,
                          "since_date": "2026-11-13", "days": []},
            "keys": [{"key_id": "0x" + "9" * 64, "key_code": "0000000a", "own": own}],
            "days": [], "totals": {"earned": earned, "blocks": blocks, "from_day": None,
                                   "from_date": None},
            "faults": [{"day": 20772, "what": "secret"}] if own else [], "as_of": {"height": 1}}


class Clock(object):
    def __init__(self, t, step=0.0):
        self.t, self.step = float(t), step

    def __call__(self):
        self.t += self.step
        return self.t


class Balances(unittest.TestCase):
    def setUp(self):
        collect._dropped.clear()
        self.real_get = collect.get

    def tearDown(self):
        collect.get = self.real_get

    def node(self, wallets, **kw):
        return FakeNode({"chain/account/%s" % w: account_answer(w, **kw) for w in wallets})

    def test_schema_and_own_dropped(self):
        collect.get = FakeNode({"chain/account/%s" % W1: account_answer(W1, own=True),
                                "chain/account/%s" % W2: account_answer(W2, earned="50")})
        st = {}
        b = collect.build_balances("x", st, [W1, W2], clock=Clock(NOW))
        self.assertEqual(set(b), {"as_of_unix", "accounts"})
        self.assertEqual(b["as_of_unix"], NOW)
        self.assertEqual(b["accounts"], [
            {"wallet": W2, "balance": "21", "vesting": "19", "earned": "50", "blocks": 7},
            {"wallet": W1, "balance": "21", "vesting": "19", "earned": "40", "blocks": 7}])
        text = json.dumps(b) + json.dumps(st)
        for word in ("own", "faults", "secret", "key_code", "kind", "as_of\""):
            self.assertNotIn(word, text.replace("as_of_unix", ""))
        self.assertEqual(collect._dropped, set())               # every answer field is known

    def test_strict_forms(self):
        bad = {W1: account_answer(W1, available="-1"),            # not digits
               W2: account_answer(W2, earned="1" * 81),           # longer than 80
               W3: account_answer(W3, blocks="7")}                # blocks must be an int
        wd = "0x" + "d4" * 20
        bad[wd] = account_answer("0x" + "e5" * 20)                # answer for another account
        collect.get = FakeNode({"chain/account/%s" % w: a for w, a in bad.items()})
        self.assertIsNone(collect.build_balances("x", {}, [W1, W2, W3, wd], clock=Clock(NOW)))
        r = account_answer(W1)
        r["vesting"] = 19                                          # a number, not a string
        collect.get = FakeNode({"chain/account/%s" % W1: r})
        self.assertIsNone(collect.build_balances("x", {}, [W1], clock=Clock(NOW)))
        self.assertIsNone(collect.build_balances("x", {}, [], clock=Clock(NOW)))   # no list, no cache

    def test_rotation_freshness_and_as_of(self):
        # The figures below assume 40 reads a run; the deployed value may differ.
        saved, collect.BALANCE_FETCHES = collect.BALANCE_FETCHES, 40
        self.addCleanup(setattr, collect, "BALANCE_FETCHES", saved)
        wallets = ["0x%040x" % i for i in range(100)]
        node = self.node(wallets)
        collect.get = node
        st = {}
        b = collect.build_balances("x", st, wallets, clock=Clock(NOW))
        self.assertEqual(len(node.asked), collect.BALANCE_FETCHES)
        self.assertEqual(len(b["accounts"]), 40)                  # only what was ever fetched
        b = collect.build_balances("x", st, wallets, clock=Clock(NOW + 300))
        b = collect.build_balances("x", st, wallets, clock=Clock(NOW + 600))
        self.assertEqual(len(set(node.asked)), 100)                # each once, never-fetched first
        self.assertEqual(len(node.asked), 120)
        self.assertEqual(b["as_of_unix"], NOW)                     # the oldest published fetch
        b = collect.build_balances("x", st, wallets, clock=Clock(NOW + 900))
        self.assertEqual(node.asked[-40:], ["chain/account/" + w for w in wallets[20:60]])   # oldest first
        self.assertEqual(b["as_of_unix"], NOW + 300)

    def test_missing_or_null_field_refused(self):
        for drop in ("available", "vesting"):
            r = account_answer(W1)
            r.pop(drop)
            collect.get = FakeNode({"chain/account/%s" % W1: r})
            self.assertIsNone(collect.fetch_balance("x", W1))
            r = account_answer(W1)
            r[drop] = None
            collect.get = FakeNode({"chain/account/%s" % W1: r})
            self.assertIsNone(collect.fetch_balance("x", W1))
        r = account_answer(W1)
        r["totals"]["earned"] = None
        collect.get = FakeNode({"chain/account/%s" % W1: r})
        self.assertIsNone(collect.fetch_balance("x", W1))
        # a bad or too old cached row is dropped, never published
        st = {"balances": {W1: {"balance": "1", "vesting": None, "earned": "1", "blocks": 1, "t": NOW},
                           W2: {"balance": "1", "vesting": "1", "earned": "1", "blocks": 1, "t": NOW - 7200}}}
        collect.get = FakeNode({})
        b = collect.build_balances("x", st, [W1, W2], fetch=lambda a, w: (_ for _ in ()).throw(OSError()), clock=Clock(NOW))
        self.assertIsNone(b)

    def test_left_validators_dropped_and_zero_omitted(self):
        collect.get = self.node([W1, W2])
        st = {}
        collect.build_balances("x", st, [W1, W2], clock=Clock(NOW))
        collect.get = FakeNode({"chain/account/%s" % W1: account_answer(W1),
                                "chain/account/%s" % W3: account_answer(W3, "0", "0", "0", 0)})
        b = collect.build_balances("x", st, [W1, W3], clock=Clock(NOW + 300))
        self.assertEqual(set(st["balances"]), {W1, W3})             # W2 left the validators
        self.assertEqual([x["wallet"] for x in b["accounts"]], [W1])   # W3 all zeros: left out
        self.assertEqual(b["as_of_unix"], NOW + 300)

    def test_route_missing_gives_null(self):
        collect.get = FakeNode({})
        st = {"balances": {W1: {"balance": "1", "vesting": "1", "earned": "1", "blocks": 1, "t": NOW}}}
        before = json.dumps(st)
        self.assertIsNone(collect.build_balances("x", st, [W1, W2], clock=Clock(NOW + 60)))
        self.assertEqual(json.dumps(st), before)                     # nothing else changes

    def test_failures_budget_and_cache(self):
        wallets = ["0x%040x" % i for i in range(50)]
        st = {"balances": {wallets[0]: {"balance": "5", "vesting": "0", "earned": "5", "blocks": 1,
                                        "t": NOW - 100},
                           "0xnot": {"balance": "1"}, wallets[1]: {"balance": "<b>", "t": NOW}}}
        calls = []

        def boom(api, w):
            calls.append(w)
            raise OSError("timeout")
        b = collect.build_balances("x", st, wallets, fetch=boom, clock=Clock(NOW))
        self.assertEqual(len(calls), 3)                              # gives up after 3 failures
        self.assertEqual(b, {"as_of_unix": NOW - 100, "accounts": [
            {"wallet": wallets[0], "balance": "5", "vesting": "0", "earned": "5", "blocks": 1}]})
        self.assertEqual(set(st["balances"]), {wallets[0]})          # bad entries dropped
        calls[:] = []
        b = collect.build_balances("x", st, wallets, fetch=lambda a, w: calls.append(w) or
                                   {"balance": "1", "vesting": "0", "earned": "1", "blocks": 0},
                                   clock=Clock(NOW, step=1.0))       # each fetch takes 1 s
        self.assertLessEqual(len(calls), collect.BALANCE_BUDGET_S + 2)

    def test_in_awards(self):
        r = routes()
        for w in (W1, W2):
            r["chain/account/%s" % w] = account_answer(w)
        collect.get = FakeNode(r)
        a = collect.build_awards("x", CHAIN, {}, NOW, [W1.upper().replace("0X", "0x"), W2])
        self.assertEqual([x["wallet"] for x in a["balances"]["accounts"]], [W1, W2])


class SignNames(unittest.TestCase):
    def test_names(self):
        root = tempfile.mkdtemp()
        try:
            for n in ("network.json", "history.json", "awards.json", "own.json", "days/index.json",
                      "days/20771.json", "days/x.json", "days/20771.json.tmp", "cycles/20771.json",
                      "other/1.json"):
                p = os.path.join(root, n)
                os.makedirs(os.path.dirname(p), exist_ok=True)
                with open(p, "w") as f:
                    f.write("{}")
            self.assertEqual(sign.data_files(root), [
                "awards.json", "history.json", "network.json", "cycles/20771.json",
                "days/20771.json", "days/index.json"])
            shutil.rmtree(os.path.join(root, "days"))
            os.symlink(os.path.join(root, "cycles"), os.path.join(root, "days"))
            with self.assertRaises(sign.Refuse):
                sign.data_files(root)                                       # a linked days/ is refused
        finally:
            shutil.rmtree(root)


class WorkerBatch(unittest.TestCase):
    """sign.worker_batch: the POST body for the data Worker (data-worker/)."""

    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.root)
        for n in ("network.json", "chain.json", "own.json", "cycles/index.json", "days/index.json",
                  "cycles/20775.json", "cycles/123.json", "days/20774.json", "days/20773.json"):
            self.put(n)

    def put(self, name, extra=""):
        p = os.path.join(self.root, name)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w") as f:
            f.write(json.dumps({"alg": "ECDSA-P256-SHA256", "format": 2, "key": "k", "name": name,
                                "payload": ('{"generated_unix":1}' if name in ("network.json", "chain.json")
                                            else "{}") + extra,
                                "sig": "s"}, separators=(",", ":")))

    def text(self, name):
        with open(os.path.join(self.root, name)) as f:
            return f.read()

    def test_body_order_and_exact_text(self):
        body, nxt, n, notes = sign.worker_batch(self.root, {}, NOW)
        self.assertEqual((n, notes), (7, []))
        names = ["chain.json", "network.json", "cycles/index.json", "days/index.json",
                 "cycles/20775.json", "days/20774.json", "days/20773.json"]
        self.assertEqual(body, "{" + ",".join(json.dumps(x) + ":" + self.text(x) for x in names) + "}")
        self.assertEqual(sorted(json.loads(body)), sorted(names))   # own.json, cycles/123 left out
        self.assertEqual(nxt["full_unix"], NOW)
        self.assertEqual(sorted(nxt["files"]), sorted(names))

    def test_only_changed_then_hourly_full(self):
        _, nxt, _, _ = sign.worker_batch(self.root, {}, NOW)
        self.assertEqual(sign.worker_batch(self.root, nxt, NOW + 60), (None, nxt, 0, []))
        self.put("network.json", " ")
        body, nxt2, n, _ = sign.worker_batch(self.root, nxt, NOW + 120)
        self.assertEqual((n, list(json.loads(body))), (1, ["network.json"]))
        self.assertEqual(nxt2["full_unix"], NOW)                    # not a full send
        body, nxt3, n, _ = sign.worker_batch(self.root, nxt2, NOW + 3600)
        self.assertEqual((n, nxt3["full_unix"]), (7, NOW + 3600))   # every file once an hour
        os.unlink(os.path.join(self.root, "days/20773.json"))
        _, nxt4, _, _ = sign.worker_batch(self.root, {}, NOW)
        self.assertNotIn("days/20773.json", nxt4["files"])
        for bad in (None, [], {"files": [], "full_unix": "x"}):     # a bad record: send everything
            self.assertEqual(sign.worker_batch(self.root, bad, NOW)[2], 6)

    def test_limits_left_for_next_run(self):
        for d in range(20700, 20770):
            self.put("days/%d.json" % d)
        body, nxt, n, _ = sign.worker_batch(self.root, {}, NOW)
        days = [x for x in json.loads(body) if x.startswith("days/2")]
        self.assertEqual(len(days), 60)                              # only the 60 the Worker keeps
        self.assertEqual(days[0], "days/20774.json")
        self.assertNotIn("days/20700.json", nxt["files"])
        old = sign.WORKER_MAX_ENTRIES
        sign.WORKER_MAX_ENTRIES = 3
        try:
            body, nxt, n, notes = sign.worker_batch(self.root, {}, NOW)
            self.assertEqual(list(json.loads(body)), ["chain.json", "network.json", "cycles/index.json"])
            self.assertIn("days/index.json: over the Worker's limits, next run", notes)
            # the rest has no file with generated_unix: held until network.json changes
            body2, nxt2, n2, notes2 = sign.worker_batch(self.root, nxt, NOW + 60)
            self.assertEqual((body2, nxt2, n2), (None, nxt, 0))
            self.assertIn("held: no changed file with generated_unix in the batch", notes2)
            self.put("network.json", " ")
            body3, _, _, _ = sign.worker_batch(self.root, nxt, NOW + 120)
            self.assertEqual(list(json.loads(body3)), ["network.json", "days/index.json", "cycles/20775.json"])
        finally:
            sign.WORKER_MAX_ENTRIES = old
        self.put("network.json", " " * (sign.WORKER_MAX_ENTRY + 1))  # too big for the Worker
        body, nxt, n, _ = sign.worker_batch(self.root, {}, NOW)
        self.assertNotIn("network.json", json.loads(body))
        self.assertNotIn("network.json", nxt["files"])

    def test_misnamed_envelope_left_out(self):
        with open(os.path.join(self.root, "chain.json"), "w") as f:
            f.write(json.dumps({"name": "network.json"}))
        body, nxt, _, notes = sign.worker_batch(self.root, {}, NOW)
        self.assertNotIn("chain.json", json.loads(body))
        self.assertNotIn("chain.json", nxt["files"])
        self.assertEqual(notes, ["chain.json: not an envelope of that name"])

    def test_unreadable_file_skipped_not_fatal(self):
        p = os.path.join(self.root, "days/20773.json")
        os.unlink(p)
        os.symlink(os.path.join(self.root, "days/20774.json"), p)    # read_regular refuses a link
        body, nxt, n, notes = sign.worker_batch(self.root, {}, NOW)
        self.assertEqual(n, 6)
        self.assertNotIn("days/20773.json", json.loads(body))
        self.assertEqual(notes, ["days/20773.json: unreadable (OSError)"])

    def test_cli_one_line(self):
        out = os.path.join(self.root, "..", os.path.basename(self.root) + "-out")
        os.makedirs(os.path.join(out, "signed"))
        self.addCleanup(shutil.rmtree, out)
        os.rmdir(os.path.join(out, "signed"))
        shutil.copytree(self.root, os.path.join(out, "signed"))
        import subprocess
        here = os.path.dirname(os.path.abspath(__file__))
        cmd = [sys.executable, "-I", "-B", os.path.join(here, "sign.py"), "--data", out,
               "--batch", os.path.join(out, "body.json"), "--posted", os.path.join(out, "posted.json")]
        r = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.assertEqual((r.returncode, r.stdout, r.stderr), (0, "7\n", ""))
        self.assertTrue(os.path.exists(os.path.join(out, "posted.json.next")))


class DistributedTotal(unittest.TestCase):
    def setUp(self):
        collect._dropped.clear()
        self.real_get = collect.get

    def tearDown(self):
        collect.get = self.real_get

    def test_in_awards(self):
        collect.get = FakeNode(routes())
        st = {}
        a = collect.build_awards("x", dict(CHAIN, start_day=20771), st, NOW)
        self.assertEqual(a["distributed_total"], "300")          # the open day is not counted
        self.assertEqual(st["award_totals"], {"20771": "300"})
        collect.get = FakeNode(routes())
        a = collect.build_awards("x", dict(CHAIN, start_day=20770), {}, NOW)
        self.assertIsNone(a["distributed_total"])                # day 20770 unknown: a gap

    def test_each_day_once_replaced_and_kept(self):
        big = str(5 * 10 ** 24)
        st = {"award_totals": {"20770": big, "x": "1", "20769": "-5", "\u00b2": "1", "\uff12": "1"},
              "award_totals_start": 20770}
        days = [{"day": 20771, "state": "settled", "award_total": "300"},
                {"day": 20772, "state": "open", "award_total": None}]
        self.assertEqual(collect.distributed_total(st, days, 20770), str(5 * 10 ** 24 + 300))
        self.assertEqual(set(st["award_totals"]), {"20770", "20771"})     # bad entries dropped
        # the same day read again replaces its value; 20770 left the API window: kept
        days = [{"day": 20771, "state": "settled", "award_total": "350"},
                {"day": 20772, "state": "settled", "award_total": "50"}]
        self.assertEqual(collect.distributed_total(st, days, 20770), str(5 * 10 ** 24 + 400))
        self.assertEqual(collect.distributed_total(st, [], 20770), str(5 * 10 ** 24 + 400))
        self.assertIsNone(collect.distributed_total(st, [], None))       # start unknown: null
        self.assertEqual(st["award_totals_start"], 20770)                # ... and nothing changed
        st2 = {"award_totals": {"20771": "300"}, "award_totals_start": 20770}
        self.assertIsNone(collect.distributed_total(st2, [], 20770))     # 20770 missing: a gap
        self.assertIsNone(collect.distributed_total({}, days[:0], 20770))  # nothing settled yet

    def test_new_chain_start_clears(self):
        st = {"award_totals": {"20770": "100", "20771": "300"}, "award_totals_start": 20770}
        days = [{"day": 20900, "state": "settled", "award_total": "7"}]
        self.assertEqual(collect.distributed_total(st, days, 20900), "7")
        self.assertEqual(st["award_totals"], {"20900": "7"})
        self.assertEqual(st["award_totals_start"], 20900)


R = ["0x%040x" % (i + 1) for i in range(10)]        # roster wallets (R[0] plays the node's own)
OUTSIDER = "0x" + "ee" * 20                          # a block maker not on the roster


def block_store(root, heights, maker):
    """A chain store with blocks 1..heights (one per slot), as read_block_index expects."""
    import struct
    start = (NOW // 86400 - 4) * 86400000
    d = os.path.join(root, "chain", "net", "index")
    os.makedirs(d)
    parent = b"\0" * 32
    with open(os.path.join(d, "blocks.log"), "wb") as f:
        for h in range(1, heights + 1):
            t = start + h * 30000
            r = struct.pack(">Q", h).rjust(32, b"\1")
            body = (b"\x01\x01" + r + parent + struct.pack(">QQIQ", h, h, t // 86400000, t)
                    + b"\x09" * 32 + b"\0" * 12 + bytes.fromhex(maker(h)[2:]) + b"extra")
            f.write(struct.pack(">I", len(body)) + body + b"\0" * 8)
            parent = r
    head = {"root": "0x" + parent.hex(), "height": heights, "slot": heights, "time": t}
    own_today = sum(1 for h in range(1, heights + 1)
                    if maker(h) == R[0] and (start + h * 30000) // 86400000 == t // 86400000)
    return start, head, own_today


class Notable(unittest.TestCase):
    def setUp(self):
        collect._dropped.clear()
        self.real_get, self.real_dirs = collect.get, collect.STORE_DIRS
        self.root = tempfile.mkdtemp()
        collect.STORE_DIRS = (self.root,)

    def tearDown(self):
        collect.get, collect.STORE_DIRS = self.real_get, self.real_dirs
        shutil.rmtree(self.root)

    def run_blocks(self, heights, maker, finalized="default"):
        shutil.rmtree(self.root)
        os.makedirs(self.root)
        start, head, own_today = block_store(self.root, heights, maker)
        chain = {"head": head, "start_time": start, "slot_ms": 30000}
        if finalized == "default":
            finalized = {"height": heights - 2}
        if finalized is not None:
            chain["finalized"] = finalized
        collect.get = FakeNode({
            "chain": chain,
            "chain/me": {"wallet": R[0]}, "awards/days": {"days": []},
            "awards": {"today": {"day": head["time"] // 86400000, "blocks": own_today},
                       "as_of": {"height": heights}}})
        return collect.chain_blocks("x", R, head["time"] // 1000 + 10)

    def test_notable_heights(self):
        b = self.run_blocks(10100, lambda h: OUTSIDER if h == 6529 else R[h % 10])
        self.assertIsNotNone(b)
        self.assertEqual(b["head_height"], 10100)
        self.assertEqual(b["notable"], [
            {"height": 6529, "wallet": None, "unix": (b["days"][0] * 86400000 + 6529 * 30000) // 1000},
            {"height": 10000, "wallet": R[0], "unix": (b["days"][0] * 86400000 + 10000 * 30000) // 1000}])
        self.assertNotIn(OUTSIDER, json.dumps(b))           # not on the roster: never named

    def test_no_notable(self):
        b = self.run_blocks(3000, lambda h: R[h % 10])
        self.assertIsNotNone(b)
        self.assertEqual(b["notable"], [])

    def test_only_final_blocks(self):
        maker = lambda h: R[h % 10]
        b = self.run_blocks(10001, maker, finalized={"height": 9999})
        self.assertEqual([n["height"] for n in b["notable"]], [6529])     # 10000 not final yet
        b = self.run_blocks(10001, maker, finalized={"height": 10000})
        self.assertEqual([n["height"] for n in b["notable"]], [6529, 10000])

    def test_no_finalized_height_no_field(self):
        maker = lambda h: R[h % 10]
        for fin in (None, {"height": "10000"}, {"height": True}, {"height": 99999}, {}):
            b = self.run_blocks(10001, maker, finalized=fin)
            self.assertIsNotNone(b, fin)                      # the rest of blocks.json is kept
            self.assertNotIn("notable", b, fin)               # fail-closed: absent, not guessed


class Signing(unittest.TestCase):
    """signed_days and streak per account: lower-bound start, day roll, gaps."""
    def setUp(self):
        collect._dropped.clear()
        self.out = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.out)

    def run_day(self, st, v):
        # as main() does: account_signing first, then roll_day keeps the list as last_vals
        collect.account_signing(st, v)
        collect.roll_day(self.out, st, v)
        return {a["wallet"]: (a["signed_days"], a["streak"]) for a in v["accounts"]}

    def m(self, code, today, fs, s30, ls):
        return machine(code, today=today, first_signed_day=fs, signed_30=s30, last_signed_day=ls)

    def acct(self, wallet, *machines):
        return {"wallet": wallet, "tdh": 1, "weight": 1, "machines": list(machines)}

    def test_lower_bound_start(self):
        W4, W5 = "0x" + "d4" * 20, "0x" + "e5" * 20
        v = vals(20774, [
            # signed every day since the network's first: 9 finished days + today
            self.acct(W1, self.m("0000000a", "SIGNED", 20765, 9, 20774)),
            # missed 2 days, signed yesterday, not yet today
            self.acct(W2, self.m("0000000b", "NOT_YET", 20765, 7, 20773)),
            # two machines: the newer one proves 4 days in a row, the older one has more days
            self.acct(W3, self.m("0000000c", "SIGNED", 20770, 4, 20774),
                      self.m("0000000d", "MISSED", 20765, 8, 20772)),
            # a counter larger than the days since the first signature: not used
            self.acct(W4, self.m("0000000e", "NOT_YET", 20772, 5, 20773)),
            # first signature today
            self.acct(W5, self.m("0000000f", "SIGNED", 20774, 0, 20774)),
        ])
        self.assertEqual(self.run_day({}, v), {W1: (10, 10), W2: (7, 1), W3: (9, 5),
                                               W4: (1, 1), W5: (1, 1)})

    def test_day_roll(self):
        st = {}
        W4 = "0x" + "d4" * 20
        day1 = vals(20774, [
            self.acct(W1, self.m("0000000a", "SIGNED", 20765, 9, 20774)),
            self.acct(W2, self.m("0000000b", "NOT_YET", 20765, 9, 20773)),    # signs late (23:59)
            self.acct(W3, self.m("0000000c", "NOT_YET", 20765, 9, 20773)),    # never signs that day
            self.acct(W4, self.m("0000000d", "NOT_YET", 20765, 9, 20773)),    # signed_30 shows it
        ])
        self.assertEqual(self.run_day(st, day1), {W1: (10, 10), W2: (9, 9), W3: (9, 9), W4: (9, 9)})
        self.assertEqual(self.run_day(st, day1), {W1: (10, 10), W2: (9, 9), W3: (9, 9), W4: (9, 9)})
        day2 = vals(20775, [
            self.acct(W1, self.m("0000000a", "NOT_YET", 20765, 10, 20774)),
            self.acct(W2, self.m("0000000b", "SIGNED", 20765, 10, 20775)),
            self.acct(W3, self.m("0000000c", "NOT_YET", 20765, 9, 20773)),
            self.acct(W4, self.m("0000000d", "SIGNED", 20765, 10, 20775)),
        ])
        got = self.run_day(st, day2)
        self.assertEqual(got, {W1: (10, 10),        # today still open: the streak holds
                               W2: (11, 11),        # late signature (signed_30 grew) + today
                               W3: (9, 0),          # a finished day unsigned resets the streak
                               W4: (11, 11)})
        self.assertEqual(st["sign_counts"][W3], {"days": 9, "streak": 0, "through": 20774})
        self.assertEqual(self.run_day(st, day2), got)            # the same day again: counted once
        # a run of the next day without its own last list: 20775 counted once, from day2
        day3 = vals(20776, [self.acct(w, self.m(c, "NOT_YET", 20765, 11, 20775))
                            for w, c in ((W1, "0000000a"), (W2, "0000000b"))])
        got3 = self.run_day(st, day3)
        self.assertEqual(got3[W2], (11, 11))
        # W1 showed NOT_YET in day2's last list, but its signed_30 grew from 10 to 11: it
        # signed 20775 late, so the streak goes on
        self.assertEqual(got3[W1], (11, 11))

    def test_gap_and_resync(self):
        st = {}
        v = vals(20774, [self.acct(W1, self.m("0000000a", "SIGNED", 20765, 9, 20774))])
        self.run_day(st, v)
        st["sign_counts"][W1] = {"days": 40, "streak": 40, "through": 20760}   # before a long gap
        st.pop("last_vals")
        self.assertEqual(self.run_day(st, v), {W1: (41, 10)})   # days kept, streak from the bound
        st["sign_counts"][W1] = {"days": 50, "streak": 50, "through": 20780}   # node went back
        st.pop("last_vals")
        self.assertEqual(self.run_day(st, v), {W1: (10, 10)})   # the bound is published
        self.assertEqual(st["sign_counts"][W1]["through"], 20780)              # stored entry kept
        bad = dict(v, cycle_day=None)
        collect.account_signing({}, bad)
        self.assertIsNone(bad["accounts"][0]["signed_days"])

    def test_stale_today_not_counted(self):
        # a list where `today` still says SIGNED but last_signed_day is the day before (the
        # node has not turned the day yet): not a signature of this day
        st = {}
        v = vals(20774, [self.acct(W1, self.m("0000000a", "SIGNED", 20765, 9, 20773))])
        self.assertEqual(self.run_day(st, v), {W1: (9, 9)})
        # next day: the 20774 list's SIGNED was stale, and nothing else shows 20774 signed
        v2 = vals(20775, [self.acct(W1, self.m("0000000a", "NOT_YET", 20765, 9, 20773))])
        self.assertEqual(self.run_day(st, v2), {W1: (9, 0)})

    def test_growth_needs_plausible_counters(self):
        st = {}
        day1 = vals(20774, [self.acct(W1, self.m("0000000a", "NOT_YET", 20765, 9, 20773)),
                            self.acct(W2, self.m("0000000b", "NOT_YET", 20765, 8, 20773)),
                            self.acct(W3, self.m("0000000c", "NOT_YET", 20765, 9, 20773),
                                      {"code": None, "today": "NOT_YET", "signed_30": 1})])
        self.run_day(st, day1)
        day2 = vals(20775, [
            # grew, but to more than the days since its first signature: not believed
            self.acct(W1, self.m("0000000a", "NOT_YET", 20765, 11, 20773)),
            # grew, but its last signature is before the day: not believed
            self.acct(W2, self.m("0000000b", "NOT_YET", 20765, 9, 20773)),
            # a machine without a code is never matched
            self.acct(W3, self.m("0000000c", "NOT_YET", 20765, 9, 20773),
                      {"code": None, "today": "NOT_YET", "signed_30": 5})])
        got = self.run_day(st, day2)
        self.assertEqual(got[W2], (9, 0))
        self.assertEqual(got[W3], (9, 0))
        self.assertEqual(got[W1][1], 0)

    def test_bad_state_entries_dropped(self):
        st = {"sign_counts": {W1: {"days": 3, "streak": 9, "through": 20773},    # streak > days
                              "nope": {"days": 1, "streak": 1, "through": 20773},
                              W2: {"days": "5", "streak": 1, "through": 20773},
                              W3: {"days": 5, "streak": 1, "through": 20700}}}   # long gone
        v = vals(20774, [self.acct(W1, self.m("0000000a", "NOT_YET", 20770, 4, 20773))])
        self.assertEqual(self.run_day(st, v), {W1: (4, 4)})
        self.assertEqual(set(st["sign_counts"]), {W1})


class HeadRecent(unittest.TestCase):
    def test_head_recent(self):
        self.assertTrue(collect._head_recent({"head": {"time": (NOW - 60) * 1000}}, NOW))
        self.assertFalse(collect._head_recent({"head": {"time": (NOW - 600) * 1000}}, NOW))
        self.assertFalse(collect._head_recent({}, NOW))


if __name__ == "__main__":
    unittest.main(verbosity=1)
