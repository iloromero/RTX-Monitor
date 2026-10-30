# rtx-monitor

A low-overhead, real-time resource dashboard for this machine (Ryzen 9 7950X + RTX 5090), served
on the LAN at **http://192.0.2.115:8888** (Wi-Fi: http://192.0.2.137:8888).

- `server.py`: Python 3 standard library only. It reads the GPU through NVML (`ctypes` on
  `libnvidia-ml.so.1`, with no `nvidia-smi` subprocess) and everything else from `/proc` and `/sys`.
- `index.html`: a single page with no external assets. It receives samples over Server-Sent Events
  at `/stream`.
- `rtx-monitor.service`: a systemd **user** unit with `Nice=10`.

> **There is no authentication.** Anyone who can reach port 8888 sees process names, command lines
> (which can contain arguments or paths), PIDs, IP addresses and loaded Ollama models. Keep it on a
> trusted LAN, or bind it to one address with `--host`.

## Run

```bash
python3 server.py                     # 0.0.0.0:8888, 1 s interval
python3 server.py --host 192.0.2.115 --port 8888 --interval 1 --ollama http://127.0.0.1:11434
python3 server.py --once              # print one full sample as JSON and exit (debugging)
```

Endpoints: `/` (the page), `/stream` (SSE: one `info` event, then one sample per interval),
`/healthz`.

## Install as a service (no sudo needed except for the firewall)

```bash
mkdir -p ~/.config/systemd/user
cp ~/projects/rtx-monitor/rtx-monitor.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now rtx-monitor.service
loginctl enable-linger "$USER"        # start at boot without a login (already "Linger=yes" here)
journalctl --user -u rtx-monitor -f   # start-up NVML timing report
```

If a copy is already running by hand, stop it first with `pkill -f '^python3 server.py'`.

Firewall: ufw is currently **disabled** (`ENABLED=no` in `/etc/ufw/ufw.conf`), so nothing needs
to change. If you enable it later, run:

```bash
sudo ufw allow 8888/tcp
# or, LAN only:
sudo ufw allow from 192.0.2.0/24 to any port 8888 proto tcp
```

## How it keeps overhead low

- **The server samples only while a browser is connected.** The sampler thread blocks on a
  condition variable while no client is connected, so it uses no CPU at all. The page closes its
  `EventSource` when the tab is hidden (`visibilitychange`) and reopens it when the tab becomes
  visible again.
- **Files stay open.** `/proc/stat`, `/proc/meminfo`, `/proc/diskstats`, each core's
  `scaling_cur_freq`, the hwmon inputs and the NIC counters are opened once and re-read with
  `pread(fd, …, 0)`.
- **Expensive collectors run on a 3 s tier.** This tier covers the `/proc/*/stat` scan for the top
  processes, the Ollama `/api/ps` poll (in its own thread with a 0.5 s timeout, so a stopped Ollama
  never delays a sample), and every NVML call or sensor read that turned out to be slow.
- **NVML and sensor calls are timed.** Each NVML probe is timed at startup and during runtime:

  | probe | wall | notes |
  |---|---|---|
  | utilization, memory, temperature, power, clocks, P-state, throttle, enc/dec, PCIe link | < 0.05 ms | every sample |
  | running processes (`_v3`) | ~0.3 ms | every sample |
  | `nvmlDeviceGetFanSpeed` | ~1 ms (spins up to ~7 ms of CPU) | every 6 s |
  | `nvmlDeviceGetPcieThroughput` RX / TX | ~24 ms each (~4 ms CPU each) | every 6 s |
  | memory temperature (field 82) | — | not exposed on this board: shown as n/a |

  A probe slower than 0.5 ms runs on the 3 s tier. One that also burns more than 2 ms of CPU runs
  on every other tick (every 6 s). The SMBus DIMM sensors (`spd5118`, ~1.3 ms each) and the NVMe
  sensor (~3 ms) are timed the same way and refreshed every 3 s.

The header shows each sample's cost: wall time and the sampler thread's CPU time.

**Measured on this machine** (`utime+stime` from `/proc/<pid>/stat` of the `python3 server.py`
process itself):

| condition | CPU | RSS |
|---|---|---|
| one browser connected, 10 s | 0.5 % of one core | 43.6 MB |
| one browser connected, 30 s | 0.73 % of one core | 43.6 MB |
| no clients, 10 s | 0.0 % | 43.6 MB |

Per sample: about 1.5 ms CPU on a normal tick, about 9 ms on a 3 s tick, and about 24 ms on a 6 s
tick (PCIe and fan included). That averages to about 6.5 ms of CPU per second.

## What it shows

- **Dials (270°, amber at ≥70 %, red at ≥90 %, 2-minute sparkline):** GPU utilization, VRAM, GPU
  power as a percentage of the enforced limit, CPU, RAM (`MemTotal − MemAvailable`), disk busy
  (`io_ticks`), network, and root filesystem. The network dial shows the busiest wired NIC: the
  higher of rx and tx as a share of its link speed (`/sys/class/net/*/speed`; full duplex, so each
  direction is measured against the full speed). Wi-Fi drivers don't report a speed, so Wi-Fi is
  left out of the dial but still listed in the Disk & network panel. With more than one GPU, each GPU gets its own three dials.
- **CPU cores:** one bar per logical CPU, laid out per physical core so SMT siblings sit together
  (cpu 0/16, …). Cores are grouped by CCD (L3 domain) on AMD, and by P-cores/E-cores
  (`cpuinfo_max_freq`) on hybrid Intel parts. The panel also shows per-core GHz, total, iowait and
  per-group average.
- **GPU memory:** a VRAM bar split into Ollama and other, plus the Ollama models table (size,
  % on GPU, context, time until unload). A model below 100 % on GPU is highlighted, because the
  part not in VRAM runs on the CPU and is much slower. If the API is down, the table reads
  "not reachable".
- **System memory:** used, page cache, buffers, free, available, shmem and swap.
- **GPU details:** SM and memory clocks against their max, utilization, encoder and decoder, fan,
  power against the limit, P-state, core temperature against the slowdown threshold, PCIe current
  and max generation and width, PCIe RX/TX, decoded clock-limit (throttle) reasons, and a power
  sparkline.
- **Temperatures:** GPU core (NVML), CPU Tctl and Tccd1/2 (`k10temp`), NVMe, four DIMMs
  (`spd5118`), the iGPU (`amdgpu`) and the NIC (`r8169`). Each bar is scaled to the sensor's
  crit/max where one exists.
- **Disk & network:** NVMe busy % and R/W throughput, filesystems of 2 GiB or more (snap loops,
  tmpfs and overlay are skipped), and RX/TX for each physical NIC that is up.
- **Processes:** a GPU process table (`nvmlDevice*RunningProcesses_v3`, so it includes the
  `llama-server` inside the Open WebUI container) and the top 8 processes by CPU. All names and
  command lines are HTML-escaped.

If NVML cannot be loaded, the GPU dials and panels read "n/a" and the rest keeps working.

## Notes for this host

- Ollama runs inside the `open-webui` container (`ghcr.io/open-webui/open-webui:ollama`), which
  publishes 11434 on the host, so the default `--ollama http://127.0.0.1:11434` works.
- The `amdgpu` sensor belongs to the Ryzen iGPU, not the RTX card.
- The GPU's PCIe link drops to Gen 1 at idle to save power. "Gen 1/5" at idle is normal.
