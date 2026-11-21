"""Run the local service with a file-based graceful stop for the Windows launcher."""
from __future__ import annotations

import argparse
import os
import sys
import threading
from pathlib import Path

import uvicorn

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8765)
    arguments = parser.parse_args()
    from backend.app.config import get_settings
    marker = get_settings().runtime_dir / ("web-stop-request" if os.environ.get("WORKBENCH_SUPERVISED") == "1" else "stop-request")
    marker.unlink(missing_ok=True)
    os.environ['WORKBENCH_PROCESS_ROLE'] = 'web'
    server = uvicorn.Server(uvicorn.Config("backend.app.main:app", host="127.0.0.1", port=arguments.port, log_level="info", access_log=False, timeout_graceful_shutdown=12,
                                         proxy_headers=True, forwarded_allow_ips='127.0.0.1'))
    done = threading.Event()

    def monitor():
        while not done.wait(0.5):
            if marker.exists():
                server.should_exit = True
                marker.unlink(missing_ok=True)
                return

    threading.Thread(target=monitor, daemon=True, name="launcher-stop").start()
    try:
        server.run()
    finally:
        done.set()


if __name__ == "__main__":
    main()
