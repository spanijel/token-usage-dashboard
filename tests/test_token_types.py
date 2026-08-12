import unittest

import server


class TokenTypeTests(unittest.TestCase):
    def test_normalize_token_usage_preserves_parent_child_relationships(self):
        usage = server.normalize_token_usage(
            {
                "total_tokens": 150,
                "input_tokens": 100,
                "cached_input_tokens": 80,
                "output_tokens": 40,
                "reasoning_output_tokens": 15,
            }
        )

        self.assertEqual(usage["uncached_input_tokens"], 20)
        self.assertEqual(usage["non_reasoning_output_tokens"], 25)
        self.assertEqual(usage["unclassified_tokens"], 10)
        self.assertTrue(usage["has_token_types"])

    def test_scale_token_usage_matches_the_bar_total(self):
        usage = server.scale_token_usage(
            {
                "total_tokens": 200,
                "input_tokens": 160,
                "cached_input_tokens": 120,
                "output_tokens": 40,
                "reasoning_output_tokens": 10,
            },
            50,
        )

        self.assertEqual(usage["total_tokens"], 50)
        self.assertEqual(usage["input_tokens"], 40)
        self.assertEqual(usage["cached_input_tokens"], 30)
        self.assertEqual(usage["output_tokens"], 10)
        self.assertEqual(usage["reasoning_output_tokens"], 2)

    def test_grouped_usage_reports_coverage_and_unclassified_tokens(self):
        records = [
            {
                "month": "2026-07",
                "thread_count": 1,
                "usage": server.normalize_token_usage(
                    {
                        "total_tokens": 100,
                        "input_tokens": 80,
                        "cached_input_tokens": 60,
                        "output_tokens": 20,
                        "reasoning_output_tokens": 5,
                    }
                ),
            },
            {
                "month": "2026-07",
                "thread_count": 1,
                "usage": server.normalize_token_usage(None, 25),
            },
        ]

        row = server.grouped_token_usage(records, "month")[0]

        self.assertEqual(row["value"], 125)
        self.assertEqual(row["unclassified_tokens"], 25)
        self.assertEqual(row["typed_thread_count"], 1)
        self.assertEqual(row["thread_count"], 2)
        self.assertEqual(row["detail_coverage_pct"], 50.0)

    def test_fleet_series_adds_token_types_without_double_counting_children(self):
        rows = server.aggregate_monthly_series(
            [
                [
                    {
                        "month": "2026-07",
                        "value": 100,
                        "thread_count": 1,
                        "total_tokens": 100,
                        "input_tokens": 80,
                        "cached_input_tokens": 60,
                        "output_tokens": 20,
                        "reasoning_output_tokens": 5,
                        "typed_thread_count": 1,
                    }
                ],
                [
                    {
                        "month": "2026-07",
                        "value": 50,
                        "thread_count": 1,
                        "total_tokens": 50,
                        "input_tokens": 40,
                        "cached_input_tokens": 30,
                        "output_tokens": 10,
                        "reasoning_output_tokens": 2,
                        "typed_thread_count": 1,
                    }
                ],
            ]
        )

        self.assertEqual(rows[0]["value"], 150)
        self.assertEqual(rows[0]["total_tokens"], 150)
        self.assertEqual(rows[0]["input_tokens"], 120)
        self.assertEqual(rows[0]["cached_input_tokens"], 90)
        self.assertEqual(rows[0]["output_tokens"], 30)
        self.assertEqual(rows[0]["detail_coverage_pct"], 100.0)

    def test_remote_query_collects_rollout_token_types(self):
        script = server.remote_query_script()

        self.assertIn("token_usage", script)
        self.assertIn("cached_input_tokens", script)
        self.assertIn("reasoning_output_tokens", script)
        self.assertIn("mode=ro&immutable=1", script)
        self.assertIn("accounting_rows(raw_rows)", script)
        self.assertIn("deduplicated_subagent_count", script)
        self.assertIn("allocate_daily(counter_events(path), target, fallback_day)", script)
        self.assertNotIn('grouped(records, "day")', script)
        self.assertNotIn("datetime.fromisoformat", script)

    def test_daily_counter_usage_splits_a_session_by_event_day(self):
        events = [
            {
                "timestamp": "2026-07-21T08:00:00Z",
                "usage": {"total_tokens": 100, "input_tokens": 80, "output_tokens": 20},
            },
            {
                "timestamp": "2026-07-21T12:00:00Z",
                "usage": {"total_tokens": 150, "input_tokens": 120, "output_tokens": 30},
            },
            {
                "timestamp": "2026-07-22T12:00:00Z",
                "usage": {"total_tokens": 250, "input_tokens": 200, "output_tokens": 50},
            },
        ]

        rows = server.allocate_daily_counter_usage(events, 250)

        self.assertEqual([(row["day"], row["usage"]["total_tokens"]) for row in rows], [
            ("2026-07-21", 150),
            ("2026-07-22", 100),
        ])
        self.assertEqual(sum(row["usage"]["total_tokens"] for row in rows), 250)

    def test_daily_counter_usage_clips_to_source_owned_range(self):
        events = [
            {"timestamp": "2026-07-21T08:00:00Z", "usage": {"total_tokens": 100}},
            {"timestamp": "2026-07-22T08:00:00Z", "usage": {"total_tokens": 200}},
            {"timestamp": "2026-07-23T08:00:00Z", "usage": {"total_tokens": 300}},
        ]

        sqlite_rows = server.allocate_daily_counter_usage(events, 150)
        app_rows = server.allocate_daily_counter_usage(events, 150, baseline_total=150)

        self.assertEqual([(row["day"], row["usage"]["total_tokens"]) for row in sqlite_rows], [
            ("2026-07-21", 100),
            ("2026-07-22", 50),
        ])
        self.assertEqual([(row["day"], row["usage"]["total_tokens"]) for row in app_rows], [
            ("2026-07-22", 50),
            ("2026-07-23", 100),
        ])

    def test_daily_counter_usage_scales_exactly_and_uses_final_reset_segment(self):
        events = [
            {"timestamp": "2026-07-20T08:00:00Z", "usage": {"total_tokens": 500}},
            {"timestamp": "2026-07-21T08:00:00Z", "usage": {"total_tokens": 40}},
            {"timestamp": "2026-07-22T08:00:00Z", "usage": {"total_tokens": 100}},
        ]

        rows = server.allocate_daily_counter_usage(events, 50)

        self.assertEqual([(row["day"], row["usage"]["total_tokens"]) for row in rows], [
            ("2026-07-21", 40),
            ("2026-07-22", 10),
        ])
        self.assertEqual(sum(row["usage"]["total_tokens"] for row in rows), 50)

    def test_official_cost_uses_model_and_token_type_rates(self):
        sol = {
            "model": "gpt-5.6-sol",
            "total_tokens": 1_100_000,
            "input_tokens": 1_000_000,
            "cached_input_tokens": 800_000,
            "uncached_input_tokens": 200_000,
            "output_tokens": 100_000,
        }
        unknown = {
            "model": "codex-auto-review",
            "total_tokens": 100_000,
            "input_tokens": 90_000,
            "cached_input_tokens": 80_000,
            "uncached_input_tokens": 10_000,
            "output_tokens": 10_000,
        }

        cost = server.build_cost_summary(
            1_200_000,
            [{"month": "2026-07", "value": 1_200_000}],
            server.OFFICIAL_CODEX_PRICING,
            models=[sol, unknown],
            model_months=[{"month": "2026-07", **sol}, {"month": "2026-07", **unknown}],
        )

        self.assertEqual(cost["api_equivalent_cost_total_usd"], 4.4)
        self.assertEqual(cost["cached_input_cost_usd"], 0.4)
        self.assertEqual(cost["uncached_input_cost_usd"], 1.0)
        self.assertEqual(cost["output_cost_usd"], 3.0)
        self.assertEqual(cost["unpriced_tokens"], 100_000)
        self.assertEqual(cost["pricing_coverage_pct"], 91.67)
        self.assertEqual(cost["latest_month_api_equivalent_cost_usd"], 4.4)


if __name__ == "__main__":
    unittest.main()
