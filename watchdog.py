"""Respect STOP, restart a missing/stalled engine, and optionally send webhook alerts.

同时监管两个引擎：
  * 实盘（`quant_engine.py --loop`）—— 依赖单实例锁，账本 state/portfolio.json
  * 纸面（`quant_engine.py --loop --paper`）—— 不申请锁，账本 state/portfolio_paper.json

两者用**各自的心跳文件**判断存活：heartbeat.json / heartbeat_paper.json。
旧实现只看实盘，纸面进程挂掉（例如系统重启后）不会有人拉起，
面板就会一直刷新同一份旧快照。
"""
import argparse
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import psutil
import requests

BASE = Path(__file__).resolve().parent
STOP_FILE = BASE / "STOP"
PYTHON = Path(r"D:\Programs\Python\Python314\python.exe")
if not PYTHON.exists():
    PYTHON = Path(sys.executable)

# 心跳超过该秒数视为卡死
STALE_SECONDS = 900

# 外部实例监护（阶段 A/B 对照）
# --------------------------------------------------
# 会话里启动的进程会被会话清理机制干掉（DETACHED_PROCESS 也拦不住），
# 而本看门狗是用户双击 bat 启动的，不受会话影响 ——
# 所以让它顺带看护 035 副本的 A/B 两组，对照实验才能连续跑。
#
# ⚠️ 只监控 + 用对方的启动器拉起，**不跨部署 kill 进程**。
#    跨部署 terminate 是最危险的误操作（副本阶段 A 踩过）。
#
# 格式：{"dir": 部署目录, "tag": 实例标签, "launcher": 启动脚本名}
EXTERNAL_INSTANCES = [
    {
        "dir": r"D:\2026_APPs by AI\035_polymarket_paper",
        "tag": "net",
        "launcher": "start_ab.py",
    },
    {
        "dir": r"D:\2026_APPs by AI\035_polymarket_paper",
        "tag": "gross",
        "launcher": "start_ab.py",
    },
]


def engine_processes(paper: bool = False):
    """返回指定模式的引擎进程（按命令行是否含 --paper 区分）。

    跨部署隔离（重要）：只认工作目录在本 BASE 下的进程。
    旧实现按命令行**全局扫描**，不区分部署目录 —— 于是：

      1. 035_polymarket_paper 副本的纸面实例会被误认成 032 自己的纸面进程。
         结果：032 的真纸面进程挂掉时，watchdog 看到「副本还活着」→ **不重启**；
      2. 更危险：心跳超时时 watchdog 会把**别的部署**的引擎 terminate 掉。

    同一个电脑上跑多个部署（032 实盘 + 035 纸盘副本）时，这两个后果都是真实故障。
    psutil 的 proc.cwd() 在 Windows 上可读，用它做部署归属判定是可靠的。
    """
    base = os.path.normcase(os.path.abspath(str(BASE)))
    rows = []
    for proc in psutil.process_iter(["pid", "name", "cmdline"]):
        try:
            cmd = proc.info["cmdline"] or []
            if not (proc.info["name"] and "python" in proc.info["name"].lower()):
                continue
            if not any(str(item).endswith("quant_engine.py") for item in cmd):
                continue
            is_paper = any("--paper" in str(item) for item in cmd)
            if is_paper != paper:
                continue
            try:
                cwd = os.path.normcase(os.path.abspath(proc.cwd()))
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
            if cwd != base:
                continue
            rows.append(proc)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass
    return rows


def heartbeat_file(paper: bool = False) -> Path:
    return BASE / "state" / ("heartbeat_paper.json" if paper else "heartbeat.json")


def heartbeat_age_seconds(paper: bool = False):
    try:
        data = json.loads(heartbeat_file(paper).read_text(encoding="utf-8"))
        updated = datetime.fromisoformat(data["updated_at_utc"])
        if updated.tzinfo is None:
            updated = updated.replace(tzinfo=timezone.utc)
        return max(0.0, (datetime.now(timezone.utc) - updated).total_seconds())
    except Exception:
        return None


def send_alert(message):
    url = os.getenv("ALERT_WEBHOOK_URL", "").strip()
    if not url:
        return False
    try:
        response = requests.post(url, json={"text": message}, timeout=12)
        response.raise_for_status()
        return True
    except requests.RequestException:
        return False


def start_engine(paper: bool = False):
    suffix = "_paper" if paper else ""
    stdout = open(BASE / "logs" / f"loop_stdout{suffix}.log", "a", encoding="utf-8")
    stderr = open(BASE / "logs" / f"loop_stderr{suffix}.log", "a", encoding="utf-8")
    flags = (
        getattr(subprocess, "DETACHED_PROCESS", 0x00000008)
        | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200)
        | getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
    )
    args = [str(PYTHON), "quant_engine.py", "--loop", "--interval", "120" if paper else "180"]
    env = None
    if paper:
        # 纸面必须允许开新仓（.env 里是 false，这里显式覆盖；dotenv 不会覆盖已有环境变量）
        args.append("--paper")
        env = dict(os.environ)
        env["PAPER"] = "true"
        env["ALLOW_NEW_ENTRIES"] = "true"
    proc = subprocess.Popen(
        args, cwd=str(BASE), env=env, stdout=stdout, stderr=stderr,
        creationflags=flags, close_fds=True,
    )
    return proc.pid


def check_mode(paper: bool) -> dict:
    """检查并（必要时）拉起某一种模式的引擎。"""
    label = "paper" if paper else "live"
    processes = engine_processes(paper=paper)
    age = heartbeat_age_seconds(paper=paper)
    if processes and age is not None and age <= STALE_SECONDS:
        return {
            "mode": label, "action": "HEALTHY",
            "pid": processes[0].pid, "heartbeat_age_s": round(age, 1),
        }
    if processes and age is None:
        return {"mode": label, "action": "HEALTHY_STARTING", "pid": processes[0].pid}
    if processes:
        send_alert(f"Polymarket {label} 引擎心跳超过 {STALE_SECONDS // 60} 分钟，准备恢复 PID {processes[0].pid}")
        for proc in processes:
            try:
                proc.terminate()
                proc.wait(timeout=10)
            except psutil.TimeoutExpired:
                proc.kill()
            except psutil.NoSuchProcess:
                pass
    else:
        send_alert(f"Polymarket {label} 引擎不在运行，看门狗正在恢复")
    pid = start_engine(paper=paper)
    return {"mode": label, "action": "RESTARTED", "pid": pid, "previous_heartbeat_age_s": age}


def check_external_instances() -> dict:
    """监护**其它部署**里的实例（当前：035_polymarket_paper 的 A/B 对照组）。

    为什么需要：会话里启动的进程会被会话清理机制干掉，
    DETACHED_PROCESS 也拦不住。而 032 的看门狗是用户双击 bat 启动的，
    不受会话影响 —— 所以让它顺带看护 A/B 两组，对照实验才能连续跑。

    关键点：**只监控、不 kill**。跨部署的进程不属于本看门狗的职责范围，
    只在「该实例彻底不在了」时用对应部署自己的启动脚本拉起。
    """
    base = os.path.normcase(os.path.abspath(str(BASE)))
    results = {}
    for spec in EXTERNAL_INSTANCES:
        directory = Path(spec["dir"])
        tag = spec["tag"]
        if not directory.exists():
            results[tag] = {"mode": f"external[{tag}]", "action": "SKIP", "detail": "目录不存在"}
            continue
        # 该部署自己的启动脚本（用它拉起，避免本看门狗跨部署拼命令行）
        launcher = directory / spec.get("launcher", "")
        rows = []
        for proc in psutil.process_iter(["pid", "cmdline"]):
            try:
                cmd = proc.info.get("cmdline") or []
                if not any(str(item).endswith("quant_engine.py") for item in cmd):
                    continue
                if not any("--paper" in str(item) for item in cmd):
                    continue
                if os.path.normcase(os.path.abspath(proc.cwd())) != os.path.normcase(str(directory)):
                    continue
                parts = [str(i) for i in cmd]
                found = ""
                for idx, item in enumerate(parts):
                    if item == "--instance-tag" and idx + 1 < len(parts):
                        found = parts[idx + 1].strip()
                if found == tag:
                    rows.append(proc)
            except Exception:
                continue
        if rows:
            results[tag] = {"mode": f"external[{tag}]", "action": "HEALTHY", "pid": rows[0].pid}
            continue
        # 实例不在 → 用该部署自己的启动脚本拉起
        # ⚠️ 用 `ensure` 而不是 `start`：只补缺失的那一组，
        #    不重启另一组正在正常跑的实例（否则两组会反复被重启，数据作废）。
        launcher = directory / spec.get("launcher", "")
        action = spec.get("action", "ensure")
        if not launcher.exists():
            results[tag] = {
                "mode": f"external[{tag}]", "action": "MISSING",
                "detail": f"实例不在且找不到启动器 {launcher.name}",
            }
            continue
        try:
            subprocess.Popen(
                [str(PYTHON), str(launcher), action],
                cwd=str(directory),
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=(
                    getattr(subprocess, "DETACHED_PROCESS", 0x00000008)
                    | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200)
                    | getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
                ),
                close_fds=True,
            )
            results[tag] = {"mode": f"external[{tag}]", "action": "RESTARTED_VIA_LAUNCHER"}
        except Exception as exc:
            results[tag] = {
                "mode": f"external[{tag}]", "action": "RESTART_FAILED",
                "detail": f"{type(exc).__name__}",
            }
    return results


def check_once():
    if STOP_FILE.exists():
        return {"action": "STOPPED_BY_USER", "detail": "STOP present; watchdog will not restart"}
    out = {
        "live": check_mode(paper=False),
        "paper": check_mode(paper=True),
    }
    if EXTERNAL_INSTANCES:
        out["external"] = check_external_instances()
    return out


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--interval", type=int, default=60)
    args = parser.parse_args()
    if args.once:
        print(json.dumps(check_once(), ensure_ascii=False))
        return
    while True:
        try:
            print(json.dumps(check_once(), ensure_ascii=False), flush=True)
        except Exception as exc:
            send_alert(f"Polymarket 看门狗异常: {type(exc).__name__}")
        time.sleep(max(15, args.interval))


if __name__ == "__main__":
    main()
