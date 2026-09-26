#!/bin/bash
# MindGod shadow watchdog: Discord heartbeat + stall alert.
# Run every 5 minutes from cron:
#   */5 * * * * /path/to/muse-trader/deploy/watchdog.sh
#
# - Posts "alive" to Discord every HEARTBEAT_EVERY_MIN (default 30).
# - Alerts when the container is down or the DB has not been written
#   for STALL_AFTER_MIN (default 12): a silent crash must never look
#   like a quiet slate.
# Reads the webhook from ../.env (prefers DISCORD_WEBHOOK_OPS, falls
# back to DISCORD_WEBHOOK_URL). Never prints secrets.
set -u

DIR="$(cd "$(dirname "$0")" && pwd)"
ENV_FILE="$DIR/../.env"
STATE_DIR="$DIR/.watchdog"
DB="$DIR/data/mindgod.db"
HEARTBEAT_EVERY_MIN="${HEARTBEAT_EVERY_MIN:-30}"
STALL_AFTER_MIN="${STALL_AFTER_MIN:-12}"

mkdir -p "$STATE_DIR"

webhook() {
  local w=""
  if [ -f "$ENV_FILE" ]; then
    w="$(grep -E '^DISCORD_WEBHOOK_OPS=' "$ENV_FILE" | cut -d= -f2- | tr -d ' ')"
    if [ -z "$w" ]; then
      w="$(grep -E '^DISCORD_WEBHOOK_URL=' "$ENV_FILE" | cut -d= -f2- | tr -d ' ')"
    fi
  fi
  printf '%s' "$w"
}

send() { # $1 = message text
  local hook
  hook="$(webhook)"
  [ -n "$hook" ] || return 0
  local payload
  payload="$(python3 -c 'import json,sys; print(json.dumps({"content": sys.stdin.read()}))' <<< "$1")"
  curl -s -m 15 -H "Content-Type: application/json" -d "$payload" "$hook" > /dev/null
}

container_running() {
  docker compose -f "$DIR/docker-compose.yml" ps -q mindgod 2>/dev/null | grep -q .
}

db_age_min() {
  if [ ! -f "$DB" ]; then
    echo 999999
    return
  fi
  local mtime now
  if stat -c %Y "$DB" >/dev/null 2>&1; then
    mtime="$(stat -c %Y "$DB")"   # Linux
  else
    mtime="$(stat -f %m "$DB")"   # macOS
  fi
  now="$(date +%s)"
  echo $(( (now - mtime) / 60 ))
}

min_since() { # $1 = state file; prints minutes since it was touched, or huge
  if [ ! -f "$1" ]; then
    echo 999999
    return
  fi
  local mtime now
  if stat -c %Y "$1" >/dev/null 2>&1; then
    mtime="$(stat -c %Y "$1")"
  else
    mtime="$(stat -f %m "$1")"
  fi
  now="$(date +%s)"
  echo $(( (now - mtime) / 60 ))
}

DOWN_FLAG="$STATE_DIR/container_down"
STALL_FLAG="$STATE_DIR/stall_alerted"
HB_SENT="$STATE_DIR/last_heartbeat"

# 1. Container check.
if ! container_running; then
  if [ ! -f "$DOWN_FLAG" ]; then
    send "🚨 MindGod shadow: container is DOWN (watchdog $(date -u +%H:%M UTC))."
    touch "$DOWN_FLAG"
  fi
  exit 0
fi
rm -f "$DOWN_FLAG"

# 2. Tick freshness check: the loop writes observations every minute, so a
#    DB untouched for STALL_AFTER_MIN means the ticks stopped.
age="$(db_age_min)"
if [ "$age" -ge "$STALL_AFTER_MIN" ]; then
  if [ ! -f "$STALL_FLAG" ]; then
    send "🚨 MindGod shadow: no DB writes for ${age}m — ticks may have stalled (watchdog $(date -u +%H:%M UTC))."
    touch "$STALL_FLAG"
  fi
  exit 0
fi
rm -f "$STALL_FLAG"

# 3. Heartbeat.
if [ "$(min_since "$HB_SENT")" -ge "$HEARTBEAT_EVERY_MIN" ]; then
  calls=""
  if command -v sqlite3 >/dev/null 2>&1 && [ -f "$DB" ]; then
    calls="$(sqlite3 "$DB" "select count(*) from calls;" 2>/dev/null)"
    [ -n "$calls" ] && calls=", ${calls} calls recorded"
  fi
  send "💓 MindGod shadow alive (last tick ~${age}m ago${calls})."
  touch "$HB_SENT"
fi
