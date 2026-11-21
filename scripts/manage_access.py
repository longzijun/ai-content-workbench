"""Initialize/reset the single owner's password locally, never over HTTP."""
import argparse
import getpass
import os
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backend.app import access, db


def main():
    parser = argparse.ArgumentParser(description="在本机初始化工作台登录密码")
    parser.add_argument("--reset-password", action="store_true", help="重设并注销全部设备")
    parser.add_argument("--runtime", type=Path, help="正式运行数据目录")
    parser.add_argument("--keep-open", action="store_true", help="完成后等待回车，便于桌面初始化")
    args = parser.parse_args()
    if args.runtime:
        os.environ["WORKBENCH_RUNTIME_DIR"] = str(args.runtime.resolve())
    db.init_db()
    if access.configured() and not args.reset_password:
        print("工作台密码已经设置；如需重设，请使用显式 reset 操作。")
        return
    while True:
        password = getpass.getpass("新密码（至少12位，不显示）：")
        if not 12 <= len(password) <= 256:
            print("密码需要12到256个字符，请重试。")
            continue
        confirmation = getpass.getpass("再次输入同一个密码：")
        if password != confirmation:
            print("两次输入不一致，尚未保存。请直接重新输入。")
            continue
        access.setup_password(password, reset=args.reset_password)
        password = confirmation = ""
        break
    print("登录密码已配置，全部旧设备会话已失效。")
    if args.keep_open:
        input("现在可以回到工作台登录；按回车关闭此窗口。")


if __name__ == "__main__":
    main()
