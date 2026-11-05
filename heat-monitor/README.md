# Heat Monitor

A live temperature monitor for your computer, in the terminal. One Python file, no packages to install.

```
 HEAT MONITOR  my-laptop · Linux 6.8.0 · 15:20:10   every 2s · Ctrl+C to quit
 Hottest: coretemp Package id 0  85.0°C  TOO HOT   load 3.12 2.40 1.95

 SENSOR                    NOW                                       MIN     MAX  HISTORY
 coretemp/Package id 0  85.0°C ███████████████████████████░░░░░░  67.6°C  85.2°C  ▁▂▂▄▅▆▆▆▇▇▇▇███▇▇▇██
 coretemp/Core 0        82.1°C ██████████████████████████░░░░░░░  63.2°C  83.6°C  ▁▄▂▄▅▆▅▅▇▇▇██▆▇▆▆▆▆▇
 GPU0/NVIDIA RTX 4070   69.1°C ████████████████████░░░░░░░░░░░░░  56.3°C  71.6°C  ▁▁▂▂▃▅▅▅▆▇████▇▇▇▇▇▆
 nvme/Composite         44.0°C ██████████░░░░░░░░░░░░░░░░░░░░░░░  41.0°C  44.0°C  ▁▁▁▂▂▂▂▂▃▃▃▃▃▃▃▃▃▃▃▃

 Fans: CPU fan 2498 rpm

 Recent changes
   15:20:02  WARM  coretemp/Core 0 at 77.3°C
   15:20:07  HOT   coretemp/Package id 0 at 85.0°C
```

Rows turn yellow when a sensor is **warm** and red when it is **hot**. The terminal beeps when a sensor turns hot. A sensor has to cool 2 °C below a limit before it counts as back down, so a reading sitting on the line doesn't keep re-alerting.

## Run it

```bash
python3 heat-monitor/heat_monitor.py
```

To see the display working before you set up sensors, run it with simulated readings:

```bash
python3 heat-monitor/heat_monitor.py --demo
```

Needs Python 3.8 or newer. On Windows use `py` instead of `python3`.

## Where the readings come from

| Computer | What it reads | Setup |
|---|---|---|
| **Linux** | `/sys/class/hwmon`: CPU package and cores, NVMe drives, chipset, AMD/nouveau GPUs, fans. Falls back to `/sys/class/thermal`. | Usually none. If nothing shows up, run `sudo apt install lm-sensors && sudo sensors-detect`, then reboot. |
| **macOS** | `smctemp`, `osx-cpu-temp` or `istats`, whichever is installed. With `sudo` it also reads `powermetrics` and macOS's thermal pressure level. | `brew tap narugit/tap && brew install narugit/tap/smctemp` (Apple Silicon and Intel). |
| **Windows** | LibreHardwareMonitor or OpenHardwareMonitor sensors (CPU, GPU, disks, fans). Without either, ACPI thermal zones. | Start [LibreHardwareMonitor](https://github.com/LibreHardwareMonitor/LibreHardwareMonitor) and leave it running. The ACPI fallback needs an Administrator terminal and is often coarse. |
| **NVIDIA GPUs** | `nvidia-smi`, on any OS | Comes with the NVIDIA driver. |

Virtual machines, containers and cloud servers usually have no temperature sensors. The monitor tells you when it finds none.

## Limits

When a sensor reports its own "high" and "critical" limits (most CPUs and drives on Linux do), the monitor uses them. Otherwise a sensor is warm from 80 °C and hot from 95 °C. To use your own limits for every sensor:

```bash
python3 heat-monitor/heat_monitor.py --warn 75 --crit 90
```

## Options

| Option | |
|---|---|
| `-i, --interval N` | Seconds between refreshes (default 2) |
| `--warn C`, `--crit C` | Warm and hot limits in °C for every sensor |
| `-f, --fahrenheit` | Show °F |
| `--filter REGEX` | Only show matching sensors, e.g. `--filter "package\|gpu"` |
| `--log FILE.csv` | Append every reading to a CSV file, to graph later |
| `--notify` | Desktop notification when a sensor turns hot (Linux `notify-send`, macOS) |
| `-q, --quiet` | No beep |
| `--once` | Print one reading and exit |
| `--json` | Print one reading as JSON and exit |
| `--ascii`, `--no-color` | For terminals without Unicode or colour |
| `--demo` | Simulated sensors |

With `--once` or `--json` the exit status is `0` normal, `1` warm, `2` hot, `3` no sensors found. You can use it in a cron job, for example:

```bash
python3 heat_monitor.py --once --crit 90 > /dev/null || notify-send "Computer is running hot"
```

## Tests

```bash
python3 -m unittest discover -s heat-monitor/tests
```
