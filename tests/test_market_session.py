from datetime import date, datetime, timedelta
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import textwrap
import unittest
from unittest.mock import patch
from io import StringIO

from scripts.market_session import Market, evaluate, main
from workflow_helpers import run_gate, steps


ROOT = Path(__file__).resolve().parents[1]
CLI = ROOT / "scripts/market_session.py"

# https://www.nyse.com/trade/hours-calendars
NYSE_CLOSURES_2026 = {
    "2026-01-01", "2026-01-19", "2026-02-16", "2026-04-03", "2026-05-25",
    "2026-06-19", "2026-07-03", "2026-09-07", "2026-11-26", "2026-12-25",
}
# https://www.sse.com.cn/disclosure/dealinstruc/closed/c/c_20251222_10802510.shtml
SSE_CLOSURES_2026 = (
    ("2026-01-01", "2026-01-03"),
    ("2026-02-15", "2026-02-23"),
    ("2026-04-04", "2026-04-06"),
    ("2026-05-01", "2026-05-05"),
    ("2026-06-19", "2026-06-21"),
    ("2026-09-25", "2026-09-27"),
    ("2026-10-01", "2026-10-07"),
)


class SessionTests(unittest.TestCase):
    def test_explicit_date_ignores_hour_and_uses_beijing_date(self):
        cases = [
            ("today", "2026-10-08T07:59:59+08:00", "2026-10-08", ("open", "open")),
            ("today", "2026-10-08T08:00:00+08:00", "2026-10-08", ("open", "open")),
            ("today", "2026-10-08T17:59:59+08:00", "2026-10-08", ("open", "open")),
            ("today", "2026-10-08T18:00:00+08:00", "2026-10-08", ("open", "open")),
            ("yesterday", "2026-10-08T07:59:59+08:00", "2026-10-07", ("open", "closed")),
            ("yesterday", "2026-10-08T08:00:00+08:00", "2026-10-07", ("open", "closed")),
            ("yesterday", "2026-10-08T17:59:59+08:00", "2026-10-07", ("open", "closed")),
            ("yesterday", "2026-10-08T18:00:00+08:00", "2026-10-07", ("open", "closed")),
            ("today", "2026-10-07T16:30:00+00:00", "2026-10-08", ("open", "open")),
            ("yesterday", "2026-10-07T16:30:00+00:00", "2026-10-07", ("open", "closed")),
            ("yesterday", "2026-03-01T07:59:00+08:00", "2026-02-28", ("closed", "closed")),
            ("yesterday", "2027-01-01T07:59:00+08:00", "2026-12-31", ("open", "open")),
        ]
        for selection, now, target, statuses in cases:
            with self.subTest(selection=selection, now=now):
                result = subprocess.run(
                    [sys.executable, str(CLI), "en,zh", "--date", selection, "--now", now],
                    capture_output=True, text=True,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(json.loads(result.stdout), [
                    {"market": "en", "date": target, "status": statuses[0]},
                    {"market": "zh", "date": target, "status": statuses[1]},
                ])

    def test_all_2026_dates_against_official_calendars(self):
        for offset in range(365):
            candidate = date(2026, 1, 1) + timedelta(days=offset)
            stamp = candidate.isoformat()
            for market in Market:
                holiday = (
                    stamp in NYSE_CLOSURES_2026 if market == Market.US else
                    any(start <= stamp <= end for start, end in SSE_CLOSURES_2026)
                )
                expected = "closed" if candidate.weekday() >= 5 or holiday else "open"
                with self.subTest(market=market, date=stamp):
                    decision = evaluate(market, candidate)
                    self.assertEqual(decision.date, candidate)
                    self.assertEqual(decision.status, expected)

    def test_half_days_and_non_exchange_holidays_are_open(self):
        for candidate in ("2026-11-27", "2026-12-24", "2026-10-12", "2026-11-11"):
            with self.subTest(date=candidate):
                decision = evaluate(Market.US, date.fromisoformat(candidate))
                self.assertEqual((decision.date.isoformat(), decision.status), (candidate, "open"))

    def test_china_makeup_weekends_are_closed(self):
        for candidate in (
            "2026-01-04", "2026-02-14", "2026-02-28", "2026-05-09",
            "2026-09-20", "2026-10-10",
        ):
            with self.subTest(date=candidate):
                decision = evaluate(Market.CHINA, date.fromisoformat(candidate))
                self.assertEqual((decision.date.isoformat(), decision.status), (candidate, "closed"))

    def test_unknown_calendar_year_fails(self):
        for day in ("2027-01-01", "2027-01-02"):
            with self.subTest(day=day), self.assertRaisesRegex(ValueError, "2026"):
                evaluate(Market.CHINA, date.fromisoformat(day))

    def test_cli(self):
        for market, selection, now, expected in (
            ("en", "yesterday", "2026-10-03T10:17:00+08:00", {"market": "en", "date": "2026-10-02", "status": "open"}),
            ("zh", "today", "2026-10-06T18:07:00+08:00", {"market": "zh", "date": "2026-10-06", "status": "closed"}),
        ):
            with self.subTest(market=market):
                result = subprocess.run([sys.executable, str(CLI), market, "--date", selection, "--now", now], capture_output=True, text=True)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(json.loads(result.stdout), expected)
                self.assertEqual(result.stderr, "")
        for market, now in (
            ("zh", "2027-01-02T18:07:00+08:00"),
            ("en", "2026-10-03T10:17:00"),
            ("invalid", "2026-10-03T10:17:00+08:00"),
            ("en", "invalid"),
        ):
            with self.subTest(market=market, now=now):
                result = subprocess.run([sys.executable, str(CLI), market, "--date", "today", "--now", now], capture_output=True, text=True)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(result.stdout, "")
                self.assertTrue(result.stderr.strip())


    def test_cli_requires_valid_date_without_emitting_output(self):
        for selection in ([], ["--date", "tomorrow"], ["--date", ""], ["--date", "2026-10-08"]):
            with self.subTest(selection=selection), tempfile.TemporaryDirectory() as directory:
                output = Path(directory) / "output"
                result = subprocess.run(
                    [sys.executable, str(CLI), "en,zh", *selection,
                     "--now", "2026-10-09T09:00:00+08:00", "--github-output", str(output)],
                    capture_output=True, text=True,
                )
                self.assertEqual(result.returncode, 2)
                self.assertEqual(result.stdout, "")
                self.assertIn("--date", result.stderr)
                self.assertFalse(output.exists())

    def test_cli_uses_comma_separated_markets(self):
        for selection, order in (("en,zh", ("en", "zh")), ("zh,en", ("zh", "en"))):
            with self.subTest(selection=selection):
                result = subprocess.run(
                    [sys.executable, str(CLI), selection, "--date", "yesterday", "--now", "2026-10-09T09:00:00+08:00"],
                    capture_output=True, text=True,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(json.loads(result.stdout), [
                    {"market": market, "date": "2026-10-08", "status": "open"}
                    for market in order
                ])
        for selection in ([], [""], ["en", "zh"], ["en zh"], ["en,"], [",zh"], ["en,,zh"], ["en,invalid"]):
            with self.subTest(selection=selection), tempfile.TemporaryDirectory() as directory:
                output = Path(directory) / "output"
                result = subprocess.run(
                    [sys.executable, str(CLI), *selection, "--date", "yesterday", "--now", "2026-10-09T09:00:00+08:00",
                     "--github-output", str(output)],
                    capture_output=True, text=True,
                )
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(result.stdout, "")
                self.assertTrue(result.stderr.strip())
                self.assertFalse(output.exists())

    def test_batch_uses_one_target_date_across_midnight(self):
        with patch("sys.argv", [str(CLI), "en,zh", "--date", "yesterday"]), \
             patch("scripts.market_session.datetime", wraps=datetime) as clock, \
             patch("sys.stdout", new_callable=StringIO) as output:
            clock.now.side_effect = [
                datetime.fromisoformat("2026-10-09T23:59:59+08:00"),
                datetime.fromisoformat("2026-10-10T00:00:00+08:00"),
            ]
            self.assertEqual(main(), 0)
            self.assertEqual(json.loads(output.getvalue()), [
                {"market": "en", "date": "2026-10-08", "status": "open"},
                {"market": "zh", "date": "2026-10-08", "status": "open"},
            ])

    def test_github_output_appends_and_write_failure_emits_no_success(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "output"
            output.write_text("existing=value\n")
            args = [sys.executable, str(CLI), "zh", "--date", "today", "--now", "2026-10-06T18:07:00+08:00", "--github-output"]
            result = subprocess.run([*args, str(output)], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(result.stdout), {"market": "zh", "date": "2026-10-06", "status": "closed"})
            self.assertEqual(output.read_text(), "existing=value\nshould_run=false\n")
            for invalid_path in (directory, ""):
                with self.subTest(path=invalid_path):
                    result = subprocess.run([*args, invalid_path], capture_output=True, text=True)
                    self.assertEqual(result.returncode, 1)
                    self.assertEqual(result.stdout, "")
                    self.assertIn("Market calendar check failed", result.stderr)


class GateTests(unittest.TestCase):
    def test_delayed_china_workflow_checks_previous_evening(self):
        session = next(step for step in steps(".github/workflows/refresh-market-breadth-zh.yml")
                       if step.get("id") == "prepare")["with"]
        with tempfile.TemporaryDirectory() as directory:
            result, output = run_gate(Path(directory), session["market"], session["date"],
                                      "2026-10-09T16:56:46+00:00")
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(result.stdout),
                             {"market": "zh", "date": "2026-10-09", "status": "open"})
            self.assertEqual(output, "should_run=true\n")

    def test_workflow_dates_control_actual_action_results(self):
        for market, now, target, status in (
            ("zh", "2026-10-09T18:07:00+08:00", "2026-10-09", "open"),
            ("zh", "2026-10-09T17:59:59+08:00", "2026-10-08", "open"),
            ("zh", "2026-10-09T18:00:00+08:00", "2026-10-09", "open"),
            ("zh", "2026-10-10T18:07:00+08:00", "2026-10-10", "closed"),
            ("zh", "2026-10-06T18:07:00+08:00", "2026-10-06", "closed"),
            ("en", "2026-10-09T07:59:59+08:00", "2026-10-07", "open"),
            ("en", "2026-10-09T08:00:00+08:00", "2026-10-08", "open"),
            ("en", "2026-10-09T00:00:00+00:00", "2026-10-08", "open"),
            ("en", "2026-10-10T10:17:00+08:00", "2026-10-09", "open"),
            ("en", "2026-10-12T10:17:00+08:00", "2026-10-11", "closed"),
            ("en", "2026-07-04T10:17:00+08:00", "2026-07-03", "closed"),
        ):
            session = next(step for step in steps(f".github/workflows/refresh-market-breadth-{market}.yml")
                           if step.get("id") == "prepare")["with"]
            with self.subTest(market=market, now=now), tempfile.TemporaryDirectory() as directory:
                result, output = run_gate(Path(directory), session["market"], session["date"], now)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(json.loads(result.stdout), {"market": market, "date": target, "status": status})
                self.assertEqual(output, f"should_run={str(status == 'open').lower()}\n")

    def test_report_workflow_still_checks_yesterday_regardless_of_hour(self):
        session = next(step for step in steps(".github/workflows/market-analysis.yml")
                       if step.get("id") == "session")["with"]
        for hour in ("07:59:59", "18:00:00"):
            with self.subTest(hour=hour), tempfile.TemporaryDirectory() as directory:
                result, output = run_gate(Path(directory), session["markets"], session["date"],
                                          f"2026-10-08T{hour}+08:00")
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(json.loads(result.stdout), [
                    {"market": "en", "date": "2026-10-07", "status": "open"},
                    {"market": "zh", "date": "2026-10-07", "status": "closed"},
                ])
                self.assertEqual(output, "should_run=true\n")

    def test_actual_action_shell_all_market_combinations(self):
        for now, target, statuses, expected in (
            ("2026-01-02T09:00:00+08:00", "2026-01-01", ("closed", "closed"), "false"),
            ("2026-10-02T09:00:00+08:00", "2026-10-01", ("open", "closed"), "true"),
            ("2026-07-04T09:00:00+08:00", "2026-07-03", ("closed", "open"), "true"),
            ("2026-10-09T09:00:00+08:00", "2026-10-08", ("open", "open"), "true"),
        ):
            with self.subTest(now=now), tempfile.TemporaryDirectory() as directory:
                result, output = run_gate(Path(directory), "en,zh", "yesterday", now)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(json.loads(result.stdout), [
                    {"market": "en", "date": target, "status": statuses[0]},
                    {"market": "zh", "date": target, "status": statuses[1]},
                ])
                self.assertEqual(output, f"should_run={expected}\n")

    def test_action_invalid_inputs_and_later_calendar_failure_emit_no_output(self):
        for markets, now in (
            ("", "2026-10-09T09:00:00+08:00"),
            ("en,invalid", "2026-10-09T09:00:00+08:00"),
            ("en,*", "2026-10-09T09:00:00+08:00"),
            ("en zh", "2026-10-09T09:00:00+08:00"),
            ("en", "2026-10-09T09:00:00"),
            ("en,zh", "2027-01-05T09:00:00+08:00"),
        ):
            with self.subTest(markets=markets, now=now), tempfile.TemporaryDirectory() as directory:
                result, output = run_gate(Path(directory), markets, "yesterday", now)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(result.stdout, "")
                self.assertEqual(output, "")
                self.assertTrue(result.stderr.strip())
        self.assertEqual(evaluate(Market.US, date(2027, 1, 4)).status, "open")

    def test_action_rejects_invalid_date_as_one_argument(self):
        for selection in ("", "tomorrow", "today --help", "*"):
            with self.subTest(selection=selection), tempfile.TemporaryDirectory() as directory:
                result, output = run_gate(Path(directory), "en,zh", selection, "2026-10-09T09:00:00+08:00")
                self.assertEqual(result.returncode, 2)
                self.assertEqual(result.stdout, "")
                self.assertIn("--date", result.stderr)
                self.assertEqual(output, "")

    def test_workflows_connect_calendar_output_to_business_step(self):
        for filename, markets, selection, business_name, command in (
            ("refresh-market-breadth-en.yml", "en", "breadth", "Refresh cached breadth snapshot", "bash scripts/refresh-market-breadth.sh en"),
            ("refresh-market-breadth-zh.yml", "zh", "breadth", "Refresh cached breadth snapshot", "bash scripts/refresh-market-breadth.sh zh"),
            ("market-analysis.yml", "en,zh", "yesterday", "Generate market analysis report", None),
        ):
            with self.subTest(workflow=filename):
                workflow = steps(f".github/workflows/{filename}")
                session = next(step for step in workflow if step.get("id") in ("session", "prepare"))
                business = next(step for step in workflow if step.get("name") == business_name)
                self.assertEqual(session["with"]["date"], selection)
                self.assertEqual(session["with"].get("markets", session["with"].get("market")), markets)
                self.assertEqual(business["if"], f"steps.{session['id']}.outputs.should_run == 'true'")
                if command:
                    self.assertEqual(business["run"], command)
        import yaml
        action = yaml.safe_load((ROOT / ".github/actions/check-market-session/action.yml").read_text())
        self.assertEqual(action["outputs"]["should_run"]["value"], "${{ steps.check.outputs.should_run }}")
        self.assertEqual(action["runs"]["using"], "composite")
        install = next(step for step in action["runs"]["steps"] if step.get("name") == "Install exchange calendar")
        check = next(step for step in action["runs"]["steps"] if step.get("id") == "check")
        self.assertEqual(install["run"], "python -m pip install -r requirements-market-calendar.txt")
        self.assertEqual(check["env"], {"MARKETS": "${{ inputs.markets }}", "SESSION_DATE": "${{ inputs.date }}"})
        self.assertEqual(install["shell"], "bash")
        self.assertEqual(check["shell"], "bash")


if __name__ == "__main__":
    unittest.main()
