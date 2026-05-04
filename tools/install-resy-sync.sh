#!/bin/bash
# install-resy-sync.sh — one-time setup for the daily Resy CSV launchd job.
#
# Idempotent — re-running re-templates the plist and reloads the agent
# (so changes to the plist template land cleanly).
#
# What this does:
#   1. Verifies python3 + playwright + chromium are installed
#   2. Creates ~/.config/method-dashboards/ for storage state + venue map
#   3. Creates ~/Documents/Method/resy-csvs/ for date-stamped CSV exports
#   4. Templates the plist with this repo's absolute path + $HOME
#   5. Installs to ~/Library/LaunchAgents/com.methodco.resy-sync.plist
#   6. Loads the agent (will fire daily at 06:00 local)

set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PLIST_SRC="$REPO_DIR/tools/com.methodco.resy-sync.plist"
PLIST_DST="$HOME/Library/LaunchAgents/com.methodco.resy-sync.plist"
WRAPPER_SRC="$REPO_DIR/tools/resy-sync-wrapper.sh"
CONFIG_DIR="$HOME/.config/method-dashboards"
CSV_DIR="$HOME/Documents/Method/resy-csvs"
LOG_DIR="$HOME/Library/Logs"

say() { printf '\033[1;36m▸\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m⚠\033[0m %s\n' "$*"; }
fail() { printf '\033[1;31m✗\033[0m %s\n' "$*" >&2; exit 1; }

say "Verifying python3 + playwright"
command -v python3 >/dev/null || fail "python3 not on PATH"
python3 -c "import playwright" 2>/dev/null || {
  warn "playwright not installed — installing"
  python3 -m pip install --user playwright
}
python3 -m playwright install chromium >/dev/null 2>&1 || warn \
  "playwright chromium install failed — re-run manually if needed"

say "Creating directories"
mkdir -p "$CONFIG_DIR" "$CSV_DIR" "$LOG_DIR" "$(dirname "$PLIST_DST")"

# Storage state — copy if it exists in the repo root, else prompt
if [[ ! -f "$CONFIG_DIR/resy-storage-state.json" ]]; then
  if [[ -f "$REPO_DIR/resy-storage-state.json" ]]; then
    cp "$REPO_DIR/resy-storage-state.json" "$CONFIG_DIR/resy-storage-state.json"
    say "Copied storage state from repo root → $CONFIG_DIR/"
  else
    warn "No storage state found at $CONFIG_DIR/resy-storage-state.json"
    warn "Run: python3 tools/refresh_resy_storage.py"
    warn "Then: cp resy-storage-state.json $CONFIG_DIR/ && rm resy-storage-state.json"
  fi
fi

# Venue mapping — pull from GH secret if available, else prompt
if [[ ! -f "$CONFIG_DIR/resy-venues.txt" ]]; then
  if command -v gh >/dev/null 2>&1; then
    say "Fetching venue mapping from GH secret RESY_OS_VENUES"
    # GH won't print secret values, so just leave a template
    cat > "$CONFIG_DIR/resy-venues.txt" <<'EOF'
# Map outlet_id → Resy OS slug (city/venue-name).
# Format: oid=city/slug, one per line OR semicolon-separated.
# Pull current value with: gh secret list (then paste from your records)
lsbr=det/le-supreme
hiroki_phl=pha/hiroki
hiroki_det=det/hiroki-san
kampers=det/kampers
lowland=chs/lowland
mulherins=pha/wm-mulherins-sons
quoin=wld/the-quoin
rosemary_rose=chs/rosemary-rose
vessel=bal/vessel-md
EOF
    warn "Wrote default venue map → $CONFIG_DIR/resy-venues.txt"
    warn "Verify slugs match Resy OS URLs before first run"
  else
    fail "Need either $CONFIG_DIR/resy-venues.txt or gh CLI"
  fi
fi

say "Templating launchd plist"
sed "s|__REPO_DIR__|${REPO_DIR}|g; s|__HOME__|${HOME}|g" "$PLIST_SRC" > "$PLIST_DST"
chmod 644 "$PLIST_DST"

# Same template substitution on the wrapper, in place. Wrapper has
# REPO_DIR fallback to env var so an unsubstituted copy still works
# when launchd injects REPO_DIR from EnvironmentVariables — but
# substituting also lets it run standalone.
sed -i '' "s|__REPO_DIR__|${REPO_DIR}|g" "$WRAPPER_SRC"
chmod +x "$WRAPPER_SRC"

say "Loading launchd agent"
# Unload first so changes to plist take effect on re-install
launchctl unload "$PLIST_DST" 2>/dev/null || true
launchctl load -w "$PLIST_DST"
launchctl list | grep com.methodco.resy-sync || warn "agent not in launchctl list"

cat <<EOF

✓ Installed.

Schedule:    06:00 local, every day
Plist:       $PLIST_DST
Wrapper:     $WRAPPER_SRC
CSVs:        $CSV_DIR/<YYYY-MM-DD>/<outlet>.csv
Log:         $LOG_DIR/method-resy-sync.log

Run now manually:
  launchctl start com.methodco.resy-sync

Watch it run:
  tail -F $LOG_DIR/method-resy-sync.log

Disable:
  launchctl unload $PLIST_DST

Reseed expired storage state (every ~21 days):
  python3 tools/refresh_resy_storage.py
  cp resy-storage-state.json $CONFIG_DIR/
  rm resy-storage-state.json
EOF
