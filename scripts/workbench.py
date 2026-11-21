"""Initialize, run, and stop a standalone local workbench."""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import socket
import subprocess
import sys

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT))


def local_environment(runtime: Path, vault: Path | None) -> None:
    excluded = {"HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "PYTHONHOME", "PYTHONPATH"}
    for key in list(os.environ):
        if key.upper().startswith("WORKBENCH_") or key.upper() in excluded:
            os.environ.pop(key, None)
    os.environ.update(WORKBENCH_RUNTIME_DIR=str(runtime), PYTHONUTF8="1")
    if vault is not None:
        os.environ["WORKBENCH_VAULT_PATH"] = str(vault)


def main() -> int:
    parser = argparse.ArgumentParser(description="启动或停止本机 AI 内容工作台")
    parser.add_argument("command", choices=("init", "start", "stop"))
    parser.add_argument("--runtime", type=Path, default=Path("runtime"),
                        help="项目内运行目录，可使用 runtime 或 runtime-*")
    parser.add_argument("--port", type=int, default=18765, help="本机端口，默认 18765")
    parser.add_argument("--vault", type=Path, help="允许写入的现有 Obsidian 库目录")
    parser.add_argument("--reset-password", action="store_true", help="与 init 一起使用以重设密码")
    args = parser.parse_args()
    runtime = (PROJECT / args.runtime).resolve()
    if runtime.parent != PROJECT or not (runtime.name == "runtime" or runtime.name.startswith("runtime-")):
        parser.error("运行目录必须是项目内的 runtime 或 runtime-* 目录。")
    if not 1 <= args.port <= 65535:
        parser.error("端口须为 1 到 65535。")
    if args.reset_password and args.command != "init":
        parser.error("--reset-password 只能与 init 一起使用。")
    vault = args.vault.resolve() if args.vault is not None else None
    if vault is not None and not vault.is_dir():
        parser.error("--vault 必须指定一个现有目录。")
    if args.command != "stop" and (runtime / "private-network.json").exists():
        parser.error("请为本地版选择一个独立运行目录。")
    local_environment(runtime, vault)

    if args.command == "init":
        command = [sys.executable, str(PROJECT / "scripts" / "manage_access.py")]
        if args.reset_password:
            command.append("--reset-password")
        return subprocess.call(command, cwd=PROJECT)
    if args.command == "stop":
        from backend.app.execution_runtime import ProcessLock

        lock = ProcessLock(runtime / "supervisor.lock")
        if not lock.path.is_file():
            print("工作台当前未运行。")
            return 0
        if lock.acquire():
            lock.release()
            print("工作台当前未运行。")
            return 0
        (runtime / "stop-request").touch()
        print("已请求停止，正在等待 Web 和 Worker 退出。")
        return 0
    if not (PROJECT / "frontend" / "dist" / "index.html").is_file():
        parser.error("缺少前端构建，安装方法见 README。")
    from backend.app.access import configured
    from supervisor import Supervisor

    if not configured():
        parser.error("请先使用 init 设置登录密码。")
    try:
        with socket.socket() as listener:
            if os.name == "nt":
                listener.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            listener.bind(("127.0.0.1", args.port))
    except OSError as error:
        parser.error(f"无法使用端口 {args.port}，请选择其他端口。{error}")
    print(f"工作台地址 http://127.0.0.1:{args.port}，按 Ctrl+C 停止。", flush=True)
    try:
        Supervisor(runtime, args.port).run()
    except KeyboardInterrupt:
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
