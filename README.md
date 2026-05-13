# Codex Usage Observatory

Local web app for analyzing Codex usage from the newest SQLite database in `~/.codex/state_*.sqlite`
and from configured remote machines over SSH.

## What it shows

- Total tokens, last 30 days, last 7 days, thread count
- Fleet totals across local and remote Codex machines
- Average, median, p90, and largest thread token counts
- Monthly and last-30-day activity charts
- Top workspaces by token share
- Model and reasoning-effort breakdowns
- Entry-point breakdowns such as CLI, VS Code, and subagents
- Remote machine summaries and reachability state
- Heaviest threads and most recent threads
- Approval and sandbox-policy distribution
- Optional cost estimation based on your own pricing assumptions

## Run

```bash
cd /Users/sampan01/token-usage-dashboard
python3 server.py
```

Then open `http://127.0.0.1:8765`.

Optional overrides:

```bash
TOKEN_USAGE_DASHBOARD_HOST=127.0.0.1 TOKEN_USAGE_DASHBOARD_PORT=9000 python3 server.py
```

## Run as a macOS service

The repo includes a LaunchAgent plist at:

```text
launchd/com.sampan01.token-usage-dashboard.plist
```

Install and start it:

```bash
mkdir -p ~/Library/LaunchAgents
cp ~/token-usage-dashboard/launchd/com.sampan01.token-usage-dashboard.plist ~/Library/LaunchAgents/
launchctl bootstrap "gui/$(id -u)" ~/Library/LaunchAgents/com.sampan01.token-usage-dashboard.plist
launchctl kickstart -k "gui/$(id -u)/com.sampan01.token-usage-dashboard"
```

The service listens on `http://127.0.0.1:8765` and logs to:

```text
/tmp/token-usage-dashboard.out.log
/tmp/token-usage-dashboard.err.log
```

## Cost estimation

By default, the app uses the official OpenAI GPT-5.4 API price table as a rough Codex estimate.
It shows:

- rough blended estimate
- input-only floor
- output-only ceiling
- cached-input floor

The reason this is still only an estimate is that `threads.tokens_used` is a single total and does not
tell us how many tokens were input, cached input, or output.

You can override the default with one or both of:

- `usdPerBlock`
- `unitsPerBlock`
- `monthlyFlatUsd`
- `pricingLabel`

Examples:

- Tokens priced per million:
  - `usdPerBlock = 1.25`
  - `unitsPerBlock = 1000000`
- Flat monthly subscription:
  - `monthlyFlatUsd = 20`

The dashboard uses those values to estimate total usage cost and latest-month cost.

## Notes

- Codex totals currently come from `threads.tokens_used`.
- Remote machines are queried over `ssh` and need to be reachable without an interactive prompt.
- If your local DB does not contain detailed `response.completed` usage logs, the dashboard falls back to thread-level totals only.
- Some threads may show zero tokens. In practice these are usually short exec-only sessions or metadata-only threads.
