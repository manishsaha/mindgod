#!/bin/bash
# Odds API credit check: one cheap probe request, then project weekend burn.
# Run AFTER filling in ODDS_API_KEY in ../.env.
# The probe uses minimal params (1 region, 1 market, 1 book) to cost as
# little as possible; the response headers reveal the true per-request cost.
set -u

DIR="$(cd "$(dirname "$0")" && pwd)"
ENV_FILE="$DIR/../.env"

KEY="$(grep -E '^ODDS_API_KEY=' "$ENV_FILE" 2>/dev/null | cut -d= -f2- | sed 's/^ *//;s/ *$//')"
if [ -z "$KEY" ]; then
  echo "ODDS_API_KEY is not set in $ENV_FILE"
  exit 1
fi

TMP="$(mktemp)"
curl -s -m 30 -D "$TMP" -o /dev/null \
  "https://api.the-odds-api.com/v4/sports/americanfootball_nfl/odds?apiKey=${KEY}&regions=us&markets=h2h&oddsFormat=american&bookmakers=draftkings"

echo "--- quota headers from probe ---"
grep -i "^x-requests-" "$TMP" || echo "(no quota headers returned)"
rm -f "$TMP"

echo ""
echo "--- weekend projection ---"
echo "Sportsbook refresh every 300s = 12 requests/hour, 1 sport (NFL)."
echo "Sat 8pm -> Mon 7am ET is ~35h, so ~420 requests at the probe's"
echo "observed cost. Compare against x-requests-remaining above."
echo "Knob: raise polling.sportsbook_interval_s in config.week4.yaml"
echo "to cut burn (e.g. 600s halves it)."
