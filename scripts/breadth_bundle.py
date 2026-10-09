from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
import subprocess


FILES = (
    "lib/breadth_trace.py",
    "lib/constituent_cache.py",
    "lib/json_cache.py",
    "lib/market_breadth_en.py",
    "lib/market_breadth_zh.py",
    "lib/market_snapshot_cache.py",
    "lib/market_volatility.py",
    "lib/utils/market_metrics_utils.py",
    "lib/utils/json_utils.py",
    "lib/utils/trading_calendar.py",
    "scripts/refresh_market_breadth.py",
    "server_error.py",
    "pyproject.toml",
    "uv.lock",
)
ROOT = Path(__file__).resolve().parents[1]


def pinned_revision(root: Path) -> str:
    revision = (root / "stock-analysis-revision.txt").read_text().strip()
    if not re.fullmatch(r"[0-9a-f]{40}", revision):
        raise ValueError("Producer revision must be a full commit SHA")
    return revision


def verify(root: Path = ROOT) -> str:
    revision = pinned_revision(root)
    manifest = json.loads((root / "stock-analysis-bundle.json").read_text())
    if manifest["revision"] != revision or set(manifest["files"]) != set(FILES):
        raise ValueError("Producer bundle does not match the pinned revision or allowlist")
    producer = root / "producer"
    for name in FILES:
        path = producer / name
        if (
            path.is_symlink()
            or hashlib.sha256(path.read_bytes()).hexdigest() != manifest["files"][name]
        ):
            raise ValueError(f"Producer bundle hash mismatch: {name}")
    for path in producer.rglob("*"):
        relative = path.relative_to(producer)
        if relative.parts[0] == ".venv" or "__pycache__" in relative.parts:
            continue
        if path.is_symlink() or (
            path.is_file() and relative.as_posix() not in FILES
        ):
            raise ValueError(f"Unlisted producer file: {relative}")
    return revision


def export(source: Path, root: Path = ROOT) -> None:
    revision = pinned_revision(root)
    contents = {
        name: subprocess.check_output(
            ["git", "-C", str(source), "show", f"{revision}:{name}"]
        )
        for name in FILES
    }
    for name, content in contents.items():
        target = root / "producer" / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
    (root / "stock-analysis-bundle.json").write_text(
        json.dumps(
            {
                "revision": revision,
                "files": {
                    name: hashlib.sha256(content).hexdigest()
                    for name, content in contents.items()
                },
            },
            indent=2,
        ) + "\n"
    )
    verify(root)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Export or verify the allowlisted breadth producer"
    )
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("check")
    sync = commands.add_parser("export")
    sync.add_argument("source", type=Path, help="Local stock_analysis Git checkout")
    args = parser.parse_args()
    if args.command == "export":
        export(args.source)
    print(f"Breadth producer revision {verify()}")


if __name__ == "__main__":
    main()
