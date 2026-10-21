from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import tempfile
from threading import Event, Lock, Thread
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from instrument_layer import (  # noqa: E402
    FakeNanonisBackend,
    DataLogRecipe,
    NanonisConnectionConfig,
    NanonisCommandError,
    NanonisDevice,
    benchmark_signal_polling,
    sample_signal_chart,
    load_connection_config,
)
from instrument_layer.backends import OfficialNanonisBackend  # noqa: E402
import instrument_layer.nanonis_device as nanonis_device_module  # noqa: E402
from instrument_layer.models import ZControllerStatus, to_jsonable  # noqa: E402


class NanonisDeviceTests(unittest.TestCase):
    def test_official_backend_serializes_commands_on_its_single_connection(self) -> None:
        state_lock = Lock()
        state = {"active": 0, "maximum": 0}

        class ConcurrentProbeClient:
            def __init__(self, connection) -> None:
                self.connection = connection

            def Bias_Get(self):
                with state_lock:
                    state["active"] += 1
                    state["maximum"] = max(
                        state["maximum"],
                        state["active"],
                    )
                time.sleep(0.02)
                with state_lock:
                    state["active"] -= 1
                return (0.2,)

            def close(self) -> None:
                pass

        with patch.dict(
            sys.modules,
            {
                "nanonis_spm": SimpleNamespace(
                    Nanonis=ConcurrentProbeClient
                )
            },
        ):
            backend = OfficialNanonisBackend(
                NanonisConnectionConfig(),
                connection=object(),
            )
            workers = [
                Thread(target=backend.Bias_Get)
                for _ in range(4)
            ]
            for worker in workers:
                worker.start()
            for worker in workers:
                worker.join()
            backend.close()

        self.assertEqual(state["maximum"], 1)

    def test_official_backend_repairs_bias_spectroscopy_status_shape(self) -> None:
        calls = []

        class StatusClient:
            def __init__(self, connection) -> None:
                self.connection = connection

            def quickSend(self, *args):
                calls.append(args)
                return ("", b"raw", [1])

            def close(self) -> None:
                pass

        with patch.dict(
            sys.modules,
            {"nanonis_spm": SimpleNamespace(Nanonis=StatusClient)},
        ):
            backend = OfficialNanonisBackend(
                NanonisConnectionConfig(),
                connection=object(),
            )
            self.assertTrue(
                NanonisDevice(
                    backend=backend
                ).get_bias_spectroscopy_status()
            )

        self.assertEqual(
            calls,
            [("BiasSpectr.StatusGet", [], [], ["I"])],
        )

    def test_official_backend_prioritizes_waiting_signal_reads(self) -> None:
        first_low_started = Event()
        release_first_low = Event()
        order = []
        low_count = 0
        count_lock = Lock()

        class PriorityProbeClient:
            def __init__(self, connection) -> None:
                self.connection = connection

            def Bias_Get(self):
                nonlocal low_count
                with count_lock:
                    low_count += 1
                    index = low_count
                order.append(f"low-{index}-start")
                if index == 1:
                    first_low_started.set()
                    release_first_low.wait(timeout=1.0)
                order.append(f"low-{index}-end")
                return (0.2,)

            def Signals_ValsGet(self, indexes, wait_newest):
                order.append("signals")
                return (1, [0.0])

            def close(self) -> None:
                pass

        with patch.dict(
            sys.modules,
            {"nanonis_spm": SimpleNamespace(Nanonis=PriorityProbeClient)},
        ):
            backend = OfficialNanonisBackend(
                NanonisConnectionConfig(),
                connection=object(),
            )
            first_low = Thread(target=backend.Bias_Get)
            signals = Thread(
                target=lambda: backend.Signals_ValsGet([0], 0)
            )
            second_low = Thread(target=backend.Bias_Get)
            first_low.start()
            self.assertTrue(first_low_started.wait(timeout=1.0))
            signals.start()
            time.sleep(0.01)
            second_low.start()
            release_first_low.set()
            for worker in (first_low, signals, second_low):
                worker.join(timeout=1.0)
                self.assertFalse(worker.is_alive())
            backend.close()

        self.assertLess(
            order.index("signals"),
            order.index("low-2-start"),
        )

    def test_load_config_and_env_overrides(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "nanonis.toml"
            config_path.write_text(
                "[connection]\nhost='192.0.2.10'\nport=6501\ntimeout_s=4.0\nbackend='fake'\n",
                encoding="utf-8",
            )
            old_env = {key: os.environ.get(key) for key in ("NANONIS_HOST", "NANONIS_PORT", "NANONIS_TIMEOUT_S")}
            try:
                os.environ["NANONIS_HOST"] = "192.0.2.99"
                os.environ["NANONIS_PORT"] = "6502"
                os.environ["NANONIS_TIMEOUT_S"] = "1.5"
                config = load_connection_config(config_path)
            finally:
                for key, value in old_env.items():
                    if value is None:
                        os.environ.pop(key, None)
                    else:
                        os.environ[key] = value

        self.assertEqual(config.host, "192.0.2.99")
        self.assertEqual(config.port, 6502)
        self.assertEqual(config.timeout_s, 1.5)
        self.assertEqual(config.backend, "fake")

    def test_device_uses_fake_by_default(self) -> None:
        device = NanonisDevice(config=NanonisConnectionConfig())

        self.assertIsInstance(device.backend, FakeNanonisBackend)

    def test_live_backend_selection_is_explicit(self) -> None:
        class BackendSentinel:
            def __init__(self, config):
                self.config = config

            def close(self) -> None:
                pass

        config = NanonisConnectionConfig(backend="fake")
        with patch.object(nanonis_device_module, "OfficialNanonisBackend", BackendSentinel):
            device = NanonisDevice(config=config, live=True)

        self.assertIsInstance(device.backend, BackendSentinel)
        self.assertEqual(device.backend.config.host, "127.0.0.1")
        self.assertIs(OfficialNanonisBackend.__name__, "OfficialNanonisBackend")

    def test_status_snapshot_serializes_units_and_status(self) -> None:
        backend = FakeNanonisBackend()
        device = NanonisDevice(backend=backend)

        snapshot = device.get_status_snapshot()
        payload = to_jsonable(snapshot)

        self.assertEqual(snapshot.bias_v, -3.5)
        self.assertAlmostEqual(snapshot.current_a, -4.8112e-11)
        self.assertAlmostEqual(snapshot.z_position_m, 128e-9)
        self.assertEqual(snapshot.z_controller_status, ZControllerStatus.ON)
        self.assertTrue(snapshot.scan_status.running)
        json.dumps(payload)
        self.assertEqual(payload["z_controller_status"], "on")
        self.assertEqual(payload["scan_frame"]["width_m"], 1e-7)

    def test_tip_state_snapshot_uses_only_read_only_getters(self) -> None:
        backend = FakeNanonisBackend(
            current_a=-2e-12,
            z_position_m=100e-9,
            z_status_code=2,
            z_controller_on=True,
            z_limits_enabled=True,
            z_high_limit_m=200e-9,
            z_low_limit_m=-200e-9,
            tip_lift_m=3e-9,
        )
        device = NanonisDevice(backend=backend)

        snapshot = device.get_tip_state_snapshot()

        self.assertEqual(snapshot.z_controller_status, ZControllerStatus.ON)
        self.assertTrue(snapshot.z_controller_on)
        self.assertAlmostEqual(snapshot.z_position_m, 100e-9)
        self.assertTrue(snapshot.z_limits_enabled)
        self.assertAlmostEqual(snapshot.z_limits.high_limit_m, 200e-9)
        self.assertAlmostEqual(snapshot.z_limits.low_limit_m, -200e-9)
        self.assertAlmostEqual(snapshot.tip_lift_m, 3e-9)
        self.assertAlmostEqual(snapshot.current_a, -2e-12)
        self.assertEqual(
            [name for name, _args in backend.command_log],
            ["ZCtrl_OnOffGet", "ZCtrl_LimitsEnabledGet", "ZCtrl_LimitsGet", "ZCtrl_TipLiftGet"],
        )

    def test_auto_approach_status_is_read_only_and_typed(self) -> None:
        backend = FakeNanonisBackend(auto_approach_running=True)

        self.assertTrue(NanonisDevice(backend=backend).get_auto_approach_running())
        self.assertEqual(backend.command_log, [("AutoApproach_OnOffGet", ())])

    def test_spectrum_analyzer_settings_and_data_are_typed(self) -> None:
        backend = FakeNanonisBackend()
        device = NanonisDevice(backend=backend)

        settings = device.get_spectrum_analyzer_settings(instance=1)
        data = device.get_spectrum_analyzer_data(instance=1)

        self.assertEqual(settings.channel_index, 0)
        self.assertEqual(settings.frequency_range_hz, 2_000.0)
        self.assertEqual(settings.frequency_resolution_hz, 1.0)
        self.assertEqual(settings.averaging_count, 10)
        self.assertFalse(settings.ac_coupling)
        self.assertEqual(data.density, (0.1, 0.2, 0.3))
        self.assertEqual(data.frequencies_hz, (100.0, 101.0, 102.0))
        self.assertEqual(data.last_frequency_hz, 102.0)

    def test_spectrum_analyzer_rejects_invalid_instance_and_count_mismatch(self) -> None:
        backend = FakeNanonisBackend()
        device = NanonisDevice(backend=backend)

        with self.assertRaisesRegex(ValueError, "instance must be 1 or 2"):
            device.get_spectrum_analyzer_data(instance=3)

        class BadCountFake(FakeNanonisBackend):
            def SpectrumAnlzr_DataGet(self, Spectrum_Analyzer_instance):
                return (100.0, 1.0, 3, [0.1, 0.2])

        with self.assertRaisesRegex(ValueError, "returned 2 values; expected 3"):
            NanonisDevice(backend=BadCountFake()).get_spectrum_analyzer_data()

    def test_signal_lookup_reports_missing_names(self) -> None:
        device = NanonisDevice(backend=FakeNanonisBackend())

        values = device.read_signals(["Missing Signal", "Current (A)"])

        self.assertEqual(values[0].name, "Missing Signal")
        self.assertIsNone(values[0].value)
        self.assertEqual(values[0].error, "signal_not_found")
        self.assertEqual(values[1].name, "Current (A)")
        self.assertAlmostEqual(values[1].value or 0.0, -4.8e-11)

    def test_signal_chart_sampling_uses_resolved_signal_indexes(self) -> None:
        backend = FakeNanonisBackend()
        device = NanonisDevice(backend=backend)

        references = device.resolve_signals(["Current (A)", "Z (m)"])
        sample = sample_signal_chart(device, references, clock=lambda: 42.0)

        self.assertEqual([(item.index, item.name) for item in references], [(0, "Current (A)"), (1, "Z (m)")])
        self.assertEqual(sample.timestamp_s, 42.0)
        self.assertEqual(backend.command_log, [("Signals_ValsGet", ([0, 1], 0))])
        self.assertAlmostEqual(sample.values[0].value or 0.0, -4.8e-11)
        self.assertAlmostEqual(sample.values[1].value or 0.0, 128e-9)

    def test_signal_polling_benchmark_reports_rate(self) -> None:
        device = NanonisDevice(backend=FakeNanonisBackend())
        references = device.resolve_signals(["Current (A)", "Z (m)"])

        result = benchmark_signal_polling(device, references, duration_s=0.001)

        self.assertGreater(result.samples, 0)
        self.assertGreater(result.samples_per_s, 0)

    def test_data_log_read_api_is_typed_and_uses_full_signal_indexes(self) -> None:
        names = [f"Signal {index}" for index in range(31)]
        names[0] = "Current (A)"
        names[28] = "X (m)"
        names[29] = "Y (m)"
        names[30] = "Z (m)"
        backend = FakeNanonisBackend(
            signal_names=names,
            data_log_channel_indexes=[0, 28, 29, 30],
            data_log_saved_points=123,
            data_log_saved_file_path=r"C:\data\example-00001.dat",
        )
        device = NanonisDevice(backend=backend)

        channels = device.get_data_log_channels()
        properties = device.get_data_log_properties()
        status = device.get_data_log_status()

        self.assertEqual(channels.indexes, (0, 28, 29, 30))
        self.assertEqual([channel.name for channel in channels.channels], ["Current (A)", "X (m)", "Y (m)", "Z (m)"])
        self.assertEqual(properties.averaging, 1)
        self.assertEqual(status.saved_points, 123)
        self.assertEqual(status.saved_file_path, r"C:\data\example-00001.dat")

    def test_data_log_fake_configuration_start_and_stop_are_bounded(self) -> None:
        names = [f"Signal {index}" for index in range(31)]
        names[0], names[28], names[29], names[30] = "Current (A)", "X (m)", "Y (m)", "Z (m)"
        backend = FakeNanonisBackend(signal_names=names)
        device = NanonisDevice(backend=backend)
        recipe = DataLogRecipe(
            signals=("Current (A)", "X (m)", "Y (m)", "Z (m)"),
            basename="SPiRE_test",
            comment="lossless",
        )

        channels, properties = device.configure_data_log(recipe)
        started = device.start_data_log()
        stopped = device.stop_data_log()

        self.assertEqual(channels.indexes, (0, 28, 29, 30))
        self.assertEqual(properties.averaging, 1)
        self.assertTrue(started.running)
        self.assertFalse(stopped.running)
        self.assertEqual(
            [name for name, _args in backend.command_log if name.startswith("DataLog_")],
            [
                "DataLog_ChsSet",
                "DataLog_PropsSet",
                "DataLog_ChsGet",
                "DataLog_PropsGet",
                "DataLog_ChsGet",
                "DataLog_PropsGet",
                "DataLog_Start",
                "DataLog_StatusGet",
                "DataLog_Stop",
                "DataLog_StatusGet",
            ],
        )

    def test_data_log_official_singleton_tuple_indexes_are_unwrapped(self) -> None:
        class OfficialShapeFake(FakeNanonisBackend):
            def DataLog_ChsGet(self):
                return ("", b"raw", [4, [(0,), (28,), (29,), (30,)]])

        names = [f"Signal {index}" for index in range(31)]
        names[0], names[28], names[29], names[30] = "Current (A)", "X (m)", "Y (m)", "Z (m)"
        channels = NanonisDevice(backend=OfficialShapeFake(signal_names=names)).get_data_log_channels()

        self.assertEqual(channels.indexes, (0, 28, 29, 30))

    def test_scan_buffer_official_singleton_tuple_indexes_are_unwrapped(self) -> None:
        class OfficialShapeFake(FakeNanonisBackend):
            def Scan_BufferGet(self):
                return ("", b"raw", [3, [(0,), (28,), (30,)], 512, 256])

        buffer = NanonisDevice(backend=OfficialShapeFake()).get_scan_buffer()

        self.assertEqual(buffer.channel_indexes, [0, 28, 30])
        self.assertEqual((buffer.pixels, buffer.lines), (512, 256))

    def test_data_log_rejects_averaging_or_path_like_basename(self) -> None:
        device = NanonisDevice(backend=FakeNanonisBackend())

        with self.assertRaisesRegex(ValueError, "averaging=1"):
            device.configure_data_log(DataLogRecipe(("Current (A)",), "SPiRE", averaging=2))
        with self.assertRaisesRegex(ValueError, "safe filename"):
            device.configure_data_log(DataLogRecipe(("Current (A)",), r"..\escape"))

    def test_official_response_shape_is_unwrapped(self) -> None:
        class OfficialShapeFake(FakeNanonisBackend):
            def Signals_NamesGet(self):
                return ("", b"raw", [42, 2, ["Current (A)", "Z (m)"]])

            def Signals_ValsGet(self, Signals_indexes, Wait_for_newest_data):
                self._record("Signals_ValsGet", list(Signals_indexes), Wait_for_newest_data)
                return ("", b"raw", [2, [(-1.2e-12,), (128e-9,)]])

            def Bias_Get(self):
                return ("", b"raw", [-3.5])

        backend = OfficialShapeFake()
        device = NanonisDevice(backend=backend)

        self.assertEqual(device.get_signal_catalog(), ["Current (A)", "Z (m)"])
        self.assertEqual(device.resolve_signals(["Current (A)", "Z (m)"])[1].index, 1)
        self.assertEqual(device.get_bias_v(), -3.5)
        values = device.read_signals(["Current (A)", "Z (m)"])
        self.assertAlmostEqual(values[0].value or 0.0, -1.2e-12)
        self.assertAlmostEqual(values[1].value or 0.0, 128e-9)

    def test_recent_data_files_are_read_from_configured_session_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            older = root / "older.sxm"
            newer = root / "newer.dat"
            ignored = root / "ignored.txt"
            older.write_text("older", encoding="utf-8")
            newer.write_text("newer", encoding="utf-8")
            ignored.write_text("ignored", encoding="utf-8")
            os.utime(older, (1000, 1000))
            os.utime(newer, (2000, 2000))
            backend = FakeNanonisBackend(session_path=str(root))
            device = NanonisDevice(backend=backend)

            files = device.list_recent_data_files(limit=10)

        self.assertEqual([item.path.name for item in files], ["newer.dat", "older.sxm"])

    def test_stop_all_motion_without_withdraw_has_safe_command_order(self) -> None:
        backend = FakeNanonisBackend()
        device = NanonisDevice(backend=backend)

        device.stop_all_motion(withdraw=False)

        self.assertEqual(
            backend.command_log,
            [
                ("Scan_Action", (1, 1)),
                ("FolMe_Stop", ()),
                ("Motor_StopMove", ()),
            ],
        )

    def test_withdraw_tip_is_explicit(self) -> None:
        backend = FakeNanonisBackend()
        device = NanonisDevice(backend=backend)

        device.withdraw_tip(wait=True, timeout_ms=1234)

        self.assertEqual(backend.command_log, [("ZCtrl_Withdraw", (1, 1234))])
        self.assertEqual(device.get_z_controller_status(), ZControllerStatus.WITHDRAWING)

    def test_signal_chart_tool_once_defaults_to_fake_backend(self) -> None:
        result = subprocess.run(
            [sys.executable, "-B", "tools/signal_chart_readout.py", "--once"],
            cwd=Path(__file__).resolve().parents[1],
            capture_output=True,
            text=True,
            check=False,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("backend=fake", result.stdout)
        self.assertIn("Current (A)", result.stdout)
        self.assertIn("Z (m)", result.stdout)

    @unittest.skipUnless(os.environ.get("RUN_LIVE_NANONIS") == "1", "live Nanonis smoke test is opt-in only")
    def test_live_readonly_smoke(self) -> None:
        device = NanonisDevice(live=True)
        try:
            version = device.get_version()
            status = device.get_scan_status()
        finally:
            device.close()

        self.assertTrue(version.product_line)
        self.assertIn(status.raw_code, {0, 1})


if __name__ == "__main__":
    unittest.main()
