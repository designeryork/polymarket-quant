"""从 GitHub API 下载云端状态快照。

为什么不用 `git show origin/state-snapshot:...`
----------------------------------------------
Actions 里 `actions/checkout` 配的 origin 是浅克隆（fetch-depth 0 也不行，
因为快照分支被 --force 覆盖过），`git fetch --depth 1 origin
+refs/heads/state-snapshot:refs/remotes/origin/state-snapshot` 在浅克隆下
经常更新不了远程跟踪引用 —— 结果就是 `git show` 读到的是**过期甚至不存在**
的引用，引擎便以空白账本启动，把持仓和净值曲线清零。

GitHub API 直接读分支内容，没有引用缓存问题，也不受浅克隆影响。
"""

import base64
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

BASE = Path(__file__).resolve().parent

# 与 sync_state.py 的 FILES 保持一致
FILES = [
    "state/portfolio_paper.json",
    "state/equity_history_paper.jsonl",
    "state/heartbeat_paper.json",
    "state/scan_rotation.json",
    "dashboard_paper.html",
]

BRANCH = "state-snapshot"
API_ROOT = "https://api.github.com"
TIMEOUT = 60


def _token() -> str:
    return (
        os.getenv("GITHUB_TOKEN")
        or os.getenv("GH_TOKEN")
        or ""
    ).strip()


def _request(path: str) -> bytes:
    """请求 GitHub API，返回文件原始字节。"""
    url = f"{API_ROOT}{path}"
    headers = {
        "Accept": "application/vnd.github.raw",
        "User-Agent": "polymarket-quant-bot",
    }
    token = _token()
    if token:
        headers["Authorization"] = f"Bearer {token}"

    request = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
        return response.read()


def fetch(repo: str, branch: str = BRANCH) -> dict:
    """下载指定分支下的所有快照文件，返回 {相对路径: bytes}。"""
    result = {}
    for rel in FILES:
        try:
            result[rel] = _request(
                f"/repos/{repo}/contents/{urllib.parse.quote(rel)}?ref={branch}"
            )
        except urllib.error.HTTPError as exc:
            print(f"[fetch_state] {rel}: HTTP {exc.code}（跳过）")
        except Exception as exc:
            print(f"[fetch_state] {rel}: {type(exc).__name__}: {exc}（跳过）")
    return result


def restore(repo: str, branch: str = BRANCH) -> int:
    """把快照写回本地工作区。返回成功恢复的文件数。"""
    files = fetch(repo, branch)
    if not files:
        print("[fetch_state] 未取到任何快照文件")
        return 0

    count = 0
    for rel, content in files.items():
        target = BASE / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            if rel.endswith(".html"):
                target.write_bytes(content)
            else:
                target.write_bytes(content)
            count += 1
        except OSError as exc:
            print(f"[fetch_state] 写入 {rel} 失败: {exc}")

    # 账本是关键：单独校验它是否真的被恢复
    portfolio = BASE / "state" / "portfolio_paper.json"
    if portfolio.exists():
        try:
            data = json.loads(portfolio.read_text(encoding="utf-8"))
            print(
                "[fetch_state] 已恢复账本: 净值 ${:.2f} | 持仓 {} | 更新时间 {}".format(
                    float(data.get("total_equity_usdc") or 0),
                    len(data.get("open_positions") or []),
                    data.get("last_updated", "?"),
                )
            )
        except Exception as exc:
            print(f"[fetch_state] 账本解析失败: {exc}")
    else:
        print("[fetch_state] 警告：账本未恢复，引擎将以空白账本启动")

    print(f"[fetch_state] 共恢复 {count} 个文件")
    return count


def main() -> int:
    repo = os.getenv("GITHUB_REPOSITORY") or "designeryork/polymarket-quant"
    branch = os.getenv("SNAPSHOT_BRANCH") or BRANCH
    if len(sys.argv) > 1 and sys.argv[1] == "fetch":
        # 只打印内容摘要，用于排查
        files = fetch(repo, branch)
        for rel, content in files.items():
            print(f"--- {rel} ({len(content)} bytes) ---")
            if rel.endswith(".json"):
                try:
                    data = json.loads(content.decode("utf-8"))
                    if "total_equity_usdc" in data:
                        print(
                            "  净值 ${:.2f} | 持仓 {}".format(
                                float(data.get("total_equity_usdc") or 0),
                                len(data.get("open_positions") or []),
                            )
                        )
                    else:
                        print(f"  keys={list(data)[:8]}")
                except Exception:
                    pass
        return 0
    restore(repo, branch)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
