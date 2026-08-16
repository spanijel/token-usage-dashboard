import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

import usage_accounting


def event(timestamp, event_type, payload, ordinal=None):
    record = {"timestamp": timestamp}
    if ordinal is not None:
        record["ordinal"] = ordinal
    record.update({"type": event_type, "payload": payload})
    return json.dumps(record)


def token_event(timestamp, cumulative, increment, ordinal=None):
    return event(
        timestamp,
        "event_msg",
        {
            "type": "token_count",
            "info": {
                "total_token_usage": cumulative,
                "last_token_usage": increment,
            },
        },
        ordinal=ordinal,
    )


class LogicalUsageAccountingTests(unittest.TestCase):
    def write_rollout(self, root, name, session_id, lines):
        path = Path(root) / name
        content = [
            event(
                "2026-08-14T08:00:00Z",
                "session_meta",
                {"id": session_id, "cwd": "/work/repo", "originator": "Codex Desktop"},
            ),
            event(
                "2026-08-14T08:00:01Z",
                "turn_context",
                {"model": "gpt-test", "effort": "high", "cwd": "/work/repo"},
            ),
            *lines,
        ]
        path.write_text("\n".join(content) + "\n", encoding="utf-8")
        return path

    def test_replayed_logical_history_is_counted_once(self):
        first = token_event(
            "2026-08-14T10:00:00Z",
            {"input_tokens": 100, "cached_input_tokens": 60, "output_tokens": 20, "total_tokens": 120},
            {"input_tokens": 100, "cached_input_tokens": 60, "output_tokens": 20, "total_tokens": 120},
        )
        second = token_event(
            "2026-08-15T10:00:00Z",
            {"input_tokens": 150, "cached_input_tokens": 90, "output_tokens": 30, "total_tokens": 180},
            {"input_tokens": 50, "cached_input_tokens": 30, "output_tokens": 10, "total_tokens": 60},
        )
        with tempfile.TemporaryDirectory() as root:
            one = self.write_rollout(root, "rollout-one.jsonl", "logical-a", [first, second])
            replay = self.write_rollout(root, "rollout-replay.jsonl", "logical-a", [first, second])
            result = usage_accounting.scan_rollouts([one, replay])

        totals = usage_accounting.aggregate_events(result["events"])
        self.assertEqual(totals["total_tokens"], 180)
        self.assertEqual(totals["cached_input_tokens"], 90)
        self.assertEqual(totals["thread_count"], 1)
        self.assertEqual(result["raw_event_count"], 4)
        self.assertEqual(result["unique_event_count"], 2)
        self.assertEqual(result["duplicate_event_count"], 2)

    def test_inherited_baseline_counts_only_child_increment_on_its_day(self):
        inherited = token_event(
            "2026-08-15T23:58:00Z",
            {"input_tokens": 6_000_000_000, "cached_input_tokens": 5_900_000_000, "output_tokens": 20_000_000, "total_tokens": 6_020_000_000},
            {"input_tokens": 2_000_000, "cached_input_tokens": 1_900_000, "output_tokens": 20_000, "total_tokens": 2_020_000},
        )
        with tempfile.TemporaryDirectory() as root:
            path = self.write_rollout(root, "rollout-child.jsonl", "logical-child", [inherited])
            result = usage_accounting.scan_rollouts([path])

        totals = usage_accounting.aggregate_events(result["events"])
        daily = usage_accounting.group_events(result["events"], "day")
        self.assertEqual(totals["total_tokens"], 2_020_000)
        self.assertEqual(daily[0]["value"], 2_020_000)
        expected_day = datetime.fromisoformat("2026-08-15T23:58:00+00:00").astimezone().strftime("%Y-%m-%d")
        self.assertEqual(daily[0]["day"], expected_day)

    def test_optional_ordinal_token_event_is_not_filtered_out(self):
        ordinal_event = token_event(
            "2026-08-15T10:00:00Z",
            {"input_tokens": 80, "cached_input_tokens": 40, "output_tokens": 20, "total_tokens": 100},
            {"input_tokens": 80, "cached_input_tokens": 40, "output_tokens": 20, "total_tokens": 100},
            ordinal=28,
        )
        with tempfile.TemporaryDirectory() as root:
            path = self.write_rollout(root, "rollout-ordinal.jsonl", "logical-ordinal", [ordinal_event])
            result = usage_accounting.scan_rollouts([path])

        self.assertEqual(result["raw_event_count"], 1)
        self.assertEqual(usage_accounting.aggregate_events(result["events"])["total_tokens"], 100)


if __name__ == "__main__":
    unittest.main()
