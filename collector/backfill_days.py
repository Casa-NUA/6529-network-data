#!/usr/bin/env python3
"""One-time: rebuild days/<day>.json for the days before the collector kept them.

Walks the history of docs/data/validators.json in a clone of this repository and, for
every cycle day, takes the LAST validators.json committed for that day. Each one goes
through the same schema as the live files (collect.day_record), so nothing that the
collector would not publish today is written. Handles come from the collector's
handle cache (null where it has none yet). Today (the newest day in the history) is
left out: it is still being collected. A day file that already exists is kept as it is.

Read-only toward the clone (git log / git cat-file); writes only into --out.

    python3 -I backfill_days.py --repo /path/to/clone --out /path/to/state/data \
        [--state /path/to/state/collector-state.json]
"""
import argparse
import json
import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import collect  # noqa: E402

PATH = "docs/data/validators.json"
GIT = ["git", "-c", "core.hooksPath=/dev/null", "-c", "core.fsmonitor=false"]


def commits(repo):
    out = subprocess.run(GIT + ["-C", repo, "log", "--format=%H", "--", PATH],
                         capture_output=True, text=True, check=True).stdout
    return [h for h in out.split() if len(h) == 40]   # newest first


def blobs(repo, shas):
    """(sha, parsed JSON or None) for each commit's validators.json, via one cat-file."""
    p = subprocess.Popen(GIT + ["-C", repo, "cat-file", "--batch"],
                         stdin=subprocess.PIPE, stdout=subprocess.PIPE)
    try:
        for sha in shas:
            p.stdin.write(("%s:%s\n" % (sha, PATH)).encode())
            p.stdin.flush()
            head = p.stdout.readline().decode().split()
            if len(head) < 2 or head[1] == "missing":
                yield sha, None
                continue
            size = int(head[2])
            body = p.stdout.read(size)
            p.stdout.read(1)               # the newline after each object
            try:
                yield sha, json.loads(body.decode("utf-8")) if size <= 16 * 1024 * 1024 else None
            except ValueError:
                yield sha, None
    finally:
        p.stdin.close()
        p.wait()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", required=True, help="a clone of 6529-network-data (read only)")
    ap.add_argument("--out", required=True, help="the collector's data directory")
    ap.add_argument("--state", default=None, help="the collector's state file (for handles)")
    a = ap.parse_args()
    out = os.path.abspath(a.out)
    state_path = os.path.abspath(a.state or os.path.join(os.path.dirname(out), "collector-state.json"))
    state = collect.load_state(state_path)          # read only: the cache is not changed here

    last = {}                                         # day -> (sha, validators) newest first wins
    shas = commits(a.repo)
    for sha, vals in blobs(a.repo, shas):
        if not isinstance(vals, dict):
            continue
        day = vals.get("cycle_day")
        if isinstance(day, int) and not isinstance(day, bool) and day not in last:
            last[day] = (sha, vals)
    if not last:
        print("no validators.json in the history", file=sys.stderr)
        return 1
    today = max(last)
    written, kept, bad = [], [], []
    for day in sorted(last):
        if day >= today:
            continue
        rec = collect.day_record(last[day][1], state)
        if not rec or rec["day"] != day:
            bad.append(day)
            continue
        (written if collect.write_day(out, rec) else kept).append(day)
    days = collect.write_days_index(out, today)
    for what in sorted(collect._dropped - collect.KNOWN_UNPUBLISHED):
        print("not published: %s" % what, file=sys.stderr)
    print("backfill: %d commits read, days %s; written %s, kept %s, unusable %s; index %s" % (
        len(shas), sorted(last), written, kept, bad, days))
    return 0


if __name__ == "__main__":
    sys.exit(main())
