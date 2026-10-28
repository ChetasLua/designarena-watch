# Design Arena Watch

A Discord alert bot for Design Arena stealth models, like the Arena bot but for designarena.ai.
Every 2 minutes it polls the public `https://www.designarena.ai/api/registry` (the JSON every visitor's browser loads) and posts to a Discord webhook when something changes.

| Alert | Meaning |
|---|---|
| 🕵️ New stealth model | A new codename slot appeared: provider row with `private.png`, no model row yet. Shows the name family (trees, Canadian places…), a date-suffix hint (`lithium-1015` → Oct 15) and the other slots added in the same poll. |
| 🎭 Revealed | A model row appeared whose id is a known codename, e.g. `pelican` → Canva Craft. Shows lab, arenas and price. |
| 🫥 Slot removed | The codename row was deleted without a codename model. Lists the models that appeared in the same poll as possible matches. |
| 🔁 Slot changed | The codename row got a new name, a real logo, or a model under it. |
| 🆕 / ❌ / ✏️ / 🏢 | New / removed / changed model rows, new lab. More than 4 changes are folded into one digest. Pricing-only edits are muted. |

Replay test: run against 23 Wayback copies of the registry (Mar 30 – Oct 26, 2026), it re-detects every known reveal (evergreen → Muse Spark 1.1, magnesium → GPT 5.6 Sol, hampshire → MiMo-V2.6-Pro, new-mexico → Ling-3.1-flash, arkansas → FLUX 3 Max, oregon → HeyGen, dakota/hawaii → Krea 3, pelican → Canva Craft, snowhare → MiniMax M3.1 Flash…). All 66 replayed messages are within Discord's limits.

## Running 24/7 on GitHub Actions (current setup)
`.github/workflows/watch.yml` polls on a cron and commits `state/` back to the repo when something changes. The repo history is therefore a timestamped record of every registry change.
1. Add the webhook as a repo secret named `DA_WEBHOOK_URL` (optional: `DA_MENTION_ROLE_ID` to ping a role on new or revealed stealth models).
2. Actions → designarena-watch → Run workflow → `test-post`, then `roster` to post the fresh stealth slots.
3. Cadence: every 5 minutes (`*/5`, GitHub's minimum). That is free because the repo is public. If it ever goes private, use `*/30` to stay inside a free account's 2,000 Actions minutes a month. GitHub may run scheduled jobs a few minutes late.
Alerts found before the secret exists are queued in `state/pending.json` and delivered on the first run after it's set (up to 50).

## Local setup
```bash
cd ~/Downloads/designarena-watch
cp config.example.json config.json && chmod 600 config.json   # paste your webhook URL
python3 -I watch.py test-post                                  # "connected" message
python3 -I watch.py baseline --since 2026-09-17 --post         # current fresh stealth roster
```
Run it every 2 minutes on this Mac (stops while the Mac sleeps):
```bash
cp com.chetaslua.designarena-watch.plist ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/com.chetaslua.designarena-watch.plist
```
On a server, alongside the Arena bot: `python3 watch.py loop --interval 120`. It's stdlib only, with state in `./state/`.

## Other commands
- `status [--since DATE]`: list current stealth slots, newest first, with first-seen windows.
- `seed DIR`: backfill first-seen windows from dated registry snapshots (`reg_YYYYmmddHHMMSS.json`).
- `replay DIR [--payloads]`: run the diff engine over a snapshot series (test).
- Add `--dry-run` to anything to print the Discord payloads instead of sending them.

## Notes
- State lives in `state/registry.json` (last good copy), `first_seen.json`, `events.jsonl` (every event), `snapshots/*.json.gz` (written only when something changed) and `pending.json` (payloads to retry if Discord was down).
- A truncated or odd response (fewer than 100 models) is rejected, so a bad fetch can't produce a burst of fake "removed" alerts.
- Limits of the signal: since about June, stealth model rows are withheld from the public registry, so only the codename slot is visible until the reveal. A stealth model that launches under its real id instead of the codename shows up as 🫥 + 🆕 in the same poll, not as 🎭.
