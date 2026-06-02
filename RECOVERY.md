# Token Usage Dashboard Recovery

This app is a local Codex token usage dashboard. It reads local Codex SQLite state
from `~/.codex/state_*.sqlite` and can refresh configured remote machine summaries
over SSH.

## Restore

```bash
cd ~
git clone git@gitlab.geo.arm.com:gpu/shared/token-usage-dashboard.git
cd ~/token-usage-dashboard
python3 server.py
```

Open `http://127.0.0.1:8765`.

## Optional LaunchAgent Service

Install the LaunchAgent:

```bash
mkdir -p ~/Library/LaunchAgents
cp ~/token-usage-dashboard/launchd/com.sampan01.token-usage-dashboard.plist ~/Library/LaunchAgents/
launchctl bootstrap "gui/$(id -u)" ~/Library/LaunchAgents/com.sampan01.token-usage-dashboard.plist
launchctl kickstart -k "gui/$(id -u)/com.sampan01.token-usage-dashboard"
```

Verify:

```bash
launchctl print "gui/$(id -u)/com.sampan01.token-usage-dashboard"
curl -s http://127.0.0.1:8765/api/dashboard
tail -50 /tmp/token-usage-dashboard.out.log
tail -50 /tmp/token-usage-dashboard.err.log
```

Unload:

```bash
launchctl bootout "gui/$(id -u)" ~/Library/LaunchAgents/com.sampan01.token-usage-dashboard.plist
```

## Runtime Data

Generated/imported JSON under `data/` is intentionally not committed because it
contains local usage summaries and machine-specific cached snapshots. The app
recreates this data as it runs.
