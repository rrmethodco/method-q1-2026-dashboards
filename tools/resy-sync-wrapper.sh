#!/bin/bash
# resy-sync-wrapper.sh — orchestrator for the daily Resy CSV sync.
# Invoked by launchd (com.methodco.resy-sync.plist).
#
# Steps:
#   1. cd into the repo (path templated by install-resy-sync.sh)
#   2. git pull --rebase to absorb any nightly autocommits
#   3. run resy_csv_sync.py (downloads CSVs, ingests into data/*.json)
#   4. if data/*.json changed, commit + push
#
# All output → /Users/rossrichardson/Library/Logs/method-resy-sync.log
# (rotated weekly via the macOS newsyslog default).

set -uo pipefail

REPO_DIR="${REPO_DIR:-__REPO_DIR__}"
LOG_FILE="${LOG_FILE:-$HOME/Library/Logs/method-resy-sync.log}"
PYTHON="${PYTHON:-/usr/bin/python3}"
PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
export PATH

ts() { date -u +"%Y-%m-%dT%H:%M:%SZ"; }

# Tee everything to the log file. Rolling delete >5MB so it doesn't grow forever.
exec >>"$LOG_FILE" 2>&1
if [[ -f "$LOG_FILE" ]] && [[ $(stat -f%z "$LOG_FILE") -gt 5242880 ]]; then
  : > "$LOG_FILE"
fi

echo
echo "=== resy-sync-wrapper $(ts) ==="

if [[ ! -d "$REPO_DIR" ]]; then
  echo "ERROR: REPO_DIR=$REPO_DIR does not exist"
  exit 1
fi

cd "$REPO_DIR" || exit 1

echo "[$(ts)] git pull --rebase origin main"
git fetch origin main >/dev/null 2>&1
git checkout main >/dev/null 2>&1
git pull --rebase origin main

echo "[$(ts)] running resy_csv_sync.py"
"$PYTHON" tools/resy_csv_sync.py
SCRAPE_RC=$?
if [[ $SCRAPE_RC -ne 0 ]]; then
  echo "[$(ts)] resy_csv_sync.py exited $SCRAPE_RC — bailing without commit"
  exit $SCRAPE_RC
fi

echo "[$(ts)] checking for data changes"
git add data/*.json 2>/dev/null
if git diff --cached --quiet; then
  echo "[$(ts)] no Resy data changes — done"
  exit 0
fi

# Rebase-and-retry push pattern (mirrors guest-sync.yml). Three attempts
# with linear backoff against concurrent toast-sync / budget-sync.
STAMP="$(date -u +%Y-%m-%dT%H:%MZ)"
git commit -m "chore(data): local Resy CSV sync ${STAMP}"
for i in 1 2 3; do
  if git push origin main; then
    echo "[$(ts)] push succeeded (attempt $i)"
    exit 0
  fi
  echo "[$(ts)] push rejected (attempt $i) — pulling and retrying"
  git pull --rebase origin main || {
    echo "[$(ts)] rebase failed; aborting to avoid clobbering remote"
    exit 1
  }
  sleep $((i * 5))
done
echo "[$(ts)] push failed after 3 attempts"
exit 1
