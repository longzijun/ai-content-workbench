from __future__ import annotations
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def main():
    parser = argparse.ArgumentParser(description='Verify a backup and restore only to a new/empty directory; no services are started.')
    parser.add_argument('--backup', type=Path, required=True)
    parser.add_argument('--destination', type=Path)
    args = parser.parse_args()
    from backend.app.backup_tools import restore_archive, verify_archive
    result = restore_archive(args.backup, args.destination) if args.destination else verify_archive(args.backup)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
