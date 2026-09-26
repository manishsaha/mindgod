# MindGod shadow runbook (weekend of Sep 26-27, 2026)

Runs the service in **paper** mode against NFL week 4 Kalshi moneylines:
alerts fire to Discord, fills are simulated, no real orders. Live execution
is not wired (`execution.mode=live` raises until RFQs exist).

## 1. Secrets (you do this, on this machine)

```bash
cp ../.env.example ../.env
# edit ../.env and fill in:
#   ODDS_API_KEY        the-odds-api.com dashboard
#   DISCORD_WEBHOOK_URL webhook for #calls
```

`.env` is git-ignored. Nothing here ever prints your keys.

## 2. Credit check (after the key is in)

```bash
./credit_check.sh
```

One cheap probe, then a weekend burn projection (12 req/hour at the
default 300s sportsbook refresh). If the remaining quota is tight, raise
`polling.sportsbook_interval_s` in `config.week4.yaml`.

## 3. Start

```bash
docker compose up -d --build
docker compose logs -f   # watch the first ticks land
```

The DB lands at `deploy/data/mindgod.db` on the host (bind mount:
visible, survives container recreation and reboots, not on the
container's temporary disk).

## 4. Watchdog (heartbeat + stall alert)

```bash
chmod +x watchdog.sh credit_check.sh
crontab -e
# add:
*/5 * * * * /ABS/PATH/TO/muse-trader/deploy/watchdog.sh
```

Posts 💓 alive to Discord every 30 min (uses `DISCORD_WEBHOOK_OPS` if
set, else the #calls webhook) and 🚨 the moment the container is down
or the DB goes 12+ minutes without a write. A silent crash never looks
like a quiet slate.

Keep this machine awake while shadowing: `caffeinate -dimsu` (macOS).

## 5. What "working" looks like

- `docker compose logs` shows ticks every ~60s.
- Sunday's games produce calls -> Discord alerts -> paper fills.
- Closing lines captured at each game's start; settlements as games end.
- Monday: first grades via the recompute CLI against a **copy** of
  `data/mindgod.db` (copy-first, dry-run, review, then apply).

## 6. Stop / wipe

```bash
docker compose down        # stop (DB kept in deploy/data/)
rm -rf data               # wipe the shadow DB entirely
```

## Notes

- Only the 15 week-4 home moneylines are registered. Unregistered
  markets go to the review queue, never traded.
- Snapshot/reaction tasks are fire-and-forget asyncio tasks: a
  container restart loses in-flight ones (known gap, open work item).
  The DB keeps everything already recorded.
- On EC2 later: same files, but put `data/` on an EBS volume instead
  of the instance root disk.
