#!/usr/bin/env python3
"""Tests for thinkpad-fand, focused on the optional NVIDIA GPU source.

No NVIDIA hardware (and no root) is needed: the GPU sensor takes its backends
and its view of the PCI bus as constructor arguments, so every scenario -
no GPU, GPU present, GPU asleep, GPU lost, GPU back, garbage readings - is
exercised with fakes.

Run with:  python3 -m unittest discover -s tests -v
"""

import importlib.machinery
import importlib.util
import json
import os
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _load(module_name, filename):
    """Import one of the src/ scripts (they have no .py extension)."""
    path = os.path.join(ROOT, "src", filename)
    loader = importlib.machinery.SourceFileLoader(module_name, path)
    spec = importlib.util.spec_from_loader(module_name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


fand = _load("thinkpad_fand", "thinkpad-fand")


class FakeClock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


def boom():
    raise AssertionError("this backend must not be called")


class GpuSensorTest(unittest.TestCase):
    """The GPU sensor is optional and must fail quietly, always."""

    def build(self, backends, devices=("0000:01:00.0",), awake=True):
        self.clock = FakeClock()
        self.awake = awake
        return fand.GpuTempSensor(
            backends=backends,
            pci_devices=lambda: list(devices),
            pci_awake=lambda _d: self.awake,
            clock=self.clock,
        )

    # --- no GPU ---------------------------------------------------------- #
    def test_no_nvidia_gpu_present(self):
        """No NVIDIA PCI device: nothing that talks to a driver is ever called."""
        sensor = self.build([("nvml", True, boom), ("nvidia-smi", True, boom)],
                            devices=())
        self.assertIsNone(sensor.read())
        self.assertEqual(sensor.state, "absent")
        self.assertIsNone(sensor.backend)

    def test_machine_without_gpu_never_spawns_nvidia_smi(self):
        calls = []
        sensor = self.build([("nvidia-smi", True, lambda: calls.append(1))], devices=())
        for _ in range(10):
            sensor.read()
        self.assertEqual(calls, [])

    def test_monitoring_off_reads_nothing(self):
        sensor = self.build([("nvml", True, boom)])
        self.assertIsNone(sensor.read(mode="off"))
        self.assertEqual(sensor.state, "off")

    # --- GPU present ----------------------------------------------------- #
    def test_gpu_detected(self):
        sensor = self.build([("nvml", True, lambda: 61.4)])
        self.assertEqual(sensor.read(), 61)
        self.assertEqual(sensor.state, "ok")
        self.assertEqual(sensor.backend, "nvml")

    def test_falls_through_to_the_next_backend(self):
        sensor = self.build([
            ("nvml", True, lambda: None),
            ("hwmon", False, lambda: None),
            ("thinkpad-ec", False, lambda: 58.0),
        ])
        self.assertEqual(sensor.read(), 58)
        self.assertEqual(sensor.backend, "thinkpad-ec")

    def test_working_backend_is_preferred_next_time(self):
        order = []
        sensor = self.build([
            ("nvml", True, lambda: (order.append("nvml"), None)[1]),
            ("hwmon", False, lambda: (order.append("hwmon"), 50.0)[1]),
        ])
        self.assertEqual(sensor.read(), 50)
        order.clear()
        self.clock.advance(1)
        self.assertEqual(sensor.read(), 50)
        self.assertEqual(order[0], "hwmon")      # tried the known-good one first

    # --- unavailable / asleep / lost ------------------------------------- #
    def test_gpu_temperature_unavailable(self):
        sensor = self.build([("nvml", True, lambda: None)])
        self.assertIsNone(sensor.read())
        self.assertEqual(sensor.state, "unavailable")

    def test_suspended_gpu_is_left_alone(self):
        """A runtime-suspended dGPU must not be woken just to read its temp."""
        sensor = self.build([("nvml", True, boom)], awake=False)
        self.awake = False
        self.assertIsNone(sensor.read())
        self.assertEqual(sensor.state, "asleep")

    def test_gpu_disappears_after_startup(self):
        readings = [64.0, None, None]
        sensor = self.build([("nvml", True, lambda: readings.pop(0))])
        self.assertEqual(sensor.read(), 64)
        self.clock.advance(2)
        self.assertIsNone(sensor.read())
        self.assertEqual(sensor.state, "unavailable")

    def test_gpu_reappears(self):
        readings = [None, 55.0]
        sensor = self.build([("nvml", True, lambda: readings.pop(0))])
        self.assertIsNone(sensor.read())
        self.clock.advance(1)
        self.assertIsNone(sensor.read())          # still on cooldown, not retried
        self.clock.advance(fand.GpuTempSensor.RETRY_SECONDS)
        self.assertEqual(sensor.read(), 55)
        self.assertEqual(sensor.state, "ok")

    def test_gpu_sleeps_then_wakes(self):
        sensor = self.build([("nvml", True, lambda: 70.0)])
        self.assertEqual(sensor.read(), 70)
        self.awake = False
        self.clock.advance(2)
        self.assertIsNone(sensor.read())
        self.assertEqual(sensor.state, "asleep")
        self.awake = True
        self.clock.advance(2)
        self.assertEqual(sensor.read(), 70)

    # --- hostile readings ------------------------------------------------ #
    def test_malformed_readings_never_crash(self):
        for bad in ("hot", object(), float("nan"), -5, 0, 900, [1], None):
            with self.subTest(value=bad):
                sensor = self.build([("nvml", True, lambda v=bad: v)])
                self.assertIsNone(sensor.read())

    def test_backend_exception_is_contained(self):
        def explode():
            raise RuntimeError("driver went away")

        sensor = self.build([("nvml", True, explode), ("hwmon", False, lambda: 44.0)])
        self.assertEqual(sensor.read(), 44)       # the healthy backend still answers

    def test_anything_escaping_the_sensor_is_contained(self):
        """Even a bug inside the sensor itself must not reach the control loop."""
        sensor = self.build([("nvml", True, lambda: 50.0)])

        def explode(_mode):
            raise RuntimeError("bug in the sensor")

        sensor._read = explode
        self.assertIsNone(sensor.read())
        self.assertIsNone(sensor.read())              # and it only logs once
        self.assertEqual(sensor.state, "unavailable")

    def test_pci_scan_failure_is_contained(self):
        def bad_scan():
            raise OSError("sysfs unreadable")

        sensor = fand.GpuTempSensor(backends=[("hwmon", False, lambda: 40.0)],
                                    pci_devices=bad_scan,
                                    pci_awake=lambda _d: True,
                                    clock=FakeClock())
        self.assertEqual(sensor.read(), 40)

    # --- nvidia-smi ------------------------------------------------------ #
    def test_nvidia_smi_is_rate_limited(self):
        calls = []

        def fake_run():
            calls.append(1)
            return 62.0

        sensor = self.build([("nvidia-smi", True, None)])
        sensor.backends = [("nvidia-smi", True, sensor._smi)]
        fand.run_nvidia_smi, real = fake_run, fand.run_nvidia_smi
        try:
            for _ in range(6):                    # 6 polls, 1 s apart
                sensor.read()
                self.clock.advance(1)
        finally:
            fand.run_nvidia_smi = real
        self.assertLessEqual(len(calls), 2)       # not once per poll

    def test_parse_nvidia_smi(self):
        self.assertEqual(fand.parse_nvidia_smi("51\n"), 51.0)
        self.assertEqual(fand.parse_nvidia_smi("45\n60\n"), 60.0)
        self.assertIsNone(fand.parse_nvidia_smi("[N/A]\n"))
        self.assertIsNone(fand.parse_nvidia_smi("[Not Supported]"))
        self.assertIsNone(fand.parse_nvidia_smi(""))
        self.assertIsNone(fand.parse_nvidia_smi(None))
        self.assertEqual(fand.parse_nvidia_smi("warning: blah\n58"), 58.0)


class LevelSelectionTest(unittest.TestCase):
    """fan_level = max(cpu_requested_level, gpu_requested_level)."""

    def setUp(self):
        self.cfg = fand.validate_config({})

    def test_cpu_requests_higher_level(self):
        levels = fand.requested_levels({"cpu": 84, "gpu": 50}, {}, self.cfg)
        self.assertEqual(fand.select_level(levels), (7, "cpu"))

    def test_gpu_requests_higher_level(self):
        levels = fand.requested_levels({"cpu": 45, "gpu": 83}, {}, self.cfg)
        self.assertEqual(levels["cpu"], 0)
        self.assertEqual(fand.select_level(levels), (6, "gpu"))

    def test_ties_report_the_cpu(self):
        self.assertEqual(fand.select_level({"cpu": 3, "gpu": 3}), (3, "cpu"))

    def test_missing_gpu_leaves_cpu_in_charge(self):
        levels = fand.requested_levels({"cpu": 70, "gpu": None}, {}, self.cfg)
        self.assertEqual(set(levels), {"cpu"})
        self.assertEqual(fand.select_level(levels), (4, "cpu"))

    def test_gpu_only_still_drives_the_fan(self):
        levels = fand.requested_levels({"cpu": None, "gpu": 79}, {}, self.cfg)
        self.assertEqual(fand.select_level(levels), (5, "gpu"))

    def test_no_readings_at_all(self):
        self.assertEqual(fand.select_level({}), (None, None))

    def test_sources_use_their_own_curves(self):
        """65 C is level 3 on the CPU curve but only level 2 on the GPU curve."""
        levels = fand.requested_levels({"cpu": 65, "gpu": 65}, {}, self.cfg)
        self.assertEqual(levels["cpu"], 3)
        self.assertEqual(levels["gpu"], 2)

    def test_hysteresis_is_tracked_per_source(self):
        """The GPU stepping down must not drag the CPU down with it."""
        prev = {"cpu": 5, "gpu": 5}
        levels = fand.requested_levels({"cpu": 74, "gpu": 60}, prev, self.cfg)
        self.assertEqual(levels["cpu"], 5)        # held by CPU hysteresis
        self.assertEqual(levels["gpu"], 1)        # GPU free to fall


class CriticalTemperatureTest(unittest.TestCase):
    def test_gpu_critical_latches_and_releases(self):
        engaged = fand.critical_latch(91, 90, False)
        self.assertTrue(engaged)
        engaged = fand.critical_latch(88, 90, engaged)
        self.assertTrue(engaged)                  # inside the release margin
        engaged = fand.critical_latch(84, 90, engaged)
        self.assertFalse(engaged)

    def test_unknown_temperature_keeps_the_latch(self):
        self.assertTrue(fand.critical_latch(None, 90, True))
        self.assertFalse(fand.critical_latch(None, 90, False))

    def test_cpu_critical_behaviour_unchanged(self):
        self.assertTrue(fand.critical_latch(87, 87, False))
        self.assertFalse(fand.critical_latch(82, 87, True))

    def test_gpu_critical_is_clamped_to_the_hard_cap(self):
        cfg = fand.validate_config({"gpu_critical_temp": 250})
        self.assertEqual(cfg["gpu_critical_temp"], fand.GPU_ABSOLUTE_CRITICAL)
        cfg = fand.validate_config({"gpu_critical_temp": "nonsense"})
        self.assertEqual(cfg["gpu_critical_temp"], 90)

    def test_critical_gpu_requests_maximum_cooling(self):
        """A GPU over its limit forces full-speed the way a hot CPU does."""
        cfg = fand.validate_config({})
        cpu_force = fand.critical_latch(45, cfg["critical_temp"], False)
        gpu_force = fand.critical_latch(95, cfg["gpu_critical_temp"], False)
        self.assertFalse(cpu_force)
        self.assertTrue(gpu_force)
        command = "full-speed" if (cpu_force or gpu_force) else "0"
        self.assertEqual(command, "full-speed")


class ConfigCompatibilityTest(unittest.TestCase):
    LEGACY = {
        "mode": "auto",
        "manual_level": "auto",
        "curve": [[0, 0], [50, 1], [57, 2], [63, 3], [69, 4], [75, 5], [80, 6], [84, 7]],
        "hysteresis": 4,
        "critical_temp": 87,
        "poll_interval": 2,
        "smoothing_seconds": 12,
        "rekick_seconds": 5,
    }

    def test_pre_gpu_config_still_works(self):
        cfg = fand.validate_config(self.LEGACY)
        for key, value in self.LEGACY.items():
            self.assertEqual(cfg[key], value, key)

    def test_pre_gpu_config_gains_gpu_defaults(self):
        cfg = fand.validate_config(self.LEGACY)
        self.assertEqual(cfg["gpu_monitoring"], "auto")
        self.assertEqual(cfg["gpu_critical_temp"], 90)
        self.assertEqual(cfg["gpu_curve"], fand.DEFAULT_CONFIG["gpu_curve"])

    def test_shipped_default_config_validates_unchanged(self):
        with open(os.path.join(ROOT, "data", "config.json")) as f:
            shipped = json.load(f)
        cfg = fand.validate_config(shipped)
        for key, value in shipped.items():
            self.assertEqual(cfg[key], value, key)

    def test_gpu_monitoring_is_validated(self):
        self.assertEqual(fand.validate_config({"gpu_monitoring": "off"})["gpu_monitoring"], "off")
        self.assertEqual(fand.validate_config({"gpu_monitoring": "on"})["gpu_monitoring"], "on")
        self.assertEqual(fand.validate_config({"gpu_monitoring": 42})["gpu_monitoring"], "auto")

    def test_gpu_curve_is_clamped_and_sorted(self):
        cfg = fand.validate_config({"gpu_curve": [[300, 99], [70, 3], [70, 4], "junk", None]})
        self.assertEqual(cfg["gpu_curve"], [[0, 0], [70, 3], [100, 7]])

    def test_gpu_curve_levels_still_apply(self):
        cfg = fand.validate_config({"gpu_curve": [[0, 0], [50, 4]]})
        self.assertEqual(fand.curve_target_level(55, 0, cfg["gpu_curve"], 4), 4)


class SmootherTest(unittest.TestCase):
    def test_averages_over_the_window(self):
        s = fand.Smoother()
        self.assertEqual(s.update(50, 4), 50)
        s.update(60, 4)
        self.assertEqual(s.update(70, 4), 60)

    def test_missing_readings_drain_the_window(self):
        """A source that vanishes must fade out, never pin the fan on stale data."""
        s = fand.Smoother()
        for _ in range(4):
            s.update(80, 4)
        for _ in range(4):
            value = s.update(None, 4)
        self.assertIsNone(value)

    def test_a_single_dropped_reading_barely_matters(self):
        s = fand.Smoother()
        for _ in range(4):
            s.update(60, 4)
        self.assertEqual(s.update(None, 4), 60)

    def test_window_can_change_at_runtime(self):
        s = fand.Smoother()
        for t in (40, 50, 60, 70):
            s.update(t, 4)
        self.assertEqual(s.update(80, 2), 75)


class ControllerCompositionTest(unittest.TestCase):
    """The pieces main() wires together, driven the same way."""

    def setUp(self):
        self.cfg = fand.validate_config({})
        self.smoothers = {"cpu": fand.Smoother(), "gpu": fand.Smoother()}
        self.prev = {"cpu": 0, "gpu": 0}

    def step(self, cpu, gpu, window=1):
        temps = {"cpu": self.smoothers["cpu"].update(cpu, window),
                 "gpu": self.smoothers["gpu"].update(gpu, window)}
        requests = fand.requested_levels(temps, self.prev, self.cfg)
        self.prev.update(requests)
        return fand.select_level(requests)

    def test_hot_gpu_takes_over_from_a_cool_cpu(self):
        self.assertEqual(self.step(40, 40), (0, "cpu"))
        self.assertEqual(self.step(40, 83), (6, "gpu"))

    def test_gpu_going_away_hands_control_back_to_the_cpu(self):
        window = 3
        for _ in range(window):
            self.step(52, 83, window)
        self.assertEqual(self.step(52, 83, window)[1], "gpu")
        for _ in range(window):                   # dGPU suspends: no more readings
            level, source = self.step(52, None, window)
        self.assertEqual((level, source), (1, "cpu"))

    def test_winning_source_does_not_capture_the_other_s_hysteresis(self):
        """A hot GPU winning must not leave the CPU latched at the GPU's level."""
        self.step(45, 85)                         # GPU wins with a high level
        self.assertEqual(self.prev["gpu"], 7)
        self.assertEqual(self.prev["cpu"], 0)     # CPU stays where its own curve puts it
        self.assertEqual(self.step(45, 40), (0, "cpu"))   # GPU cools -> fan drops

    def test_no_readings_at_all_falls_back_to_firmware(self):
        for _ in range(3):
            level, _ = self.step(None, None)
        self.assertIsNone(level)                  # main() turns this into "level auto"


class SysfsReaderTest(unittest.TestCase):
    """The hwmon/sysfs readers, against a fake sysfs tree."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = self.tmp.name
        self.real = fand._hwmon_dirs_named
        fand._hwmon_dirs_named = lambda name: [self.dir] if name == "thinkpad" else []
        self.addCleanup(lambda: setattr(fand, "_hwmon_dirs_named", self.real))

    def write(self, name, content):
        with open(os.path.join(self.dir, name), "w") as f:
            f.write(content)

    def test_reads_both_fan_tachometers(self):
        self.write("fan1_input", "3962\n")
        self.write("fan2_input", "4155\n")
        self.assertEqual(fand.read_fan_rpms(), [3962, 4155])

    def test_unreadable_tachometer_is_skipped(self):
        self.write("fan1_input", "3000\n")
        self.write("fan2_input", "\n")            # P1 EC does this while idle
        self.assertEqual(fand.read_fan_rpms(), [3000])

    def test_ec_gpu_sensor(self):
        self.write("temp1_label", "CPU\n")
        self.write("temp1_input", "52000\n")
        self.write("temp2_label", "GPU\n")
        self.write("temp2_input", "61000\n")
        self.assertEqual(fand.read_ec_gpu_temp(), 61.0)

    def test_blank_ec_gpu_sensor_reads_as_unavailable(self):
        self.write("temp2_label", "GPU\n")
        self.write("temp2_input", "\n")           # exactly what a P1 Gen 7 reports
        self.assertIsNone(fand.read_ec_gpu_temp())

    def test_zeroed_ec_gpu_sensor_is_not_trusted(self):
        self.write("temp2_label", "GPU\n")
        self.write("temp2_input", "0\n")
        self.assertIsNone(fand.read_ec_gpu_temp())


class StatusFileTest(unittest.TestCase):
    def test_status_is_written_atomically_and_world_readable(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "sub", "status.json")
            fand.write_status({"level": "3", "source": "gpu"}, path)
            with open(path) as f:
                self.assertEqual(json.load(f)["source"], "gpu")
            self.assertEqual(os.stat(path).st_mode & 0o777, 0o644)
            self.assertFalse(os.path.exists(path + ".tmp"))

    def test_status_failure_is_never_fatal(self):
        fand.write_status({"level": "3"}, "/proc/definitely/not/writable/status.json")
        fand.write_status({"bad": object()}, "/tmp/thinkpad-fand-test-status.json")


class GuiTest(unittest.TestCase):
    """Pure logic in the GUI - no display needed."""

    @classmethod
    def setUpClass(cls):
        try:
            cls.gui = _load("thinkpad_fan_gui", "thinkpad-fan-gui")
        except ImportError as e:              # python3-tk not installed
            raise unittest.SkipTest(f"tkinter unavailable: {e}")

    def test_stale_status_is_ignored(self):
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
            json.dump({"updated": 0, "level": "3"}, f)
            path = f.name
        self.addCleanup(os.unlink, path)
        real, self.gui.STATUS_PATH = self.gui.STATUS_PATH, path
        try:
            self.assertIsNone(self.gui.read_status())
        finally:
            self.gui.STATUS_PATH = real

    def test_missing_status_file_is_fine(self):
        real, self.gui.STATUS_PATH = self.gui.STATUS_PATH, "/nonexistent/status.json"
        try:
            self.assertIsNone(self.gui.read_status())
        finally:
            self.gui.STATUS_PATH = real

    def test_driving_source_is_reported(self):
        text = self.gui.FanGUI._driver_text(
            {"source": "gpu", "cpu_level": 2, "gpu_level": 5, "mode": "auto"})
        self.assertIn("GPU", text)
        self.assertIn("5", text)

    def test_critical_override_is_reported(self):
        text = self.gui.FanGUI._driver_text(
            {"source": "gpu", "critical": True, "mode": "auto"})
        self.assertIn("critical", text.lower())

    def test_manual_mode_reports_no_sensor(self):
        text = self.gui.FanGUI._driver_text({"source": None, "mode": "manual"})
        self.assertIn("manual", text.lower())

    def test_curve_round_trip(self):
        temps = [60, 65, 70, 74, 78, 82, 85]
        curve = self.gui.FanGUI._temps_to_curve(temps)
        self.assertEqual(curve[0], [0, 0])
        self.assertEqual(fand.validate_config({"gpu_curve": curve})["gpu_curve"], curve)


if __name__ == "__main__":
    unittest.main()
