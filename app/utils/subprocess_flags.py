"""Windows でサブプロセス起動時にコンソールウィンドウを出さないための設定。

--windowed でパッケージ化した EXE には親コンソールが無いため、フラグを
指定せずに ffmpeg/ffprobe (コンソールアプリ) を起動すると、実行の度に
新しいコンソールウィンドウや conhost.exe プロセスが残ってしまう。
creationflags=CREATE_NO_WINDOW だけでは PyInstaller の --windowed ビルドから
起動した場合に conhost.exe が残留することがあるため、STARTUPINFO による
明示的な非表示指定もあわせて使う。全ての ffmpeg/ffprobe 起動箇所で
hidden_subprocess_kwargs() の戻り値を渡すこと。
"""

from __future__ import annotations

import subprocess
import sys

CREATE_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0


def hidden_subprocess_kwargs() -> dict:
    """subprocess.run()/Popen() にそのまま展開して渡す追加キーワード引数。"""
    if sys.platform != "win32":
        return {}
    startupinfo = subprocess.STARTUPINFO()
    startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    startupinfo.wShowWindow = subprocess.SW_HIDE
    return {"creationflags": CREATE_NO_WINDOW, "startupinfo": startupinfo}


_job_handle: int | None = None


def ensure_child_process_job() -> None:
    """Windows専用: 以降 assign_to_job() で登録した ffmpeg/ffprobe 子プロセスを
    1つの Job Object にまとめ、JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE を設定する。

    通常は各ワーカーの cancel()(terminate())で子プロセスを止めるが、アプリが
    クラッシュ・強制終了した場合はそれが呼ばれない。その場合でも、このプロセスが
    終了して Job ハンドルが閉じられた瞬間に OS が子プロセスをまとめて kill する
    ため、ffmpeg/ffprobe が孤児プロセスとしてディスクへのアクセスを続けることを防げる。
    アプリ起動時に一度だけ呼ぶこと。失敗しても致命的ではないため呼び出し側で無視してよい。
    """
    global _job_handle
    if sys.platform != "win32" or _job_handle is not None:
        return

    import ctypes
    from ctypes import wintypes

    class _JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", ctypes.c_int64),
            ("PerJobUserTimeLimit", ctypes.c_int64),
            ("LimitFlags", wintypes.DWORD),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", wintypes.DWORD),
            ("Affinity", ctypes.c_size_t),
            ("PriorityClass", wintypes.DWORD),
            ("SchedulingClass", wintypes.DWORD),
        ]

    class _IO_COUNTERS(ctypes.Structure):
        _fields_ = [
            ("ReadOperationCount", ctypes.c_uint64),
            ("WriteOperationCount", ctypes.c_uint64),
            ("OtherOperationCount", ctypes.c_uint64),
            ("ReadTransferCount", ctypes.c_uint64),
            ("WriteTransferCount", ctypes.c_uint64),
            ("OtherTransferCount", ctypes.c_uint64),
        ]

    class _JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", _JOBOBJECT_BASIC_LIMIT_INFORMATION),
            ("IoInfo", _IO_COUNTERS),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t),
        ]

    JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000
    JobObjectExtendedLimitInformation = 9

    kernel32 = ctypes.windll.kernel32
    kernel32.CreateJobObjectW.restype = wintypes.HANDLE
    kernel32.CreateJobObjectW.argtypes = [wintypes.LPVOID, wintypes.LPCWSTR]
    kernel32.SetInformationJobObject.restype = wintypes.BOOL
    kernel32.SetInformationJobObject.argtypes = [
        wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD,
    ]
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]

    handle = kernel32.CreateJobObjectW(None, None)
    if not handle:
        return

    info = _JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
    info.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    ok = kernel32.SetInformationJobObject(
        handle, JobObjectExtendedLimitInformation, ctypes.byref(info), ctypes.sizeof(info),
    )
    if not ok:
        kernel32.CloseHandle(handle)
        return

    _job_handle = handle


def assign_to_job(process: subprocess.Popen) -> None:
    """起動済みの ffmpeg/ffprobe プロセスを ensure_child_process_job() の Job Object に
    割り当てる。未実行/失敗している場合は何もしない(失敗してもクラッシュ時の保険が
    効かないだけで、通常の cancel() による終了処理には影響しない)。"""
    if sys.platform != "win32" or _job_handle is None:
        return
    import ctypes
    from ctypes import wintypes

    try:
        kernel32 = ctypes.windll.kernel32
        kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
        kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        kernel32.AssignProcessToJobObject(_job_handle, int(process._handle))
    except (OSError, AttributeError, ValueError):
        pass
