from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import textwrap
import unittest

from workflow_helpers import run_gate, steps


ROOT = Path(__file__).resolve().parents[1]


class MarketAnalysisTests(unittest.TestCase):
    def run_workflow(self, now, *, failure=""):
        workflow = steps(".github/workflows/market-analysis.yml")
        session = next(step for step in workflow if step.get("id") == "session")["with"]
        report = next(step for step in workflow if step.get("name") == "Generate market analysis report")
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            gate, output = run_gate(folder, session["markets"], session["date"], now)
            if gate.returncode or output == "should_run=false\n":
                return gate, [], output
            self.assertEqual(output, "should_run=true\n")
            wrapper = folder / "wrapper"
            wrapper.write_text(f"#!{sys.executable}\n" + textwrap.dedent('''\
                from datetime import datetime, timedelta
                import json
                import os
                from pathlib import Path
                import sys
                from zoneinfo import ZoneInfo

                command = Path(sys.argv[0]).name
                args = sys.argv[1:]
                if command == "date":
                    if args == ["-d", "yesterday", "+%Y-%m-%d"]:
                        now = datetime.fromisoformat(os.environ["TEST_NOW"])
                        print((now.astimezone(ZoneInfo("Asia/Shanghai")) - timedelta(days=1)).date())
                    else:
                        sys.exit(f"Unexpected date arguments: {args}")
                    sys.exit(0)
                if command == "head":
                    if args != ["-n", "-1"]:
                        sys.exit(f"Unexpected head arguments: {args}")
                    sys.stdout.write("".join(sys.stdin.readlines()[:-1]))
                    sys.exit(0)

                url = next(arg for arg in args if arg.startswith("https://"))
                payload = json.load(sys.stdin) if "@-" in args else None
                with open(os.environ["TEST_CURL_LOG"], "a") as log:
                    log.write(json.dumps({"url": url, "payload": payload}) + "\\n")
                if url.endswith("/api/macro/en"):
                    body = {"inflation": {"value": 2.5}}
                elif "/api/market-performance/" in url:
                    body = {"data": {"benchmark": {"date": "2026-10-08"},
                                     "tickers": [{"symbol": "fixture", "change": 1}]}}
                elif url.endswith("/chat/completions"):
                    body = {"choices": [{"message": {"content": "Fixture daily report"}}]}
                elif url.endswith("/api/reports/daily"):
                    body = {"ok": True}
                else:
                    sys.exit(f"Unexpected request: {url}")
                code = "200"
                failure = os.environ["TEST_FAILURE"]
                if failure == "fetch-http" and url.endswith("/api/macro/en"):
                    code = "503"
                elif failure == "fetch-shape" and url.endswith("/api/market-performance/en"):
                    body = {"data": {}}
                elif failure == "ai-http" and url.endswith("/chat/completions"):
                    code = "503"
                elif failure == "ai-empty" and url.endswith("/chat/completions"):
                    body = {"choices": [{"message": {"content": " "}}]}
                elif failure == "store-http" and url.endswith("/api/reports/daily"):
                    code = "503"
                if "--output" in args:
                    Path(args[args.index("--output") + 1]).write_text(json.dumps(body))
                    print(code, end="")
                else:
                    print(json.dumps(body))
                    print(code, end="")
                '''))
            wrapper.chmod(0o755)
            for command in ("date", "head", "curl"):
                (folder / command).symlink_to(wrapper)
            env = {
                **os.environ,
                "PATH": str(folder) + os.pathsep + os.environ["PATH"],
                "MARKET_API_TOKEN": "test-token",
                "ARK_API_KEY": "test-key",
                "TEST_NOW": datetime.fromisoformat(now).astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "TEST_FAILURE": failure,
                "TEST_CURL_LOG": str(folder / "curl.jsonl"),
            }
            result = subprocess.run(
                ["bash", "-c", report["run"]], cwd=ROOT, env=env,
                capture_output=True, text=True,
            )
            requests = [json.loads(line) for line in (folder / "curl.jsonl").read_text().splitlines()] if (folder / "curl.jsonl").exists() else []
            result.stdout = gate.stdout + result.stdout
            return result, requests, output

    def test_real_calendar_controls_complete_report_flow(self):
        for now, target, statuses in (
            ("2026-01-02T09:00:00+08:00", "2026-01-01", ("closed", "closed")),
            ("2026-10-02T09:00:00+08:00", "2026-10-01", ("open", "closed")),
            ("2026-07-04T09:00:00+08:00", "2026-07-03", ("closed", "open")),
            ("2026-10-09T09:00:00+08:00", "2026-10-08", ("open", "open")),
        ):
            with self.subTest(now=now):
                result, requests, output = self.run_workflow(now)
                self.assertEqual(result.returncode, 0, result.stderr)
                for market, status in zip(("en", "zh"), statuses):
                    self.assertIn(json.dumps({"market": market, "date": target, "status": status}), result.stdout)
                if statuses == ("closed", "closed"):
                    self.assertEqual(output, "should_run=false\n")
                    self.assertEqual(requests, [])
                else:
                    self.assertEqual(output, "should_run=true\n")
                    self.assertIn("Report stored successfully", result.stdout)
                    self.assertEqual([request["url"] for request in requests], [
                        "https://stock-analysis-umber.vercel.app/api/macro/en",
                        "https://stock-analysis-umber.vercel.app/api/market-performance/en",
                        "https://stock-analysis-umber.vercel.app/api/market-performance/zh",
                        "https://ark.cn-beijing.volces.com/api/v3/chat/completions",
                        "https://stock-analysis-umber.vercel.app/api/reports/daily",
                    ])
                    prompt = requests[3]["payload"]["messages"][0]["content"]
                    self.assertIn(target, prompt)
                    self.assertIn('"inflation"', prompt)
                    self.assertIn('"us_market"', prompt)
                    self.assertIn('"china_market"', prompt)
                    self.assertEqual(requests[4]["payload"], {"summary": "Fixture daily report"})

    def test_later_calendar_error_stops_before_requests(self):
        result, requests, output = self.run_workflow("2027-01-05T09:00:00+08:00")
        self.assertEqual(result.returncode, 1)
        self.assertIn("2026", result.stderr)
        self.assertEqual(result.stdout, "")
        self.assertEqual(output, "")
        self.assertEqual(requests, [])

    def test_report_http_and_body_errors_stop_the_request_sequence(self):
        for failure, count, message in (
            ("fetch-http", 1, "Failed to fetch /api/macro/en"),
            ("fetch-shape", 3, "Unexpected market API response shape"),
            ("ai-http", 4, "Failed to get AI response"),
            ("ai-empty", 4, "AI response has no report content"),
            ("store-http", 5, "Failed to store report"),
        ):
            with self.subTest(failure=failure):
                result, requests, output = self.run_workflow("2026-10-09T09:00:00+08:00", failure=failure)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(output, "should_run=true\n")
                self.assertIn(message, result.stdout + result.stderr)
                self.assertEqual(len(requests), count)
                self.assertNotIn("Report stored successfully", result.stdout)


if __name__ == "__main__":
    unittest.main()
