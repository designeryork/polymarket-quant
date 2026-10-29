"""Double-click helper: start Polymarket quant_engine --loop if not already running."""
import os
import subprocess
import sys
import time
from pathlib import Path

# PyInstaller onefile: __file__ is under _MEI* temp — use exe dir as project root.
if getattr(sys, "frozen", False):
    BASE = Path(sys.executable).resolve().parent
else:
    BASE = Path(__file__).resolve().parent
PY = Path(r"D:\Programs\Python\Python314\python.exe")
if not PY.exists():
    PY = Path(sys.executable)
LOG_DIR = BASE / "logs"
LOG_DIR.mkdir(exist_ok=True)
STOP = BASE / "STOP"

def loop_running() -> bool:
    """检查是否已有引擎实例在运行。

    优先使用原子 PID 锁文件（内核级排他，可识别 pythonw.exe 与 PyInstaller 打包体）；
    回退到命令行匹配，仅用于锁文件缺失的兼容场景。

    重要：锁文件存在不等于进程存活。机器重启或进程被强杀后会留下陈旧锁，
    此时必须按"进程是否存活"判断，否则会误报"已在运行"而永远启动不了后台。
    """
    try:
        sys.path.insert(0, str(BASE))
        import single_instance

        status = single_instance.lock_status()
        # 只有"锁存在 且 持锁进程仍存活"才算真正在运行
        if status.get("held") and status.get("alive"):
            return True
    except Exception:
        pass
    try:
        # 回退匹配必须转义正则并锚定，否则 `paper_copy.py --loop` 这类
        # 其它项目进程会被 `.` 通配误判为"引擎已在运行"。
        out = subprocess.check_output(
            [
                "powershell", "-NoProfile", "-Command",
                "Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | "
                "Where-Object { $_.CommandLine -match 'quant_engine\\.py\\s+--loop' } | "
                "Select-Object -ExpandProperty ProcessId",
            ],
            text=True,
            stderr=subprocess.DEVNULL,
        )
        return any(line.strip().isdigit() for line in out.splitlines())
    except Exception:
        return False

def main() -> int:
    os.chdir(BASE)
    print("=" * 60)
    print(" Polymarket 无人值守启动器")
    print("=" * 60)
    if STOP.exists():
        # 旧实现会直接删除 STOP 文件 —— 这让"紧急停机开关"失去可靠性：
        # 用户手动创建 STOP 后，任何一次启动器运行都会悄悄恢复交易。
        # 现在改为显式确认，并把原文件重命名保留（而非删除）。
        print("[警告] 检测到 STOP 文件（紧急停机开关）。")
        try:
            answer = input("输入 YES 确认移除 STOP 并恢复无人值守巡检: ").strip().upper()
        except EOFError:
            answer = ""
        if answer != "YES":
            print("[已取消] 未移除 STOP，自动交易保持停止状态。")
            return 1
        backup = STOP.with_name("STOP.disabled")
        try:
            if backup.exists():
                backup.unlink()
            STOP.rename(backup)
            print(f"[提示] 原 STOP 文件已重命名为 {backup.name}（未删除，可随时改回）。")
        except Exception as exc:
            print(f"[失败] 无法处理 STOP 文件: {exc}")
            return 1
    if loop_running():
        print("[OK] 循环已在运行，无需重复启动。")
        print("按任意键关闭…")
        try:
            input()
        except EOFError:
            pass
        return 0
    stdout = open(LOG_DIR / "loop_stdout.log", "a", encoding="utf-8")
    stderr = open(LOG_DIR / "loop_stderr.log", "a", encoding="utf-8")
    # 必须让子进程完全脱离父进程与作业对象，否则启动器一退出，
    # 子进程就会被连带终止（表现为"exe 显示启动成功，但后台跑了几轮就没了"）。
    # CREATE_NO_WINDOW 只隐藏窗口，不提供脱离语义；
    # DETACHED_PROCESS + CREATE_NEW_PROCESS_GROUP 才是正确的组合。
    creation = (
        getattr(subprocess, "DETACHED_PROCESS", 0x00000008)
        | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200)
        | getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
    )
    proc = subprocess.Popen(
        [str(PY), "quant_engine.py", "--loop", "--interval", "180"],
        cwd=str(BASE),
        stdout=stdout,
        stderr=stderr,
        creationflags=creation,
        close_fds=True,
    )
    time.sleep(2.5)
    # 以"锁文件是否被活进程持有"作为成功判据，比 proc.poll() 更可靠：
    # 引擎启动时会先抢单实例锁，拿到锁才说明真的跑起来了。
    lock_ok = False
    try:
        import single_instance

        lock_ok = single_instance.lock_status().get("alive") is True
    except Exception:
        lock_ok = proc.poll() is None
    if lock_ok or proc.poll() is None:
        print(f"[OK] 已启动无人值守：每 3 分钟巡检 (PID {proc.pid})")
        print(f"日志: {LOG_DIR / 'loop_stdout.log'}")
    else:
        print("[失败] 进程未能保持运行，请查看 logs/loop_stderr.log")
        print("按任意键关闭…")
        try:
            input()
        except EOFError:
            pass
        return 1
    print("可以关闭本窗口；后台会继续跑。")
    print("按任意键关闭…")
    try:
        input()
    except EOFError:
        pass
    return 0

if __name__ == "__main__":
    raise SystemExit(main())