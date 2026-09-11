"""Bounded, dependency-free cross-process locking for caller-owned files.

The caller owns opening, positioning and closing the descriptor. This module
only takes an exclusive lock: POSIX uses ``flock`` and Windows locks byte zero
with ``LockFileEx``. Neither backend changes the descriptor's file position,
and Windows may lock that byte even when the stable lock file is still empty.

Every acquisition attempt is non-blocking. Contention is retried only until
the caller's monotonic deadline; ordinary timeout and a broken or unavailable
backend are different exceptions so each caller can preserve its own fallback.
"""
from __future__ import annotations

import errno
import math
import os
import time


class LockError(Exception):
    """Base class for failures to acquire the requested file lock."""


class LockTimeout(LockError):
    """Another descriptor held the lock through the supplied deadline."""


class LockBackendError(LockError):
    """The platform lock backend was unavailable or failed unexpectedly."""


class _PosixBackend:
    name = "fcntl.flock"

    def __init__(self) -> None:
        import fcntl

        self._fcntl = fcntl

    def try_acquire(self, handle: int) -> bool:
        try:
            self._fcntl.flock(
                handle, self._fcntl.LOCK_EX | self._fcntl.LOCK_NB)
            return True
        except OSError as exc:
            if exc.errno in (errno.EACCES, errno.EAGAIN):
                return False
            raise LockBackendError(
                "fcntl.flock could not acquire the file lock: %s" % exc
            ) from exc

    def release(self, handle: int) -> None:
        self._fcntl.flock(handle, self._fcntl.LOCK_UN)


class _WindowsBackend:
    name = "Win32 LockFileEx"

    def __init__(self) -> None:
        import ctypes
        import msvcrt
        from ctypes import wintypes

        class Overlapped(ctypes.Structure):
            _fields_ = [
                ("Internal", ctypes.c_size_t),
                ("InternalHigh", ctypes.c_size_t),
                ("Offset", wintypes.DWORD),
                ("OffsetHigh", wintypes.DWORD),
                ("hEvent", wintypes.HANDLE),
            ]

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.LockFileEx.argtypes = [
            wintypes.HANDLE,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.DWORD,
            ctypes.POINTER(Overlapped),
        ]
        kernel32.LockFileEx.restype = wintypes.BOOL
        kernel32.UnlockFileEx.argtypes = [
            wintypes.HANDLE,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.DWORD,
            ctypes.POINTER(Overlapped),
        ]
        kernel32.UnlockFileEx.restype = wintypes.BOOL

        self._ctypes = ctypes
        self._msvcrt = msvcrt
        self._overlapped = Overlapped
        self._lock_file = kernel32.LockFileEx
        self._unlock_file = kernel32.UnlockFileEx

    def _os_handle(self, handle: int):
        try:
            raw = self._msvcrt.get_osfhandle(handle)
        except OSError as exc:
            raise LockBackendError(
                "msvcrt.get_osfhandle could not resolve descriptor %s: %s"
                % (handle, exc)
            ) from exc
        if raw == -1:
            raise LockBackendError(
                "msvcrt.get_osfhandle returned an invalid Windows handle")
        return self._ctypes.c_void_p(raw)

    def try_acquire(self, handle: int) -> bool:
        # An explicit OVERLAPPED offset makes the byte range independent of the
        # descriptor's current file position. Locking beyond EOF is supported,
        # which is what makes a new, empty stable lock file usable.
        overlapped = self._overlapped()
        flags = 0x00000002 | 0x00000001  # EXCLUSIVE | FAIL_IMMEDIATELY
        if self._lock_file(
                self._os_handle(handle), flags, 0, 1, 0,
                self._ctypes.byref(overlapped)):
            return True
        error = self._ctypes.get_last_error()
        if error == 33:  # ERROR_LOCK_VIOLATION
            return False
        raise LockBackendError(
            "LockFileEx could not acquire the file lock: %s"
            % self._ctypes.WinError(error))

    def release(self, handle: int) -> None:
        overlapped = self._overlapped()
        if not self._unlock_file(
                self._os_handle(handle), 0, 1, 0,
                self._ctypes.byref(overlapped)):
            error = self._ctypes.get_last_error()
            raise LockBackendError(
                "UnlockFileEx could not release the file lock: %s"
                % self._ctypes.WinError(error))


def _select_backend():
    try:
        if os.name == "nt":
            return _WindowsBackend(), None
        return _PosixBackend(), None
    except Exception as exc:  # noqa: BLE001 - absence is reported on acquire
        return None, exc


_BACKEND, _BACKEND_LOAD_ERROR = _select_backend()
_RETRY_S = 0.005


def acquire_exclusive(handle: int, deadline: float) -> None:
    """Acquire exclusively by ``deadline`` (from ``time.monotonic()``).

    Raises ``LockTimeout`` for ordinary contention and ``LockBackendError``
    when the platform primitive is unavailable or fails. The descriptor stays
    open and owned by the caller, and its file position is unchanged.
    """
    deadline = float(deadline)
    if not math.isfinite(deadline):
        raise LockBackendError("the file-lock deadline must be finite")

    backend = _BACKEND
    if backend is None:
        detail = (": %s" % _BACKEND_LOAD_ERROR
                  if _BACKEND_LOAD_ERROR is not None else "")
        raise LockBackendError("no file-lock backend is available%s" % detail)

    while True:
        try:
            acquired = backend.try_acquire(handle)
        except LockBackendError:
            raise
        except Exception as exc:  # noqa: BLE001 - normalize backend failures
            raise LockBackendError(
                "%s backend failed unexpectedly: %s" % (backend.name, exc)
            ) from exc
        if acquired:
            return
        now = time.monotonic()
        if now >= deadline:
            raise LockTimeout("the file lock stayed held through its deadline")
        time.sleep(min(_RETRY_S, max(0.0, deadline - now)))


def release_exclusive(handle: int) -> bool:
    """Release best effort; descriptor close remains the final safety net."""
    backend = _BACKEND
    if backend is None:
        return False
    try:
        backend.release(handle)
        return True
    except Exception:  # noqa: BLE001 - closing the descriptor also releases it
        return False


def backend_name() -> str:
    """The selected primitive, or ``unavailable`` for diagnostics and tests."""
    backend = _BACKEND
    return str(backend.name) if backend is not None else "unavailable"
