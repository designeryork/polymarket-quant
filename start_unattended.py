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
    """
    try:
        sys.path.insert(0, str(BASE))
        import single_instance

        status = single_instance.lock_status()
        if status.get("held"):
            return True
    except Exception:
        pass
    try:
        out = subprocess.check_output(
            [
                "powershell", "-NoProfile", "-Command",
                "Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | "
                "Where-Object { $_.CommandLine -match 'quant_engine.py --loop' } | "
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
    creation = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
    proc = subprocess.Popen(
        [str(PY), "quant_engine.py", "--loop", "--interval", "180"],
        cwd=str(BASE),
        stdout=stdout,
        stderr=stderr,
        creationflags=creation,
    )
    time.sleep(1.5)
    if loop_running() or proc.poll() is None:
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