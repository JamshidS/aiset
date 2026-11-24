"""Per-task usage summaries from hand-built Office usage aggregates."""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))

from aiset.run.cost import cost_line, cost_table  # noqa: E402

USAGE = {"tokens": {"total": 1930000, "cost": 4.123, "byModel": {"opus": 812000, "sonnet": 1118000}},
         "agents": [{"agent": "pm", "runs": 1, "tokens": {"total": 812000, "cost": 2.1}}], "runs": [{}]}


def single_run_usage(total, cost, name="pm", by_model=None):
    tokens = {"total": total, "cost": cost, "byModel": by_model or {}}
    return {"tokens": tokens, "agents": [{"agent": name, "runs": 1, "tokens": tokens}], "runs": [{}]}


class CostTest(unittest.TestCase):
    def test_line_starts_with_dollars(self):
        self.assertTrue(cost_line(USAGE).startswith("$4.12"))

    def test_no_usage(self):
        for usage in (None, {}, {"tokens": {"total": 0}}):
            self.assertEqual(cost_line(usage), "cost not available (no transcripts found for this task)")
        self.assertEqual(cost_table(None), "")

    def test_task_summary_and_table(self):
        agents = [
            {"agent": "pm", "runs": 1, "tokens": {"total": 812_000, "cost": 2.10, "byModel": {"opus": 812_000}}},
            {"agent": "root", "runs": 3, "tokens": {
                "total": 1_020_000, "cost": 1.80, "byModel": {"sonnet": 1_020_000},
            }},
            {"agent": "tester", "runs": 1, "tokens": {"total": 96_000, "cost": 0.22, "byModel": {"sonnet": 96_000}}},
            {"agent": "integration-checker", "runs": 1, "tokens": {
                "total": 2_000, "cost": 0.004, "byModel": {"sonnet": 2_000},
            }},
        ]
        usage = {"tokens": {"total": 1_930_000, "cost": 4.124, "byModel": {"opus": 812_000, "sonnet": 1_118_000}},
                 "agents": agents, "runs": [{} for _ in range(6)]}
        self.assertEqual(cost_line(usage),
                         "$4.12 · 1.93M tokens · 6 runs: pm, root ×3, tester, integration-checker "
                         "(estimated at API list prices)")
        self.assertEqual(cost_table(usage), "\n".join([
            "| Agent | Runs | Model | Tokens | Cost |",
            "|---|---:|---|---:|---:|",
            "| pm | 1 | opus | 812k | $2.10 |",
            "| root | 3 | sonnet | 1.02M | $1.80 |",
            "| tester | 1 | sonnet | 96k | $0.22 |",
            "| integration-checker | 1 | sonnet | 2k | <$0.01 |",
            "| **Total** | 6 | sonnet+opus | 1.93M | $4.12 |",
        ]))

    def test_token_boundaries(self):
        for total, expected in [(999, "999"), (1_000, "1k"), (2_500, "3k"),
                                (999_499, "999k"), (999_500, "1.00M"), (999_999, "1.00M"),
                                (1_000_000, "1.00M")]:
            with self.subTest(total=total):
                usage = single_run_usage(total, 0.01)
                self.assertEqual(cost_line(usage),
                                 f"$0.01 · {expected} tokens · 1 run: pm (estimated at API list prices)")
                self.assertEqual(cost_table(usage).splitlines()[-1], f"| **Total** | 1 |  | {expected} | $0.01 |")

    def test_empty_runs_and_zero_tokens(self):
        for usage in ({"agents": [], "runs": [], "tokens": {"total": 0, "cost": 0, "byModel": {}}},
                      {**single_run_usage(10, 0.01), "agents": []},
                      single_run_usage(0, 0)):
            with self.subTest(usage=usage):
                self.assertEqual(cost_line(usage), "cost not available (no transcripts found for this task)")
                self.assertEqual(cost_table(usage), "")

    def test_unknown_and_sub_cent_cost(self):
        for cost, expected in [(0, "cost unknown"), (0.004, "<$0.01")]:
            with self.subTest(cost=cost):
                usage = single_run_usage(1_930_000, cost)
                self.assertEqual(cost_line(usage),
                                 f"{expected} · 1.93M tokens · 1 run: pm (estimated at API list prices)")
                self.assertEqual(cost_table(usage).splitlines()[2:], [
                    f"| pm | 1 |  | 1.93M | {expected} |",
                    f"| **Total** | 1 |  | 1.93M | {expected} |",
                ])

    def test_malformed_usage(self):
        for usage in ({}, {"tokens": {"total": 10}}, {"tokens": {"total": 10}, "agents": None},
                      {"tokens": {"total": 10}, "agents": [{"agent": "pm"}]},
                      {"tokens": {"total": 10}, "agents": ["pm"], "runs": [{}]}):
            with self.subTest(usage=usage):
                self.assertEqual(cost_line(usage), "cost not available (no transcripts found for this task)")
                self.assertEqual(cost_table(usage), "")

    def test_runs_counted_from_agents(self):
        usage = {"tokens": {"total": 500, "cost": 0.02},
                 "agents": [{"agent": "pm", "runs": 2}, {"agent": "tester"}]}
        self.assertEqual(cost_line(usage), "$0.02 · 500 tokens · 2 runs: pm ×2, tester (estimated at API list prices)")
        self.assertEqual(cost_table(usage).splitlines()[2:], [
            "| pm | 2 |  | 0 | $0.00 |",
            "| tester | 0 |  | 0 | $0.00 |",
            "| **Total** | 2 |  | 500 | $0.02 |",
        ])

    def test_agent_cells_and_model_ties(self):
        usage = single_run_usage(400, 0.01, name="root\\worker|qa\nhelper",
                                 by_model={"opus": 100, "sonnet": 200, "haiku": 100})
        usage["agents"].append({"agent": None, "runs": 1, "tokens": {"total": 0, "cost": 0}})
        usage["runs"].append({})

        self.assertIn("2 runs: root\\worker|qa helper, unknown (", cost_line(usage))
        self.assertEqual(cost_table(usage).splitlines()[2:4], [
            r"| root\\worker\|qa helper | 1 | sonnet+opus+haiku | 400 | $0.01 |",
            "| unknown | 1 |  | 0 | $0.00 |",
        ])


if __name__ == "__main__":
    unittest.main()
