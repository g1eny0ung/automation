from __future__ import annotations

import json
import os
import shutil
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
        self.root = self.folder / "automation"
        self.root.mkdir()
        for name in ("scripts", "producer"):
            shutil.copytree(
                ROOT / name,
                self.root / name,
                ignore=shutil.ignore_patterns(".venv", "__pycache__"),
            )
        for name in ("stock-analysis-revision.txt", "stock-analysis-bundle.json"):
            shutil.copyfile(ROOT / name, self.root / name)
        self.checkout = self.root / "producer"
        self.revision = (ROOT / "stock-analysis-revision.txt").read_text().strip()
        self.env = {
            **os.environ,
            "PATH": str(self.bin) + os.pathsep + os.environ["PATH"],
            "RUNNER_TEMP": str(self.folder),
            "TEST_LOG": str(self.folder / "calls.jsonl"),
            "TEST_EXIT": "0",
            "UPSTASH_REDIS_REST_URL": "https://redis.example",
            "UPSTASH_REDIS_REST_TOKEN": "redis-test-token",
        }
        self.executable(
            "python",
            f"#!{sys.executable}\nimport os,sys\nos.execv(sys.executable, [sys.executable, *sys.argv[1:]])\n",
        )
        self.executable("git", "#!/bin/sh\necho FORBIDDEN_GIT >&2\nexit 99\n")
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
            ["bash", str(self.root / "scripts" / script), *args],
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

    def test_bundled_producer_prepares_without_gitlab_or_git(self):
        result = self.invoke("prepare-market-breadth.sh")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            self.calls(),
            [["uv", str(self.checkout), "sync", "--locked", "--python", "3.14"]],
        )
        self.assertIn(self.revision, result.stdout)

    def test_tampered_bundle_rejected_before_install_or_computation(self):
        (self.checkout / "lib/market_breadth_en.py").write_text(
            "raise RuntimeError('tampered')\n"
        )
        for script, args in [
            ("prepare-market-breadth.sh", ()),
            ("refresh-market-breadth.sh", ("en",)),
        ]:
            result = self.invoke(script, *args)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("hash mismatch", result.stdout + result.stderr)
        self.assertEqual(self.calls(), [])

    def test_unlisted_files_and_changed_manifest_are_rejected(self):
        extra = self.checkout / ".env"
        extra.write_text("PRIVATE=value\n")
        result = self.invoke("prepare-market-breadth.sh")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Unlisted producer file", result.stderr)
        extra.unlink()
        path = self.root / "stock-analysis-bundle.json"
        manifest = json.loads(path.read_text())
        manifest["files"]["extra.py"] = "0" * 64
        path.write_text(json.dumps(manifest))
        result = self.invoke("prepare-market-breadth.sh")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("allowlist", result.stderr)
        self.assertEqual(self.calls(), [])

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
        (self.root / "stock-analysis-revision.txt").write_text("0" * 40)
        result = self.invoke("refresh-market-breadth.sh", "en")
        self.assertEqual(result.returncode, 1)
        self.assertIn("does not match", result.stdout)
        (self.root / "stock-analysis-revision.txt").write_text(self.revision)
        self.env["UPSTASH_REDIS_REST_TOKEN"] = ""
        result = self.invoke("refresh-market-breadth.sh", "en")
        self.assertEqual(result.returncode, 1)
        self.assertIn("UPSTASH_REDIS_REST_TOKEN is required", result.stdout)
        self.assertFalse(any(call[0] == "uv" for call in self.calls()))

    def test_setup_failure_summary_requires_no_producer_or_dependencies(self):
        shutil.rmtree(self.checkout)
        self.env["GITHUB_STEP_SUMMARY"] = str(self.folder / "job-summary.md")
        result = subprocess.run(
            [
                sys.executable,
                str(self.root / "scripts/summarize_market_breadth.py"),
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
        output = self.folder / "breadth-zh"
        output.mkdir()
        (output / "events.jsonl").write_text(
            json.dumps({"event": "run_start", "wall_seconds": 0, "market": "zh"})
            + "\n"
            + json.dumps(
                {
                    "event": "start",
                    "wall_seconds": 1,
                    "id": 1,
                    "kind": "source",
                    "unit": "symbol",
                    "operation": "history",
                    "source": "eastmoney",
                    "symbol": "600519",
                }
            )
            + "\n"
        )
        (output / "runner.json").write_text(
            json.dumps({"exit_code": 124, "process_wall_seconds": 855.0})
        )
        result = subprocess.run(
            [
                sys.executable,
                "-I",
                str(self.root / "scripts/summarize_market_breadth.py"),
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
        self.assertEqual(len(summary["unfinished"]), 1)
        self.assertEqual(summary["unfinished"][0]["source"], "eastmoney")
        self.assertEqual(summary["unfinished"][0]["symbol"], "600519")

    def test_missing_log_and_closed_market_summary_are_explicit(self):
        for should_run, expected in [
            ("true", "missing_log"),
            ("false", "skipped_market_closed"),
        ]:
            result = subprocess.run(
                [
                    sys.executable,
                    "-I",
                    str(self.root / "scripts/summarize_market_breadth.py"),
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
            self.assertNotIn("env", prepare)
            refresh = next(step for step in workflow if step.get("id") == "refresh")
            self.assertEqual(
                set(refresh["env"]),
                {"UPSTASH_REDIS_REST_URL", "UPSTASH_REDIS_REST_TOKEN"},
            )
