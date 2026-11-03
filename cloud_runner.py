"""云端长驻巡检器：跑引擎循环 + 周期性推送状态快照。

为什么需要这个脚本
------------------
GitHub Actions 的单 job 最长 6 小时，云端要用"长驻 job + 自续跑"实现每分钟一轮。
但如果状态只在 job 结束时推送，一旦 job 中途崩溃、被取消、或 Runner 异常退出，
这几小时累积的账本与净值曲线就全丢了 —— 对量化策略来说这是不可接受的：
账本丢失意味着持仓归零、净值曲线断裂。

本脚本的职责：
  1. 后台跑 `quant_engine.py --loop --interval 60`（每分钟一轮）；
  2. 每隔 N 分钟（默认 5）把状态推到 state-snapshot 分支 —— 即使随后崩溃，
     也只损失 N 分钟内的数据；
  3. 到点后创建 STOP 文件让引擎优雅停机，确保账本写完整再退出。

用法（GitHub Actions 中）：
    python cloud_runner.py --hours 5 --push-interval 300
"""
import argparse
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

BASE = Path(__file__).resolve().parent
STATE_DIR = BASE / "state"
STOP_FILE = BASE / "STOP"
SYNC_SCRIPT = BASE / "sync_state.py"


def log(message: str) -> None:
    print(f"[cloud_runner] {message}", flush=True)


def run_sync(reason: str) -> None:
    """推送一次状态快照；失败只告警，不中断巡检。"""
    try:
        result = subprocess.run(
            [sys.executable, str(SYNC_SCRIPT), "push"],
            cwd=str(BASE),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=180,
        )
        if result.returncode == 0:
            log(f"状态快照已推送（{reason}）")
        else:
            log(f"状态快照推送失败（{reason}）: {(result.stderr or '').strip()[:200]}")
    except Exception as exc:
        log(f"状态快照推送异常（{reason}）: {type(exc).__name__}: {exc}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--hours", type=float, default=5.0, help="本次连续运行时长（小时）")
    parser.add_argument("--interval", type=int, default=60, help="引擎轮询间隔（秒）")
    parser.add_argument("--push-interval", type=int, default=300, help="状态推送间隔（秒）")
    args = parser.parse_args()

    total_seconds = max(60.0, args.hours * 3600)
    deadline = time.time() + total_seconds

    # 引擎子进程：--loop 持续轮询，--paper 强制纸盘账本
    env = dict(os.environ)
    env.setdefault("PAPER", "true")
    env.setdefault("ALLOW_LIVE", "false")
    env.setdefault("ALLOW_DIRECT", "true")

    # 启动前清掉可能残留的 STOP（上一轮崩溃留下的）
    if STOP_FILE.exists():
        STOP_FILE.unlink()
        log("清理了残留的 STOP 文件")

    engine = subprocess.Popen(
        [sys.executable, "quant_engine.py", "--loop", "--interval", str(args.interval), "--paper"],
        cwd=str(BASE),
        env=env,
    )
    log(f"引擎已启动 PID {engine.pid}，轮询间隔 {args.interval}s，总时长 {args.hours}h")

    def shutdown(signum, frame):
        log(f"收到信号 {signum}，准备停止引擎")
        try:
            STOP_FILE.write_text("cloud_runner shutdown", encoding="utf-8")
        except OSError:
            pass

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)

    next_push = time.time() + args.push_interval
    last_push_ok = True

    try:
        while True:
            # 引擎自行退出（异常或读到 STOP）→ 收尾退出
            if engine.poll() is not None:
                log(f"引擎已退出，返回码 {engine.returncode}")
                break

            now = time.time()
            if now >= deadline:
                log("到达运行时长上限，创建 STOP 文件让引擎优雅停机")
                try:
                    STOP_FILE.write_text("cloud_runner: run window finished", encoding="utf-8")
                except OSError:
                    pass
                # 给引擎一轮时间写完账本
                try:
                    engine.wait(timeout=180)
                except subprocess.TimeoutExpired:
                    log("引擎未在 180s 内退出，强制终止")
                    engine.terminate()
                    try:
                        engine.wait(timeout=30)
                    except subprocess.TimeoutExpired:
                        engine.kill()
                break

            if now >= next_push:
                run_sync("周期性")
                last_push_ok = True
                next_push = time.time() + args.push_interval

            time.sleep(5)
    finally:
        # 无论怎么退出，都推最后一次，确保最新状态落地
        if STOP_FILE.exists():
            try:
                STOP_FILE.unlink()
            except OSError:
                pass
        run_sync("收尾")
        log("巡检结束")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
