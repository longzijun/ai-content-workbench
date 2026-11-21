"""The only production owner of generation, monitoring and scheduling loops."""
from __future__ import annotations
import asyncio
import os
import signal
import sys
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT))


async def run():
    from backend.app import execution_runtime as execution, scheduler, trends, worker, research_queue
    from backend.app.config import get_settings
    from backend.app.backup_scheduler import tick as backup_tick
    settings = get_settings()
    if not execution.check_schema():
        raise RuntimeError('Database migration required before starting Worker')
    marker = settings.runtime_dir / 'worker-stop-request'
    marker.unlink(missing_ok=True)
    done = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, done.set)
        except (NotImplementedError, RuntimeError):
            pass
    with execution.worker_instance() as owner:
        worker.start_worker(owner_id=owner)
        automation = os.environ.get('WORKBENCH_DISABLE_AUTOMATION') != '1'
        if automation:
            research_queue.start(owner)
            trends.start_monitor()
            scheduler.start()
        try:
            while not done.is_set():
                if marker.exists():
                    marker.unlink(missing_ok=True)
                    break
                # Backups run in a dedicated bounded thread so keepalive is not
                # stalled by large immutable asset copies.
                await asyncio.to_thread(backup_tick)
                try:
                    await asyncio.wait_for(done.wait(), timeout=5)
                except asyncio.TimeoutError:
                    pass
        finally:
            scheduler.stop()
            trends.stop_monitor()
            research_queue.stop()
            worker.stop_worker()


if __name__ == '__main__':
    os.environ['WORKBENCH_PROCESS_ROLE'] = 'worker'
    asyncio.run(run())
