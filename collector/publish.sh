#!/bin/bash
# Collect public network data from the local validator and push it when it changed.
# Usage: publish.sh /path/to/this-repo-clone   (run every 10 minutes by launchd)
#
# The clone is a publishing copy: every run resets it to origin/main, so do not edit
# files in it by hand. The commit identity must be set once in the clone
# (git config user.name / user.email) so nothing personal goes into the history.
set -euo pipefail
REPO="${1:?usage: publish.sh /path/to/repo-clone}"
cd "$REPO"
export GIT_TERMINAL_PROMPT=0

export TZ=UTC
if [ -z "$(git config --local user.name || true)" ] || [ -z "$(git config --local user.email || true)" ]; then
  echo "no repo-local git identity; refusing to commit" >&2
  exit 1
fi

# Start from what is published: the data files are regenerated below.
git rebase --abort >/dev/null 2>&1 || true
git fetch --quiet origin main
git reset --quiet --hard origin/main
git clean -fdq -- docs/data
/usr/bin/python3 collector/collect.py --out docs/data

git add docs/data
# network.json and chain.json carry the collection time and the chain's head, so they
# change on every run. Commit only when something else changed, or once an hour so the
# data's freshness stays visible.
if git diff --cached --quiet -- docs/data ':!docs/data/network.json' ':!docs/data/chain.json'; then
  last=$(git log -1 --format=%ct -- docs/data/network.json 2>/dev/null || echo 0)
  if [ $(( $(date +%s) - ${last:-0} )) -lt 3600 ]; then
    git reset --quiet --hard origin/main
    exit 0
  fi
fi
git diff --cached --quiet && exit 0
git -c commit.gpgsign=false commit --quiet -m "data: $(date -u +%Y-%m-%dT%H:%MZ)"
git push --quiet origin HEAD:main
echo "pushed $(date -u +%H:%MZ)"
