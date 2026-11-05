"""跟单纸盘的云端常驻器：每分钟轮询，定期落账本、发页面。

一次 job 跑 N 小时（GitHub 单 job 上限 6h），结束前触发下一轮，形成不断链的 24/7。
与主策略 job 使用不同的 concurrency 组，互不阻塞。
"""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import time
from pathlib import Path

import copy_dashboard
import copy_engine as engine
import copy_ledger

BASE = Path(__file__).resolve().parent
PAGES_DIR = BASE / "state" / "pages_repo_copy"
PAGES_FILE = "copy.html"


def log(message: str) -> None:
    print(f"[copy_runner] {message}", flush=True)


# ---------------------------------------------------------------------------
# 面板发布（与主策略 job 共用同一个 Pages 仓，只写 copy.html）
# ---------------------------------------------------------------------------
def _git(args, cwd: Path, timeout: int = 180):
    return subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=timeout,
    )


def publish_pages(pages_repo: str, token: str) -> None:
    if not pages_repo or not token or not (BASE / PAGES_FILE).exists():
        return
    url = f"https://x-access-token:{token}@github.com/{pages_repo}.git"
    try:
        if not (PAGES_DIR / ".git").exists():
            shutil.rmtree(PAGES_DIR, ignore_errors=True)
            clone = _git(["clone", "--depth", "1", url, str(PAGES_DIR)], BASE)
            if clone.returncode != 0:
                log(f"面板仓克隆失败：{(clone.stderr or '').strip()[:150]}")
                return
        else:
            fetch = _git(["fetch", "--depth", "1", "origin", "main"], PAGES_DIR)
            if fetch.returncode == 0:
                _git(["reset", "--hard", "FETCH_HEAD"], PAGES_DIR)

        shutil.copy2(BASE / PAGES_FILE, PAGES_DIR / PAGES_FILE)
        _git(["config", "user.name", "polymarket-copy-bot"], PAGES_DIR)
        _git(["config", "user.email", "bot@users.noreply.github.com"], PAGES_DIR)
        _git(["add", PAGES_FILE], PAGES_DIR)
        if _git(["diff", "--cached", "--quiet"], PAGES_DIR).returncode == 0:
            log("面板无变化，跳过提交")
            return
        _git(["commit", "-m", f"update copy dashboard {engine.utc_now()}"], PAGES_DIR)
        push = _git(["push", "origin", "HEAD:main"], PAGES_DIR)
        if push.returncode != 0:
            # 主策略 job 可能刚推过 index.html：rebase 后再试
            _git(["pull", "--rebase", "origin", "main"], PAGES_DIR)
            _git(["push", "origin", "HEAD:main"], PAGES_DIR)
        log("面板已发布（copy.html）")
    except Exception as exc:
        log(f"面板发布异常：{type(exc).__name__}: {exc}")


# ---------------------------------------------------------------------------
# 主循环
# ---------------------------------------------------------------------------
def run(hours: float, interval: int, push_interval: int) -> None:
    deadline = time.time() + hours * 3600
    session = engine.build_session()
    state = engine.load_state()
    log(f"启动：{hours} 小时 / {interval}s 轮询 / {push_interval}s 落账")

    last_push = 0.0
    cycles = 0
    while time.time() < deadline:
        if (BASE / "STOP").exists():
            log("检测到 STOP 文件，优雅退出")
            break
        started = time.time()
        try:
            engine.run_cycle(session, state)
            cycles += 1
        except Exception as exc:
            log(f"本轮异常：{type(exc).__name__}: {exc}")

        try:
            copy_dashboard.write(state)
        except Exception as exc:
            log(f"面板生成失败：{type(exc).__name__}: {exc}")

        if time.time() - last_push >= push_interval:
            copy_ledger.push(f"copy ledger {engine.utc_now()} ({cycles} cycles)")
            publish_pages(
                (os.getenv("PAGES_REPO") or "").strip(),
                (os.getenv("PAGES_TOKEN") or "").strip(),
            )
            last_push = time.time()
            summary = " | ".join(
                f"{tid} ${engine.account_summary(acc)['equity_usd']:.2f}"
                for tid, acc in state["accounts"].items()
            )
            log(f"周期 {cycles} 完成：{summary}")

        elapsed = time.time() - started
        time.sleep(max(1.0, interval - elapsed))

    # 收尾：确保最后一轮数据落地并发布
    try:
        copy_dashboard.write(state)
        copy_ledger.push(f"copy ledger final {engine.utc_now()}")
        publish_pages(
            (os.getenv("PAGES_REPO") or "").strip(),
            (os.getenv("PAGES_TOKEN") or "").strip(),
        )
    except Exception as exc:
        log(f"收尾失败：{type(exc).__name__}: {exc}")
    log(f"结束，共 {cycles} 轮")


def main() -> int:
    parser = argparse.ArgumentParser(description="跟单纸盘云端常驻器")
    parser.add_argument("--hours", type=float, default=5.0)
    parser.add_argument("--interval", type=int, default=60)
    parser.add_argument("--push-interval", type=int, default=300)
    args = parser.parse_args()

    copy_ledger.restore()
    run(args.hours, args.interval, args.push_interval)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
