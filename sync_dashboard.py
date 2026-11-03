"""本地纸盘面板同步器。

作用：云端 GitHub Actions 每分钟巡检一轮，状态与面板通过 sync_state.py 推到
`state-snapshot` 分支（该分支恒为 1 个提交，仓库不膨胀）。本脚本在本机
每 60 秒从该分支拉取最新状态并覆盖本地面板，让本地打开的面板始终接近云端。

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
SNAPSHOT_BRANCH = "state-snapshot"
INTERVAL = 60  # 秒

# 从云端拉取的文件（与 sync_state.py 的 FILES 保持一致）
SNAPSHOT_FILES = [
    "state/portfolio_paper.json",
    "state/equity_history_paper.jsonl",
    "state/heartbeat_paper.json",
    "state/scan_rotation.json",
    "dashboard_paper.html",
]


def run(cmd, timeout=120):
    return subprocess.run(
        cmd,
        cwd=str(BASE),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
    )


def fetch_snapshot() -> bool:
    """从 state-snapshot 分支拉取最新状态；成功返回 True。"""
    fetch = run(["git", "fetch", "--depth", "1", "origin", SNAPSHOT_BRANCH])
    if fetch.returncode != 0:
        stamp = time.strftime("%H:%M:%S")
        print(f"[{stamp}] 拉取 {SNAPSHOT_BRANCH} 失败：{(fetch.stderr or '').strip()[:120]}")
        return False

    updated = []
    for rel in SNAPSHOT_FILES:
        target = BASE / rel
        result = run(["git", "show", f"origin/{SNAPSHOT_BRANCH}:{rel}"])
        if result.returncode != 0:
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        # 面板是文本，状态 JSON 也按文本写（避免本地编码差异）
        try:
            target.write_text(result.stdout, encoding="utf-8")
            updated.append(rel)
        except OSError as exc:
            print(f"写入 {rel} 失败: {exc}")

    if updated:
        stamp = time.strftime("%H:%M:%S")
        print(f"[{stamp}] 已同步云端状态：{', '.join(updated)}")
        return True
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
    print(" 纸盘面板本地同步器（每 %d 秒从云端拉取一次）" % INTERVAL)
    print("=" * 60)
    fetch_snapshot()
    open_dashboard()
    while True:
        if once:
            return 0
        try:
            time.sleep(INTERVAL)
        except KeyboardInterrupt:
            print("\n已退出。")
            return 0
        fetch_snapshot()


if __name__ == "__main__":
    raise SystemExit(main())
