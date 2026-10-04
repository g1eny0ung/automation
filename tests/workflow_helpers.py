import os
from pathlib import Path
import shlex
import subprocess
import sys

import yaml


ROOT = Path(__file__).resolve().parents[1]


def steps(path):
    document = yaml.safe_load((ROOT / path).read_text())
    if "runs" in document:
        return document["runs"]["steps"]
    return next(iter(document["jobs"].values()))["steps"]


def run_gate(folder, markets, session_date, now):
    python = folder / "python"
    python.write_text(
        '#!/bin/bash\n'
        f'exec {shlex.quote(sys.executable)} "$@" --now "$TEST_NOW"\n'
    )
    python.chmod(0o755)
    output = folder / "github-output"
    env = {
        **os.environ,
        "PATH": str(folder) + os.pathsep + os.environ["PATH"],
        "MARKETS": markets,
        "SESSION_DATE": session_date,
        "TEST_NOW": now,
        "GITHUB_OUTPUT": str(output),
    }
    check = next(step for step in steps(".github/actions/check-market-session/action.yml") if step.get("id") == "check")
    result = subprocess.run(["bash", "-c", check["run"]], cwd=ROOT, env=env, capture_output=True, text=True)
    return result, output.read_text() if output.exists() else ""
