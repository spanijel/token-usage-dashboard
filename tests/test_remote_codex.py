import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import server


class RemoteCodexTests(unittest.TestCase):
    def test_dashboard_snapshot_reuses_fresh_result(self):
        payload = {"generated_at": "now"}
        server.DASHBOARD_CACHE_PAYLOAD = None
        server.DASHBOARD_CACHE_AT = 0.0
        server.DASHBOARD_REFRESHING = False
        with mock.patch("server.build_dashboard", return_value=payload) as build:
            first = server.get_dashboard_snapshot()
            second = server.get_dashboard_snapshot()

        self.assertIs(first, payload)
        self.assertIs(second, payload)
        build.assert_called_once_with()
        server.DASHBOARD_CACHE_PAYLOAD = None
        server.DASHBOARD_CACHE_AT = 0.0

    def test_dashboard_snapshot_invalidation_discards_cached_result(self):
        server.DASHBOARD_CACHE_PAYLOAD = {"generated_at": "old"}
        server.DASHBOARD_CACHE_AT = 1.0
        generation = server.DASHBOARD_CACHE_GENERATION

        server.invalidate_dashboard_snapshot()

        self.assertIsNone(server.DASHBOARD_CACHE_PAYLOAD)
        self.assertEqual(server.DASHBOARD_CACHE_AT, 0.0)
        self.assertEqual(server.DASHBOARD_CACHE_GENERATION, generation + 1)

    def test_remote_query_uses_immutable_read_only_sqlite(self):
        script = server.remote_query_script()
        self.assertIn("mode=ro&immutable=1", script)
        self.assertIn("uri=True", script)

    def test_shared_eu03_source_falls_back_to_second_login(self):
        config = {
            "host": "login3.example",
            "fallback_hosts": ["login4.example"],
            "display_host": "login3 + login4",
            "label": "EU03 shared",
        }
        payload = {
            "status": "configured",
            "db_path": "/home/user/.codex/state_5.sqlite",
            "totals": {
                "thread_count": 2,
                "total_tokens": 100,
                "tokens_30d": 100,
                "tokens_7d": 100,
                "first_seen": 10,
                "last_seen": 20,
            },
            "monthly": [],
            "daily": [],
            "models": [],
            "cwds": [],
            "top_threads": [
                {
                    "session_id": "remote-thread",
                    "title": "Remote heavy thread",
                    "cwd": "/work/remote",
                    "model": "gpt-test",
                    "reasoning_effort": "high",
                    "tokens_used": 75,
                    "updated_at": 20,
                }
            ],
        }
        failure = subprocess.CalledProcessError(1, ["ssh"], stderr="login3 unavailable")
        success = SimpleNamespace(stdout=json.dumps(payload), stderr="", returncode=0)
        with tempfile.TemporaryDirectory() as directory:
            cache_path = Path(directory) / "remote.json"
            with mock.patch("server.remote_snapshot_file", return_value=cache_path), mock.patch(
                "server.subprocess.run", side_effect=[failure, success]
            ) as run:
                result = server.run_remote_codex_snapshot(config)

        self.assertEqual(run.call_count, 2)
        self.assertEqual(result["status"], "configured")
        self.assertEqual(result["host"], "login3 + login4")
        self.assertEqual(result["queried_host"], "login4.example")
        self.assertEqual(result["configured_hosts"], ["login3.example", "login4.example"])
        self.assertEqual(result["total_tokens"], 100)
        self.assertEqual(result["top_threads"][0]["title_short"], "Remote heavy thread")
        self.assertEqual(result["top_threads"][0]["machine_label"], "EU03 shared")
        self.assertEqual(result["top_threads"][0]["share_pct"], 75.0)


if __name__ == "__main__":
    unittest.main()
