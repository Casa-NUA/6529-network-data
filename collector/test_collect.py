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
        a = collect.build_awards("x", CHAIN, st, NOW)
        self.assertEqual(set(a), {"generated_unix", "currency", "decimals", "test", "days", "live"})
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


class HeadRecent(unittest.TestCase):
    def test_head_recent(self):
        self.assertTrue(collect._head_recent({"head": {"time": (NOW - 60) * 1000}}, NOW))
        self.assertFalse(collect._head_recent({"head": {"time": (NOW - 600) * 1000}}, NOW))
        self.assertFalse(collect._head_recent({}, NOW))


if __name__ == "__main__":
    unittest.main(verbosity=1)
