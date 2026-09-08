# NVIDIA GPU temperature support - design note

Target/test system: ThinkPad P1 Gen 7, Intel Core Ultra 7 165H, NVIDIA RTX 4060
Laptop GPU, Pop!_OS. The implementation is generic to NVIDIA-equipped ThinkPads.

## What the hardware actually offers (measured on the P1 Gen 7)

| Interface | Result |
|---|---|
| `hwmon` named `nvidia`/`nouveau` | Absent - the proprietary driver exposes no hwmon node |
| `/proc/driver/nvidia/gpus/*/information` | No temperature field |
| ThinkPad EC `temp2_label=GPU` | Intermittent: read empty once, `50000` later |
| `nvidia-smi` | Works, ~27 ms per spawn |
| `libnvidia-ml.so.1` (NVML) | Present in `/lib/x86_64-linux-gnu`, works via `ctypes` |

Timings measured on the target machine:

- NVML with a persistent handle: **0.03 ms** per read
- NVML `init` -> read -> `shutdown` per read: **12-18 ms**
- `nvidia-smi` subprocess: **~27 ms**

## Detection strategy chosen

Backend chain, tried best-first, with the working backend remembered:

1. **NVML via `ctypes`** on `libnvidia-ml.so.1`
2. GPU `hwmon` (`nvidia`, `nouveau`) - covers nouveau users
3. ThinkPad EC `GPU`-labelled sensor - covers machines with no NVIDIA driver
4. `nvidia-smi`, rate-limited to one spawn per 5 s

### Why NVML over `pynvml`

`pynvml` is a thin `ctypes` wrapper over the same library. Adding it would mean
a new `Depends:` on the `.deb` (`python3-pynvml`, not installed by default on
Ubuntu/Pop!_OS) for a root daemon that is otherwise **pure stdlib**. Three
`ctypes` calls avoid that entirely. The library itself always exists wherever an
NVIDIA driver does, so there is nothing extra to install.

### Why NVML is opened and closed around every read

This is the one non-obvious decision. Holding an NVML device handle open keeps
the dGPU's runtime-PM reference count raised, so on a hybrid-graphics laptop the
card would **never runtime-suspend** - a permanent battery cost caused purely by
monitoring. Init/read/shutdown per poll costs 12-18 ms and is only paid while
the GPU is already awake, so the trade is clearly worth it.

### Runtime-PM gate

Before any driver-touching backend runs, the daemon reads the NVIDIA card's PCI
`power/runtime_status`. `suspended` -> no reading at all, and no wake-up. A
powered-down GPU produces no heat, so "no reading" is the correct answer rather
than a degraded one.

## Control model

```
fan_level = max(cpu_requested_level, gpu_requested_level)
```

Maximum of *requested levels*, not of raw temperatures, so each source can have
its own curve (70 C is hot for a CPU, unremarkable for a GPU). Hysteresis state
is tracked per source: `prev_levels = {"cpu": n, "gpu": m}`.

Ties report the CPU, purely so the "requested by" label is stable.

## Safety analysis

| Mechanism | Effect of the GPU work |
|---|---|
| BIOS/firmware fallback | Unchanged; still used when no source has a reading |
| Hardware watchdog | Unchanged; armed every loop before anything else |
| CPU critical override | Unchanged, including its 5 C release margin |
| GPU critical override | New, additive: `cpu_force or gpu_force`. Own default (90 C) and own hard cap (95 C) |
| Hysteresis | Now per source, so one source cannot drag another's request down |
| Smoothing | Now per source |
| Failure handling | Every GPU backend is wrapped; any exception becomes "no reading" plus a 30 s cooldown |

GPU monitoring can only ever ask for **more** cooling, never less, so it cannot
weaken existing CPU thermal protection.

### Two latent bugs fixed along the way

1. **Stale smoothing window.** The old code appended to the smoothing deque only
   when a reading existed. If the sensor went away permanently, the deque kept
   its last values forever and the fan stayed pinned at that level, instead of
   falling back to firmware `auto` as the code intended. `Smoother.update(None)`
   now drains one sample per miss, so a lost source fades out over the smoothing
   window and then reports `None`.
2. **GPU critical latch on a vanished sensor.** A raw-temperature latch that
   only updates when a reading exists would hold full-speed forever if the GPU
   disappeared while over its limit. The GPU latch is released once the GPU has
   been unreadable for a whole smoothing window. The **CPU** latch deliberately
   keeps the old fail-loud behaviour: a laptop whose CPU sensor dies is better
   off loud than silent.

### CPU last-resort sensor scan

`read_cpu_temp()`'s final fallback (any hwmon device) now excludes GPU hwmon
names, so a GPU sensor can never be fed into the **CPU** curve on a machine
without `coretemp`/`k10temp`/`thinkpad`.

## Multiple fans

`/proc/acpi/ibm/fan` exposes exactly one shared EC `level` and one `speed`. The
P1 Gen 7 has two tachometers (`fan1_input`, `fan2_input`) reading ~3962 and
~4155 RPM independently. Nothing in the ThinkPad interface supports addressing
them separately, so both are **read and displayed only**; control stays on the
single shared level. The stall re-kick now treats "any fan turning" as
not-stalled on dual-fan machines.

## GUI integration

The daemon publishes `/run/thinkpad-fan/status.json` (atomic replace, mode 0644,
`RuntimeDirectory=` in the unit) every poll: both temperatures, both smoothed
values, each source's requested level, the GPU sensor state and backend, both
fan RPMs, the selected level and the source responsible.

The GUI reads that snapshot, so the panel shows exactly what the daemon decided
and can never disagree with the fan. When there is no snapshot (daemon stopped,
or an older daemon), it falls back to reading the sensors directly, including a
trimmed NVML read of its own.

The curve editor was factored into a reusable `CurveEditor` frame and placed in
a two-tab notebook (CPU / GPU) inside the existing "Automatic curve" panel, so
the window layout and footprint are unchanged.

## Configuration

New keys, all optional and defaulted, so existing config files upgrade silently:

| Key | Default |
|---|---|
| `gpu_monitoring` | `"auto"` (`auto` / `on` / `off`) |
| `gpu_curve` | `[[0,0],[60,1],[65,2],[70,3],[74,4],[78,5],[82,6],[85,7]]` |
| `gpu_critical_temp` | `90` (hard cap 95) |

`validate_config()` already merges over `DEFAULT_CONFIG`, so absent keys take
defaults; the curve validator was factored out and is now shared by both curves.

## Tests

`tests/test_thinkpad_fand.py`, stdlib `unittest`, 55 tests, no NVIDIA hardware
required - `GpuTempSensor` takes its backends and its view of the PCI bus as
constructor arguments.

```bash
python3 -m unittest discover -s tests -v
```
