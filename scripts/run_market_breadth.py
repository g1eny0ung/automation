from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import signal
import subprocess
import time

from scripts.breadth_bundle import verify


def run(
    command: list[str], cwd: Path, *, seconds: float = 840, grace: float = 15
) -> int:
    process = subprocess.Popen(command, cwd=cwd, start_new_session=True)

    def stop(signum: int) -> None:
        try:
            os.killpg(process.pid, signum)
        except ProcessLookupError:
            pass

    def interrupted(signum: int, _frame: object) -> None:
        raise InterruptedError(signum)

    previous = {
        sig: signal.signal(sig, interrupted) for sig in (signal.SIGTERM, signal.SIGINT)
    }
    try:
        try:
            code = process.wait(timeout=seconds)
            return code if code >= 0 else 128 - code
        except (subprocess.TimeoutExpired, InterruptedError) as exc:
            for sig in previous:
                signal.signal(sig, signal.SIG_IGN)
            stop(signal.SIGTERM)
            # 子进程退出后，其同组后代仍可能存活；宽限期结束后统一清理。
            deadline = time.monotonic() + grace
            while time.monotonic() < deadline:
                try:
                    os.killpg(process.pid, 0)
                except ProcessLookupError:
                    break
                time.sleep(min(0.1, max(0, deadline - time.monotonic())))
            stop(signal.SIGKILL)
            process.wait()
            return (
                124
                if isinstance(exc, subprocess.TimeoutExpired)
                else 128 + int(exc.args[0])
            )
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run the pinned breadth producer with a process deadline"
    )
    parser.add_argument("market", choices=("en", "zh"))
    args = parser.parse_args()
    root = Path(__file__).resolve().parent.parent
    output = Path(os.environ["RUNNER_TEMP"]) / f"breadth-{args.market}"
    output.mkdir(parents=True, exist_ok=True)
    code = 1
    started = time.monotonic()
    try:
        revision = verify(root)
        checkout = root / "producer"
        for name in ("UPSTASH_REDIS_REST_URL", "UPSTASH_REDIS_REST_TOKEN"):
            if not os.environ.get(name):
                raise ValueError(f"{name} is required")
        code = run(
            [
                "uv",
                "run",
                "--no-sync",
                "python",
                "-m",
                "scripts.refresh_market_breadth",
                args.market,
                "--events",
                str(output / "events.jsonl"),
                "--revision",
                revision,
                "--result",
                str(output / "snapshot.json"),
            ],
            checkout,
        )
        return code
    except Exception as exc:
        print(f"Breadth runner failed: {type(exc).__name__}: {exc}")
        return code
    finally:
        (output / "runner.json").write_text(
            json.dumps(
                {
                    "exit_code": code,
                    "process_wall_seconds": round(time.monotonic() - started, 6),
                }
            )
            + "\n"
        )


if __name__ == "__main__":
    raise SystemExit(main())
