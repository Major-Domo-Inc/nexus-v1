#!/usr/bin/env python3
"""
heat-monitor: a live temperature monitor for your computer.

Reads the machine's hardware temperature sensors and shows them in the
terminal with colour-coded bars, a short history sparkline, and the
min/max seen since start. Alerts when a sensor runs warm or hot.

Sources, by platform (no Python packages required):
  Linux    /sys/class/hwmon (CPU, NVMe, chipset, GPU via amdgpu/nouveau, fans),
           falling back to /sys/class/thermal zones
  macOS    smctemp, osx-cpu-temp or istats if installed; with sudo, powermetrics
  Windows  LibreHardwareMonitor / OpenHardwareMonitor (if running), else ACPI
           thermal zones (needs an Administrator terminal)
  Any OS   NVIDIA GPUs through nvidia-smi; psutil if it is installed

Run `python3 heat_monitor.py --help` for options.
"""
from __future__ import annotations

import argparse
import collections
import csv
import glob
import json
import math
import os
import platform
import random
import re
import shutil
import socket
import subprocess
import sys
import time
from dataclasses import dataclass, field
from typing import Callable, Deque, Dict, List, Optional, Tuple

DEFAULT_WARN_C = 80.0
DEFAULT_CRIT_C = 95.0
HYSTERESIS_C = 2.0  # must cool this far below a limit before the level drops
PLAUSIBLE_C = (1.0, 127.0)  # outside this a sensor is disconnected or reporting junk

OK, WARM, HOT = 0, 1, 2
LEVEL_NAMES = {OK: "OK", WARM: "WARM", HOT: "HOT"}


# --------------------------------------------------------------------------- data

@dataclass
class Reading:
    source: str  # chip or device, e.g. "coretemp", "nvme", "GPU0"
    label: str  # sensor on it, e.g. "Package id 0"
    temp_c: float
    high_c: Optional[float] = None  # sensor-reported "high"/"max" threshold
    crit_c: Optional[float] = None  # sensor-reported critical threshold

    @property
    def key(self) -> str:
        return f"{self.source}/{self.label}"


@dataclass
class Fan:
    source: str
    label: str
    rpm: int


@dataclass
class Snapshot:
    temps: List[Reading] = field(default_factory=list)
    fans: List[Fan] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)


# ----------------------------------------------------------------------- helpers

def _plausible(t: float) -> bool:
    return PLAUSIBLE_C[0] <= t < PLAUSIBLE_C[1]


def _read(path: str) -> Optional[str]:
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return f.read().strip()
    except OSError:
        return None


def _read_int(path: str) -> Optional[int]:
    s = _read(path)
    if s is None:
        return None
    try:
        return int(s)
    except ValueError:
        return None


def _milli_c(path: str) -> Optional[float]:
    v = _read_int(path)
    if v is None:
        return None
    t = v / 1000.0
    return t if _plausible(t) else None


def _natural(s: str) -> list:
    return [int(p) if p.isdigit() else p for p in re.split(r"(\d+)", s)]


def _first_float(text: Optional[str]) -> Optional[float]:
    if not text:
        return None
    m = re.search(r"-?\d+(?:\.\d+)?", text)
    return float(m.group()) if m else None


def _run(cmd: List[str], timeout: float = 10) -> Optional[str]:
    try:
        # On Windows, don't flash a console window for every PowerShell call.
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, creationflags=flags)
    except (OSError, subprocess.SubprocessError):
        return None
    return r.stdout if r.returncode == 0 else None


def _dedupe_labels(temps: List[Reading]) -> None:
    """Give repeated source/label pairs a #2, #3 suffix so history stays separate."""
    seen: Dict[str, int] = {}
    for r in temps:
        n = seen.get(r.key, 0) + 1
        seen[r.key] = n
        if n > 1:
            r.label = f"{r.label} #{n}"


# ----------------------------------------------------------------- Linux sources

def read_linux(root: str = "/sys") -> Snapshot:
    snap = Snapshot()
    seen_files = set()
    for hw in sorted(glob.glob(os.path.join(root, "class/hwmon/hwmon*")), key=_natural):
        name = _read(os.path.join(hw, "name")) or os.path.basename(hw)
        # Older kernels put the sensor files under device/ instead.
        for d in (hw, os.path.join(hw, "device")):
            for inp in sorted(glob.glob(os.path.join(d, "temp*_input")), key=_natural):
                real = os.path.realpath(inp)
                if real in seen_files:
                    continue
                seen_files.add(real)
                raw = _read_int(inp)
                if raw is None or not _plausible(raw / 1000.0):
                    continue
                base = inp[: -len("_input")]
                label = _read(base + "_label") or os.path.basename(base)
                snap.temps.append(Reading(name, label, raw / 1000.0,
                                          _milli_c(base + "_max"), _milli_c(base + "_crit")))
            for inp in sorted(glob.glob(os.path.join(d, "fan*_input")), key=_natural):
                real = os.path.realpath(inp)
                if real in seen_files:
                    continue
                seen_files.add(real)
                rpm = _read_int(inp)
                if rpm is None:
                    continue
                base = inp[: -len("_input")]
                snap.fans.append(Fan(name, _read(base + "_label") or os.path.basename(base), rpm))

    if not snap.temps:
        snap.temps.extend(_read_thermal_zones(root))
    return snap


def _read_thermal_zones(root: str) -> List[Reading]:
    out = []
    for tz in sorted(glob.glob(os.path.join(root, "class/thermal/thermal_zone*")), key=_natural):
        raw = _read_int(os.path.join(tz, "temp"))
        if raw is None or not _plausible(raw / 1000.0):
            continue
        high = crit = None
        for tp in sorted(glob.glob(os.path.join(tz, "trip_point_*_type")), key=_natural):
            kind = _read(tp)
            t = _milli_c(tp[: -len("_type")] + "_temp")
            if t is None:
                continue
            if kind == "critical":
                crit = t if crit is None else min(crit, t)
            elif kind in ("hot", "passive"):
                high = t if high is None else min(high, t)
        kind_name = _read(os.path.join(tz, "type")) or "thermal"
        out.append(Reading(kind_name, os.path.basename(tz), raw / 1000.0, high, crit))
    return out


# ----------------------------------------------------------------- macOS sources

def parse_powermetrics(text: str) -> Tuple[List[Reading], Optional[str]]:
    temps = [Reading("smc", f"{m.group(1)} die", float(m.group(2)))
             for m in re.finditer(r"^(CPU|GPU) die temperature:\s*([\d.]+)\s*C", text, re.M)]
    m = re.search(r"Current pressure level:\s*(\w+)", text)
    return temps, (m.group(1) if m else None)


def read_macos() -> Snapshot:
    snap = Snapshot()
    if shutil.which("smctemp"):  # works on Apple Silicon and Intel
        for flag, label in (("-c", "CPU"), ("-g", "GPU")):
            t = _first_float(_run(["smctemp", flag]))
            if t is not None and _plausible(t):
                snap.temps.append(Reading("smc", label, t))
    if not snap.temps and shutil.which("osx-cpu-temp"):  # Intel Macs
        t = _first_float(_run(["osx-cpu-temp"]))
        if t is not None and _plausible(t):
            snap.temps.append(Reading("smc", "CPU", t))
    if not snap.temps and shutil.which("istats"):  # Intel Macs
        t = _first_float(_run(["istats", "cpu", "temp", "--value-only"]))
        if t is not None and _plausible(t):
            snap.temps.append(Reading("smc", "CPU", t))

    if hasattr(os, "geteuid") and os.geteuid() == 0:
        out = _run(["powermetrics", "--samplers", "smc,thermal", "-n", "1", "-i", "200"], 15) \
            or _run(["powermetrics", "--samplers", "thermal", "-n", "1", "-i", "200"], 15)
        if out:
            temps, pressure = parse_powermetrics(out)
            if not snap.temps:
                snap.temps.extend(temps)
            if pressure:
                snap.notes.append(f"macOS thermal pressure: {pressure}")
    else:
        out = _run(["pmset", "-g", "therm"]) or ""
        m = re.search(r"CPU_Speed_Limit\s*=\s*(\d+)", out)
        if m and int(m.group(1)) < 100:
            snap.notes.append(f"macOS is limiting CPU speed to {m.group(1)}% because of heat")

    if not snap.temps:
        snap.notes.append(
            "No temperature reader found. Install one with Homebrew: "
            "brew tap narugit/tap && brew install narugit/tap/smctemp  "
            "(or run with sudo to see thermal pressure)")
    return snap


# --------------------------------------------------------------- Windows sources

_PS_LHM = (
    "foreach($ns in 'root/LibreHardwareMonitor','root/OpenHardwareMonitor'){"
    "try{$s=Get-CimInstance -Namespace $ns -ClassName Sensor -ErrorAction Stop|"
    "Where-Object {$_.SensorType -eq 'Temperature' -or $_.SensorType -eq 'Fan'}|"
    "Select-Object Name,SensorType,Value,Parent;"
    "if($s){ConvertTo-Json -InputObject @($s) -Compress;exit 0}}catch{}}"
)
_PS_ACPI = (
    "$z=Get-CimInstance -Namespace root/wmi -ClassName MSAcpi_ThermalZoneTemperature -ErrorAction Stop|"
    "Select-Object InstanceName,CurrentTemperature,CriticalTripPoint;"
    "ConvertTo-Json -InputObject @($z) -Compress"
)


def decikelvin_to_c(v: float) -> float:
    return v / 10.0 - 273.15


def parse_lhm(text: str) -> Snapshot:
    snap = Snapshot()
    for s in json.loads(text or "[]"):
        try:
            value = float(s.get("Value"))
        except (TypeError, ValueError):
            continue
        parent = (s.get("Parent") or "").strip("/").replace("/", " ") or "sensor"
        name = s.get("Name") or "?"
        if s.get("SensorType") == "Fan":
            snap.fans.append(Fan(parent, name, int(value)))
        elif _plausible(value):
            snap.temps.append(Reading(parent, name, value))
    return snap


def parse_acpi(text: str) -> List[Reading]:
    out = []
    for z in json.loads(text or "[]"):
        try:
            t = decikelvin_to_c(float(z["CurrentTemperature"]))
        except (KeyError, TypeError, ValueError):
            continue
        if not _plausible(t):
            continue
        crit = z.get("CriticalTripPoint")
        crit_c = decikelvin_to_c(float(crit)) if crit else None
        label = (z.get("InstanceName") or "zone").split("\\")[-1]
        out.append(Reading("acpi", label, t, None, crit_c if crit_c and _plausible(crit_c) else None))
    return out


def read_windows() -> Snapshot:
    ps = shutil.which("powershell") or shutil.which("pwsh")
    if not ps:
        return Snapshot(notes=["PowerShell not found, so no sensors can be read."])
    base = [ps, "-NoProfile", "-NonInteractive", "-Command"]
    try:
        out = _run(base + [_PS_LHM], 15)
        if out and out.strip():
            snap = parse_lhm(out)
            if snap.temps:
                return snap
        out = _run(base + [_PS_ACPI], 15)
        temps = parse_acpi(out) if out and out.strip() else []
    except ValueError:  # malformed JSON
        temps = []
    snap = Snapshot(temps=temps)
    if not temps:
        snap.notes.append(
            "No sensors found. Start LibreHardwareMonitor (free, open source) so its sensors are "
            "published to Windows, or run this terminal as Administrator for basic ACPI zones.")
    else:
        snap.notes.append("Showing ACPI zones only; run LibreHardwareMonitor for per-core, GPU and disk sensors.")
    return snap


# ------------------------------------------------------------- portable sources

def read_nvidia() -> List[Reading]:
    exe = shutil.which("nvidia-smi")
    if not exe:
        return []
    out = _run([exe, "--query-gpu=index,name,temperature.gpu", "--format=csv,noheader,nounits"])
    temps = []
    for line in (out or "").splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) < 3:
            continue
        try:
            t = float(parts[2])
        except ValueError:
            continue
        if _plausible(t):
            temps.append(Reading(f"GPU{parts[0]}", parts[1], t))
    return temps


def read_psutil() -> Snapshot:
    snap = Snapshot()
    try:
        import psutil  # type: ignore
    except ImportError:
        return snap
    for name, entries in (getattr(psutil, "sensors_temperatures", lambda: {})() or {}).items():
        for i, e in enumerate(entries):
            if e.current is not None and _plausible(e.current):
                snap.temps.append(Reading(name, e.label or f"temp{i + 1}", e.current,
                                          e.high or None, e.critical or None))
    for name, entries in (getattr(psutil, "sensors_fans", lambda: {})() or {}).items():
        for i, e in enumerate(entries):
            snap.fans.append(Fan(name, e.label or f"fan{i + 1}", int(e.current)))
    return snap


def make_demo_reader() -> Callable[[], Snapshot]:
    """Synthetic sensors, so the display can be tried on any machine."""
    start = time.monotonic()
    rng = random.Random(7)
    drift = {"core": 0.0, "gpu": 0.0}

    def read() -> Snapshot:
        t = time.monotonic() - start
        for k in drift:
            drift[k] = max(-6, min(6, drift[k] + rng.uniform(-1, 1)))
        load = 0.5 + 0.5 * math.sin(t / 8)
        pkg = 48 + 40 * load + drift["core"]
        temps = [Reading("coretemp", "Package id 0", pkg, 84, 100)]
        temps += [Reading("coretemp", f"Core {i}", pkg - 3 + rng.uniform(-2, 2), 84, 100) for i in range(4)]
        temps += [Reading("GPU0", "Demo GPU", 42 + 30 * load + drift["gpu"]),
                  Reading("nvme", "Composite", 38 + 6 * load, 82, 85)]
        fans = [Fan("demo", "CPU fan", int(900 + 1600 * load))]
        return Snapshot(temps, fans, ["Demo mode: these readings are simulated."])

    return read


def make_reader(demo: bool = False, sysfs_root: str = "/sys") -> Callable[[], Snapshot]:
    if demo:
        return make_demo_reader()
    system = platform.system()

    def read() -> Snapshot:
        if system == "Linux":
            snap = read_linux(sysfs_root)
        elif system == "Darwin":
            snap = read_macos()
        elif system == "Windows":
            snap = read_windows()
        else:
            snap = read_psutil()
        if not any(r.source in ("nouveau", "nvidia") for r in snap.temps):
            gpus = read_nvidia()
            snap.temps.extend(gpus)
        if not snap.temps and system == "Linux":
            snap.notes.append(
                "No sensors found under /sys/class/hwmon or /sys/class/thermal. On a physical machine, "
                "install lm-sensors and run `sudo sensors-detect`; virtual machines and containers "
                "usually expose no temperature sensors at all.")
        _dedupe_labels(snap.temps)
        return snap

    return read


# ---------------------------------------------------------------- thresholds

def thresholds(r: Reading, warn: Optional[float], crit: Optional[float]) -> Tuple[float, float]:
    """(warm, hot) limits in °C. Command-line values win, then the sensor's own, then defaults."""
    c = crit if crit is not None else (r.crit_c or DEFAULT_CRIT_C)
    w = warn if warn is not None else (r.high_c or DEFAULT_WARN_C)
    if w >= c:
        w = c - 10
    return w, c


def level_of(r: Reading, warn: Optional[float], crit: Optional[float]) -> int:
    w, c = thresholds(r, warn, crit)
    return HOT if r.temp_c >= c else WARM if r.temp_c >= w else OK


# ------------------------------------------------------------------- tracking

@dataclass
class Event:
    when: float
    key: str
    level: int
    temp_c: float


class Tracker:
    """Per-sensor history, min/max and level-change events."""

    def __init__(self, history: int = 30, warn: Optional[float] = None, crit: Optional[float] = None):
        self.warn, self.crit = warn, crit
        self.history: Dict[str, Deque[float]] = collections.defaultdict(lambda: collections.deque(maxlen=history))
        self.min: Dict[str, float] = {}
        self.max: Dict[str, float] = {}
        self.levels: Dict[str, int] = {}
        self.events: Deque[Event] = collections.deque(maxlen=6)

    def update(self, snap: Snapshot, now: Optional[float] = None) -> List[Event]:
        now = time.time() if now is None else now
        new = []
        for r in snap.temps:
            k = r.key
            self.history[k].append(r.temp_c)
            self.min[k] = min(self.min.get(k, r.temp_c), r.temp_c)
            self.max[k] = max(self.max.get(k, r.temp_c), r.temp_c)
            lvl = level_of(r, self.warn, self.crit)
            prev = self.levels.get(k, OK)
            if lvl < prev:  # don't flap around a limit
                w, c = thresholds(r, self.warn, self.crit)
                if r.temp_c > (c if prev == HOT else w) - HYSTERESIS_C:
                    lvl = prev
            if lvl != prev:
                ev = Event(now, k, lvl, r.temp_c)
                self.events.append(ev)
                new.append(ev)
            self.levels[k] = lvl
        return new


# ------------------------------------------------------------------ rendering

class Style:
    def __init__(self, color: bool, ascii_only: bool, fahrenheit: bool):
        self.color, self.ascii, self.fahrenheit = color, ascii_only, fahrenheit

    def c(self, code: str, s: str) -> str:
        return f"\033[{code}m{s}\033[0m" if self.color else s

    def level(self, lvl: int, s: str) -> str:
        return self.c({OK: "32", WARM: "33", HOT: "1;31"}[lvl], s)

    def temp(self, t_c: float) -> str:
        return f"{t_c * 9 / 5 + 32:5.1f}°F" if self.fahrenheit else f"{t_c:5.1f}°C"

    def bar(self, t: float, w: float, c: float, width: int, lvl: int) -> str:
        lo, hi = 20.0, max(c + 5, 100.0)
        filled = int(round(max(0.0, min(1.0, (t - lo) / (hi - lo))) * width))
        full, empty = ("#", "-") if self.ascii else ("█", "░")
        return self.level(lvl, full * filled) + self.c("2", empty * (width - filled))

    def spark(self, values) -> str:
        ticks = " .:-=+*#" if self.ascii else "▁▂▃▄▅▆▇█"
        vals = list(values)
        if not vals:
            return ""
        lo, hi = min(vals), max(vals)
        span = max(hi - lo, 5.0)  # don't magnify sensor noise into a mountain range
        return "".join(ticks[min(len(ticks) - 1, int((v - lo) / span * (len(ticks) - 1) + 0.5))] for v in vals)


def _load_text() -> str:
    if hasattr(os, "getloadavg"):
        try:
            return "load " + " ".join(f"{x:.2f}" for x in os.getloadavg())
        except OSError:
            pass
    try:
        import psutil  # type: ignore
        return f"cpu {psutil.cpu_percent():.0f}%"
    except ImportError:
        return ""


def render(snap: Snapshot, tr: Tracker, st: Style, interval: float, width: int, spark_len: int) -> str:
    lines = []
    host = socket.gethostname()
    title = st.c("1;36", " HEAT MONITOR ")
    lines.append(f"{title} {host} · {platform.system()} {platform.release()} · {time.strftime('%H:%M:%S')}"
                 f"   {st.c('2', f'every {interval:g}s · Ctrl+C to quit')}")

    if snap.temps:
        hottest = max(snap.temps, key=lambda r: r.temp_c)
        worst = max(tr.levels.get(r.key, OK) for r in snap.temps)
        status = {OK: "all normal", WARM: "running warm", HOT: "TOO HOT"}[worst]
        lines.append(f" Hottest: {hottest.source} {hottest.label} {st.level(worst, st.temp(hottest.temp_c))}"
                     f"  {st.level(worst, status)}   {st.c('2', _load_text())}")
    lines.append("")

    label_w = min(28, max([len(r.key) for r in snap.temps] + [6]))
    show_spark = spark_len > 0 and width >= label_w + 70
    bar_w = max(10, min(40, width - label_w - 34 - (spark_len + 2 if show_spark else 0)))
    if snap.temps:
        hdr = f" {'SENSOR':<{label_w}} {'NOW':>7} {'':<{bar_w}} {'MIN':>7} {'MAX':>7}"
        if show_spark:
            hdr += "  HISTORY"
        lines.append(st.c("2", hdr))
    for r in snap.temps:
        k = r.key
        w, c = thresholds(r, tr.warn, tr.crit)
        lvl = tr.levels.get(k, OK)
        name = k if len(k) <= label_w else k[: label_w - 1] + "…"
        row = (f" {name:<{label_w}} {st.level(lvl, st.temp(r.temp_c))} {st.bar(r.temp_c, w, c, bar_w, lvl)}"
               f" {st.c('2', st.temp(tr.min[k]))} {st.c('2', st.temp(tr.max[k]))}")
        if show_spark:
            row += "  " + st.level(lvl, st.spark(tr.history[k]))
        lines.append(row)

    if snap.fans:
        lines.append("")
        lines.append(" Fans: " + "   ".join(f"{f.label} {f.rpm} rpm" for f in snap.fans))
    if tr.events:
        lines.append("")
        lines.append(st.c("2", " Recent changes"))
        for e in list(tr.events)[-5:]:
            lines.append(f"   {time.strftime('%H:%M:%S', time.localtime(e.when))}  "
                         f"{st.level(e.level, f'{LEVEL_NAMES[e.level]:<4}')}  {e.key} at {st.temp(e.temp_c).strip()}")
    for n in snap.notes:
        lines.append("")
        lines.append(st.c("33", " ! " + n))
    lines.append("")
    lines.append(st.c("2", f" Limits: sensor's own when reported, else warm {st.temp(tr.warn or DEFAULT_WARN_C).strip()}"
                           f" / hot {st.temp(tr.crit or DEFAULT_CRIT_C).strip()}"))
    return "\n".join(lines)


# -------------------------------------------------------------- side effects

def desktop_notify(title: str, msg: str) -> None:
    system = platform.system()
    try:
        if system == "Linux" and shutil.which("notify-send"):
            subprocess.Popen(["notify-send", "-u", "critical", title, msg])
        elif system == "Darwin":
            script = f'display notification "{msg}" with title "{title}"'
            subprocess.Popen(["osascript", "-e", script])
    except OSError:
        pass


class CsvLog:
    def __init__(self, path: str):
        new = not os.path.exists(path) or os.path.getsize(path) == 0
        self.f = open(path, "a", newline="", encoding="utf-8")
        self.w = csv.writer(self.f)
        if new:
            self.w.writerow(["timestamp", "source", "sensor", "temp_c", "level"])

    def write(self, snap: Snapshot, tr: Tracker) -> None:
        ts = time.strftime("%Y-%m-%dT%H:%M:%S")
        for r in snap.temps:
            self.w.writerow([ts, r.source, r.label, f"{r.temp_c:.1f}", LEVEL_NAMES[tr.levels.get(r.key, OK)]])
        self.f.flush()

    def close(self) -> None:
        self.f.close()


# --------------------------------------------------------------------- modes

def filtered(snap: Snapshot, pattern: Optional[re.Pattern]) -> Snapshot:
    if pattern:
        snap.temps = [r for r in snap.temps if pattern.search(r.key)]
    return snap


def run_once(args, read, pattern) -> int:
    snap = filtered(read(), pattern)
    tr = Tracker(warn=args.warn, crit=args.crit)
    tr.update(snap)
    if args.log:
        log = CsvLog(args.log)
        log.write(snap, tr)
        log.close()
    if args.json:
        print(json.dumps({
            "host": socket.gethostname(),
            "time": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "sensors": [{"source": r.source, "sensor": r.label, "temp_c": round(r.temp_c, 1),
                         "warm_c": thresholds(r, args.warn, args.crit)[0],
                         "hot_c": thresholds(r, args.warn, args.crit)[1],
                         "level": LEVEL_NAMES[tr.levels[r.key]]} for r in snap.temps],
            "fans": [{"source": f.source, "fan": f.label, "rpm": f.rpm} for f in snap.fans],
            "notes": snap.notes,
        }, indent=2))
    else:
        st = Style(color=args.color, ascii_only=args.ascii, fahrenheit=args.fahrenheit)
        print(render(snap, tr, st, args.interval, shutil.get_terminal_size((100, 24)).columns, 0))
    if not snap.temps:
        return 3
    return max(tr.levels.values())


def run_live(args, read, pattern) -> int:
    st = Style(color=args.color, ascii_only=args.ascii, fahrenheit=args.fahrenheit)
    spark_len = 20
    tr = Tracker(history=spark_len, warn=args.warn, crit=args.crit)
    log = CsvLog(args.log) if args.log else None
    out = sys.stdout
    out.write("\033[?25l")  # hide cursor
    try:
        while True:
            started = time.monotonic()
            snap = filtered(read(), pattern)
            for ev in tr.update(snap):
                if ev.level == HOT:
                    if not args.quiet:
                        out.write("\a")
                    if args.notify:
                        desktop_notify("Computer running hot", f"{ev.key} is at {st.temp(ev.temp_c).strip()}")
            if log:
                log.write(snap, tr)
            frame = render(snap, tr, st, args.interval, shutil.get_terminal_size((100, 24)).columns, spark_len)
            out.write("\033[H" + frame.replace("\n", "\033[K\n") + "\033[K\033[J")
            out.flush()
            time.sleep(max(0.0, args.interval - (time.monotonic() - started)))
    except KeyboardInterrupt:
        pass
    finally:
        out.write("\033[?25h\n")
        if log:
            log.close()
    if tr.max:
        peak = max(tr.max, key=tr.max.get)
        print(f"Session peak: {peak} at {st.temp(tr.max[peak]).strip()}")
    return 0


def _is_tty() -> bool:
    # sys.stdout is None under pythonw/pyw (Windows background runs).
    return sys.stdout is not None and sys.stdout.isatty()


def parse_args(argv=None):
    p = argparse.ArgumentParser(
        prog="heat_monitor",
        description="Live temperature monitor for this computer.",
        epilog="Exit status with --once/--json: 0 normal, 1 warm, 2 hot, 3 no sensors found.")
    p.add_argument("-i", "--interval", type=float, default=2.0, help="seconds between refreshes (default 2)")
    p.add_argument("--warn", type=float, help="°C at which a sensor counts as warm (overrides the sensor's own)")
    p.add_argument("--crit", type=float, help="°C at which a sensor counts as hot (overrides the sensor's own)")
    p.add_argument("-f", "--fahrenheit", action="store_true", help="show °F")
    p.add_argument("--filter", metavar="REGEX", help="only show sensors whose source/name matches")
    p.add_argument("--log", metavar="FILE.csv", help="append every reading to a CSV file")
    p.add_argument("--notify", action="store_true", help="desktop notification when a sensor turns hot")
    p.add_argument("-q", "--quiet", action="store_true", help="no terminal bell on alerts")
    p.add_argument("--once", action="store_true", help="print one reading and exit")
    p.add_argument("--json", action="store_true", help="print one reading as JSON and exit")
    p.add_argument("--no-color", action="store_true", help="plain output")
    p.add_argument("--ascii", action="store_true", help="ASCII bars, for terminals without Unicode")
    p.add_argument("--demo", action="store_true", help="simulated sensors, to try the display anywhere")
    p.add_argument("--sysfs-root", default="/sys", help=argparse.SUPPRESS)  # for tests
    args = p.parse_args(argv)
    if args.interval <= 0:
        p.error("--interval must be positive")
    args.color = not args.no_color and _is_tty() and "NO_COLOR" not in os.environ
    return args


def main(argv=None) -> int:
    args = parse_args(argv)
    if platform.system() == "Windows":
        os.system("")  # turns on ANSI escape handling in the Windows console
    try:
        sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    except (AttributeError, ValueError):
        pass
    try:
        pattern = re.compile(args.filter, re.I) if args.filter else None
    except re.error as e:
        print(f"Bad --filter pattern: {e}", file=sys.stderr)
        return 64
    read = make_reader(args.demo, args.sysfs_root)
    if args.once or args.json:
        return run_once(args, read, pattern)
    if not _is_tty():
        print("Not a terminal; use --once or --json for a single reading.", file=sys.stderr)
        return 64
    return run_live(args, read, pattern)


if __name__ == "__main__":
    sys.exit(main())
