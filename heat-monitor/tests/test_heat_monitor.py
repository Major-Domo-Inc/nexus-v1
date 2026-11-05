import contextlib
import io
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import heat_monitor as hm  # noqa: E402


def write(root, rel, text):
    path = os.path.join(root, rel)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(text + "\n")


class LinuxSysfs(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = self.tmp.name

    def tearDown(self):
        self.tmp.cleanup()

    def test_hwmon_labels_limits_fans_and_junk(self):
        write(self.root, "class/hwmon/hwmon0/name", "coretemp")
        write(self.root, "class/hwmon/hwmon0/temp1_input", "61000")
        write(self.root, "class/hwmon/hwmon0/temp1_label", "Package id 0")
        write(self.root, "class/hwmon/hwmon0/temp1_max", "84000")
        write(self.root, "class/hwmon/hwmon0/temp1_crit", "100000")
        write(self.root, "class/hwmon/hwmon0/temp2_input", "-128000")  # disconnected
        write(self.root, "class/hwmon/hwmon0/temp10_input", "55500")  # sorts after temp2
        write(self.root, "class/hwmon/hwmon1/name", "nct6775")
        write(self.root, "class/hwmon/hwmon1/device/fan1_input", "1234")
        snap = hm.read_linux(self.root)
        self.assertEqual([(r.label, r.temp_c) for r in snap.temps],
                         [("Package id 0", 61.0), ("temp10", 55.5)])
        self.assertEqual((snap.temps[0].high_c, snap.temps[0].crit_c), (84.0, 100.0))
        self.assertEqual([(f.source, f.rpm) for f in snap.fans], [("nct6775", 1234)])

    def test_thermal_zone_fallback(self):
        write(self.root, "class/thermal/thermal_zone0/type", "x86_pkg_temp")
        write(self.root, "class/thermal/thermal_zone0/temp", "47000")
        write(self.root, "class/thermal/thermal_zone0/trip_point_0_type", "passive")
        write(self.root, "class/thermal/thermal_zone0/trip_point_0_temp", "90000")
        write(self.root, "class/thermal/thermal_zone0/trip_point_1_type", "critical")
        write(self.root, "class/thermal/thermal_zone0/trip_point_1_temp", "105000")
        [r] = hm.read_linux(self.root).temps
        self.assertEqual((r.source, r.temp_c, r.high_c, r.crit_c), ("x86_pkg_temp", 47.0, 90.0, 105.0))

    def test_once_json_and_exit_code_on_fake_tree(self):
        write(self.root, "class/hwmon/hwmon0/name", "k10temp")
        write(self.root, "class/hwmon/hwmon0/temp1_input", "91000")
        write(self.root, "class/hwmon/hwmon0/temp1_label", "Tctl")
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = hm.main(["--json", "--sysfs-root", self.root])
        data = json.loads(buf.getvalue())
        self.assertEqual(data["sensors"][0]["level"], "WARM")
        self.assertEqual(code, hm.WARM)

    def test_no_sensors_exit_code(self):
        if hm.platform.system() != "Linux":
            self.skipTest("Linux reader")
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(hm.main(["--once", "--no-color", "--sysfs-root", self.root]), 3)


class NoConsole(unittest.TestCase):
    def test_runs_with_no_stdout_like_pythonw(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        log = os.path.join(tmp.name, "temps.csv")
        old, sys.stdout = sys.stdout, None
        try:
            code = hm.main(["--demo", "--once", "--log", log])
        finally:
            sys.stdout = old
        self.assertEqual(code, hm.OK)
        with open(log) as f:
            rows = f.read().splitlines()
        self.assertEqual(rows[0], "timestamp,source,sensor,temp_c,level")
        self.assertEqual(len(rows), 1 + 7)  # header + 7 demo sensors


class Parsers(unittest.TestCase):
    def test_powermetrics(self):
        temps, pressure = hm.parse_powermetrics(
            "**** SMC sensors ****\nCPU die temperature: 52.31 C\nGPU die temperature: 44.00 C\n"
            "**** Thermal pressure ****\nCurrent pressure level: Nominal\n")
        self.assertEqual([(r.label, r.temp_c) for r in temps], [("CPU die", 52.31), ("GPU die", 44.0)])
        self.assertEqual(pressure, "Nominal")

    def test_acpi_decikelvin(self):
        [r] = hm.parse_acpi(json.dumps([{"InstanceName": "ACPI\\ThermalZone\\TZ00_0",
                                         "CurrentTemperature": 3232, "CriticalTripPoint": 3732}]))
        self.assertAlmostEqual(r.temp_c, 50.05, places=2)
        self.assertAlmostEqual(r.crit_c, 100.05, places=2)
        self.assertEqual(r.label, "TZ00_0")

    def test_lhm(self):
        snap = hm.parse_lhm(json.dumps([
            {"Name": "CPU Package", "SensorType": "Temperature", "Value": 66.0, "Parent": "/intelcpu/0"},
            {"Name": "Fan #1", "SensorType": "Fan", "Value": 1100.4, "Parent": "/lpc/nct6798d"},
            {"Name": "Broken", "SensorType": "Temperature", "Value": None, "Parent": "/x"},
        ]))
        self.assertEqual([(r.source, r.label, r.temp_c) for r in snap.temps], [("intelcpu 0", "CPU Package", 66.0)])
        self.assertEqual(snap.fans[0].rpm, 1100)


class Levels(unittest.TestCase):
    def test_threshold_precedence(self):
        r = hm.Reading("x", "y", 85, high_c=84, crit_c=100)
        self.assertEqual(hm.thresholds(r, None, None), (84, 100))
        self.assertEqual(hm.thresholds(r, 70, 80), (70, 80))
        self.assertEqual(hm.thresholds(hm.Reading("x", "y", 50), None, None), (80, 95))
        self.assertEqual(hm.thresholds(hm.Reading("x", "y", 50, high_c=100, crit_c=100), None, None), (90, 100))

    def test_tracker_events_on_level_change_only(self):
        tr = hm.Tracker(warn=70, crit=90)
        seq = [60, 75, 76, 95, 60]
        events = [tr.update(hm.Snapshot([hm.Reading("cpu", "pkg", t)]), now=i) for i, t in enumerate(seq)]
        self.assertEqual([[e.level for e in ev] for ev in events], [[], [hm.WARM], [], [hm.HOT], [hm.OK]])
        self.assertEqual((tr.min["cpu/pkg"], tr.max["cpu/pkg"]), (60, 95))

    def test_hysteresis(self):
        tr = hm.Tracker(warn=70, crit=90)
        levels = []
        for t in (91, 89, 88.5, 87.9, 69, 67.9):
            tr.update(hm.Snapshot([hm.Reading("cpu", "pkg", t)]))
            levels.append(tr.levels["cpu/pkg"])
        self.assertEqual(levels, [hm.HOT, hm.HOT, hm.HOT, hm.WARM, hm.WARM, hm.OK])

    def test_duplicate_labels_kept_apart(self):
        temps = [hm.Reading("nvme", "Composite", 40), hm.Reading("nvme", "Composite", 45)]
        hm._dedupe_labels(temps)
        self.assertEqual([r.label for r in temps], ["Composite", "Composite #2"])

    def test_render_smoke(self):
        read = hm.make_demo_reader()
        tr = hm.Tracker()
        for _ in range(3):
            snap = read()
            tr.update(snap)
        for ascii_only in (False, True):
            out = hm.render(snap, tr, hm.Style(False, ascii_only, True), 2, 120, 20)
            self.assertIn("coretemp/Package id 0", out)
            self.assertIn("°F", out)


if __name__ == "__main__":
    unittest.main()
