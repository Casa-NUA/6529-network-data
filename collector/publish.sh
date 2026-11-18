#!/bin/bash
# Collect public network data from the local validator, send the signed files to the data
# Worker, and push them to GitHub at most every 4 minutes.
# Usage: publish.sh /path/to/the-publishing-clone   (run every 2 minutes by launchd)
#
# Each run: collect and sign (in this folder's state/data), then POST the signed files that
# changed to the data Worker (WORKER_URL below; data-worker/ in the repository), then, if the
# last push was at least 4 minutes ago (state/last-push), commit and push to GitHub, which
# stays the public archive and the apps' fallback. A failed POST is logged and never stops the
# run; a git failure never stops the POST (it comes first).
#
# INSTALL THIS FOLDER OUTSIDE THE CLONE. This script, collect.py and sign.py must run
# from a fixed copy (for example ~/Library/Application Support/6529-network-data/),
# never from the publishing clone. The clone is reset to origin/main on every run, so
# its content is whatever was last pushed: running code from it, or reading files
# from it, would let anyone able to push to the repository act on this machine.
# So this script:
#   - refuses to start from inside the clone or from any git work tree;
#   - collects and signs in its OWN folder ("state/data" next to this script), which
#     nothing from the repository ever enters;
#   - then replaces the clone's docs/data with a copy of that folder and pushes.
# To update the fixed copy, review the change and copy the three files again by hand.
#
# The commit identity must be set once in the clone (git config user.name / user.email).
#
# Signing: if signing-key.pem sits next to this script (or SIGNING_KEY names it), the
# data files are signed into state/data/signed/ (see sign.py). Once signed files exist,
# a missing key stops the run rather than publishing fresh data beside stale signatures.
set -euo pipefail
REPO="${1:?usage: publish.sh /path/to/the-publishing-clone}"
HERE="$(cd "$(dirname "$0")" && pwd -P)"
REPO="$(cd "$REPO" && pwd -P)"
PY="/usr/bin/python3 -I -B"
RUN_START=$(date +%s)
# The data Worker (pinned here; nothing secret is sent: the signatures are the credential).
WORKER_URL="https://monitor6529-data.casanua.workers.dev/v1/data"
PUSH_EVERY=240                       # seconds between GitHub pushes, at least

# The fixed copy must be real files, outside the clone and outside any git work tree.
for f in publish.sh collect.py sign.py; do
  if [ -L "$HERE/$f" ] || [ ! -f "$HERE/$f" ]; then
    echo "refusing: $HERE/$f must be a real file (not a link)" >&2; exit 1
  fi
done
d="$HERE"
while :; do
  if [ "$d" -ef "$REPO" ]; then
    echo "refusing to run from inside the publishing clone ($HERE); install the collector folder outside it" >&2; exit 1
  fi
  [ "$d" = "/" ] && break
  d="$(dirname "$d")"
done
if git -C "$HERE" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  echo "refusing to run from inside a git work tree ($HERE)" >&2; exit 1
fi

cd "$REPO"
export GIT_TERMINAL_PROMPT=0
export TZ=UTC
unset PYTHONPATH PYTHONSTARTUP PYTHONHOME || true
# Nothing the repository contains may run or redirect anything here.
G=(git -c core.hooksPath=/dev/null -c core.fsmonitor=false -c core.symlinks=false
   -c submodule.recurse=false -c fetch.recurseSubmodules=false
   -c filter.lfs.smudge= -c filter.lfs.clean= -c filter.lfs.process= -c filter.lfs.required=false)

if [ -z "$(git config --local user.name || true)" ] || [ -z "$(git config --local user.email || true)" ]; then
  echo "no repo-local git identity; refusing to commit" >&2
  exit 1
fi

# Collect and sign in our own folder.
STATE="$HERE/state/data"
mkdir -p "$STATE"
find "$STATE" \( -name '*.tmp' -o -name '.DS_Store' \) -type f -delete
# The collector's own state (handle cache, the day's last validator list, settled award
# days) sits NEXT TO state/data, never inside it: everything inside state/data is published.
$PY "$HERE/collect.py" --out "$STATE" --state "$HERE/state/collector-state.json"
KEY="${SIGNING_KEY:-$HERE/signing-key.pem}"
if [ -f "$KEY" ]; then
  $PY "$HERE/sign.py" --data "$STATE" --key "$KEY" >/dev/null
elif [ -e "$STATE/signed" ]; then
  echo "refusing: signed files exist but the signing key is missing ($KEY)" >&2; exit 1
fi

# Send the signed files that changed to the data Worker. sign.py --batch writes the body (each
# envelope's exact text) and the record to keep if the Worker accepts it (204); the record
# (state/worker-posted.json) and the body sit next to state/data, never inside it. Short
# timeouts; any failure is one log line and the run goes on.
if [ -d "$STATE/signed" ]; then
  BODY="$HERE/state/worker-body.json"
  POSTED="$HERE/state/worker-posted.json"
  rm -f "$BODY" "$POSTED.next"
  if out="$($PY "$HERE/sign.py" --data "$STATE" --batch "$BODY" --posted "$POSTED" 2>&1)"; then
    n="${out%% *}"
    case "$out" in *" | "*) echo "worker: left out: ${out#* | } $(date -u +%H:%MZ)" ;; esac
    if [ -s "$BODY" ]; then
      # -q: no ~/.curlrc; https only, no proxy, no redirects followed.
      code="$(/usr/bin/curl -q -sS --proto '=https' --noproxy '*' --connect-timeout 5 --max-time 25 \
        -o /dev/null -w '%{http_code}' -X POST -H 'content-type: application/json' \
        --data-binary "@$BODY" "$WORKER_URL" 2>/dev/null)" || true
      if [ "$code" = "204" ] && [ -f "$POSTED.next" ]; then
        mv -f "$POSTED.next" "$POSTED"
      else
        echo "worker: POST of $n files failed (${code:-000}) $(date -u +%H:%MZ)"
      fi
    fi
  else
    echo "worker: no batch built: $(printf '%s' "$out" | tr '\n' ' ' | cut -c1-200) $(date -u +%H:%MZ)"
  fi
  rm -f "$BODY" "$POSTED.next"
fi

"${G[@]}" rebase --abort >/dev/null 2>&1 || true
"${G[@]}" fetch --quiet origin main
# The published tree may hold only ordinary files: no links, no submodules, and none
# of the files through which git itself can be told to run things.
bad="$("${G[@]}" ls-tree -r origin/main | awk '$1 != "100644" && $1 != "100755" {print}')"
names="$("${G[@]}" ls-tree -r --name-only origin/main)"
if [ -n "$bad" ] || grep -Eqi '(^|/)\.(gitattributes|gitmodules|lfsconfig)$' <<<"$names"; then
  echo "refusing: origin/main contains a link, a submodule or a git control file" >&2; exit 1
fi
# On a case-insensitive disk "Docs/" and "docs/" are one folder: allow only the exact
# spelling docs/data/ for anything under a folder named like "docs".
if grep -i '^docs/' <<<"$names" | grep -qv '^docs/data/'; then
  echo "refusing: origin/main has files under a docs folder outside docs/data/" >&2; exit 1
fi
"${G[@]}" reset --quiet --hard origin/main

# Publish a copy of it.
if [ -L docs ] || [ -L docs/data ]; then
  echo "refusing: docs or docs/data is a link" >&2; exit 1
fi
mkdir -p docs
rm -rf "$REPO/docs/data"
cp -R "$STATE" "$REPO/docs/data"
"${G[@]}" add -A -f docs/data
# What is staged must be exactly what was collected, whatever the repository's own
# ignore rules or file names say.
if [ "$("${G[@]}" ls-files docs/data | sed 's|^docs/data/||' | LC_ALL=C sort)" != "$(cd "$STATE" && find . -type f | sed 's|^\./||' | LC_ALL=C sort)" ]; then
  echo "refusing: the files staged for publishing are not the files collected" >&2
  "${G[@]}" reset --quiet --hard origin/main
  exit 1
fi

"${G[@]}" diff --cached --quiet && exit 0

# GitHub at most every PUSH_EVERY seconds (the Worker already has this run's files). The time
# is the start of the run that last pushed, kept in state/last-push (next to state/data, never
# in the clone); with 2-minute runs a push is made every second run (every 4 minutes). Without
# that file (first run of this version), the last commit of network.json counts.
LAST_PUSH="$HERE/state/last-push"
if [ -f "$LAST_PUSH" ] && [ ! -L "$LAST_PUSH" ]; then
  last="$(head -c 20 "$LAST_PUSH" | tr -cd '0-9' | head -c 12)"
else
  last="$("${G[@]}" log -1 --format=%ct -- docs/data/network.json 2>/dev/null || true)"
fi
last="${last:-0}"
# (5 s of slack: launchd may start a run a second early, which would otherwise skip a push.)
if [ "$RUN_START" -ge "$last" ] && [ $(( RUN_START - last )) -lt $(( PUSH_EVERY - 5 )) ]; then
  "${G[@]}" reset --quiet --hard origin/main
  exit 0
fi
"${G[@]}" -c commit.gpgsign=false commit --quiet --no-verify -m "data: $(date -u +%Y-%m-%dT%H:%MZ)"
"${G[@]}" push --quiet --no-verify origin HEAD:main
echo "$RUN_START" > "$LAST_PUSH.tmp" && mv -f "$LAST_PUSH.tmp" "$LAST_PUSH"
echo "pushed $(date -u +%H:%MZ)"
