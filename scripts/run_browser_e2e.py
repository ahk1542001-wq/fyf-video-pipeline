#!/usr/bin/env python3
"""Hermetic Playwright browser test runner for FYF Video Pipeline.

Launches local FastAPI backend on port 8000 and Next.js frontend on port 3001,
waits for both to be healthy, executes Playwright E2E browser tests,
and cleans up processes reliably upon completion.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
FRONTEND_DIR = REPO_ROOT / "frontend"


def _kill_stale_port_listeners(*ports: int) -> None:
    """Force-kill any processes listening on the given ports to ensure clean test isolation."""
    for port in ports:
        try:
            result = subprocess.run(
                ["lsof", "-ti", f":{port}"],
                capture_output=True, text=True, timeout=5,
            )
            pids = result.stdout.strip()
            if pids:
                for pid_str in pids.split("\n"):
                    pid_str = pid_str.strip()
                    if pid_str:
                        try:
                            os.kill(int(pid_str), signal.SIGKILL)
                            print(f"  ✓ Killed stale process {pid_str} on port {port}")
                        except (ProcessLookupError, ValueError):
                            pass
                # Brief pause for OS to release the port
                time.sleep(0.3)
        except (subprocess.TimeoutExpired, FileNotFoundError):
            pass


def wait_for_url(url: str, timeout_sec: int = 30) -> bool:
    start = time.time()
    while time.time() - start < timeout_sec:
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "E2E-Probe"})
            with urllib.request.urlopen(req, timeout=2) as response:
                if response.status in (200, 304):
                    return True
        except Exception:
            pass
        time.sleep(0.5)
    return False


def main() -> int:
    backend_env = dict(os.environ)
    backend_env["FYF_RUNTIME_MODE"] = "hackathon"
    backend_env["PYTHONUNBUFFERED"] = "1"

    frontend_env = dict(os.environ)
    frontend_env["FYF_BACKEND_URL"] = "http://127.0.0.1:8000"
    frontend_env["NEXT_PUBLIC_API_URL"] = ""

    backend_proc: subprocess.Popen | None = None
    frontend_proc: subprocess.Popen | None = None

    try:
        print("[0/4] Cleaning stale listeners on ports 8000 and 3001...")
        _kill_stale_port_listeners(8000, 3001)

        print("[1/4] Starting FastAPI backend on port 8000...")
        backend_cmd = [
            "uv", "run", "uvicorn", "backend.main:app",
            "--host", "127.0.0.1",
            "--port", "8000",
            "--log-level", "warning",
        ]
        backend_proc = subprocess.Popen(
            backend_cmd,
            cwd=str(REPO_ROOT),
            env=backend_env,
        )

        if not wait_for_url("http://127.0.0.1:8000/health", timeout_sec=20):
            print("ERROR: FastAPI backend failed to respond at http://127.0.0.1:8000/health", file=sys.stderr)
            return 1
        print("  ✓ FastAPI backend healthy.")

        print("[2/4] Starting Next.js production server on port 3001...")
        frontend_cmd = ["npx", "next", "start", "-p", "3001"]
        frontend_proc = subprocess.Popen(
            frontend_cmd,
            cwd=str(FRONTEND_DIR),
            env=frontend_env,
        )

        if not wait_for_url("http://127.0.0.1:3001", timeout_sec=20):
            print("ERROR: Next.js frontend failed to respond at http://127.0.0.1:3001", file=sys.stderr)
            return 1
        print("  ✓ Next.js frontend healthy.")

        print("[3/4] Executing Playwright browser test suite...")
        playwright_cmd = ["npx", "playwright", "test"]
        test_res = subprocess.run(
            playwright_cmd,
            cwd=str(FRONTEND_DIR),
            env=frontend_env,
        )
        print(f"[4/4] Playwright exited with code {test_res.returncode}")
        return test_res.returncode

    finally:
        print("Cleaning up server processes...")
        for proc, name in ((frontend_proc, "Next.js frontend"), (backend_proc, "FastAPI backend")):
            if proc is not None and proc.poll() is None:
                try:
                    proc.terminate()
                    proc.wait(timeout=3)
                except Exception:
                    try:
                        proc.kill()
                    except Exception:
                        pass
                print(f"  ✓ {name} stopped.")
        # Final cleanup to ensure ports are released
        _kill_stale_port_listeners(3001, 8000)


if __name__ == "__main__":
    sys.exit(main())
