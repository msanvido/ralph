"""Shared helpers for the benchmark runners.

Every benchmark spins up per-task workspaces under benchmark/_runs/ and runs
`python -m ralph` as a subprocess against them. The preflight and subprocess
plumbing used to be copy-pasted across run.py / longbench.py / oolong_synth.py;
it lives here now.
"""
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).parent
RUNS_DIR = HERE / "_runs"


def list_tools(workspace: Path) -> set[str]:
    """Names of the user-defined dynamic tools in a workspace's tools/ dir."""
    tools_dir = workspace / "tools"
    if not tools_dir.is_dir():
        return set()
    return {p.name for p in tools_dir.glob("*.py") if not p.name.startswith("_")}


def preflight(script_name: str, *, needs_datasets: bool = False) -> None:
    """Fail fast if `<sys.executable> -m ralph --help` doesn't even start. Otherwise
    every task would spin up a workspace just to die on import with the failure
    hidden inside captured stderr."""
    proc = subprocess.run(
        [sys.executable, "-m", "ralph", "--help"],
        capture_output=True, text=True, timeout=20,
    )
    if proc.returncode != 0:
        msg = (proc.stderr + proc.stdout).strip()[-1000:]
        sys.exit(
            f"`{sys.executable} -m ralph --help` failed (rc={proc.returncode}).\n"
            f"Most likely the Python you're running with doesn't have ralph installed.\n"
            f"Try: .venv/bin/python benchmark/{script_name} {' '.join(sys.argv[1:])}\n"
            f"(or activate the venv first.)\n\n"
            f"Subprocess output:\n{msg}"
        )
    if needs_datasets:
        try:
            import datasets  # noqa: F401
        except ImportError:
            sys.exit(
                "The `datasets` package is required for this benchmark.\n"
                "Install with: pip install -e '.[benchmarks]'"
            )


def run_ralph(workspace: Path, prompt: Path, model: str | None,
              timeout: int = 600, extra_args: list[str] | None = None,
              bus_dir: Path | None = None) -> tuple[int, float, str]:
    """Run one Ralph subprocess against `workspace`. Returns (rc, elapsed, output_tail).
    Raises subprocess.TimeoutExpired on timeout — callers decide how to score that.
    bus_dir defaults to a private dir inside the workspace (no peers)."""
    cmd = [sys.executable, "-m", "ralph",
           "--workspace", str(workspace),
           "--prompt", str(prompt),
           "--bus-dir", str(bus_dir or workspace / "_bus")]
    if model:
        cmd += ["--model", model]
    cmd += extra_args or []
    started = time.time()
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    # Combine the tail of stdout+stderr so callers can surface real errors
    # (e.g., a model-validation exit or a startup ImportError) instead of
    # the misleading "solution.py not found" verifier message.
    tail = (proc.stdout + proc.stderr).strip()[-1500:]
    return proc.returncode, time.time() - started, tail
