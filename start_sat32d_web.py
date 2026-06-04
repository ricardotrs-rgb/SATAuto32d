from __future__ import annotations

import argparse
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from urllib.error import URLError
from urllib.request import urlopen


HOST = "0.0.0.0"
PORT = 8000
HEALTHCHECK_HOST = "127.0.0.1"
URL = f"http://{HEALTHCHECK_HOST}:{PORT}/"
STARTUP_TIMEOUT_SECONDS = 20.0
WATCHDOG_POLL_SECONDS = 2.0
WATCHDOG_RESTART_DELAY_SECONDS = 2.0
WATCHDOG_PID_FILE = "sat32d_web_watchdog.pid"
WATCHDOG_LOCK_FILE = "sat32d_web_watchdog.lock"


_WATCHDOG_LOCK_HANDLE = None
_WATCHDOG_RUNTIME_DIR: Path | None = None


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Arranque local de SAT32D web.")
    parser.add_argument(
        "--watchdog",
        action="store_true",
        help="Mantiene la web arriba y la reinicia si se cae.",
    )
    return parser


def _run_init_if_needed(python_executable: Path, repo_root: Path) -> None:
    subprocess.run(
        [str(python_executable), "main.py", "init"],
        cwd=repo_root,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=True,
    )


def _is_sat32d_web_running() -> bool:
    try:
        with urlopen(URL, timeout=1) as response:
            body = response.read(4096).decode("utf-8", errors="ignore")
            return response.status == 200 and "SAT32D" in body
    except URLError:
        return False
    except OSError:
        return False


def _get_detached_creation_flags() -> int:
    if sys.platform != "win32":
        return 0

    return (
        getattr(subprocess, "DETACHED_PROCESS", 0)
        | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    )


def _get_no_window_creation_flags() -> int:
    if sys.platform != "win32":
        return 0

    return getattr(subprocess, "CREATE_NO_WINDOW", 0)


def _is_directory_writable(path: Path) -> bool:
    try:
        path.mkdir(parents=True, exist_ok=True)
    except OSError:
        return False

    probe_file = path / ".sat32d_write_probe"
    try:
        probe_file.write_text("ok", encoding="utf-8")
        probe_file.unlink(missing_ok=True)
        return True
    except OSError:
        return False


def _get_watchdog_runtime_dir() -> Path:
    global _WATCHDOG_RUNTIME_DIR

    if _WATCHDOG_RUNTIME_DIR is not None:
        return _WATCHDOG_RUNTIME_DIR

    configured_runtime_dir = os.environ.get("SAT32D_WEB_RUNTIME_DIR", "").strip()
    if configured_runtime_dir:
        configured_path = Path(configured_runtime_dir).expanduser().resolve()
        if _is_directory_writable(configured_path):
            _WATCHDOG_RUNTIME_DIR = configured_path
            return _WATCHDOG_RUNTIME_DIR

    temp_runtime_dir = Path(tempfile.gettempdir()) / "sat32d_web_runtime"
    if _is_directory_writable(temp_runtime_dir):
        _WATCHDOG_RUNTIME_DIR = temp_runtime_dir
        return _WATCHDOG_RUNTIME_DIR

    script_dir = Path(__file__).resolve().parent
    if _is_directory_writable(script_dir):
        _WATCHDOG_RUNTIME_DIR = script_dir
        return _WATCHDOG_RUNTIME_DIR

    temp_runtime_dir.mkdir(parents=True, exist_ok=True)
    _WATCHDOG_RUNTIME_DIR = temp_runtime_dir
    return _WATCHDOG_RUNTIME_DIR


def _get_pid_file_path() -> Path:
    return _get_watchdog_runtime_dir() / WATCHDOG_PID_FILE


def _get_lock_file_path() -> Path:
    return _get_watchdog_runtime_dir() / WATCHDOG_LOCK_FILE


def _write_watchdog_pid(pid_file: Path) -> None:
    try:
        pid_file.write_text(str(os.getpid()), encoding="utf-8")
    except OSError:
        # Evita que el watchdog caiga si OneDrive bloquea temporalmente el archivo PID.
        return


def _clear_watchdog_pid(pid_file: Path) -> None:
    try:
        pid_file.unlink(missing_ok=True)
    except OSError:
        return


def _open_watchdog_lock(lock_file: Path):
    lock_file.parent.mkdir(parents=True, exist_ok=True)
    handle = lock_file.open("a+", encoding="utf-8")
    handle.seek(0)
    if not handle.read(1):
        handle.write("1")
        handle.flush()
    handle.seek(0)
    return handle


def _try_lock_watchdog(handle) -> bool:
    if sys.platform == "win32":
        import msvcrt

        try:
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            return True
        except OSError:
            return False

    import fcntl

    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except OSError:
        return False


def _unlock_watchdog(handle) -> None:
    if sys.platform == "win32":
        import msvcrt

        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        return

    import fcntl

    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _find_running_watchdog_pids() -> list[int]:
    script_path = str(Path(__file__).resolve())

    if sys.platform == "win32":
        escaped_script_path = script_path.replace("'", "''")
        command = (
            f"$scriptPath = '{escaped_script_path}'; "
            "Get-CimInstance Win32_Process | "
            "Where-Object { $_.Name -match '^pythonw?\\.exe$' -and $_.CommandLine -like ('*' + $scriptPath + '*--watchdog*') } | "
            "Select-Object -ExpandProperty ProcessId"
        )
        result = subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", command],
            capture_output=True,
            text=True,
            check=False,
            creationflags=_get_no_window_creation_flags(),
        )
    else:
        result = subprocess.run(
            ["pgrep", "-f", f"{script_path} --watchdog"],
            capture_output=True,
            text=True,
            check=False,
        )

    pids: list[int] = []
    for line in result.stdout.splitlines():
        candidate = line.strip()
        if not candidate.isdigit():
            continue

        pid = int(candidate)
        if pid == os.getpid() or pid in pids:
            continue
        pids.append(pid)

    return pids


def _has_active_watchdog() -> bool:
    try:
        lock_handle = _open_watchdog_lock(_get_lock_file_path())
    except PermissionError:
        return True

    try:
        if not _try_lock_watchdog(lock_handle):
            return True
        _unlock_watchdog(lock_handle)
        return bool(_find_running_watchdog_pids())
    finally:
        lock_handle.close()


def _acquire_watchdog_lock() -> bool:
    global _WATCHDOG_LOCK_HANDLE

    if _WATCHDOG_LOCK_HANDLE is not None:
        return True

    try:
        lock_handle = _open_watchdog_lock(_get_lock_file_path())
    except PermissionError:
        return False

    if not _try_lock_watchdog(lock_handle):
        lock_handle.close()
        return False

    _WATCHDOG_LOCK_HANDLE = lock_handle
    return True


def _release_watchdog_lock() -> None:
    global _WATCHDOG_LOCK_HANDLE

    if _WATCHDOG_LOCK_HANDLE is None:
        return

    try:
        _unlock_watchdog(_WATCHDOG_LOCK_HANDLE)
    finally:
        _WATCHDOG_LOCK_HANDLE.close()
        _WATCHDOG_LOCK_HANDLE = None


def _spawn_web_process(launcher: Path, repo_root: Path) -> subprocess.Popen[str]:
    return subprocess.Popen(
        [str(launcher), "main.py", "web", "--host", HOST, "--port", str(PORT)],
        cwd=repo_root,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=_get_detached_creation_flags(),
    )


def _wait_for_web(timeout_seconds: float) -> bool:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if _is_sat32d_web_running():
            return True
        time.sleep(0.25)
    return False


def _run_watchdog(
    launcher: Path,
    repo_root: Path,
    *,
    announce_start: bool = False,
) -> int:
    if not _acquire_watchdog_lock():
        return 0

    pid_file = _get_pid_file_path()
    _write_watchdog_pid(pid_file)
    managed_process: subprocess.Popen[str] | None = None
    python_executable = _resolve_python_executable()
    has_announced_start = False

    try:
        while True:
            _write_watchdog_pid(pid_file)

            if managed_process is not None and managed_process.poll() is not None:
                managed_process = None

            if _is_sat32d_web_running():
                if announce_start and not has_announced_start:
                    print("SAT32D web iniciada en http://127.0.0.1:8000 (acceso local y red LAN)")
                    has_announced_start = True
                time.sleep(WATCHDOG_POLL_SECONDS)
                continue

            try:
                _run_init_if_needed(python_executable, repo_root)
            except subprocess.CalledProcessError:
                time.sleep(WATCHDOG_RESTART_DELAY_SECONDS)
                continue

            managed_process = _spawn_web_process(launcher, repo_root)
            if _wait_for_web(STARTUP_TIMEOUT_SECONDS):
                if announce_start and not has_announced_start:
                    print("SAT32D web iniciada en http://127.0.0.1:8000 (acceso local y red LAN)")
                    has_announced_start = True
                time.sleep(WATCHDOG_POLL_SECONDS)
                continue

            if managed_process.poll() is None:
                managed_process.terminate()
                try:
                    managed_process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    managed_process.kill()
            managed_process = None
            time.sleep(WATCHDOG_RESTART_DELAY_SECONDS)
    finally:
        _clear_watchdog_pid(pid_file)
        _release_watchdog_lock()


def _resolve_launcher() -> Path:
    python_executable = Path(sys.executable).resolve()
    pythonw_executable = python_executable.with_name("pythonw.exe")
    return pythonw_executable if pythonw_executable.exists() else python_executable


def _resolve_python_executable() -> Path:
    python_executable = Path(sys.executable).resolve()
    if python_executable.name.lower() == "pythonw.exe":
        console_python = python_executable.with_name("python.exe")
        if console_python.exists():
            return console_python
    return python_executable


def main() -> int:
    args = _build_parser().parse_args()
    repo_root = Path(__file__).resolve().parents[1]
    launcher = _resolve_launcher()
    python_executable = _resolve_python_executable()

    if args.watchdog:
        return _run_watchdog(launcher, repo_root)

    if _has_active_watchdog():
        if _wait_for_web(STARTUP_TIMEOUT_SECONDS):
            print("SAT32D web ya estaba en ejecucion y el watchdog sigue activo.")
            return 0

        print("SAT32D watchdog ya estaba en ejecucion y la web sigue arrancando.")
        return 0

    try:
        _run_init_if_needed(python_executable, repo_root)
    except subprocess.CalledProcessError:
        print("No fue posible preparar el entorno de SAT32D.", file=sys.stderr)
        return 1

    return _run_watchdog(launcher, repo_root, announce_start=True)


if __name__ == "__main__":
    raise SystemExit(main())