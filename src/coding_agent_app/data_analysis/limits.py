"""Hard worker memory/CPU limits; no claim of sandboxing arbitrary Python."""

from __future__ import annotations

import os
import sys

_job_handle: object = None


def apply_worker_limits() -> None:
    global _job_handle
    if sys.platform == "win32":
        import ctypes
        from ctypes import wintypes

        class BasicLimits(ctypes.Structure):
            _fields_ = [
                ("per_process_time", ctypes.c_longlong),
                ("per_job_time", ctypes.c_longlong),
                ("flags", wintypes.DWORD),
                ("min_working", ctypes.c_size_t),
                ("max_working", ctypes.c_size_t),
                ("active", wintypes.DWORD),
                ("affinity", ctypes.c_size_t),
                ("priority", wintypes.DWORD),
                ("scheduling", wintypes.DWORD),
            ]

        class IoCounters(ctypes.Structure):
            _fields_ = [
                (name, ctypes.c_ulonglong)
                for name in (
                    "reads",
                    "writes",
                    "other",
                    "read_bytes",
                    "write_bytes",
                    "other_bytes",
                )
            ]

        class ExtendedLimits(ctypes.Structure):
            _fields_ = [
                ("basic", BasicLimits),
                ("io", IoCounters),
                ("process_memory", ctypes.c_size_t),
                ("job_memory", ctypes.c_size_t),
                ("peak_process", ctypes.c_size_t),
                ("peak_job", ctypes.c_size_t),
            ]

        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.CreateJobObjectW.restype = wintypes.HANDLE
        kernel.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
        kernel.SetInformationJobObject.argtypes = [
            wintypes.HANDLE,
            ctypes.c_int,
            ctypes.c_void_p,
            wintypes.DWORD,
        ]
        kernel.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        kernel.GetCurrentProcess.restype = wintypes.HANDLE
        job = kernel.CreateJobObjectW(None, None)
        limits = ExtendedLimits()
        # PROCESS_MEMORY | ACTIVE_PROCESS | PROCESS_TIME | KILL_ON_JOB_CLOSE.
        limits.basic.flags = 0x100 | 0x8 | 0x2 | 0x2000
        limits.basic.active = 1
        limits.basic.per_process_time = 60 * 10_000_000
        limits.process_memory = 1024 * 1024 * 1024
        if (
            not job
            or not kernel.SetInformationJobObject(
                job,
                9,
                ctypes.byref(limits),
                ctypes.sizeof(limits),
            )
            or not kernel.AssignProcessToJobObject(job, kernel.GetCurrentProcess())
        ):
            raise RuntimeError("worker_limits_unavailable")
        _job_handle = job  # Keep the job open until the worker exits.
    elif os.name == "posix":
        import importlib

        resource = importlib.import_module("resource")

        resource.setrlimit(resource.RLIMIT_AS, (1024**3, 1024**3))
        resource.setrlimit(resource.RLIMIT_CPU, (60, 60))
        resource.setrlimit(resource.RLIMIT_FSIZE, (20 * 1024**2, 20 * 1024**2))
    else:
        raise RuntimeError("worker_limits_unavailable")
