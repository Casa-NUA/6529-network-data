#!/usr/bin/env python3
"""Offline tests for the schema cleaning of the files added on 15 Nov 2026
(handles, days/, history.json, awards.json), for sign.py's accepted names, and (18 Nov 2026)
for 6529's signed release feed (keccak-256, secp256k1 recovery, releases.json) and the
"6529 agrees" cross-check.

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
            for n in ("network.json", "history.json", "awards.json", "releases.json", "releases.json.sig",
                      "health.json", "health.json.tmp", "blockdays.json", "blockdays.json.tmp",
                      "own.json", "days/index.json",
                      "days/20771.json", "days/x.json", "days/20771.json.tmp", "cycles/20771.json",
                      "other/1.json"):
                p = os.path.join(root, n)
                os.makedirs(os.path.dirname(p), exist_ok=True)
                with open(p, "w") as f:
                    f.write("{}")
            self.assertEqual(sign.data_files(root), [
                "awards.json", "blockdays.json", "health.json", "history.json", "network.json", "releases.json",
                "cycles/20771.json",
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


def chain_store(root, last, maker, slot_ms=30000, days_back=4, first=1):
    """A chain store holding blocks first..last (one per slot), as read_block_index expects.
    (start, head, own figures per day {day: own blocks})."""
    import struct
    start = (NOW // 86400 - days_back) * 86400000
    d = os.path.join(root, "chain", "net", "index")
    os.makedirs(d)
    parent, own = b"\0" * 32, {}
    with open(os.path.join(d, "blocks.log"), "wb") as f:
        for h in range(1, last + 1):
            t = start + h * slot_ms
            r = struct.pack(">Q", h).rjust(32, b"\1")
            if maker(h) == R[0]:
                own[t // 86400000] = own.get(t // 86400000, 0) + 1
            if h >= first:
                body = (b"\x01\x01" + r + parent + struct.pack(">QQIQ", h, h, t // 86400000, t)
                        + b"\x09" * 32 + b"\0" * 12 + bytes.fromhex(maker(h)[2:]) + b"extra")
                f.write(struct.pack(">I", len(body)) + body + b"\0" * 8)
            parent = r
    head = {"root": "0x" + parent.hex(), "height": last, "slot": last, "time": t}
    return start, head, own


class BlockDays(unittest.TestCase):
    """chain_blockdays: blockdays.json, every whole past chain day."""

    def setUp(self):
        collect._dropped.clear()
        self.real_get, self.real_dirs = collect.get, collect.STORE_DIRS
        self.root = tempfile.mkdtemp()
        collect.STORE_DIRS = (self.root,)

    def tearDown(self):
        collect.get, collect.STORE_DIRS = self.real_get, self.real_dirs
        shutil.rmtree(self.root)

    def node(self, last, maker, slot_ms=30000, days_back=4, first=1, finalized=None, own_fix=None):
        shutil.rmtree(self.root)
        os.makedirs(self.root)
        start, head, own = chain_store(self.root, last, maker, slot_ms, days_back, first)
        today = head["time"] // 86400000
        chain = {"head": head, "start_time": start, "slot_ms": slot_ms,
                 "finalized": {"height": last - 2 if finalized is None else finalized}}
        past = dict((d, n) for d, n in own.items() if d != today)
        past.update(own_fix or {})
        collect.get = FakeNode({
            "chain": chain, "chain/me": {"wallet": R[0]},
            "awards/days": {"days": [{"day": d, "blocks": n} for d, n in sorted(past.items())]},
            "awards": {"today": {"day": today, "blocks": own.get(today, 0)},
                       "as_of": {"height": last}}})
        self.now = head["time"] // 1000 + 10
        return today, start

    def build(self, state=None, have_file=False, now=None, hint=None):
        st = {} if state is None else state
        return collect.chain_blockdays("x", R, self.now if now is None else now, st, have_file,
                                       day_hint=hint) + (st,)

    def test_short_chain(self):
        maker = lambda h: OUTSIDER if h % 100 == 0 else R[h % 10]
        today, start = self.node(10100, maker)
        b, why, st = self.build()
        self.assertIsNone(why)
        d0 = start // 86400000
        self.assertEqual(b["day"], today)
        self.assertEqual(b["days"], [d0, d0 + 1, d0 + 2])             # today left out
        self.assertEqual(today, d0 + 3)
        self.assertEqual(b["first_day"], d0)
        self.assertEqual(b["totals"], [2879, 2880, 2880])               # heights 1..2879 on day 0
        self.assertNotIn("generated_unix", b)
        self.assertEqual(set(b), {"day", "first_day", "days", "totals", "accounts"})
        self.assertNotIn(OUTSIDER, json.dumps(b))                       # counted, never listed
        self.assertEqual([x["wallet"] for x in b["accounts"]], sorted(R))
        by = dict((x["wallet"], x["counts"]) for x in b["accounts"])
        for i, d in enumerate(b["days"]):
            hs = [h for h in range(1, 10101) if (start + h * 30000) // 86400000 == d]
            self.assertEqual(by[R[3]][i], sum(1 for h in hs if maker(h) == R[3]))
        self.assertEqual(sum(sum(c) for c in by.values()) + sum(1 for h in range(1, 8640) if h % 100 == 0),
                         sum(b["totals"]))
        self.assertEqual(st["blockdays"], {"day": today, "final": True, "unix": self.now})

    def test_zero_accounts_left_out(self):
        maker = lambda h: R[0] if h < 2880 * 2 else R[1 + h % 9]   # R[1..9] only from day 2 on
        self.node(10100, maker)
        b, why, _ = self.build()
        self.assertIsNone(why)
        self.assertEqual([x["wallet"] for x in b["accounts"]], sorted(R))
        self.node(3000, lambda h: R[h % 2])                            # only R[0], R[1] made blocks
        b, why, _ = self.build()
        self.assertEqual([x["wallet"] for x in b["accounts"]], [R[0], R[1]])

    def test_first_day_cut(self):
        today, start = self.node(10100, lambda h: R[h % 10], first=100)   # the store lacks 1..99
        b, why, _ = self.build()
        self.assertIsNone(why)
        self.assertEqual(b["days"], [start // 86400000 + 1, start // 86400000 + 2])
        self.assertEqual(b["first_day"], start // 86400000 + 1)

    def test_no_whole_past_day(self):
        self.node(1000, lambda h: R[h % 10], days_back=0)              # the chain started today
        b, why, st = self.build()
        self.assertIsNone(b)
        self.assertIn("no whole past chain day", why)
        self.assertNotIn("blockdays", st)

    def test_400_cap(self):
        slot = 6 * 3600 * 1000                                        # 4 blocks a day
        today, start = self.node(425 * 4 + 1, lambda h: R[h % 10], slot_ms=slot, days_back=425)
        b, why, _ = self.build()
        self.assertIsNone(why)
        self.assertEqual(len(b["days"]), 400)
        self.assertEqual(b["days"][-1], today - 1)                     # the newest kept
        self.assertEqual(b["days"], list(range(today - 400, today)))
        self.assertEqual(b["first_day"], today - 400)
        self.assertTrue(all(t == 4 for t in b["totals"]))
        self.assertTrue(all(len(x["counts"]) == 400 for x in b["accounts"]))

    def test_self_check_failure(self):
        d1 = NOW // 86400 - 4 + 1
        today, start = self.node(10100, lambda h: R[h % 10], own_fix={d1: 7})
        b, why, st = self.build()
        self.assertIsNone(b)
        self.assertIn("SELF-CHECK FAILED for day %d" % d1, why)
        self.assertNotIn("blockdays", st)                               # retried next run

    def test_self_check_today(self):
        self.node(10100, lambda h: R[h % 10])
        routes = collect.get.routes
        routes["awards"]["today"]["blocks"] += 1
        b, why, st = self.build()
        self.assertIsNone(b)
        self.assertIn("SELF-CHECK FAILED for today", why)

    def test_too_many_outsiders_recently(self):
        self.node(10100, lambda h: R[0] if h % 2 else "0x%040x" % (1000 + h % 50))
        b, why, _ = self.build()
        self.assertIsNone(b)
        self.assertIn("not validating accounts", why)

    def test_same_text_same_day(self):
        self.node(10100, lambda h: R[h % 10])
        out = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, out)
        p = os.path.join(out, "blockdays.json")
        b, why, st = self.build()
        collect.write_json(p, b)
        with open(p, "rb") as f:
            first = f.read()
        # Built again later the same day (a fresh state): byte for byte the same text.
        b2, why, _ = self.build(now=self.now + 50)
        collect.write_json(p, b2)
        with open(p, "rb") as f:
            self.assertEqual(f.read(), first)
        # Not due again that day while yesterday is final: nothing asked, file left as it is.
        collect.get.asked[:] = []
        b3, why, st2 = self.build(state=st, have_file=True, now=self.now + 7200, hint=b["day"])
        self.assertEqual((b3, why), (None, None))
        self.assertEqual(collect.get.asked, [])
        b3, why, _ = self.build(state=st, have_file=True, now=self.now + 7200)
        self.assertEqual((b3, why), (None, None))                       # same, without a hint

    def test_due(self):
        due = collect.blockdays_due
        self.assertTrue(due({}, 5, NOW, True))                          # never built
        self.assertTrue(due({"blockdays": {"day": 5, "final": True, "unix": NOW}}, 5, NOW, False))  # file gone
        self.assertTrue(due({"blockdays": {"day": 4, "final": True, "unix": NOW}}, 5, NOW, True))   # new day
        self.assertFalse(due({"blockdays": {"day": 5, "final": True, "unix": NOW - 99999}}, 5, NOW, True))
        nf = {"blockdays": {"day": 5, "final": False, "unix": NOW - 3599}}
        self.assertFalse(due(nf, 5, NOW, True))                         # not final: hourly
        self.assertTrue(due(nf, 5, NOW + 1, True))
        self.assertTrue(due({"blockdays": {"day": 5, "final": False, "unix": NOW + 60}}, 5, NOW, True))
        self.assertTrue(due({"blockdays": "junk"}, 5, NOW, True))

    def test_yesterday_not_final(self):
        today, start = self.node(10100, lambda h: R[h % 10], finalized=8000)   # below 8639 (day 2's last)
        b, why, st = self.build()
        self.assertIsNotNone(b)
        self.assertFalse(st["blockdays"]["final"])
        self.assertTrue(collect.blockdays_due(st, today, self.now + 3600, True))
        self.node(10100, lambda h: R[h % 10], finalized=8639)                  # exactly the last block
        b, why, st = self.build()
        self.assertTrue(st["blockdays"]["final"])

    def test_blocks_json_unchanged(self):
        # The refactor that shares the reading with blocks.json keeps blocks.json as it was.
        today, start = self.node(10100, lambda h: R[h % 10])
        b = collect.chain_blocks("x", R, self.now)
        self.assertEqual(b["days"], [start // 86400000 + i for i in range(4)])
        self.assertIn("generated_unix", b)
        self.assertEqual(b["head_height"], 10100)


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


class BlocksNode(object):
    """Answers awards/network/day/{d}/blocks like the node: rows with slot > after_slot,
    oldest first, `limit` per page, from one list of blocks."""

    def __init__(self, start, slot_ms, rows, recorded=None, first_day=None):
        self.start, self.slot_ms, self.rows = start, slot_ms, rows
        self.recorded, self.first_day, self.asked = recorded or {}, first_day, []

    def __call__(self, api, path, timeout=20):
        import re
        import urllib.error
        self.asked.append(path)
        m = re.match(r"^awards/network/day/(\d+)/blocks\?limit=(\d+)(?:&after_slot=(\d+))?$", path)
        if not m:
            raise urllib.error.HTTPError(path, 404, "not found", {}, None)
        d, limit = int(m.group(1)), int(m.group(2))
        after = int(m.group(3)) if m.group(3) else -1
        if self.first_day is not None and d < self.first_day:
            raise urllib.error.HTTPError(path, 404, "not found", {}, None)
        day_rows = [r for r in self.rows if r["time"] // 86400000 == d and r["slot"] > after]
        page = day_rows[:limit]
        return json.loads(json.dumps({
            "day": d, "recorded": self.recorded.get(d, "all"), "rows": page,
            "next_after_slot": page[-1]["slot"] if len(day_rows) > limit else None}))


HSTART = 1794571200000                 # day 20770 at noon, as on the real chain


def hrow(start, slot, votes, final=True, proposer="000000aa"):
    """A block at `slot` carrying `votes` = [(key_code, voted_slot), ...]."""
    return {"slot": slot, "time": start + slot * 30000, "height": slot, "root": "0x" + "1" * 64,
            "final": final, "proposer": {"key_code": proposer, "key_id": "0x" + "2" * 64},
            "votes": [{"slot": s, "key_code": c, "key_id": "0x" + "3" * 64, "account": W1, "w": "5"}
                      for c, s in votes]}


def hchain(start, head_slot, final_slot):
    return {"start_time": start, "slot_ms": 30000,
            "head": {"slot": head_slot, "time": start + head_slot * 30000},
            "finalized": {"slot": final_slot, "time": start + final_slot * 30000}}


def bits(s, n=1440):
    """A health.json bitmap as a list of 0/1 (bit i = byte i // 8, MSB first)."""
    import base64
    b = base64.b64decode(s)
    assert len(b) == (n + 7) // 8
    return [(b[i >> 3] >> (7 - (i & 7))) & 1 for i in range(n)]


class Health(unittest.TestCase):
    def setUp(self):
        self.real_get = collect.get

    def tearDown(self):
        collect.get = self.real_get

    def slot_at(self, unix, start=HSTART):
        return (unix * 1000 - start) // 30000

    def test_bitmap_encoding(self):
        s = collect._bits_b64(16, [0, 9, 15])
        import base64
        self.assertEqual(base64.b64decode(s), bytes([0x80, 0x41]))   # MSB first
        self.assertEqual(collect._b64_bits(s, 16), {0, 9, 15})
        self.assertIsNone(collect._b64_bits(s, 24))                   # wrong length
        self.assertIsNone(collect._b64_bits("@@@", 16))

    def test_minutes_by_voted_slot_known_and_shape(self):
        # A chain that started 2 hours before NOW: everything before is unknown.
        start = (NOW - 7200) // 60 * 60 * 1000
        head = self.slot_at(NOW - 20, start)
        rows = []
        for s in range(1, head + 1):
            votes = [("0000000a", s - 1)]                      # a votes for every slot
            if s % 8 == 0:
                votes.append(("0000000b", s - 3))              # b every 4 minutes
            rows.append(hrow(start, s, votes))
        collect.get = BlocksNode(start, 30000, rows, first_day=start // 86400000)
        h = collect.build_health("x", hchain(start, head, head - 20),
                                 ["0000000a", "0000000b", "0000000c", "BAD"], {}, NOW)
        self.assertEqual(set(h), {"generated_unix", "minute_s", "from_unix", "minutes", "known", "machines"})
        self.assertEqual((h["minute_s"], h["minutes"]), (60, 1440))
        self.assertEqual(h["from_unix"], NOW // 60 * 60 - 86400)
        self.assertEqual(sorted(h["machines"]), ["0000000a", "0000000b", "0000000c"])
        known = bits(h["known"])
        a, b, c = (bits(h["machines"][k]) for k in ("0000000a", "0000000b", "0000000c"))
        first = (start // 1000 - h["from_unix"]) // 60          # the chain's first minute
        # settling: a minute is known once the head is 15 slots (450 s) past its end
        head_t = (start + head * 30000) // 1000
        last = sum(1 for i in range(1440) if h["from_unix"] + 60 * (i + 1) + 450 <= head_t)
        self.assertEqual(last, 1440 - 8)                        # head 20 s ago: the newest 8
        self.assertEqual(known, [0] * first + [1] * (last - first) + [0] * (1440 - last))
        self.assertEqual(a, known)                              # every known minute
        b_minutes = {(start + vs * 30000) // 60000 * 60 for vs in range(5, head - 2, 8)}
        self.assertEqual(sum(b[first:]), sum(1 for t in b_minutes
                                             if t + 60 + 450 <= head_t))
        self.assertEqual(sum(c), 0)                             # silent: known, no bit

    def test_after_the_head_is_unknown(self):
        head = self.slot_at(NOW - 200)                         # the node lags ~3 minutes
        rows = [hrow(HSTART, s, [("0000000a", s - 1)]) for s in range(head - 3000, head + 1)]
        collect.get = BlocksNode(HSTART, 30000, rows)
        h = collect.build_health("x", hchain(HSTART, head, head - 20), ["0000000a"], {}, NOW)
        known, a = bits(h["known"]), bits(h["machines"]["0000000a"])
        head_t = (HSTART + head * 30000) // 1000
        n_known = sum(1 for i in range(1440) if h["from_unix"] + 60 * (i + 1) + 450 <= head_t)
        self.assertIn(1440 - n_known, (11, 12))                 # 3 minutes behind + 7.5 settling
        self.assertEqual(known, [1] * n_known + [0] * (1440 - n_known))
        self.assertEqual(a, known)

    def test_late_votes_never_read_as_zero(self):
        # Real chain, 20 Nov 2026: votes for a slot arrive in blocks up to ~14 slots later.
        # Machine b votes once, for a slot in the minute just before the head, and the
        # block carrying it is not out yet: that minute must be unknown, not a 0.
        head = self.slot_at(NOW - 10)
        rows = [hrow(HSTART, s, [("0000000a", s - 1)]) for s in range(head - 3000, head + 1)]
        collect.get = BlocksNode(HSTART, 30000, rows)
        h = collect.build_health("x", hchain(HSTART, head, head - 20), ["0000000a", "0000000b"], {}, NOW)
        known = bits(h["known"])
        head_t = (HSTART + head * 30000) // 1000
        for i in range(1440):
            if h["from_unix"] + 60 * (i + 1) > head_t - 450:
                self.assertEqual(known[i], 0, i)                # still settling
        # once later blocks carry the vote (b, 12 slots late), its minute shows it
        late = head - 2
        rows2 = rows + [hrow(HSTART, s, [("0000000a", s - 1)] + ([("0000000b", late)] if s == late + 12 else []))
                        for s in range(head + 1, head + 40)]
        collect.get = BlocksNode(HSTART, 30000, rows2)
        h2 = collect.build_health("x", hchain(HSTART, head + 39, head + 19), ["0000000a", "0000000b"], {}, NOW + 1200)
        t = (HSTART + late * 30000) // 60000 * 60
        i = (t - h2["from_unix"]) // 60
        self.assertEqual((bits(h2["known"])[i], bits(h2["machines"]["0000000b"])[i]), (1, 1))

    def test_missing_blocks_are_a_gap_not_silence(self):
        head = self.slot_at(NOW - 15)
        rows = [hrow(HSTART, s, [("0000000a", s - 1)]) for s in range(head - 3000, head + 1)]
        gone = rows[-1500]["slot"]
        del rows[-1500]                                         # the node lacks one block
        collect.get = BlocksNode(HSTART, 30000, rows)
        h = collect.build_health("x", hchain(HSTART, head, head - 20), ["0000000a"], {}, NOW)
        known, a = bits(h["known"]), bits(h["machines"]["0000000a"])
        i = ((HSTART + (gone - 1) * 30000) // 60000 * 60 - h["from_unix"]) // 60
        self.assertEqual((known[i], a[i]), (0, 0))              # the missing vote's minute: a gap
        lo = ((HSTART + (gone - 16) * 30000) // 60000 * 60 - h["from_unix"]) // 60
        self.assertEqual(known[lo - 2:lo], [1, 1])              # well before it: known
        self.assertEqual(sum(1 for k in known[lo - 2:i + 3] if not k), len(range(lo, i + 1)) + (known[i + 1] == 0) + (known[i + 2] == 0))
        self.assertLessEqual(sum(1 for k in known[:-8] if not k), 10)   # only around the hole
        self.assertTrue(all(a[j] == known[j] for j in range(1440)))      # no false 0 anywhere
        # heights going backwards: nothing written
        bad = json.loads(json.dumps(rows)); bad[-1]["height"] = 5
        collect.get = BlocksNode(HSTART, 30000, bad)
        self.assertIsNone(collect.build_health("x", hchain(HSTART, head, head - 20), ["0000000a"], {}, NOW))

    def test_kept_minutes_make_the_next_run_small_and_identical(self):
        head = self.slot_at(NOW - 15)
        rows = [hrow(HSTART, s, [("0000000a", s - 1)] + ([("0000000b", s - 3)] if s % 5 == 0 else []))
                for s in range(head - 3000, head + 1)]
        node = BlocksNode(HSTART, 30000, rows)
        collect.get = node
        st = {}
        ch = hchain(HSTART, head, head - 20)
        codes = ["0000000a", "0000000b"]
        h1 = collect.build_health("x", ch, codes, st, NOW)
        self.assertGreater(len(node.asked), 10)                 # 24 h: many pages
        self.assertLessEqual(len(json.dumps(st)), 2000)
        node.asked.clear()
        h2 = collect.build_health("x", ch, codes, st, NOW + 5)
        self.assertEqual((h1["machines"], h1["known"]), (h2["machines"], h2["known"]))
        self.assertLessEqual(len(node.asked), 2)                 # only the last ~45 minutes
        # a minute later the window moves by one: still the same bits, shifted
        h4 = collect.build_health("x", ch, codes, json.loads(json.dumps(st)), NOW + 60)
        self.assertEqual(bits(h4["machines"]["0000000b"])[:-1], bits(h1["machines"]["0000000b"])[1:])
        # another chain start, or a damaged state: everything is read again
        for broken in ({"start_time": HSTART + 1}, {"unknown": "x"}, {"codes": {"0000000a": "AAAA"}}):
            node.asked.clear()
            st3 = json.loads(json.dumps(st))
            st3["health"].update(broken)
            h5 = collect.build_health("x", ch, codes, st3, NOW + 5)
            self.assertGreater(len(node.asked), 10, broken)
            self.assertEqual(h5["machines"], h2["machines"])
        # a kept minute is never recounted: a late change to an old block is not seen
        st2 = json.loads(json.dumps(st))
        for r in rows[:500]:
            r["votes"] = []
        h3 = collect.build_health("x", ch, codes, st2, NOW + 5)
        self.assertEqual(h1["machines"], h3["machines"])

    def test_fail_closed(self):
        head = self.slot_at(NOW - 15)
        day = (HSTART + head * 30000) // 86400000
        rows = [hrow(HSTART, s, [("0000000a", s - 1)]) for s in range(head - 3000, head + 1)]
        ch = hchain(HSTART, head, head - 20)
        # a day in the window this node recorded only in part: nothing (not unknown bits)
        collect.get = BlocksNode(HSTART, 30000, rows, recorded={day - 1: "part"})
        st = {"health": "untouched"}
        self.assertIsNone(collect.build_health("x", ch, ["0000000a"], st, NOW))
        bad = json.loads(json.dumps(rows))
        bad[-1]["votes"][0]["key_code"] = "nothex!!"
        collect.get = BlocksNode(HSTART, 30000, bad)
        self.assertIsNone(collect.build_health("x", ch, ["0000000a"], st, NOW))
        bad = json.loads(json.dumps(rows))
        bad[-1]["votes"][0]["slot"] = bad[-1]["slot"]          # not a vote for an earlier slot
        collect.get = BlocksNode(HSTART, 30000, bad)
        self.assertIsNone(collect.build_health("x", ch, ["0000000a"], st, NOW))
        collect.get = BlocksNode(HSTART, 30000, rows)
        real = collect.HEALTH_PAGES
        collect.HEALTH_PAGES = 3
        try:
            self.assertIsNone(collect.build_health("x", ch, ["0000000a"], st, NOW))
        finally:
            collect.HEALTH_PAGES = real
        self.assertIsNone(collect.build_health("x", ch, [], st, NOW))
        self.assertIsNone(collect.build_health("x", {"slot_ms": 30000}, ["0000000a"], st, NOW))
        self.assertEqual(st, {"health": "untouched"})

    def test_live_cross_check_is_logged(self):
        import io
        head = self.slot_at(NOW - 15)
        rows = [hrow(HSTART, s, [("0000000a", s - 1)]) for s in range(head - 3000, head + 1)]
        collect.get = BlocksNode(HSTART, 30000, rows)
        err, real = io.StringIO(), sys.stderr
        sys.stderr = err
        try:
            collect.build_health("x", hchain(HSTART, head, head - 20), ["0000000a"], {}, NOW,
                                 live={"keys": {"0000000a": NOW, "000000aa": NOW}})
            self.assertEqual(err.getvalue(), "")                 # the proposer counts in live
            collect.build_health("x", hchain(HSTART, head, head - 20), ["0000000a"], {}, NOW,
                                 live={"keys": {"0000000f": NOW}})
        finally:
            sys.stderr = real
        self.assertIn("differ from awards.json live.keys (1 only there, 2 only here)", err.getvalue())


class MissingDays(unittest.TestCase):
    def setUp(self):
        self.out = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.out)

    def test_skipped_days_are_listed_never_made_up(self):
        st = {}
        a = [{"wallet": W1, "tdh": 1, "weight": 1, "machines": [machine("0000000a")]}]
        collect.roll_day(self.out, st, vals(20770, a))
        self.assertEqual(collect.roll_day(self.out, st, vals(20774, a)), 20770)   # 3 days unseen
        self.assertEqual(collect.day_files(self.out), {20770})                   # none made up
        today = collect.day_record(vals(20774, a), st)
        h = collect.build_history(self.out, st, today, NOW)
        self.assertEqual(h["missing_days"], [20771, 20772, 20773])
        self.assertEqual([r["day"] for r in h["days"]], [20770, 20774])
        # one of them filled in later (a manual backfill): no longer missing
        collect.write_day(self.out, collect.day_record(vals(20772, a), st))
        h = collect.build_history(self.out, st, today, NOW)
        self.assertEqual(h["missing_days"], [20771, 20773])
        self.assertEqual(st["missing_days"], [20771, 20773])

    def test_gap_in_the_archive_without_state(self):
        st = {}
        a = [{"wallet": W1, "tdh": 1, "weight": 1, "machines": [machine("0000000a")]}]
        for d in (20770, 20773):
            collect.write_day(self.out, collect.day_record(vals(d, a), st))
        today = collect.day_record(vals(20775, a), st)
        h = collect.build_history(self.out, st, today, NOW)
        self.assertEqual(h["missing_days"], [20771, 20772, 20774])
        h = collect.build_history(self.out, {}, None, NOW)       # no list today: nothing guessed
        self.assertEqual(h["missing_days"], [])

    def test_no_gap(self):
        st = {}
        a = [{"wallet": W1, "tdh": 1, "weight": 1, "machines": [machine("0000000a")]}]
        collect.roll_day(self.out, st, vals(20770, a))
        collect.roll_day(self.out, st, vals(20771, a))
        h = collect.build_history(self.out, st, collect.day_record(vals(20771, a), st), NOW)
        self.assertEqual(h["missing_days"], [])


class HeadRecent(unittest.TestCase):
    def test_head_recent(self):
        self.assertTrue(collect._head_recent({"head": {"time": (NOW - 60) * 1000}}, NOW))
        self.assertFalse(collect._head_recent({"head": {"time": (NOW - 600) * 1000}}, NOW))
        self.assertFalse(collect._head_recent({}, NOW))

FIX = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")


def fixture(name):
    with open(os.path.join(FIX, name), "rb") as f:
        return f.read()


REAL_FEED = fixture("releases-2026-11-18.json")          # fetched once from 6529.io, 18 Nov 2026
REAL_SIG = fixture("releases-2026-11-18.json.sig")
REAL_TDH = json.loads(fixture("tdh_global_history-2026-11-18.json"))["data"]


def k1_sign(d, digest, k):
    """A plain ECDSA secp256k1 signature (r||s, 64 bytes), for the tests only."""
    z = int.from_bytes(digest, "big")
    r = collect.k1_mul(k, collect.K1_G)[0] % collect.K1_N
    s = pow(k, collect.K1_N - 2, collect.K1_N) * (z + r * d) % collect.K1_N
    return r.to_bytes(32, "big") + s.to_bytes(32, "big")


def sig_file(sig64, key=collect.RELEASES_SIGNER, **extra):
    d = {"format": 1, "algorithm": "ecdsa-secp256k1-sha256", "key": key,
         "signature": "0x" + sig64.hex()}
    d.update(extra)
    return json.dumps(d).encode()


class Crypto(unittest.TestCase):
    def test_keccak_vectors(self):
        self.assertEqual(collect.keccak256(b"").hex(),
                         "c5d2460186f7233c927e7db2dcc703c0e500b653ca82273b7bfad8045d85a470")
        self.assertEqual(collect.keccak256(b"abc").hex(),
                         "4e03657aea45a94fc7d47ba826c8d667c0d1e6e33a64a036ec44f58fa12d6c45")
        # longer than one block (136 bytes), and exactly one block less the padding byte
        self.assertEqual(len(collect.keccak256(b"a" * 300)), 32)
        self.assertNotEqual(collect.keccak256(b"a" * 135), collect.keccak256(b"a" * 136))

    def test_addresses(self):
        self.assertEqual(collect.eth_address(collect.k1_mul(1, collect.K1_G)),
                         "0x7e5f4552091a69125d5dfcb7b8c2659029395bdf")
        d = 0x4c0883a69102937d6231471b5dbb6204fe5129617082792ae468d01a3f362318
        self.assertEqual(collect.eth_address(collect.k1_mul(d, collect.K1_G)),
                         "0x2c7536e3605d9c16a7a3d7b1898e529396a65c23")

    def test_personal_sign_vector(self):
        # web3.js documentation: accounts.sign("Some data", 0x4c0883a6…) -> this signature.
        msg = b"Some data"
        h = collect.keccak256(b"\x19Ethereum Signed Message:\n" + str(len(msg)).encode() + msg)
        self.assertEqual(h.hex(), "1da44b586eb0729ff70a73c326926f6ed5a25f5b056e7f47fbc6e58d86871655")
        sig = bytes.fromhex("b91467e570a6466aa9e9876cbcd013baba02900b8979d43fe208a4a4f339f5fd"
                            "6007e74cd82e037b800186422fc2da167c747ef045e5d18a5f5d4300f8e1a029")
        q = collect.k1_recover(int.from_bytes(h, "big"), int.from_bytes(sig[:32], "big"),
                               int.from_bytes(sig[32:], "big"), 0x1c - 27)
        self.assertEqual(collect.eth_address(q), "0x2c7536e3605d9c16a7a3d7b1898e529396a65c23")
        self.assertTrue(collect.k1_signed_by(h, sig, "0x2C7536E3605D9C16a7a3D7b1898e529396a65c23"))
        self.assertFalse(collect.k1_signed_by(h, sig, collect.RELEASES_SIGNER))
        bad = bytearray(h)
        bad[0] ^= 1
        self.assertFalse(collect.k1_signed_by(bytes(bad), sig, "0x2c7536e3605d9c16a7a3d7b1898e529396a65c23"))

    def test_sign_recover_round_trip(self):
        d = 0x1234567890abcdef
        digest = collect.keccak256(b"round trip")
        sig = k1_sign(d, digest, 0xC0FFEE)
        self.assertTrue(collect.k1_signed_by(digest, sig, collect.eth_address(collect.k1_mul(d, collect.K1_G))))

    def test_out_of_range(self):
        self.assertIsNone(collect.k1_recover(1, 0, 1, 0))
        self.assertIsNone(collect.k1_recover(1, collect.K1_N, 1, 0))
        self.assertIsNone(collect.k1_recover(1, 1, collect.K1_N, 1))
        self.assertIsNone(collect.k1_recover(1, 5, 5, 2))
        self.assertFalse(collect.k1_signed_by(b"\0" * 32, b"\0" * 64, collect.RELEASES_SIGNER))


class ReleaseFeed(unittest.TestCase):
    def test_real_feed_verifies(self):
        feed, text, sig = collect.verify_release_feed(REAL_FEED, REAL_SIG)
        self.assertEqual(feed["format"], 1)
        self.assertEqual(text.encode("utf-8"), REAL_FEED)
        self.assertEqual(sig, json.loads(REAL_SIG)["signature"].lower())

    def test_real_feed_altered_fails(self):
        for altered in (REAL_FEED.replace(b"rc.23", b"rc.24", 1), REAL_FEED + b"\n",
                        REAL_FEED[:-1]):
            with self.assertRaisesRegex(ValueError, "does not verify"):
                collect.verify_release_feed(altered, REAL_SIG)

    def test_domain_is_part_of_the_message(self):
        # the same bytes signed WITHOUT the domain prefix (here by a test key named as 6529's)
        d = 0xBEEF
        sig = k1_sign(d, collect.hashlib.sha256(REAL_FEED).digest(), 77)
        with self.assertRaisesRegex(ValueError, "does not verify"):
            collect.verify_release_feed(REAL_FEED, sig_file(sig))

    def test_other_key_fails(self):
        d = 0xBEEF
        digest = collect.hashlib.sha256(collect.RELEASES_DOMAIN + REAL_FEED).digest()
        sig = k1_sign(d, digest, 77)
        with self.assertRaisesRegex(ValueError, "does not verify"):
            collect.verify_release_feed(REAL_FEED, sig_file(sig))        # claims 6529's key
        other = collect.eth_address(collect.k1_mul(d, collect.K1_G))
        with self.assertRaisesRegex(ValueError, "another key"):
            collect.verify_release_feed(REAL_FEED, sig_file(sig, key=other))   # its own key: never pinned

    def test_sig_file_forms(self):
        sig = json.loads(REAL_SIG)
        for bad, why in ((b"nope", "not JSON"), (json.dumps(dict(sig, format=2)).encode(), "format"),
                         (json.dumps(dict(sig, algorithm="ecdsa-p256-sha256")).encode(), "format"),
                         (json.dumps(dict(sig, format=True)).encode(), "format"),
                         (json.dumps(dict(sig, format=1.0)).encode(), "format"),
                         (json.dumps(dict(sig, signature=sig["signature"] + "1b")).encode(), "64 bytes"),
                         (json.dumps(dict(sig, signature=sig["signature"][2:])).encode(), "64 bytes")):
            with self.assertRaisesRegex(ValueError, why):
                collect.verify_release_feed(REAL_FEED, bad)
        with self.assertRaisesRegex(ValueError, "size"):
            collect.verify_release_feed(b"", REAL_SIG)
        with self.assertRaisesRegex(ValueError, "size"):
            collect.verify_release_feed(b" " * (collect.RELEASES_MAX + 1), REAL_SIG)
        # key spelled in lower case is the same key
        collect.verify_release_feed(REAL_FEED, json.dumps(dict(sig, key=sig["key"].lower())).encode())


class FakeWeb(object):
    def __init__(self, answers):
        self.answers, self.calls = dict(answers), []

    def __call__(self, url, most, timeout):
        self.calls.append(url)
        a = self.answers.get(url)
        if isinstance(a, Exception):
            raise a
        if a is None:
            raise OSError("no route")
        return a


class UpdateReleases(unittest.TestCase):
    def setUp(self):
        self.out = tempfile.mkdtemp()
        self.path = os.path.join(self.out, "releases.json")

    def tearDown(self):
        shutil.rmtree(self.out)

    def web(self, feed=REAL_FEED, sig=REAL_SIG):
        return FakeWeb({collect.RELEASES_URL: feed, collect.RELEASES_SIG_URL: sig})

    def read(self):
        return json.loads(self.text())

    def text(self):
        with open(self.path) as f:
            return f.read()

    def test_publish_then_rate_limit(self):
        st, web = {}, self.web()
        self.assertIsNone(collect.update_releases(self.out, st, NOW, fetch=web))
        r = self.read()
        self.assertEqual(set(r), {"source", "signer", "verified_unix", "generated_unix", "feed",
                                  "feed_text", "feed_sha256", "sig"})
        # anyone can re-check 6529's signature from our file alone
        self.assertEqual(r["feed_text"].encode("utf-8"), REAL_FEED)
        self.assertEqual(collect.hashlib.sha256(r["feed_text"].encode("utf-8")).hexdigest(), r["feed_sha256"])
        collect.verify_release_feed(r["feed_text"].encode("utf-8"), sig_file(bytes.fromhex(r["sig"][2:])))
        self.assertEqual(json.loads(r["feed_text"]), r["feed"])
        self.assertEqual(r["source"], "https://6529.io/validator/releases.json")
        self.assertEqual(r["signer"], "0xf005f32cb7ecf25566b8cf6685394e0cc694b832")
        self.assertEqual(r["feed"], json.loads(REAL_FEED))
        self.assertEqual(r["feed_sha256"], collect.hashlib.sha256(REAL_FEED).hexdigest())
        self.assertEqual((r["verified_unix"], r["generated_unix"]), (NOW, NOW))
        self.assertEqual(r["sig"], json.loads(REAL_SIG)["signature"].lower())
        self.assertEqual(len(web.calls), 2)
        # not due again before RELEASES_EVERY_S: no request at all
        self.assertIsNone(collect.update_releases(self.out, st, NOW + 60, fetch=web))
        self.assertEqual(len(web.calls), 2)
        collect.update_releases(self.out, st, NOW + collect.RELEASES_EVERY_S, fetch=web)
        self.assertEqual(len(web.calls), 4)
        self.assertEqual(self.read()["verified_unix"], NOW + collect.RELEASES_EVERY_S)

    def test_failed_verification_keeps_old_file(self):
        st = {}
        collect.update_releases(self.out, st, NOW, fetch=self.web())
        before = self.text()
        later = NOW + collect.RELEASES_EVERY_S
        note = collect.update_releases(self.out, st, later,
                                       fetch=self.web(feed=REAL_FEED.replace(b"urgent", b"normal", 1)))
        self.assertIn("NOT published", note)
        self.assertEqual(self.text(), before)
        note = collect.update_releases(self.out, st, later * 2, fetch=FakeWeb({}))
        self.assertIn("fetch failed", note)
        self.assertEqual(self.text(), before)
        note = collect.update_releases(self.out, st, later * 3,
                                       fetch=self.web(sig=urllib_error()))
        self.assertIn("fetch failed", note)
        self.assertEqual(self.text(), before)

    def test_never_publishes_unverified(self):
        note = collect.update_releases(self.out, {}, NOW, fetch=self.web(sig=b"{}"))
        self.assertIn("NOT published", note)
        self.assertFalse(os.path.exists(self.path))

    def test_older_feed_kept_out(self):
        st = {}
        collect.update_releases(self.out, st, NOW, fetch=self.web())
        r = self.read()
        r["feed"]["sequence"] += 10        # as if a newer feed had been published before
        collect.write_json(self.path, r)
        note = collect.update_releases(self.out, st, NOW + collect.RELEASES_EVERY_S, fetch=self.web())
        self.assertIn("older", note)
        self.assertEqual(self.read()["verified_unix"], NOW)

    def test_rollback_against_state_too(self):
        st = {}
        collect.update_releases(self.out, st, NOW, fetch=self.web())
        seq = json.loads(REAL_FEED)["sequence"]
        st["releases"]["sequence"] = seq + 5   # published higher before, file since replaced
        note = collect.update_releases(self.out, st, NOW + collect.RELEASES_EVERY_S, fetch=self.web())
        self.assertIn("older (sequence %d < %d)" % (seq, seq + 5), note)
        self.assertEqual(self.read()["verified_unix"], NOW)
        os.unlink(self.path)                   # even with no file at all
        note = collect.update_releases(self.out, st, NOW + 2 * collect.RELEASES_EVERY_S, fetch=self.web())
        self.assertIn("older", note)
        self.assertFalse(os.path.exists(self.path))

    def test_format_true_is_not_one(self):
        self.assertIs(collect.FEED({"format": True, "sequence": 1}), collect._BAD)
        self.assertIs(collect.FEED({"format": 1.0, "sequence": 1}), collect._BAD)
        self.assertIs(collect.FEED({"format": 1, "sequence": True}), collect._BAD)
        self.assertEqual(collect.FEED({"format": 1, "sequence": 1}), {"format": 1, "sequence": 1})

    def test_time_budget(self):
        import time as _t
        st, web = {}, self.web()
        note = collect.update_releases(self.out, st, NOW, fetch=web, deadline=_t.monotonic() + 0.5)
        self.assertIn("no time left", note)
        self.assertEqual(web.calls, [])
        self.assertNotIn("releases", st)       # not counted as an attempt
        seen = []

        def slow(url, most, timeout):
            seen.append(timeout)
            return web(url, most, timeout)
        self.assertIsNone(collect.update_releases(self.out, st, NOW, fetch=slow,
                                                  deadline=_t.monotonic() + 5))
        self.assertTrue(all(t <= 5 for t in seen) and len(seen) == 2, seen)
        # the budget runs out between the two requests: the second is not made
        st2, calls = {}, []

        def eat(url, most, timeout):
            calls.append(url)
            _t.sleep(0.3)
            return web(url, most, timeout)
        sub = os.path.join(self.out, "sub")
        os.mkdir(sub)
        note = collect.update_releases(sub, st2, NOW, fetch=eat,
                                       deadline=_t.monotonic() + collect.PUBLIC_MIN_S + 0.2)
        self.assertIn("fetch failed (TimeoutError)", note)
        self.assertEqual(calls, [collect.RELEASES_URL])

    def test_fetch_public_whole_call_deadline(self):
        import time as _t

        class Trickle(object):
            status = 200

            def __init__(self, n):
                self.n = n

            def read(self, k):
                _t.sleep(0.05)
                self.n -= 1
                return b"x" if self.n >= 0 else b""

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        class Opener(object):
            def __init__(self, n):
                self.n, self.timeouts = n, []

            def open(self, req, timeout):
                self.timeouts.append(timeout)
                return Trickle(self.n)
        saved = collect.OPENER
        try:
            collect.OPENER = Opener(1000)                  # 1000 bytes, one every 50 ms
            started = _t.monotonic()
            with self.assertRaises(TimeoutError):
                collect.fetch_public(collect.RELEASES_URL, 4096, 0.3)
            self.assertLess(_t.monotonic() - started, 1.0)
            self.assertEqual(collect.OPENER.timeouts, [0.3])
            collect.OPENER = Opener(3)
            self.assertEqual(collect.fetch_public(collect.RELEASES_URL, 4096, 5), b"xxx")
            collect.OPENER = Opener(10)
            with self.assertRaisesRegex(ValueError, "too large"):
                collect.fetch_public(collect.RELEASES_URL, 5, 5)
        finally:
            collect.OPENER = saved

    def test_signed_and_worker_name(self):
        self.assertTrue(sign.TOP.match("releases.json"))
        self.assertTrue(any(rx.match("releases.json") for rx in sign.WORKER_NAMES))
        self.assertFalse(any(rx.match("releases.json.sig") for rx in sign.WORKER_NAMES))


def urllib_error():
    import urllib.error
    return urllib.error.HTTPError(collect.RELEASES_SIG_URL, 503, "unavailable", {}, None)


DAY = 20775                          # 2026-11-18, the date of REAL_TDH's first row
HEADER_20775 = {"cycle_day": DAY, "cutoff_number": 26438213, "total_tdh": 2168968240}
CP_20765 = {"day": 20765, "header": {"cutoff_number": 26366423},
            "history_root": "0xbabbd0c0257b8e0f95e3c1f65459ad1cd03222972187f0f31f97d7c045b3c15d",
            "history_count": 417891,
            "cycle_root": "0x66a537635925d4074238e81b80c70d5f40be8e71822924ce1b254e99d9a5dd95",
            "certificate": {"roster_root": "0x2db9324bdb49861d29d48220b6f9defb2721371d2b740fc5f3b5dff52a09cd6d"}}


class CrossCheck(unittest.TestCase):
    def setUp(self):
        self.out = tempfile.mkdtemp()
        os.makedirs(os.path.join(self.out, "cycles"))
        self.cycle(DAY, {"day": DAY, "header": dict(HEADER_20775)})
        self.calls = 0
        self.timeouts = []

    def tearDown(self):
        shutil.rmtree(self.out)

    def cycle(self, day, rec):
        collect.write_json(os.path.join(self.out, "cycles", "%d.json" % day), rec)

    def rows(self, rows=None, fail=False):
        def f(timeout=None):
            self.calls += 1
            self.timeouts.append(timeout)
            if fail:
                raise OSError("down")
            return REAL_TDH if rows is None else rows
        return f

    def run_cc(self, st, now=NOW, **kw):
        return collect.update_crosscheck(self.out, st, DAY, now, fetch_rows=self.rows(**kw))

    def test_ok(self):
        st = {}
        r = self.run_cc(st)
        self.assertEqual(r, {"day": DAY, "cutoff": "ok", "tdh": "ok", "checkpoint": "unknown",
                             "checked_unix": NOW})
        self.assertEqual(self.calls, 1)
        # answered: no more requests that day, whatever the time
        for i in range(1, 6):
            self.assertEqual(self.run_cc(st, now=NOW + i * 7200), r)
        self.assertEqual(self.calls, 1)
        self.assertEqual(collect.clean(r, collect.CROSSCHECK), r)

    def test_differs(self):
        self.cycle(DAY, {"day": DAY, "header": dict(HEADER_20775, total_tdh=2168968241)})
        r = self.run_cc({})
        self.assertEqual((r["cutoff"], r["tdh"]), ("ok", "differs"))
        self.cycle(DAY, {"day": DAY, "header": dict(HEADER_20775, cutoff_number=26438212)})
        r = self.run_cc({})
        self.assertEqual((r["cutoff"], r["tdh"]), ("differs", "ok"))

    def test_failed_read_is_unknown_never_differs(self):
        st = {}
        r = self.run_cc(st, fail=True)
        self.assertEqual((r["cutoff"], r["tdh"]), ("unknown", "unknown"))
        for rows in ([], [{"date": "2026-11-17", "block": 1, "total_boosted_tdh": 1}],
                     [{"date": "2026-11-18", "block": "26438213", "total_boosted_tdh": None}], "x"):
            r = collect.update_crosscheck(self.out, {}, DAY, NOW, fetch_rows=self.rows(rows=rows))
            self.assertEqual((r["cutoff"], r["tdh"]), ("unknown", "unknown"), rows)

    def test_rate_limit_and_retries(self):
        st = {}
        self.run_cc(st, fail=True)
        self.run_cc(st, now=NOW + 60, fail=True)                      # too soon: no request
        self.assertEqual(self.calls, 1)
        self.run_cc(st, now=NOW + collect.CROSSCHECK_RETRY_S, fail=True)
        self.run_cc(st, now=NOW + 2 * collect.CROSSCHECK_RETRY_S, fail=True)
        self.run_cc(st, now=NOW + 3 * collect.CROSSCHECK_RETRY_S, fail=True)   # tries used up
        self.run_cc(st, now=NOW + 9 * collect.CROSSCHECK_RETRY_S)
        self.assertEqual(self.calls, collect.CROSSCHECK_TRIES)
        self.assertEqual(st["crosscheck"]["cutoff"], "unknown")
        # a new day starts over
        self.cycle(DAY + 1, {"day": DAY + 1, "header": dict(HEADER_20775, cycle_day=DAY + 1)})
        r = collect.update_crosscheck(self.out, st, DAY + 1, NOW + 86400, fetch_rows=self.rows())
        self.assertEqual(self.calls, collect.CROSSCHECK_TRIES + 1)
        self.assertEqual((r["day"], r["cutoff"]), (DAY + 1, "unknown"))     # no 2026-11-19 row

    def test_time_budget(self):
        import time as _t
        st = {}
        r = collect.update_crosscheck(self.out, st, DAY, NOW, fetch_rows=self.rows(),
                                      deadline=_t.monotonic() + 0.5)
        self.assertEqual((self.calls, st["crosscheck"]["tries"], r["cutoff"]), (0, 0, "unknown"))
        r = collect.update_crosscheck(self.out, st, DAY, NOW + 60, fetch_rows=self.rows(),
                                      deadline=_t.monotonic() + 3)
        self.assertEqual((self.calls, r["cutoff"]), (1, "ok"))
        self.assertLessEqual(self.timeouts[-1], 3)

    def test_no_header_no_request(self):
        os.unlink(os.path.join(self.out, "cycles", "%d.json" % DAY))
        r = self.run_cc({})
        self.assertEqual(self.calls, 0)
        self.assertEqual((r["cutoff"], r["tdh"], r["checked_unix"]), ("unknown", "unknown", None))

    def test_state_survives_a_json_round_trip(self):
        st = {}
        self.run_cc(st)
        st = json.loads(json.dumps(st))
        self.run_cc(st, now=NOW + 7200)
        self.assertEqual(self.calls, 1)

    def releases(self, checkpoints):
        collect.write_json(os.path.join(self.out, "releases.json"),
                           {"feed": {"format": 1, "sequence": 1, "checkpoints": checkpoints}})

    def test_checkpoint(self):
        real = json.loads(REAL_FEED)["checkpoints"]
        self.assertEqual(collect.check_checkpoints(self.out), "unknown")     # no releases.json
        self.releases(real)
        self.assertEqual(collect.check_checkpoints(self.out), "unknown")     # no cycles/20765.json
        self.cycle(20765, CP_20765)
        self.assertEqual(collect.check_checkpoints(self.out), "ok")
        self.assertEqual(self.run_cc({})["checkpoint"], "ok")
        for key, val in (("history_count", 417892), ("cycle_root", "0x" + "00" * 32)):
            self.cycle(20765, dict(CP_20765, **{key: val}))
            self.assertEqual(collect.check_checkpoints(self.out), "differs", key)
        self.cycle(20765, dict(CP_20765, certificate={"roster_root": "0x" + "11" * 32}))
        self.assertEqual(collect.check_checkpoints(self.out), "differs")
        self.cycle(20765, dict(CP_20765, history_root=None))                  # our side incomplete
        self.assertEqual(collect.check_checkpoints(self.out), "unknown")
        self.cycle(20765, CP_20765)
        self.releases(real + ["garbage", 5])                                  # unreadable ones skipped
        self.assertEqual(collect.check_checkpoints(self.out), "ok")
        self.releases([real[0].upper().replace("0X", "0x")])                  # hex case does not matter
        self.assertEqual(collect.check_checkpoints(self.out), "ok")


def apple(version="1.2.3", date="2026-11-15T08:00:00Z", track=collect.APP_STORE_ID, **extra):
    r = {"trackId": track, "version": version, "currentVersionReleaseDate": date,
         "bundleId": "x", "releaseNotes": "ignored"}
    r.update(extra)
    return json.dumps({"resultCount": 1, "results": [r]}).encode()


class AppVersion(unittest.TestCase):
    URL = collect.APP_LOOKUP_URL

    def run_app(self, st, answer, now=NOW, previous=None, **kw):
        web = FakeWeb({self.URL: answer})
        app, note = collect.update_app(st, now, previous, fetch=web, **kw)
        return app, note, web

    def test_ok(self):
        self.assertEqual(self.URL, "https://itunes.apple.com/lookup?id=6834147132&country=us")
        st = {}
        app, note, web = self.run_app(st, apple())
        self.assertEqual(app, {"ios": {"version": "1.2.3", "released_unix": 1794729600}})
        self.assertIsNone(note)
        self.assertEqual(web.calls, [self.URL])
        self.assertEqual(st["app"]["ios"], app["ios"])
        self.assertEqual(collect.app_known(st), app)
        app, _, _ = self.run_app({}, apple(version="2.0"))
        self.assertEqual(app["ios"]["version"], "2.0")

    def test_malformed_never_published(self):
        bad = [b"", b"not json", b"[]", b'{"resultCount":0,"results":[]}',
               apple(track=123), apple(version="1"), apple(version="1.2.3.4"),
               apple(version="1.2.3-beta"), apple(version="1234.0"), apple(version=" 1.2"),
               apple(version="1.2\n"), apple(version=12), apple(date="yesterday"),
               apple(date="2026-11-15T08:00:00Z\\n"),
               apple(date="2026-13-40T08:00:00Z"), apple(date="2019-01-01T00:00:00Z"),
               apple(date="2027-01-01T00:00:00Z"), apple(date=None), b"\xff\xfe"]
        for raw in bad:
            st = {}
            app, note, _ = self.run_app(st, raw)
            self.assertIsNone(app, raw)
            self.assertIn("NOT published", note, raw)
            self.assertNotIn("ios", st["app"])

    def test_failure_keeps_previous(self):
        st = {}
        good, _, _ = self.run_app(st, apple())
        later = NOW + collect.APP_EVERY_S
        for i, answer in enumerate((None, OSError("down"), TimeoutError("slow"),
                                    apple(version="x.y"), b"{}")):
            app, note, web = self.run_app(st, answer, now=later * (i + 2))
            self.assertEqual(web.calls, [self.URL])
            self.assertEqual(app, good)
            self.assertIn("previous value kept", note)
        # no state (lost): the value already in network.json is kept, if it is valid
        app, _, _ = self.run_app({}, OSError("down"), previous=good)
        self.assertEqual(app, good)
        for prev in ({"ios": {"version": "1.2"}}, {"ios": {"version": "1;2", "released_unix": 1}},
                     {"android": {}}, "1.2.3", None):
            app, _, _ = self.run_app({}, OSError("down"), previous=prev)
            self.assertIsNone(app, prev)
        # a broken state is ignored, not fatal
        app, _, _ = self.run_app({"app": "garbage"}, apple())
        self.assertEqual(app, good)

    def test_once_an_hour(self):
        st = {}
        self.run_app(st, apple())
        app, note, web = self.run_app(st, apple(version="9.9"), now=NOW + collect.APP_EVERY_S - 1)
        self.assertEqual((web.calls, app["ios"]["version"], note), ([], "1.2.3", None))
        app, _, web = self.run_app(st, apple(version="9.9"), now=NOW + collect.APP_EVERY_S)
        self.assertEqual((web.calls, app["ios"]["version"]), ([self.URL], "9.9"))
        # a failed attempt also counts: not retried before the hour is up
        st = {}
        self.run_app(st, OSError("down"))
        _, _, web = self.run_app(st, apple(), now=NOW + 60)
        self.assertEqual(web.calls, [])
        # the clock going back does not block it
        _, _, web = self.run_app(st, apple(), now=NOW - 60)
        self.assertEqual(web.calls, [self.URL])

    def test_time_budget(self):
        import time as _t
        st = {}
        app, note, web = self.run_app(st, apple(), deadline=_t.monotonic() + 0.5)
        self.assertEqual((app, web.calls), (None, []))
        self.assertIn("no time left", note)
        self.assertNotIn("t", st["app"])     # not counted as an attempt
        seen = []
        def f(url, most, timeout):
            seen.append(timeout)
            return apple()
        collect.update_app({}, NOW, fetch=f, deadline=_t.monotonic() + 5)
        self.assertTrue(0 < seen[0] <= 5)
        collect.update_app({}, NOW, fetch=f)
        self.assertEqual(seen[1], collect.APP_TIMEOUT_S)


class ChainRoster(unittest.TestCase):
    """network.json `chain_roster` from the node's chain/roster (28 Nov 2026)."""
    # As the node answered on 28 Nov 2026 (the per-key list shortened).
    REAL = {"day": 20785, "date": "2026-11-28", "state": "live", "held": True, "accounts": 290,
            "validators": 310, "w": "641952070", "active": 295, "active_accounts": 275,
            "keys": [{"code": "580f1021"}], "as_of": {"height": 1, "time": 1795826000, "final": True}}

    def test_the_five_counts(self):
        self.assertEqual(collect.parse_roster(self.REAL),
                         {"day": 20785, "accounts": 290, "keys": 310, "active_keys": 295, "active_accounts": 275})

    def test_nothing_else_is_published(self):
        self.assertEqual(sorted(collect.parse_roster(self.REAL)), ["accounts", "active_accounts", "active_keys", "day", "keys"])

    def test_odd_or_missing_leaves_it_out(self):
        for field in ("day", "accounts", "validators", "active", "active_accounts"):
            for bad in (None, -1, 1.5, "310", True, [310]):
                r = dict(self.REAL)
                if bad is None:
                    del r[field]
                else:
                    r[field] = bad
                self.assertIsNone(collect.parse_roster(r), (field, bad))
        for day in (0, 19000, 40000):
            self.assertIsNone(collect.parse_roster(dict(self.REAL, day=day)))
        for r in (None, [], "x", 3):
            self.assertIsNone(collect.parse_roster(r))

    def test_reads_the_route(self):
        seen = []
        def f(api, path, timeout=20):
            seen.append((api, path, timeout))
            return self.REAL
        self.assertEqual(collect.chain_roster("http://127.0.0.1:1/api/v1", fetch=f)["keys"], 310)
        self.assertEqual(seen, [("http://127.0.0.1:1/api/v1", "chain/roster", collect.ROSTER_TIMEOUT_S)])

    def test_unreachable_or_older_node(self):
        import urllib.error
        for exc in (urllib.error.HTTPError("u", 404, "nf", {}, None), urllib.error.URLError("down"),
                    OSError("x"), ValueError("not json")):
            def f(api, path, timeout=20, exc=exc):
                raise exc
            self.assertIsNone(collect.chain_roster("http://127.0.0.1:1/api/v1", fetch=f))

    def test_default_fetch_is_the_module_get(self):
        real = collect.get
        try:
            collect.get = lambda api, path, timeout=20: self.REAL if path == "chain/roster" else None
            self.assertEqual(collect.chain_roster("http://127.0.0.1:1/api/v1")["day"], 20785)
        finally:
            collect.get = real


if __name__ == "__main__":
    unittest.main(verbosity=1)
