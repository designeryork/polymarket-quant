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


def engine_processes(paper: bool = False):
    """返回指定模式的引擎进程（按命令行是否含 --paper 区分）。"""
    rows = []
    for proc in psutil.process_iter(["pid", "name", "cmdline"]):
        try:
            cmd = proc.info["cmdline"] or []
            if not (proc.info["name"] and "python" in proc.info["name"].lower()):
                continue
            if not any(str(item).endswith("quant_engine.py") for item in cmd):
                continue
            is_paper = any("--paper" in str(item) for item in cmd)
            if is_paper == paper:
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


def check_once():
    if STOP_FILE.exists():
        return {"action": "STOPPED_BY_USER", "detail": "STOP present; watchdog will not restart"}
    return {
        "live": check_mode(paper=False),
        "paper": check_mode(paper=True),
    }


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
