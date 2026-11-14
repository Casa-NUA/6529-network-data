#!/bin/bash
# Collect public network data from the local validator and push it when it changed.
# Usage: publish.sh /path/to/the-publishing-clone   (run every 5 minutes by launchd)
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

# Collect and sign in our own folder.
STATE="$HERE/state/data"
mkdir -p "$STATE"
find "$STATE" \( -name '*.tmp' -o -name '.DS_Store' \) -type f -delete
$PY "$HERE/collect.py" --out "$STATE"
KEY="${SIGNING_KEY:-$HERE/signing-key.pem}"
if [ -f "$KEY" ]; then
  $PY "$HERE/sign.py" --data "$STATE" --key "$KEY" >/dev/null
elif [ -e "$STATE/signed" ]; then
  echo "refusing: signed files exist but the signing key is missing ($KEY)" >&2; exit 1
fi

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

# network.json and chain.json carry the collection time and the chain's head, so they
# (and their signed copies) change on every run. Commit only when something else
# changed, or once an hour so the data's freshness stays visible.
if "${G[@]}" diff --cached --quiet -- docs/data ':!docs/data/network.json' ':!docs/data/chain.json' \
     ':!docs/data/signed/network.json' ':!docs/data/signed/chain.json'; then
  last=$("${G[@]}" log -1 --format=%ct -- docs/data/network.json 2>/dev/null || echo 0)
  if [ $(( $(date +%s) - ${last:-0} )) -lt 3600 ]; then
    "${G[@]}" reset --quiet --hard origin/main
    exit 0
  fi
fi
"${G[@]}" diff --cached --quiet && exit 0
"${G[@]}" -c commit.gpgsign=false commit --quiet --no-verify -m "data: $(date -u +%Y-%m-%dT%H:%MZ)"
"${G[@]}" push --quiet --no-verify origin HEAD:main
echo "pushed $(date -u +%H:%MZ)"
