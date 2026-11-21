"""Apply database migrations before starting Web/API and Worker."""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT))


def main() -> None:
    parser = argparse.ArgumentParser(description="升级工作台本地数据库")
    parser.add_argument("--runtime", type=Path)
    args = parser.parse_args()
    if args.runtime:
        os.environ["WORKBENCH_RUNTIME_DIR"] = str(args.runtime.resolve())
    from backend.app import db, execution_runtime
    from backend.app.config import get_settings

    with execution_runtime.ProcessLock(get_settings().runtime_dir / "migration.lock"):
        db.init_db()
        execution_runtime.ensure_schema()
    print("数据库迁移完成。")


if __name__ == "__main__":
    main()
