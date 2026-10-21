from __future__ import annotations

from dataclasses import dataclass
import ctypes
from ctypes import wintypes
import os
from pathlib import Path
from queue import Empty, Full, Queue
from threading import Event, Lock, Thread
import time
from typing import Any, Callable, Collection, Protocol


class _MouseHookData(ctypes.Structure):
    _fields_ = [
        ("pt", wintypes.POINT),
        ("mouseData", wintypes.DWORD),
        ("flags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ctypes.c_size_t),
    ]


@dataclass(frozen=True)
class _WindowsGuiApi:
    user32: Any
    kernel32: Any
    hook_proc_type: Any


def _configure_windows_gui_api(user32, kernel32) -> _WindowsGuiApi:
    """Declare the complete Win32 ABI before calling it, including pointer-sized inputs.

    A restype alone is insufficient: untyped Python integer arguments otherwise
    undergo c_int conversion and can truncate/overflow LPARAM, HWND and HANDLE.
    Separate DLL instances keep these declarations private to this component.
    """
    hook_proc_type = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM)
    user_signatures = {
        "SetWindowsHookExW": ([ctypes.c_int, hook_proc_type, wintypes.HINSTANCE, wintypes.DWORD], wintypes.HANDLE),
        "CallNextHookEx": ([wintypes.HANDLE, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM], ctypes.c_ssize_t),
        "UnhookWindowsHookEx": ([wintypes.HANDLE], wintypes.BOOL),
        "PostThreadMessageW": ([wintypes.DWORD, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM], wintypes.BOOL),
        "GetMessageW": ([ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT], wintypes.BOOL),
        "TranslateMessage": ([ctypes.POINTER(wintypes.MSG)], wintypes.BOOL),
        "DispatchMessageW": ([ctypes.POINTER(wintypes.MSG)], ctypes.c_ssize_t),
        "WindowFromPoint": ([wintypes.POINT], wintypes.HWND),
        "GetAncestor": ([wintypes.HWND, wintypes.UINT], wintypes.HWND),
        "GetParent": ([wintypes.HWND], wintypes.HWND),
        "IsWindowVisible": ([wintypes.HWND], wintypes.BOOL),
        "GetWindowThreadProcessId": ([wintypes.HWND, ctypes.POINTER(wintypes.DWORD)], wintypes.DWORD),
        "GetWindowRect": ([wintypes.HWND, ctypes.POINTER(wintypes.RECT)], wintypes.BOOL),
        "GetWindowTextLengthW": ([wintypes.HWND], ctypes.c_int),
        "GetWindowTextW": ([wintypes.HWND, wintypes.LPWSTR, ctypes.c_int], ctypes.c_int),
        "GetClassNameW": ([wintypes.HWND, wintypes.LPWSTR, ctypes.c_int], ctypes.c_int),
    }
    kernel_signatures = {
        "GetCurrentThreadId": ([], wintypes.DWORD),
        "OpenProcess": ([wintypes.DWORD, wintypes.BOOL, wintypes.DWORD], wintypes.HANDLE),
        "QueryFullProcessImageNameW": ([wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)], wintypes.BOOL),
        "CloseHandle": ([wintypes.HANDLE], wintypes.BOOL),
    }
    for library, signatures in ((user32, user_signatures), (kernel32, kernel_signatures)):
        for name, (arguments, result) in signatures.items():
            function = getattr(library, name)
            function.argtypes = arguments
            function.restype = result
    return _WindowsGuiApi(user32, kernel32, hook_proc_type)


def _load_windows_gui_api() -> _WindowsGuiApi:
    return _configure_windows_gui_api(
        ctypes.WinDLL("user32", use_last_error=True),
        ctypes.WinDLL("kernel32", use_last_error=True),
    )


@dataclass(frozen=True)
class DesktopMouseClick:
    """One completed physical mouse click, independent of any GUI meaning."""

    button: str
    down_x_px: int
    down_y_px: int
    up_x_px: int
    up_y_px: int
    down_monotonic_ns: int
    up_monotonic_ns: int
    up_utc_ns: int

    @property
    def duration_ms(self) -> float:
        return (self.up_monotonic_ns - self.down_monotonic_ns) / 1_000_000


@dataclass(frozen=True)
class DesktopWindow:
    """Read-only identity and geometry of the top-level window under a click."""

    handle: int
    process_id: int
    process_executable: str
    title: str
    class_name: str
    left_px: int
    top_px: int
    right_px: int
    bottom_px: int

    @property
    def width_px(self) -> int:
        return self.right_px - self.left_px

    @property
    def height_px(self) -> int:
        return self.bottom_px - self.top_px

    def normalized_point(self, x_px: int, y_px: int) -> tuple[float, float]:
        if self.width_px <= 0 or self.height_px <= 0:
            raise ValueError("window dimensions must be positive")
        return (
            (x_px - self.left_px) / self.width_px,
            (y_px - self.top_px) / self.height_px,
        )


class DesktopClickSource(Protocol):
    @property
    def failed(self) -> bool: ...

    @property
    def error(self) -> BaseException | None: ...

    @property
    def dropped_events(self) -> int: ...

    def start(self, callback: Callable[[DesktopMouseClick], None]) -> None: ...

    def stop(self, *, raise_on_failure: bool = True) -> None: ...


class DesktopWindowLocator(Protocol):
    def window_at(self, x_px: int, y_px: int) -> DesktopWindow | None: ...


@dataclass(frozen=True)
class _RawMouseEvent:
    kind: str
    x_px: int
    y_px: int
    monotonic_ns: int
    utc_ns: int
    injected: bool


class MouseClickAssembler:
    """Pair physical down/up events without interpreting or injecting input."""

    def __init__(self) -> None:
        self._pending: _RawMouseEvent | None = None

    def feed(
        self,
        *,
        kind: str,
        x_px: int,
        y_px: int,
        monotonic_ns: int,
        utc_ns: int,
        injected: bool,
    ) -> DesktopMouseClick | None:
        if kind not in {"left_down", "left_up"}:
            raise ValueError("only left_down and left_up are supported")
        event = _RawMouseEvent(
            kind=kind,
            x_px=int(x_px),
            y_px=int(y_px),
            monotonic_ns=int(monotonic_ns),
            utc_ns=int(utc_ns),
            injected=bool(injected),
        )
        if injected:
            self._pending = None
            return None
        if kind == "left_down":
            self._pending = event
            return None
        down = self._pending
        self._pending = None
        if down is None or event.monotonic_ns < down.monotonic_ns:
            return None
        return DesktopMouseClick(
            button="left",
            down_x_px=down.x_px,
            down_y_px=down.y_px,
            up_x_px=event.x_px,
            up_y_px=event.y_px,
            down_monotonic_ns=down.monotonic_ns,
            up_monotonic_ns=event.monotonic_ns,
            up_utc_ns=event.utc_ns,
        )


class WindowsGlobalMouseClickSource:
    """Observe physical global left-clicks through a read-only low-level hook."""

    _WH_MOUSE_LL = 14
    _WM_LBUTTONDOWN = 0x0201
    _WM_LBUTTONUP = 0x0202
    _WM_QUIT = 0x0012
    _LLMHF_INJECTED = 0x00000001
    _LLMHF_LOWER_IL_INJECTED = 0x00000002

    def __init__(self, *, queue_capacity: int = 4096) -> None:
        if os.name != "nt":
            raise OSError("Windows global mouse observation requires Windows")
        if queue_capacity < 16:
            raise ValueError("queue_capacity must be at least 16")
        self._queue: Queue[_RawMouseEvent | None] = Queue(queue_capacity)
        self._callback: Callable[[DesktopMouseClick], None] | None = None
        self._hook_thread: Thread | None = None
        self._dispatch_thread: Thread | None = None
        self._hook_thread_id: int | None = None
        self._hook_handle = None
        self._hook_proc = None
        self._ready = Event()
        self._stop = Event()
        self._error: BaseException | None = None
        self._dropped_events = 0
        self._state_lock = Lock()
        self._api = _load_windows_gui_api()

    @property
    def failed(self) -> bool:
        return self._error is not None

    @property
    def error(self) -> BaseException | None:
        return self._error

    @property
    def dropped_events(self) -> int:
        with self._state_lock:
            return self._dropped_events

    def start(self, callback: Callable[[DesktopMouseClick], None]) -> None:
        if self._hook_thread is not None:
            raise RuntimeError("mouse click source has already been started")
        self._callback = callback
        self._dispatch_thread = Thread(
            target=self._dispatch_loop,
            name="windows-mouse-click-dispatch",
            daemon=True,
        )
        self._hook_thread = Thread(
            target=self._hook_loop,
            name="windows-mouse-hook",
            daemon=True,
        )
        self._dispatch_thread.start()
        self._hook_thread.start()
        if not self._ready.wait(2.0):
            self.stop(raise_on_failure=False)
            raise TimeoutError("Windows mouse hook did not become ready")
        try:
            self.raise_if_failed()
        except Exception:
            self.stop(raise_on_failure=False)
            raise

    def stop(self, *, raise_on_failure: bool = True) -> None:
        self._stop.set()
        self._wake_hook_loop()
        if self._hook_thread is not None:
            self._hook_thread.join(timeout=2.0)
            if self._hook_thread.is_alive():
                raise TimeoutError("Windows mouse hook did not stop")
        self._enqueue(None)
        if self._dispatch_thread is not None:
            self._dispatch_thread.join(timeout=2.0)
            if self._dispatch_thread.is_alive():
                raise TimeoutError("mouse click dispatcher did not stop")
        if raise_on_failure:
            self.raise_if_failed()

    def raise_if_failed(self) -> None:
        if self._error is not None:
            raise RuntimeError("Windows mouse click observation failed") from self._error

    def _hook_loop(self) -> None:
        user32, kernel32 = self._api.user32, self._api.kernel32
        try:
            self._hook_thread_id = int(kernel32.GetCurrentThreadId())
            self._hook_proc = self._api.hook_proc_type(self._handle_hook_event)
            self._hook_handle = user32.SetWindowsHookExW(
                self._WH_MOUSE_LL,
                self._hook_proc,
                None,
                0,
            )
            if not self._hook_handle:
                raise ctypes.WinError(ctypes.get_last_error())
            self._ready.set()
            message = wintypes.MSG()
            while not self._stop.is_set():
                result = user32.GetMessageW(
                    ctypes.byref(message),
                    None,
                    0,
                    0,
                )
                if result == 0:
                    break
                if result == -1:
                    raise ctypes.WinError(ctypes.get_last_error())
                user32.TranslateMessage(ctypes.byref(message))
                user32.DispatchMessageW(ctypes.byref(message))
        except BaseException as exc:
            self._set_error(exc)
            self._ready.set()
        finally:
            if self._hook_handle:
                try:
                    if not user32.UnhookWindowsHookEx(self._hook_handle):
                        raise ctypes.WinError(ctypes.get_last_error())
                    self._hook_handle = None
                except BaseException as exc:
                    self._set_error(exc)
            self._enqueue(None)

    def _handle_hook_event(self, code, message, pointer):
        """Never consume input intentionally, and never let an exception cross HOOKPROC."""
        try:
            if not self._stop.is_set() and code >= 0 and message in {self._WM_LBUTTONDOWN, self._WM_LBUTTONUP}:
                data = ctypes.cast(pointer, ctypes.POINTER(_MouseHookData)).contents
                self._enqueue(_RawMouseEvent(
                    kind="left_down" if message == self._WM_LBUTTONDOWN else "left_up",
                    x_px=int(data.pt.x), y_px=int(data.pt.y),
                    monotonic_ns=time.perf_counter_ns(), utc_ns=time.time_ns(),
                    injected=bool(int(data.flags) & (self._LLMHF_INJECTED | self._LLMHF_LOWER_IL_INJECTED)),
                ))
        except BaseException as exc:
            self._disable_failed_hook(exc)
        try:
            # HHOOK is ignored by this API. Preserve the exact next-hook result.
            return self._api.user32.CallNextHookEx(None, code, message, pointer)
        except BaseException as exc:
            self._disable_failed_hook(exc)
            # A nonzero LowLevelMouseProc result suppresses delivery. If forwarding
            # itself fails, uninstall this observer and avoid suppressing the event.
            return 0

    def _disable_failed_hook(self, error: BaseException) -> None:
        self._set_error(error)
        self._stop.set()
        if self._hook_handle:
            try:
                if self._api.user32.UnhookWindowsHookEx(self._hook_handle):
                    self._hook_handle = None
            except BaseException:
                pass  # Retain the original error and retry unhooking in finally.
        self._wake_hook_loop()

    def _wake_hook_loop(self) -> None:
        if self._hook_thread_id is not None:
            try:
                self._api.user32.PostThreadMessageW(self._hook_thread_id, self._WM_QUIT, 0, 0)
            except BaseException as exc:
                self._set_error(exc)

    def _dispatch_loop(self) -> None:
        assembler = MouseClickAssembler()
        try:
            while True:
                try:
                    event = self._queue.get(timeout=0.2)
                except Empty:
                    if self._stop.is_set() and self._hook_thread is not None and not self._hook_thread.is_alive():
                        break
                    continue
                if event is None:
                    break
                click = assembler.feed(
                    kind=event.kind,
                    x_px=event.x_px,
                    y_px=event.y_px,
                    monotonic_ns=event.monotonic_ns,
                    utc_ns=event.utc_ns,
                    injected=event.injected,
                )
                if click is not None and self._callback is not None:
                    self._callback(click)
        except BaseException as exc:
            self._set_error(exc)
            self._stop.set()
            self._wake_hook_loop()

    def _enqueue(self, event: _RawMouseEvent | None) -> None:
        if event is None:
            # A shutdown marker must not be lost merely because physical input
            # filled the bounded queue. Dropping one final raw event is safer
            # than leaving the dispatcher alive indefinitely during cleanup.
            try:
                self._queue.put_nowait(None)
            except Full:
                try:
                    self._queue.get_nowait()
                except Empty:
                    pass
                self._queue.put_nowait(None)
                with self._state_lock:
                    self._dropped_events += 1
            return
        try:
            self._queue.put_nowait(event)
        except Full:
            with self._state_lock:
                self._dropped_events += 1

    def _set_error(self, error: BaseException) -> None:
        with self._state_lock:
            if self._error is None:
                self._error = error


class WindowsDesktopWindowLocator:
    """Resolve the Nanonis module window under a screen point, read-only."""

    _GA_ROOT = 2
    _PROCESS_QUERY_LIMITED_INFORMATION = 0x1000

    def __init__(
        self,
        *,
        allowed_windows: Collection[tuple[str, str]] | None = None,
    ) -> None:
        if os.name != "nt":
            raise OSError("Windows desktop window lookup requires Windows")
        self._allowed_windows = (
            None if allowed_windows is None else frozenset(allowed_windows)
        )
        self._process_cache: dict[int, str] = {}
        self._api = _load_windows_gui_api()

    def window_at(self, x_px: int, y_px: int) -> DesktopWindow | None:
        user32, kernel32 = self._api.user32, self._api.kernel32
        handle = user32.WindowFromPoint(wintypes.POINT(int(x_px), int(y_px)))
        if not handle:
            return None
        handle = self._module_or_root_window(user32, handle)
        if not user32.IsWindowVisible(handle):
            return None

        process_id = wintypes.DWORD()
        user32.GetWindowThreadProcessId(handle, ctypes.byref(process_id))
        rectangle = wintypes.RECT()
        if not user32.GetWindowRect(handle, ctypes.byref(rectangle)):
            return None
        title = self._window_text(user32, handle)
        class_name = self._class_name(user32, handle)
        if (
            self._allowed_windows is not None
            and (title, class_name) not in self._allowed_windows
        ):
            return None
        process_id_value = int(process_id.value)
        executable = self._process_cache.get(process_id_value)
        if executable is None:
            executable = self._process_executable(kernel32, process_id_value)
            if len(self._process_cache) >= 64:
                self._process_cache.clear()
            self._process_cache[process_id_value] = executable
        return DesktopWindow(
            handle=int(handle),
            process_id=process_id_value,
            process_executable=executable,
            title=title,
            class_name=class_name,
            left_px=int(rectangle.left),
            top_px=int(rectangle.top),
            right_px=int(rectangle.right),
            bottom_px=int(rectangle.bottom),
        )

    @staticmethod
    def _window_text(user32, handle) -> str:
        length = max(1, int(user32.GetWindowTextLengthW(handle)) + 1)
        buffer = ctypes.create_unicode_buffer(length)
        user32.GetWindowTextW(handle, buffer, length)
        return buffer.value

    @staticmethod
    def _class_name(user32, handle) -> str:
        buffer = ctypes.create_unicode_buffer(256)
        user32.GetClassNameW(handle, buffer, len(buffer))
        return buffer.value

    def _module_or_root_window(self, user32, handle):
        """Prefer the titled LabVIEW child hosting one Nanonis module.

        ``GA_ROOT`` can collapse a ``Motor Control`` module into the outer
        ``Nanonis Mimea`` frame. The module title and its own bounds are the
        evidence needed by the reviewed button map, so walk the parent chain
        first and preserve the nearest titled ``LVDChild``.
        """

        current = handle
        for _ in range(32):
            if (
                self._class_name(user32, current) == "LVDChild"
                and self._window_text(user32, current)
            ):
                return current
            parent = user32.GetParent(current)
            if not parent or parent == current:
                break
            current = parent
        root = user32.GetAncestor(handle, self._GA_ROOT)
        return root or handle

    def _process_executable(self, kernel32, process_id: int) -> str:
        handle = kernel32.OpenProcess(
            self._PROCESS_QUERY_LIMITED_INFORMATION,
            False,
            process_id,
        )
        if not handle:
            return ""
        try:
            capacity = wintypes.DWORD(32768)
            buffer = ctypes.create_unicode_buffer(capacity.value)
            if not kernel32.QueryFullProcessImageNameW(
                handle,
                0,
                buffer,
                ctypes.byref(capacity),
            ):
                return ""
            return Path(buffer.value).name
        finally:
            kernel32.CloseHandle(handle)
