"""本地纸盘面板同步器。

作用：云端 GitHub Actions 每轮巡检后会把 dashboard_paper.html 和纸盘状态
推回本仓库；本脚本在本机每 60 秒 git pull 一次，让本地打开的面板始终保持
接近云端最新状态（面板自身每 15 秒自动刷新页面，读到的是 pull 下来的新文件）。

用法：
    python sync_dashboard.py            # 前台运行，Ctrl+C 退出
    python sync_dashboard.py --once     # 只同步一次（适合配任务计划程序）

启动时会自动用默认浏览器打开 dashboard_paper.html。
"""
import os
import subprocess
import sys
import time
from pathlib import Path

BASE = Path(__file__).resolve().parent
DASHBOARD = BASE / "dashboard_paper.html"
INTERVAL = 60  # 秒


def git_pull() -> bool:
    """git pull --ff-only；返回是否成功（干净失败不中断循环）。"""
    try:
        result = subprocess.run(
            ["git", "pull", "--ff-only", "--autostash"],
            cwd=str(BASE),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=120,
        )
        output = (result.stdout or "") + (result.stderr or "")
        if result.returncode == 0:
            if "Already up to date" not in output and "已是最新" not in output:
                stamp = time.strftime("%H:%M:%S")
                print(f"[{stamp}] 已同步最新状态：{output.strip().splitlines()[-1] if output.strip() else ''}")
            return True
        stamp = time.strftime("%H:%M:%S")
        print(f"[{stamp}] git pull 失败（exit {result.returncode}）：{output.strip().splitlines()[-1] if output.strip() else ''}")
        return False
    except Exception as exc:
        print(f"[{time.strftime('%H:%M:%S')}] git pull 异常：{exc}")
        return False


def open_dashboard() -> None:
    if DASHBOARD.exists():
        os.startfile(str(DASHBOARD))  # noqa: S606 - Windows 默认浏览器
        print(f"[OK] 已在浏览器打开 {DASHBOARD.name}")
    else:
        print(f"[提示] {DASHBOARD.name} 尚不存在，等云端第一轮跑完并同步后再手动打开。")


def main() -> int:
    once = "--once" in sys.argv
    print("=" * 60)
    print(" 纸盘面板本地同步器（每 %d 秒 git pull 一次）" % INTERVAL)
    print("=" * 60)
    open_dashboard()
    while True:
        git_pull()
        if once:
            return 0
        try:
            time.sleep(INTERVAL)
        except KeyboardInterrupt:
            print("\n已退出。")
            return 0


if __name__ == "__main__":
    raise SystemExit(main())
