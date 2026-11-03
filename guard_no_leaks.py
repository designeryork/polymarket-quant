"""防泄露守卫：确认仓库里没有不该公开的文件。

背景
----
仓库已转公开。任何被提交进 master 的私人文件都会立刻对所有人可见，
且 git 历史一旦推送就难以彻底撤回（只能重写历史 + 强推）。

本脚本做两件事：
  1. **提交前检查**：作为 pre-commit 钩子运行，发现敏感文件被暂存就拒绝提交；
  2. **仓库自检**：可手动运行，审计当前 master 与工作区是否有泄露风险。

用法：
    python guard_no_leaks.py --check-staged    # 提交前检查（pre-commit 钩子用）
    python guard_no_leaks.py --audit            # 全仓审计
"""

import argparse
import subprocess
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent

# 绝不允许进入公开仓的路径（相对仓库根）
FORBIDDEN_PATHS = [
    "002_follow/",              # 另一个独立项目（跟单 copybot）
    "000-给ai的promt/",         # 个人 prompt 素材
    ".workbuddy/",              # AI 工作记忆与身份文件
    "035_polymarket_paper/",    # 独立部署副本
]

# 绝不允许进入公开仓的文件名模式
FORBIDDEN_PATTERNS = [
    ".env",
    ".env.*",
    "*wallet*.json",
    "*钱包*",
    "*.pem",
    "*.key",
    "id_rsa*",
    "*.p12",
    "*.pfx",
    "seed*.json",
    "mnemonic*",
]

# 已知允许公开的例外（精确路径）
ALLOW_EXCEPTIONS = [
    "setup_wallet.py",          # 密钥生成脚本，本身不含密钥
    "LICENSE",
]


def _norm(path: str) -> str:
    return path.replace("\\", "/")


def _is_forbidden(path: str) -> str:
    """返回命中的禁止规则；没命中返回空字符串。"""
    p = _norm(path)
    if p in ALLOW_EXCEPTIONS:
        return ""
    for prefix in FORBIDDEN_PATHS:
        if p.startswith(prefix):
            return f"路径禁止规则 {prefix}"
    name = p.rsplit("/", 1)[-1]
    for pattern in FORBIDDEN_PATTERNS:
        if fnmatch(name, pattern):
            return f"文件名禁止规则 {pattern}"
    return ""


def fnmatch(name: str, pattern: str) -> bool:
    import fnmatch as _fn

    return _fn.fnmatch(name, pattern)


def check_staged() -> int:
    """检查暂存区；有禁止文件则拒绝提交。"""
    result = subprocess.run(
        ["git", "diff", "--cached", "--name-only", "--diff-filter=ACMR"],
        cwd=str(BASE), capture_output=True, text=True,
        encoding="utf-8", errors="replace",
    )
    if result.returncode != 0:
        print(f"[guard] 无法读取暂存区（exit {result.returncode}），跳过检查")
        return 0

    # git 输出里的中文路径是八进制转义，需要用 -z 拿原始字节
    raw = subprocess.run(
        ["git", "diff", "--cached", "--name-only", "-z", "--diff-filter=ACMR"],
        cwd=str(BASE), capture_output=True,
    )
    if raw.returncode == 0:
        paths = [p.decode("utf-8", "replace") for p in raw.stdout.split(b"\0") if p]
    else:
        paths = [line for line in result.stdout.splitlines() if line.strip()]

    violations = []
    for path in paths:
        reason = _is_forbidden(path)
        if reason:
            violations.append((path, reason))

    if violations:
        print("::error::检测到禁止公开的文件被暂存，已阻止提交：")
        for path, reason in violations:
            print(f"  - {path}  ({reason})")
        print("\n这些文件属于私人内容或另一个项目，不应进入公开仓库。")
        print("如果确实需要提交，请先评估泄露风险并更新本脚本的规则。")
        return 1

    if paths:
        print(f"[guard] 暂存区检查通过（{len(paths)} 个文件）")
    return 0


def audit() -> int:
    """审计当前 master 分支与工作区。"""
    print("=" * 66)
    print(" 公开仓防泄露审计")
    print("=" * 66)

    issues = []

    # 1) master 分支内容
    result = subprocess.run(
        ["git", "ls-tree", "-r", "--name-only", "-z", "master"],
        cwd=str(BASE), capture_output=True,
    )
    tracked = [p.decode("utf-8", "replace") for p in result.stdout.split(b"\0") if p]
    print(f"\n[1] master 分支跟踪文件：{len(tracked)} 个")
    for path in tracked:
        reason = _is_forbidden(path)
        if reason:
            issues.append(("master", path, reason))

    # 2) 公开分支的全历史
    #    只查真正公开的分支（origin/master + master）——
    #    本地 backup/* 备份分支不推送，GitHub 上看不到，不算泄露。
    history = []
    for ref in ("master", "origin/master"):
        result = subprocess.run(
            ["git", "log", ref, "--name-only", "--pretty=format:", "-z"],
            cwd=str(BASE), capture_output=True,
        )
        if result.returncode == 0:
            history.extend(
                p.decode("utf-8", "replace") for p in result.stdout.split(b"\0") if p
            )
    print(f"[2] 公开分支历史涉及路径：{len(set(history))} 个")
    for path in sorted(set(history)):
        reason = _is_forbidden(path)
        if reason:
            issues.append(("history", path, reason))

    # 3) 敏感文件内容扫描（私钥特征）
    print("[3] 扫描跟踪文件中的私钥特征…")
    import re

    # 说明：交易哈希 / 订单 ID 也是 64 位十六进制，但它们是链上公开数据
    #（任何人都能在区块浏览器查到），不是私钥。私钥只在签名瞬间存在，
    #绝不会出现在账本或文档里。因此这里只匹配"私钥"语境，不匹配哈希。
    secret_patterns = [
        (re.compile(r"PRIVATE_KEY\s*=\s*['\"][0-9a-fA-Fx]{20,}['\"]"), "明文 PRIVATE_KEY"),
        (re.compile(r"private_?key\s*[:=]\s*['\"][0-9a-fA-Fx]{40,}['\"]"), "明文 private key"),
        (re.compile(r"(?i)mnemonic\s*[:=]\s*['\"][^'\"]{20,}"), "助记词"),
        (re.compile(r"(?i)(助记词|私钥)\s*[:：]\s*['\"]?[0-9a-zA-Z]{20,}"), "中文语境下的密钥"),
    ]
    for path in tracked:
        if path.endswith((".png", ".jpg", ".db", ".docx", ".exe", ".zip")):
            continue
        full = BASE / path
        if not full.exists() or full.stat().st_size > 3 * 1024 * 1024:
            continue
        try:
            text = full.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for pattern, label in secret_patterns:
            for match in pattern.finditer(text):
                snippet = match.group(0)[:24]
                issues.append(("content", path, f"{label}: {snippet}…"))
                break

    print()
    if issues:
        print(f"❌ 发现 {len(issues)} 个问题：")
        for scope, path, reason in issues:
            print(f"  [{scope}] {path}  —— {reason}")
        return 1

    print("✅ 未发现泄露风险：master 与历史均无禁止路径，扫描文件无私钥特征。")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check-staged", action="store_true", help="检查暂存区（pre-commit 用）")
    parser.add_argument("--audit", action="store_true", help="全仓审计")
    args = parser.parse_args()

    if args.check_staged:
        return check_staged()
    if args.audit:
        return audit()
    parser.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
