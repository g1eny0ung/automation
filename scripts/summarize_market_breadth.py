from __future__ import annotations

import argparse
import importlib.util
import json
import os
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Summarize completed or interrupted breadth events"
    )
    parser.add_argument("market", choices=("en", "zh"))
    parser.add_argument("--preparation", required=True)
    parser.add_argument("--refresh", required=True)
    parser.add_argument("--should-run", default="")
    args = parser.parse_args()
    temporary = Path(os.environ["RUNNER_TEMP"])
    output = temporary / f"breadth-{args.market}"
    output.mkdir(parents=True, exist_ok=True)
    summary = {"status": "missing_log"}
    module = temporary / "stock-analysis/lib/breadth_trace.py"
    if module.exists():
        try:
            spec = importlib.util.spec_from_file_location("breadth_trace", module)
            trace = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(trace)
            summary = trace.summarize(output / "events.jsonl")
        except Exception as exc:
            summary = {"status": "summary_failed", "error_type": type(exc).__name__}
    summary.update(
        market=args.market, preparation=args.preparation, refresh=args.refresh
    )
    if args.preparation != "success":
        summary["status"] = "setup_failed"
    elif args.should_run == "false":
        summary["status"] = "skipped_market_closed"
    exit_file = output / "runner.json"
    if exit_file.exists():
        summary.update(json.loads(exit_file.read_text()))
        if summary["exit_code"] == 124:
            summary["status"] = "timeout"
        elif summary["exit_code"] != 0 and summary["status"] == "success":
            summary["status"] = "runner_failed"
    encoded = json.dumps(summary, indent=2, ensure_ascii=False)
    (output / "summary.json").write_text(encoded + "\n")
    print(encoded)
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a") as handle:
            handle.write(
                f"## {args.market.upper()} breadth refresh\n\n```json\n{encoded}\n```\n"
            )


if __name__ == "__main__":
    main()
