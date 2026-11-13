"""Current process memory (RSS) and giving freed memory back to the OS. Used by the worker
and its picture backfill. Never raises."""
from __future__ import annotations

import ctypes
import gc
import os
from typing import Optional

_libc = None
_libc_checked = False


def rss_mb() -> Optional[float]:
    """Resident memory of this process in MB, or None where it cannot be read (not Linux)."""
    try:
        with open("/proc/self/statm", "rb") as handle:
            resident_pages = int(handle.read().split()[1])
        return resident_pages * os.sysconf("SC_PAGE_SIZE") / (1024 * 1024)
    except Exception:
        return None


def describe() -> str:
    value = rss_mb()
    return "unknown" if value is None else f"{value:.0f} MB"


def malloc_trim() -> bool:
    """glibc malloc_trim(0): hand freed heap pages back to the OS. False where not available."""
    global _libc, _libc_checked
    try:
        if not _libc_checked:
            _libc_checked = True
            try:
                _libc = ctypes.CDLL("libc.so.6")
                _libc.malloc_trim.argtypes = [ctypes.c_size_t]
            except Exception:
                _libc = None
        if _libc is None:
            return False
        _libc.malloc_trim(0)
        return True
    except Exception:
        return False


def release(trim: bool = False) -> None:
    """gc.collect(), and malloc_trim(0) too when trim is set."""
    try:
        gc.collect()
    except Exception:
        pass
    if trim:
        malloc_trim()
