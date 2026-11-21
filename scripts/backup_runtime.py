"""Create and verify a content backup for an explicitly selected runtime."""
from __future__ import annotations
import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--runtime', type=Path, required=True)
    parser.add_argument('--restore-check', type=Path)
    args = parser.parse_args()
    os.environ['WORKBENCH_RUNTIME_DIR'] = str(args.runtime.resolve())
    from backend.app.artifacts import create_backup
    from backend.app.backup_tools import restore_archive
    from backend.app.config import get_settings
    result = create_backup(get_settings())
    if args.restore_check:
        result['restore_check'] = restore_archive(Path(result['path']), args.restore_check)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
