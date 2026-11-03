"""云端状态同步器：把纸盘状态推到一个"单提交分支"，避免仓库膨胀。

为什么需要它
------------
GitHub Actions 高频巡检时，如果每轮都往 master 提交状态：
  * `state/market_universe.json` 有 15MB，每分钟提交一次 = 900MB/小时，仓库会被撑爆；
  * `state/portfolio_paper.json` 400KB+（其中 scan_coverage 占 340KB），
    60 次/小时的 commit 历史同样不可接受；
  * master 变成"日志仓库"，代码历史被状态提交淹没。

方案：把状态写到一个**孤立的单一分支** `state-snapshot`：
  * 该分支每次都被 `--force` 覆盖，历史里永远只有 1 个提交；
  * 仓库体积恒定，不会随运行时间增长；
  * master 只保留代码提交，历史干净；
  * 本地用 `git fetch origin state-snapshot` 就能拉到最新状态。

实现要点：用**临时 index + commit-tree**构造提交，
既不动 master 的工作区，也不需要额外的 worktree 目录。

被同步的文件（都做了瘦身，剔除巨大的缓存字段）：
  * `state/portfolio_paper.json`   —— 账本（去掉 scan_coverage / recent_decisions）
  * `state/equity_history_paper.jsonl` —— 净值曲线（只保留最近 N 行）
  * `state/heartbeat_paper.json`   —— 心跳
  * `state/scan_rotation.json`     —— 扫描轮转位置
  * `dashboard_paper.html`         —— 面板

用法：
    python sync_state.py push    # 把本地状态推到 state-snapshot 分支
    python sync_state.py pull    # 检查远端快照分支是否可访问
"""
import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

BASE = Path(__file__).resolve().parent
STATE_DIR = BASE / "state"
BRANCH = "state-snapshot"

# 只同步这些文件（相对 BASE）
FILES = [
    "state/portfolio_paper.json",
    "state/equity_history_paper.jsonl",
    "state/heartbeat_paper.json",
    "state/scan_rotation.json",
    "dashboard_paper.html",
]

# 净值曲线保留的最大行数（防止无限增长）
EQUITY_HISTORY_MAX_LINES = 3000


def run(cmd, cwd=None, check=True):
    result = subprocess.run(
        cmd,
        cwd=str(cwd or BASE),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if check and result.returncode != 0:
        sys.stderr.write(
            f"[sync_state] 命令失败: {' '.join(cmd)}\n{result.stdout}\n{result.stderr}\n"
        )
        raise SystemExit(result.returncode)
    return result


def slim_portfolio(src: Path) -> dict:
    """去掉 scan_coverage 等巨大诊断字段，只保留账本核心内容。

    scan_coverage 有 340KB，记录的是"本轮扫了哪些市场"，
    对账本连续性没有任何作用，但每分钟提交一次会让仓库迅速膨胀。
    """
    data = json.loads(src.read_text(encoding="utf-8"))
    data.pop("scan_coverage", None)
    data.pop("recent_decisions", None)
    return data


def slim_equity_history(src: Path) -> str:
    """只保留最近 N 行净值记录。"""
    if not src.exists():
        return ""
    lines = src.read_text(encoding="utf-8").strip().splitlines()
    lines = lines[-EQUITY_HISTORY_MAX_LINES:]
    return "\n".join(lines) + ("\n" if lines else "")


def collect(workdir: Path) -> None:
    """把要同步的文件（瘦身后）写入 workdir。"""
    for rel in FILES:
        src = BASE / rel
        dst = workdir / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        if rel == "state/portfolio_paper.json":
            if not src.exists():
                continue
            dst.write_text(
                json.dumps(slim_portfolio(src), ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        elif rel == "state/equity_history_paper.jsonl":
            dst.write_text(slim_equity_history(src), encoding="utf-8")
        else:
            if src.exists():
                shutil.copy2(src, dst)


def push(message: str = "state: snapshot") -> None:
    if not (BASE / ".git").exists():
        sys.stderr.write("[sync_state] 不是 git 仓库，跳过。\n")
        return

    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp) / "files"
        work.mkdir()
        collect(work)

        # 用临时 index 构造提交：完全不动 master 的 index 与工作区
        index_file = str(Path(tmp) / "index")
        env = dict(os.environ, GIT_INDEX_FILE=index_file)

        # 1) 清空临时 index
        subprocess.run(["git", "read-tree", "--empty"], cwd=str(BASE), env=env, check=True)
        # 2) 把 workdir 里的文件加入临时 index（路径相对仓库根）
        subprocess.run(
            ["git", "--work-tree", str(work), "add", "-A", "."],
            cwd=str(BASE),
            env=env,
            check=True,
        )
        # 3) 写出 tree 与 commit（无父提交 = 孤立根提交，历史恒为 1 个）
        tree = subprocess.run(
            ["git", "write-tree"], cwd=str(BASE), env=env,
            capture_output=True, text=True, encoding="utf-8", errors="replace", check=True,
        ).stdout.strip()
        commit = subprocess.run(
            ["git", "commit-tree", tree, "-m", message],
            cwd=str(BASE), env=env,
            capture_output=True, text=True, encoding="utf-8", errors="replace", check=True,
        ).stdout.strip()

        # 4) 强制推送覆盖该分支
        result = subprocess.run(
            ["git", "push", "--force", "origin", f"{commit}:refs/heads/{BRANCH}"],
            cwd=str(BASE),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        if result.returncode != 0:
            sys.stderr.write(f"[sync_state] push 失败: {result.stderr}\n")
            raise SystemExit(result.returncode)
        print(f"[sync_state] 已推送状态快照到 {BRANCH} 分支")


def pull() -> None:
    """检查远端快照分支是否可访问（本地只读场景）。"""
    run(["git", "fetch", "--depth", "1", "origin", BRANCH], check=False)
    result = subprocess.run(
        ["git", "show", f"origin/{BRANCH}:state/portfolio_paper.json"],
        cwd=str(BASE),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if result.returncode != 0:
        sys.stderr.write("[sync_state] 拉取失败：远端还没有 state-snapshot 分支？\n")
        raise SystemExit(result.returncode)
    print("[sync_state] state-snapshot 分支可访问")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=["push", "pull"])
    args = parser.parse_args()
    if args.action == "push":
        push()
    else:
        pull()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
