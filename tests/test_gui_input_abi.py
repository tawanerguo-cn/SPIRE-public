"""Win32 ABI regression checks without installing any desktop hook or injecting input."""
from __future__ import annotations

import ctypes
from ctypes import wintypes
import os
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from instrument_layer.gui_input import (
    WindowsGlobalMouseClickSource, _MouseHookData, _configure_windows_gui_api,
)


class Function:
    def __init__(self, result=1):
        self.result = result
        self.calls = []
        self.error = None

    def __call__(self, *args):
        self.calls.append(args)
        if self.error:
            raise self.error
        return self.result


class Library:
    def __getattr__(self, name):
        function = Function()
        setattr(self, name, function)
        return function


@unittest.skipUnless(os.name == "nt", "Windows ABI test, no hooks installed")
class WindowsMouseAbiTests(unittest.TestCase):
    def setUp(self):
        self.user32, self.kernel32 = Library(), Library()
        self.api = _configure_windows_gui_api(self.user32, self.kernel32)
        with patch("instrument_layer.gui_input._load_windows_gui_api", return_value=self.api):
            self.source = WindowsGlobalMouseClickSource(queue_capacity=16)

    def test_every_called_win32_function_has_an_explicit_abi(self):
        for library in (self.user32, self.kernel32):
            for name, function in vars(library).items():
                with self.subTest(name=name):
                    self.assertIsInstance(function.argtypes, list)
                    self.assertTrue(hasattr(function, "restype"))
        self.assertEqual(self.user32.CallNextHookEx.argtypes,
                         [wintypes.HANDLE, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM])
        self.assertEqual(self.kernel32.CloseHandle.argtypes, [wintypes.HANDLE])
        self.assertEqual(ctypes.sizeof(wintypes.LPARAM), ctypes.sizeof(ctypes.c_void_p))

    @unittest.skipUnless(ctypes.sizeof(ctypes.c_void_p) == 8, "64-bit pointer regression")
    def test_native_ffi_roundtrip_preserves_large_pointer_and_result(self):
        received = []
        result_value = 0x123456789AB
        native_type = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, wintypes.HANDLE, ctypes.c_int,
                                        wintypes.WPARAM, wintypes.LPARAM)
        def capture(handle, code, message, pointer):
            received.append((handle, code, message, pointer))
            return result_value
        native = native_type(capture)
        # A local C-callable function substitutes for user32. No desktop hook is
        # installed. The forwarding call gets its ABI only from our declarations.
        forward = ctypes.WINFUNCTYPE(ctypes.c_ssize_t)(ctypes.cast(native, ctypes.c_void_p).value)
        forward.argtypes = None
        self.user32.CallNextHookEx = forward
        self.api = _configure_windows_gui_api(self.user32, self.kernel32)
        self.source._api = self.api
        high_pointer = 0x2E1DC878AE0
        callback = self.api.hook_proc_type(self.source._handle_hook_event)
        self.assertEqual(callback(-1, 0x200, high_pointer), result_value)
        self.assertEqual(received, [(None, -1, 0x200, high_pointer)])
        self.assertFalse(self.source.failed)

    def test_mouse_move_is_forwarded_without_being_recorded(self):
        self.user32.CallNextHookEx.result = 41
        self.assertEqual(self.source._handle_hook_event(0, 0x200, 123), 41)
        self.assertTrue(self.source._queue.empty())
        self.assertEqual(len(self.user32.CallNextHookEx.calls), 1)

    def test_negative_hook_code_never_dereferences_event_pointer(self):
        self.user32.CallNextHookEx.result = -10
        self.assertEqual(self.source._handle_hook_event(-1, 0x201, 0), -10)
        self.assertTrue(self.source._queue.empty())

    def test_real_ctypes_click_pointer_is_read_and_forwarded(self):
        data = _MouseHookData()
        data.pt.x, data.pt.y, data.flags = 236, 337, 0
        pointer = ctypes.addressof(data)
        self.source._handle_hook_event(0, 0x201, pointer)
        event = self.source._queue.get_nowait()
        self.assertEqual((event.kind, event.x_px, event.y_px), ("left_down", 236, 337))
        self.assertEqual(self.user32.CallNextHookEx.calls[-1], (None, 0, 0x201, pointer))

    def test_forward_failure_is_reported_unhooks_and_returns_zero(self):
        self.source._hook_handle = 123
        self.source._hook_thread_id = 456
        self.user32.CallNextHookEx.error = ctypes.ArgumentError("synthetic LPARAM overflow")
        # Exercise the C callback boundary as well as the Python body. An escaped
        # callback exception would otherwise become an 'Exception ignored' warning.
        callback = self.api.hook_proc_type(self.source._handle_hook_event)
        self.assertEqual(callback(0, 0x200, 123), 0)
        self.assertTrue(self.source.failed)
        self.assertTrue(self.source._stop.is_set())
        self.assertIsNone(self.source._hook_handle)
        self.assertEqual(self.user32.UnhookWindowsHookEx.calls, [(123,)])
        self.assertEqual(self.user32.PostThreadMessageW.calls, [(456, 0x12, 0, 0)])

    def test_observation_failure_still_forwards_and_disables_observer(self):
        data = _MouseHookData()
        self.user32.CallNextHookEx.result = 7
        with patch.object(self.source, "_enqueue", side_effect=RuntimeError("queue failure")):
            self.assertEqual(self.source._handle_hook_event(0, 0x201, ctypes.addressof(data)), 7)
        self.assertTrue(self.source.failed)
        self.assertEqual(len(self.user32.CallNextHookEx.calls), 1)

    def test_cleanup_errors_do_not_escape_hook_callback(self):
        self.source._hook_handle = 123
        self.source._hook_thread_id = 456
        self.user32.CallNextHookEx.error = RuntimeError("forward failure")
        self.user32.UnhookWindowsHookEx.error = RuntimeError("unhook failure")
        self.user32.PostThreadMessageW.error = RuntimeError("wake failure")
        self.assertEqual(self.source._handle_hook_event(0, 0x200, 123), 0)
        self.assertEqual(str(self.source.error), "forward failure")

    @unittest.skipUnless(ctypes.sizeof(ctypes.c_void_p) == 8, "64-bit native struct layout")
    def test_hook_structure_matches_windows_64_bit_layout(self):
        self.assertEqual(ctypes.sizeof(_MouseHookData), 32)
        self.assertEqual(_MouseHookData.dwExtraInfo.offset, 24)


if __name__ == "__main__":
    unittest.main()
