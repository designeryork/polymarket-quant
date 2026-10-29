"""
单实例运行锁 (single_instance.py)
================================
防止 Polymarket 量化引擎被重复启动，从而在同一钱包上并发下单。

背景与风险
----------
旧实现仅靠 `start_unattended.py` 用 PowerShell 匹配命令行 `quant_engine.py --loop`，
存在三个缺陷：
1. 只匹配 `python.exe`，以 `pythonw.exe` 或 PyInstaller 打包体启动时检测不到自己；
2. 存在 TOCTOU 竞态：两个启动器同时执行时都看到"未运行"，会各自拉起一个进程；
3. `恢复无人值守.bat` / `恢复无人值守.exe` / `run_quant_once.bat` 三条入口互不协调。

设计
----
* 使用 `os.open(..., O_CREAT | O_EXCL)` 原子创建锁文件 —— 内核保证排他，无 TOCTOU 窗口；
* 锁文件内记录 PID、进程启动时间与主机名，用于识别陈旧锁（stale lock）；
* 若锁文件存在但持锁进程已死亡（PID 不存在），判定为陈旧锁并安全接管；
* 同一 PID 重复获取视为可重入（幂等），避免进程内重复调用自锁；
* 锁路径可通过环境变量覆盖，使云端部署与本机部署互不干扰。

跨平台说明
----------
PID 存活检测使用 `psutil`（若可用）或 Windows `tasklist` / POSIX `kill(pid, 0)`。
"""
import json
import os
import socket
import sys
import time
from pathlib import Path
from typing import Optional, Tuple

BASE_DIR = Path(__file__).resolve().parent
DEFAULT_LOCK_FILE = BASE_DIR / "state" / "engine.lock"

# 锁文件中的元数据版本，便于将来扩展而不破坏旧锁解析
LOCK_SCHEMA_VERSION = 1


def _lock_path() -> Path:
    override = os.getenv("ENGINE_LOCK_FILE", "").strip()
    return Path(override) if override else DEFAULT_LOCK_FILE


def _process_start_time(pid: int) -> Optional[float]:
    """获取进程启动时间（Unix 时间戳），失败返回 None。"""
    try:
        import psutil  # type: ignore

        return float(psutil.Process(pid).create_time())
    except Exception:
        pass
    try:
        if os.name == "nt":
            import subprocess

            out = subprocess.check_output(
                ["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
                text=True,
                stderr=subprocess.DEVNULL,
                timeout=10,
            )
            # 形如 "python.exe","1234","Console","1","20,000 K"
            for line in out.splitlines():
                parts = [p.strip('" ') for p in line.split('","')]
                if len(parts) > 1 and parts[1].isdigit() and int(parts[1]) == pid:
                    return None
            return None
        os.kill(pid, 0)
        return None
    except Exception:
        return None


def _pid_alive(pid: int) -> bool:
    """判断 PID 是否仍对应一个存活进程。"""
    if pid <= 0:
        return False
    try:
        import psutil  # type: ignore

        return psutil.pid_exists(pid)
    except Exception:
        pass
    try:
        if os.name == "nt":
            import subprocess

            out = subprocess.check_output(
                ["tasklist", "/FI", f"PID eq {pid}", "/NH"],
                text=True,
                stderr=subprocess.DEVNULL,
                timeout=10,
            )
            return str(pid) in out
        os.kill(pid, 0)
        return True
    except Exception:
        return False


def _read_lock(path: Path) -> Optional[dict]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _write_lock(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def _remove_lock(path: Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        pass
    except Exception:
        pass


def acquire_lock(role: str = "quant_engine") -> Tuple[bool, str]:
    """尝试获取单实例锁。

    返回 (是否成功, 说明文本)。成功时锁文件已创建，调用方应在退出时调用 release_lock()。
    """
    path = _lock_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    current_pid = os.getpid()

    # 快速路径：锁文件存在
    if path.exists():
        existing = _read_lock(path)
        if existing:
            other_pid = int(existing.get("pid") or 0)
            if other_pid == current_pid:
                # 同一进程重复获取：视为可重入，直接放行（幂等）
                return True, f"本进程 (PID {current_pid}) 已持有锁，视为可重入。"
            if _pid_alive(other_pid):
                role_desc = existing.get("role") or "未知任务"
                started = existing.get("started_at_utc") or "未知时间"
                host = existing.get("host") or "?"
                return False, (
                    f"检测到 {role_desc} 已在运行 (PID {other_pid}, 主机 {host}, 启动于 {started})。"
                    f"为避免同一钱包重复下单，已拒绝启动。"
                )
            # 持锁进程已死亡 -> 陈旧锁，安全接管
            _remove_lock(path)

    payload = {
        "schema_version": LOCK_SCHEMA_VERSION,
        "pid": current_pid,
        "role": role,
        "host": socket.gethostname(),
        "started_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "python": sys.executable,
    }

    # 原子创建：O_EXCL 保证并发下只有一个进程成功
    for _attempt in range(2):
        try:
            fd = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            try:
                os.write(fd, json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8"))
            finally:
                os.close(fd)
            return True, f"已获取单实例锁 (PID {current_pid})。"
        except FileExistsError:
            # 竞态：另一进程刚创建。重新读取并判断是活跃锁还是陈旧锁
            existing = _read_lock(path)
            other_pid = int((existing or {}).get("pid") or 0)
            if other_pid and other_pid != current_pid and _pid_alive(other_pid):
                return False, (
                    f"检测到 {role} 已在运行 (PID {other_pid})，已拒绝启动以避免重复下单。"
                )
            _remove_lock(path)
            continue
        except Exception as exc:
            return False, f"获取单实例锁失败：{exc}"

    return False, "获取单实例锁失败：多次尝试均遇到并发冲突。"


def release_lock() -> None:
    """释放锁（仅释放自己持有的锁）。"""
    path = _lock_path()
    existing = _read_lock(path)
    if existing and int(existing.get("pid") or 0) != os.getpid():
        # 不是自己持有的锁，不动它
        return
    _remove_lock(path)


def lock_status() -> dict:
    """返回锁的当前状态，用于面板展示与启动器判定。

    区分三种状态：
    * held=False          —— 没有锁文件，后台未启动；
    * held=True, alive=True  —— 正常持有，后台运行中；
    * held=True, alive=False —— **陈旧锁**（进程已死/机器重启过），
      启动器必须据此判定"未运行"并接管，否则会永远启动不了后台。
    """
    path = _lock_path()
    payload = _read_lock(path) if path.exists() else None
    if not payload:
        return {"held": False, "path": str(path), "alive": False}
    pid = int(payload.get("pid") or 0)
    alive = _pid_alive(pid)
    return {
        "held": True,
        "path": str(path),
        "pid": pid,
        "role": payload.get("role"),
        "host": payload.get("host"),
        "started_at_utc": payload.get("started_at_utc"),
        "alive": alive,
        "stale": not alive,
    }


if __name__ == "__main__":
    ok, message = acquire_lock(role="manual_check")
    print(json.dumps({"acquired": ok, "message": message, "status": lock_status()}, ensure_ascii=False, indent=2))
    if ok:
        release_lock()
