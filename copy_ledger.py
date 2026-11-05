"""跟单账本的持久化：把 state/copy/ 同步到独立的 `copy-ledger` 分支。

为什么单独开分支
----------------
* 主分支只放代码（保持干净、clone 快）；
* `state-snapshot` 是"单提交、每次强制覆盖"的实时快照，会丢失历史；
* 跟单数据要"永久化供分析"，所以用**只追加、不覆盖**的独立分支：
  - `state/copy/state.json`      当前账本（覆盖写，但只影响当前状态）
  - `state/copy/events.jsonl`    事件流（只追加）
  - `state/copy/equity/YYYY-MM-DD.jsonl` 每分钟净值（只追加，按天分文件）
  - `state/copy/heartbeat.json`  心跳

同步策略：本地保留一个克隆，每轮 push 时 commit 一次；不 force、不 rebase 历史。
"""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path
from typing import List, Optional

BASE = Path(__file__).resolve().parent
STATE_DIR = BASE / "state" / "copy"
WORK_DIR = BASE / "state" / "copy_ledger_repo"
BRANCH = "copy-ledger"


def _token() -> str:
    return (os.getenv("GITHUB_TOKEN") or os.getenv("GH_TOKEN") or "").strip()


def _repo() -> str:
    return os.getenv("GITHUB_REPOSITORY") or "designeryork/polymarket-quant"


def _git(args: List[str], cwd: Path, check: bool = True) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env.setdefault("GIT_TERMINAL_PROMPT", "0")
    env.setdefault("GIT_AUTHOR_NAME", "polymarket-copy-bot")
    env.setdefault("GIT_AUTHOR_EMAIL", "bot@users.noreply.github.com")
    env.setdefault("GIT_COMMITTER_NAME", "polymarket-copy-bot")
    env.setdefault("GIT_COMMITTER_EMAIL", "bot@users.noreply.github.com")
    return subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True,
        encoding="utf-8", errors="replace", env=env, timeout=180,
    )


def _log(message: str) -> None:
    print(f"[copy_ledger] {message}", flush=True)


def available() -> bool:
    return bool(_token())


def ensure_clone() -> bool:
    """准备本地克隆；分支不存在时创建 orphan 分支。"""
    if not available():
        _log("没有 GITHUB_TOKEN，跳过同步")
        return False
    url = f"https://x-access-token:{_token()}@github.com/{_repo()}.git"
    if (WORK_DIR / ".git").exists():
        fetch = _git(["fetch", "--depth", "1", "origin", BRANCH], WORK_DIR, check=False)
        if fetch.returncode == 0:
            _git(["reset", "--hard", "FETCH_HEAD"], WORK_DIR, check=False)
            return True
        # 分支被删或首次创建：重建工作目录
        shutil.rmtree(WORK_DIR, ignore_errors=True)

    WORK_DIR.mkdir(parents=True, exist_ok=True)
    _git(["init", "-q"], WORK_DIR)
    _git(["remote", "add", "origin", url], WORK_DIR, check=False)
    fetch = _git(["fetch", "--depth", "1", "origin", BRANCH], WORK_DIR, check=False)
    if fetch.returncode == 0:
        _git(["checkout", "-q", "-B", BRANCH, "FETCH_HEAD"], WORK_DIR)
        _log("已拉取 copy-ledger 分支")
    else:
        _git(["checkout", "-q", "--orphan", BRANCH], WORK_DIR)
        _log("copy-ledger 分支不存在，已创建")
    return True


def restore() -> int:
    """把账本分支里的文件恢复到本地 state/copy/。"""
    if not ensure_clone():
        return 0
    source = WORK_DIR / "state" / "copy"
    if not source.exists():
        _log("账本分支是空的（首次运行）")
        return 0
    copied = 0
    for path in source.rglob("*"):
        if path.is_dir():
            continue
        rel = path.relative_to(source)
        target = STATE_DIR / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, target)
        copied += 1
    _log(f"已恢复 {copied} 个账本文件")
    return copied


def push(message: Optional[str] = None) -> bool:
    """把本地 state/copy/ 写回克隆并推送。"""
    if not available():
        return False
    if not (WORK_DIR / ".git").exists() and not ensure_clone():
        return False
    if not STATE_DIR.exists():
        return False

    target = WORK_DIR / "state" / "copy"
    target.mkdir(parents=True, exist_ok=True)
    for path in STATE_DIR.rglob("*"):
        if path.is_dir():
            continue
        rel = path.relative_to(STATE_DIR)
        dest = target / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, dest)

    _git(["add", "-A", "state/copy"], WORK_DIR, check=False)
    staged = _git(["diff", "--cached", "--quiet"], WORK_DIR, check=False)
    if staged.returncode == 0:
        _log("账本无变化，跳过提交")
        return True
    commit = _git(["commit", "-q", "-m", message or "update copy ledger"], WORK_DIR, check=False)
    if commit.returncode != 0:
        _log(f"提交失败：{(commit.stderr or '').strip()[:160]}")
        return False
    result = _git(["push", "origin", f"HEAD:{BRANCH}"], WORK_DIR, check=False)
    if result.returncode != 0:
        # 偶发远端领先：拉一次再推
        _git(["fetch", "origin", BRANCH], WORK_DIR, check=False)
        _git(["reset", "--soft", "FETCH_HEAD"], WORK_DIR, check=False)
        result = _git(["push", "origin", f"HEAD:{BRANCH}"], WORK_DIR, check=False)
    if result.returncode != 0:
        _log(f"推送失败：{(result.stderr or '').strip()[:160]}")
        return False
    _log("账本已推送")
    return True


def main() -> int:
    import sys

    action = sys.argv[1] if len(sys.argv) > 1 else "restore"
    if action == "restore":
        restore()
    elif action == "push":
        push()
    else:
        print("用法: python copy_ledger.py [restore|push]")
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
