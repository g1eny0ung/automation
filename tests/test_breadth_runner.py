from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest

from scripts.run_market_breadth import run
from workflow_helpers import steps

ROOT = Path(__file__).resolve().parents[1]


class RunnerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.folder = Path(self.temporary.name).resolve()
        self.bin = self.folder / "bin"
        self.bin.mkdir()
        self.checkout = self.folder / "stock-analysis"
        self.checkout.mkdir()
        self.revision = (ROOT / "stock-analysis-revision.txt").read_text().strip()
        self.env = {
            **os.environ,
            "PATH": str(self.bin) + os.pathsep + os.environ["PATH"],
            "RUNNER_TEMP": str(self.folder),
            "TEST_LOG": str(self.folder / "calls.jsonl"),
            "TEST_REVISION": self.revision,
            "TEST_EXIT": "0",
            "GITLAB_DEPLOY_USER": "readonly-test-user",
            "GITLAB_DEPLOY_TOKEN": "private-test-token",
            "UPSTASH_REDIS_REST_URL": "https://redis.example",
            "UPSTASH_REDIS_REST_TOKEN": "redis-test-token",
        }
        self.executable(
            "python",
            f"#!{sys.executable}\nimport os,sys\nos.execv(sys.executable, [sys.executable, *sys.argv[1:]])\n",
        )
        self.executable(
            "git",
            f"""#!{sys.executable}
import json, os, subprocess, sys
from pathlib import Path
args = sys.argv[1:]
with open(os.environ['TEST_LOG'], 'a') as log:
    log.write(json.dumps(args) + '\\n')
if 'fetch' in args:
    for prompt, expected in [('Username', 'GITLAB_DEPLOY_USER'), ('Password', 'GITLAB_DEPLOY_TOKEN')]:
        actual = subprocess.check_output([os.environ['GIT_ASKPASS'], prompt], text=True).strip()
        if actual != os.environ[expected]:
            sys.exit(12)
if 'rev-parse' in args:
    print(os.environ['TEST_REVISION'])
""",
        )
        self.executable(
            "uv",
            f"""#!{sys.executable}
import json, os, sys
from pathlib import Path
args = sys.argv[1:]
with open(os.environ['TEST_LOG'], 'a') as log:
    log.write(json.dumps(['uv', os.getcwd(), *args]) + '\\n')
if args[0] == 'run':
    path = Path(args[args.index('--events') + 1])
    path.write_text(json.dumps({{'event': 'run_start', 'wall_seconds': 0, 'market': args[args.index('scripts.refresh_market_breadth') + 1]}}) + '\\n')
sys.exit(int(os.environ['TEST_EXIT']))
""",
        )
        self.executable("curl", "#!/bin/sh\necho FORBIDDEN_CURL >&2\nexit 99\n")

    def executable(self, name, source):
        path = self.bin / name
        path.write_text(source)
        path.chmod(0o755)

    def invoke(self, script, *args):
        return subprocess.run(
            ["bash", str(ROOT / "scripts" / script), *args],
            env=self.env,
            capture_output=True,
            text=True,
        )

    def calls(self):
        path = self.folder / "calls.jsonl"
        return (
            [json.loads(line) for line in path.read_text().splitlines()]
            if path.exists()
            else []
        )

    def test_private_checkout_pins_exact_sha_askpass_never_logs_credentials(self):
        result = self.invoke("prepare-market-breadth.sh")
        self.assertEqual(result.returncode, 0, result.stderr)
        calls = self.calls()
        self.assertIn(
            [
                "-C",
                str(self.checkout),
                "-c",
                "credential.helper=",
                "fetch",
                "--quiet",
                "--depth=1",
                "origin",
                self.revision,
            ],
            calls,
        )
        self.assertEqual(
            calls[-1],
            ["uv", str(self.checkout), "sync", "--locked", "--python", "3.14"],
        )
        logged = result.stdout + result.stderr + json.dumps(calls)
        self.assertNotIn("private-test-token", logged)
        self.assertNotIn("readonly-test-user", logged)
        self.assertEqual(list(self.folder.glob("breadth-askpass.*")), [])
        self.env["TEST_REVISION"] = "0" * 40
        result = self.invoke("prepare-market-breadth.sh")
        self.assertEqual(result.returncode, 1)
        self.assertIn("does not match", result.stderr)

    def test_both_markets_call_pinned_cli_and_preserve_exit_codes(self):
        for market, code in [("zh", 0), ("en", 3), ("zh", 17)]:
            with self.subTest(market=market, code=code):
                self.env["TEST_EXIT"] = str(code)
                result = self.invoke("refresh-market-breadth.sh", market)
                self.assertEqual(result.returncode, code, result.stderr)
                self.assertEqual(
                    json.loads(
                        (self.folder / f"breadth-{market}/runner.json").read_text()
                    )["exit_code"],
                    code,
                )
                command = self.calls()[-1]
                self.assertEqual(
                    command[:8],
                    [
                        "uv",
                        str(self.checkout),
                        "run",
                        "--no-sync",
                        "python",
                        "-m",
                        "scripts.refresh_market_breadth",
                        market,
                    ],
                )
                self.assertIn(self.revision, command)
                self.assertNotIn("FORBIDDEN_CURL", result.stderr)
                self.assertNotIn("redis-test-token", result.stdout + result.stderr)

    def test_invalid_market_revision_and_missing_redis_stop_before_computation(self):
        result = self.invoke("refresh-market-breadth.sh", "invalid")
        self.assertEqual(result.returncode, 2)
        self.env["TEST_REVISION"] = "0" * 40
        result = self.invoke("refresh-market-breadth.sh", "en")
        self.assertEqual(result.returncode, 1)
        self.assertIn("does not match", result.stdout)
        self.env["TEST_REVISION"] = self.revision
        self.env["UPSTASH_REDIS_REST_TOKEN"] = ""
        result = self.invoke("refresh-market-breadth.sh", "en")
        self.assertEqual(result.returncode, 1)
        self.assertIn("UPSTASH_REDIS_REST_TOKEN is required", result.stdout)
        self.assertFalse(any(call[0] == "uv" for call in self.calls()))

    def test_setup_failure_summary_requires_no_producer_or_dependencies(self):
        self.env["GITHUB_STEP_SUMMARY"] = str(self.folder / "job-summary.md")
        result = subprocess.run(
            [
                sys.executable,
                str(ROOT / "scripts/summarize_market_breadth.py"),
                "zh",
                "--preparation",
                "failure",
                "--refresh",
                "skipped",
            ],
            env=self.env,
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["status"], "setup_failed")
        self.assertIn("setup_failed", (self.folder / "job-summary.md").read_text())
        self.assertEqual(
            json.loads((self.folder / "breadth-zh/summary.json").read_text())["market"],
            "zh",
        )

    def test_timeout_summary_keeps_partial_evidence_and_process_wall_time(self):
        library = self.checkout / "lib"
        library.mkdir()
        (library / "breadth_trace.py").write_text(
            "def summarize(path):\n"
            "    return {'status': 'interrupted', 'observed_wall_seconds': 1, 'unfinished': [{'source': 'eastmoney', 'symbol': '600519'}]}\n"
        )
        output = self.folder / "breadth-zh"
        output.mkdir()
        (output / "runner.json").write_text(
            json.dumps({"exit_code": 124, "process_wall_seconds": 855.0})
        )
        result = subprocess.run(
            [
                sys.executable,
                "-I",
                str(ROOT / "scripts/summarize_market_breadth.py"),
                "zh",
                "--preparation",
                "success",
                "--refresh",
                "failure",
                "--should-run",
                "true",
            ],
            env=self.env,
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        summary = json.loads(result.stdout)
        self.assertEqual(summary["status"], "timeout")
        self.assertEqual(summary["process_wall_seconds"], 855.0)
        self.assertEqual(summary["observed_wall_seconds"], 1)
        self.assertEqual(
            summary["unfinished"], [{"source": "eastmoney", "symbol": "600519"}]
        )

    def test_missing_log_and_closed_market_summary_are_explicit(self):
        for should_run, expected in [
            ("true", "missing_log"),
            ("false", "skipped_market_closed"),
        ]:
            result = subprocess.run(
                [
                    sys.executable,
                    "-I",
                    str(ROOT / "scripts/summarize_market_breadth.py"),
                    "en",
                    "--preparation",
                    "success",
                    "--refresh",
                    "skipped",
                    "--should-run",
                    should_run,
                ],
                env=self.env,
                capture_output=True,
                text=True,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(result.stdout)["status"], expected)

    def test_deadline_kills_process_group_and_preserves_partial_log(self):
        pid_file = self.folder / "grandchild.pid"
        output = self.folder / "events.jsonl"
        child = self.folder / "child.py"
        child.write_text(
            textwrap.dedent(f"""
            import os, signal, subprocess, sys, time
            from pathlib import Path
            signal.signal(signal.SIGTERM, signal.SIG_IGN)
            grandchild = subprocess.Popen([sys.executable, '-c', 'import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(60)'])
            Path({str(pid_file)!r}).write_text(str(grandchild.pid))
            Path({str(output)!r}).write_text('{{"event":"start","operation":"history"}}\\n')
            time.sleep(60)
        """)
        )
        self.assertEqual(
            run([sys.executable, str(child)], self.folder, seconds=0.4, grace=0.2), 124
        )
        pid = int(pid_file.read_text())

        def alive():
            result = subprocess.run(
                ["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True
            )
            return bool(result.stdout.strip()) and not result.stdout.strip().startswith(
                "Z"
            )

        deadline = time.monotonic() + 2
        while alive() and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertFalse(alive())
        self.assertEqual(json.loads(output.read_text())["operation"], "history")
        self.assertEqual(
            run([sys.executable, "-c", "raise SystemExit(7)"], self.folder), 7
        )

    def test_workflows_finish_with_summary_and_artifact_even_after_failure(self):
        for market in ("en", "zh"):
            workflow = steps(f".github/workflows/refresh-market-breadth-{market}.yml")
            summary = next(
                step
                for step in workflow
                if step.get("name") == "Summarize breadth refresh"
            )
            artifact = next(
                step
                for step in workflow
                if step.get("uses", "").startswith("actions/upload-artifact")
            )
            self.assertEqual(summary["if"], "always()")
            self.assertEqual(artifact["if"], "always()")
            self.assertEqual(
                artifact["with"]["path"], "${{ runner.temp }}/breadth-" + market + "/"
            )
            prepare = next(step for step in workflow if step.get("id") == "prepare")
            self.assertLess(prepare["timeout-minutes"] * 60 + 840 + 15, 20 * 60)
            refresh = next(step for step in workflow if step.get("id") == "refresh")
            self.assertEqual(
                set(refresh["env"]),
                {"UPSTASH_REDIS_REST_URL", "UPSTASH_REDIS_REST_TOKEN"},
            )
