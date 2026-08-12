# Codex Usage Observatory

Local web app for analyzing Codex usage from the newest SQLite database in `~/.codex/state_*.sqlite`,
local Codex App rollout token counters in `~/.codex/sessions`, and configured remote machines over SSH.

Canonical Git repo:

```text
git@gitlab.geo.arm.com:gpu/shared/gpu_model/token-usage-dashboard.git
```

## What it shows

- Total tokens, last 30 days, last 7 days, thread count
- Fleet totals across local and remote Codex machines
- Average, median, p90, and largest thread token counts
- Daily, weekly, monthly, and cumulative token activity views
- Input, cached-input, uncached-input, output, reasoning-output, and unclassified token detail
- Contextual token-type drilldowns for clickable monthly and daily bars
- Top workspaces by token share
- Model and reasoning-effort breakdowns
- Entry-point breakdowns such as CLI, VS Code, and subagents
- Codex App sessions that are present in rollout JSONL counters but not yet fully indexed in SQLite
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

By default, the app calculates an API-equivalent estimate from measured token types and the public
OpenAI price for each recognized model. Cached input, uncached input, and output are priced separately;
reasoning output is already part of output and is not charged twice.

This is not the user's Codex subscription invoice. It excludes long-context uplifts, cache-write
charges, regional processing, tool fees, and models without a public API price. The panel reports
pricing coverage and unpriced tokens instead of silently guessing those costs.

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

The dashboard uses those values to replace the API-equivalent estimate with a custom assumption.

## Notes

- Codex totals come from `threads.tokens_used` plus local Codex App `token_count` counters that are not
  already represented by a matching `threads.rollout_path`.
- Token-type detail comes from each thread's newest cumulative rollout `token_count` event. Cached input
  is part of input, and reasoning output is part of output; the drawer keeps those parent/child rows
  visually separate so they are not added twice.
- Each dashboard refresh writes a generated snapshot to `data/codex_app_sessions.json`; generated
  `data/*.json` files are ignored by Git.
- Remote machines are queried over `ssh` and need to be reachable without an interactive prompt.
- Agent trees are counted once. Current Codex subagent rollouts can repeat the same cumulative tree
  counter in the parent and every child; summing those thread rows can inflate usage by orders of magnitude.
- The EU03 VS Code source uses `vscode-login3.hpc01.eu03.arm.com` with
  `vscode-login4.hpc01.eu03.arm.com` as a fallback. Both login nodes mount the same Codex state,
  so the shared token ledger is queried once and is not double-counted. The remote SQLite query
  uses immutable read-only access to avoid NFS `locking protocol` failures while Codex is active.
- If your local DB does not contain detailed `response.completed` usage logs, the dashboard falls back to thread-level totals only.
- Some threads may show zero tokens. In practice these are usually short exec-only sessions or metadata-only threads.
