#!/usr/bin/env python3
"""rtx-monitor — low-overhead real-time resource dashboard.

Python 3 standard library only. GPU via NVML (ctypes on libnvidia-ml.so.1),
everything else from /proc and /sys. Samples only while at least one browser
is connected to /stream (Server-Sent Events).
"""
import argparse
import ctypes
import fcntl
import http.client
import json
import os
import re
import socket
import struct
import sys
import threading
import time
from collections import deque
from ctypes import byref, c_char_p, c_int, c_uint, c_ulonglong, c_void_p, c_longlong, c_double
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

HERE = os.path.dirname(os.path.abspath(__file__))
CLK_TCK = os.sysconf("SC_CLK_TCK")
PAGE = os.sysconf("SC_PAGE_SIZE")
SLOW_PERIOD = 3.0     # seconds between runs of the expensive collectors
SLOW_CALL_MS = 0.5    # an NVML call or sensor read slower than this moves to the slow tier
SPIN_CPU_MS = 2.0     # …and one that also burns this much CPU runs every other slow tick
HISTORY = 120         # sparkline length (samples)
GIB = 1 << 30


# ---------------------------------------------------------------- file helpers

def read_text(path, default=None):
    try:
        with open(path, "rb") as f:
            return f.read().decode(errors="replace").strip()
    except OSError:
        return default


class SysFile:
    """A /proc or /sys file kept open and re-read with pread(…, 0).

    Saves an open/close pair per file per sample; both kernfs (sysfs) and
    seq_file (procfs) regenerate their content on a read at offset 0."""
    __slots__ = ("path", "fd")

    def __init__(self, path):
        self.path = path
        self.fd = os.open(path, os.O_RDONLY | os.O_CLOEXEC)

    def read(self, size=65536):
        data = os.pread(self.fd, size, 0)
        if len(data) < size:
            return data
        chunks, off = [data], len(data)          # rare: bigger than one buffer
        while True:
            more = os.pread(self.fd, size, off)
            if not more:
                return b"".join(chunks)
            chunks.append(more)
            off += len(more)

    def int(self, default=None):
        try:
            return int(self.read(64))
        except (OSError, ValueError):
            return default


def try_sysfile(path):
    try:
        return SysFile(path)
    except OSError:
        return None


def parse_cpulist(s):
    out = []
    for part in (s or "").split(","):
        if "-" in part:
            a, b = part.split("-")
            out.extend(range(int(a), int(b) + 1))
        elif part.strip():
            out.append(int(part))
    return out


def iface_ipv4(name):
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        res = fcntl.ioctl(s.fileno(), 0x8915, struct.pack("256s", name.encode()[:15]))  # SIOCGIFADDR
        return socket.inet_ntoa(res[20:24])
    except OSError:
        return None
    finally:
        s.close()


# ------------------------------------------------------------------------ NVML

NVML_SUCCESS = 0
NVML_ERROR_NOT_SUPPORTED = 3
NVML_ERROR_INSUFFICIENT_SIZE = 7
NVML_ERROR_FUNCTION_NOT_FOUND = 13
NVML_VALUE_NOT_AVAILABLE = 0xFFFFFFFFFFFFFFFF
NVML_FI_DEV_MEMORY_TEMP = 82

# nvmlClocksEventReasons / nvmlClocksThrottleReasons bit masks
THROTTLE_REASONS = [
    (0x001, "idle"),
    (0x002, "app clock setting"),
    (0x004, "power cap"),
    (0x008, "HW slowdown"),
    (0x010, "sync boost"),
    (0x020, "SW thermal"),
    (0x040, "HW thermal"),
    (0x080, "HW power brake"),
    (0x100, "display clock"),
]


class _Utilization(ctypes.Structure):
    _fields_ = [("gpu", c_uint), ("memory", c_uint)]


class _Memory(ctypes.Structure):
    _fields_ = [("total", c_ulonglong), ("free", c_ulonglong), ("used", c_ulonglong)]


class _ProcessInfo(ctypes.Structure):     # nvmlProcessInfo_t as used by the _v3 calls
    _fields_ = [("pid", c_uint), ("usedGpuMemory", c_ulonglong),
                ("gpuInstanceId", c_uint), ("computeInstanceId", c_uint)]


class _Value(ctypes.Union):
    _fields_ = [("d", c_double), ("ui", c_uint), ("ul", ctypes.c_ulong),
                ("ull", c_ulonglong), ("sll", c_longlong), ("si", c_int)]


class _FieldValue(ctypes.Structure):
    _fields_ = [("fieldId", c_uint), ("scopeId", c_uint), ("timestamp", c_longlong),
                ("latencyUsec", c_longlong), ("valueType", c_uint),
                ("nvmlReturn", c_uint), ("value", _Value)]


class NvmlError(Exception):
    def __init__(self, rc, msg):
        super().__init__(f"NVML error {rc}: {msg}")
        self.rc = rc


class Nvml:
    def __init__(self):
        self.lib = ctypes.CDLL("libnvidia-ml.so.1")
        self.lib.nvmlErrorString.restype = c_char_p
        self.call("nvmlInit_v2")

    def call(self, name, *args):
        try:
            fn = getattr(self.lib, name)
        except AttributeError:
            raise NvmlError(NVML_ERROR_FUNCTION_NOT_FOUND, f"{name} not in library") from None
        rc = fn(*args)
        if rc != NVML_SUCCESS:
            raise NvmlError(rc, (self.lib.nvmlErrorString(rc) or b"?").decode())

    def string(self, name, *args, size=96):
        buf = ctypes.create_string_buffer(size)
        self.call(name, *args, buf, c_uint(size))
        return buf.value.decode(errors="replace")

    def uint(self, name, *args):
        v = c_uint()
        self.call(name, *args, byref(v))
        return v.value


class Gpu:
    """One NVML device. Each probe is a small group of NVML calls; probes are
    timed at start-up and any that block (PCIe throughput samples for ~20 ms)
    run only on the slow tick, with the cached value reused in between."""

    def __init__(self, nv, index):
        self.nv, self.index = nv, index
        h = c_void_p()
        nv.call("nvmlDeviceGetHandleByIndex_v2", c_uint(index), byref(h))
        self.h = h
        self.name = nv.string("nvmlDeviceGetName", h)
        self.static = {"index": index, "name": self.name}
        for key, fn, args in [
            ("sm_max", "nvmlDeviceGetMaxClockInfo", (c_int(1),)),
            ("mem_max", "nvmlDeviceGetMaxClockInfo", (c_int(2),)),
            ("pcie_gen_max", "nvmlDeviceGetGpuMaxPcieLinkGeneration", ()),
            ("pcie_gen_max_link", "nvmlDeviceGetMaxPcieLinkGeneration", ()),
            ("pcie_width_max", "nvmlDeviceGetMaxPcieLinkWidth", ()),
            ("temp_slowdown", "nvmlDeviceGetTemperatureThreshold", (c_int(1),)),
            ("temp_shutdown", "nvmlDeviceGetTemperatureThreshold", (c_int(0),)),
        ]:
            try:
                self.static[key] = nv.uint(fn, h, *args)
            except NvmlError:
                self.static[key] = None
        # the link-max is what the slot+card negotiated at best; prefer it
        if self.static.get("pcie_gen_max_link"):
            self.static["pcie_gen_max"] = min(self.static["pcie_gen_max"] or 99,
                                              self.static["pcie_gen_max_link"])
        try:
            self.static["uuid"] = nv.string("nvmlDeviceGetUUID", h)
        except NvmlError:
            pass
        self.probes = {
            "utilization": self._utilization,
            "memory": self._memory,
            "temperature": self._temperature,
            "memory_temp": self._memory_temp,
            "power": self._power,
            "power_limit": self._power_limit,
            "fan": self._fan,
            "clocks": self._clocks,
            "pstate": self._pstate,
            "pcie_link": self._pcie_link,
            "pcie_rx": self._pcie_rx,
            "pcie_tx": self._pcie_tx,
            "encoder": self._encoder,
            "decoder": self._decoder,
            "throttle": self._throttle,
            "processes": self._processes,
        }
        self.timing_ms = {}
        self.cpu_ms = {}
        self.slow = set()
        self.slower = set()
        self.slow_ticks = 0
        self.disabled = {}
        self.cache = {}
        self.strikes = {}
        self._calibrate()
        # how stale the PCIe throughput figure can be, for the page's label
        pcie = "pcie_rx"
        self.static["pcie_every_s"] = (SLOW_PERIOD * (2 if pcie in self.slower else 1)
                                       if pcie in self.slow else None)

    # -- calibration
    def _calibrate(self):
        for name, fn in list(self.probes.items()):
            times, cpu = [], []
            for _ in range(3):
                t, c = time.perf_counter(), time.thread_time()
                try:
                    self.cache[name] = fn()
                except NvmlError as e:
                    if e.rc in (NVML_ERROR_NOT_SUPPORTED, NVML_ERROR_FUNCTION_NOT_FOUND):
                        self.disabled[name] = str(e)
                        del self.probes[name]
                        break
                    self.cache[name] = {}
                times.append((time.perf_counter() - t) * 1000)
                cpu.append((time.thread_time() - c) * 1000)
            if times:
                times.sort()
                cpu.sort()
                self.timing_ms[name] = round(times[len(times) // 2], 3)
                self.cpu_ms[name] = round(cpu[len(cpu) // 2], 3)
                if self.timing_ms[name] > SLOW_CALL_MS:
                    self.slow.add(name)
                # PCIe throughput spins in the driver for its ~20 ms window
                if self.cpu_ms[name] > SPIN_CPU_MS:
                    self.slower.add(name)

    def sample(self, slow_tick):
        out = dict(self.static)
        if slow_tick:
            self.slow_ticks += 1
        for name, fn in self.probes.items():
            if name in self.slow and (not slow_tick or (name in self.slower and self.slow_ticks % 2 == 0)):
                out.update(self.cache.get(name, {}))
                continue
            t, c = time.perf_counter(), time.thread_time()
            try:
                frag = fn()
            except NvmlError as e:
                frag = {}
                out.setdefault("errors", {})[name] = str(e)
            dt = (time.perf_counter() - t) * 1000
            cpu = (time.thread_time() - c) * 1000
            self.cache[name] = frag
            out.update(frag)
            # the fan query is cheap at start-up but spins for several ms once running
            if name in self.slow and cpu > SPIN_CPU_MS:
                self.slower.add(name)
            # demote a probe that turns out to block during normal running
            if name not in self.slow:
                self.strikes[name] = self.strikes.get(name, 0) + 1 if dt > SLOW_CALL_MS else 0
                if self.strikes[name] >= 3:
                    self.slow.add(name)
                    self.timing_ms[name] = round(dt, 3)
        return out

    # -- probes
    def _utilization(self):
        u = _Utilization()
        self.nv.call("nvmlDeviceGetUtilizationRates", self.h, byref(u))
        return {"util": u.gpu, "mem_util": u.memory}

    def _memory(self):
        m = _Memory()
        self.nv.call("nvmlDeviceGetMemoryInfo", self.h, byref(m))
        return {"vram_used": m.used, "vram_total": m.total}

    def _temperature(self):
        return {"temp": self.nv.uint("nvmlDeviceGetTemperature", self.h, c_int(0))}

    def _memory_temp(self):
        fv = _FieldValue(fieldId=NVML_FI_DEV_MEMORY_TEMP)
        self.nv.call("nvmlDeviceGetFieldValues", self.h, c_int(1), byref(fv))
        if fv.nvmlReturn != NVML_SUCCESS:
            raise NvmlError(fv.nvmlReturn, "memory temperature field")
        v = fv.value
        val = {0: v.d, 1: v.ui, 2: v.ul, 3: v.ull, 4: v.sll, 5: v.si}.get(fv.valueType, v.ui)
        if not val or val > 200:        # 0 = sensor absent on GeForce boards
            raise NvmlError(NVML_ERROR_NOT_SUPPORTED, "memory temperature not exposed")
        return {"mem_temp": val}

    def _power(self):
        return {"power_w": self.nv.uint("nvmlDeviceGetPowerUsage", self.h) / 1000}

    def _power_limit(self):
        return {"power_limit_w": self.nv.uint("nvmlDeviceGetEnforcedPowerLimit", self.h) / 1000}

    def _fan(self):
        return {"fan": self.nv.uint("nvmlDeviceGetFanSpeed", self.h)}

    def _clocks(self):
        return {"sm_clock": self.nv.uint("nvmlDeviceGetClockInfo", self.h, c_int(1)),
                "mem_clock": self.nv.uint("nvmlDeviceGetClockInfo", self.h, c_int(2))}

    def _pstate(self):
        v = c_int()
        self.nv.call("nvmlDeviceGetPerformanceState", self.h, byref(v))
        return {"pstate": None if v.value == 32 else v.value}

    def _pcie_link(self):
        return {"pcie_gen": self.nv.uint("nvmlDeviceGetCurrPcieLinkGeneration", self.h),
                "pcie_width": self.nv.uint("nvmlDeviceGetCurrPcieLinkWidth", self.h)}

    def _pcie_rx(self):   # KB/s over a ~20 ms window
        return {"pcie_rx": self.nv.uint("nvmlDeviceGetPcieThroughput", self.h, c_int(1)) * 1024}

    def _pcie_tx(self):
        return {"pcie_tx": self.nv.uint("nvmlDeviceGetPcieThroughput", self.h, c_int(0)) * 1024}

    def _encoder(self):
        u, p = c_uint(), c_uint()
        self.nv.call("nvmlDeviceGetEncoderUtilization", self.h, byref(u), byref(p))
        return {"enc": u.value}

    def _decoder(self):
        u, p = c_uint(), c_uint()
        self.nv.call("nvmlDeviceGetDecoderUtilization", self.h, byref(u), byref(p))
        return {"dec": u.value}

    def _throttle(self):
        v = c_ulonglong()
        try:
            self.nv.call("nvmlDeviceGetCurrentClocksEventReasons", self.h, byref(v))
        except NvmlError as e:
            if e.rc != NVML_ERROR_FUNCTION_NOT_FOUND:
                raise
            self.nv.call("nvmlDeviceGetCurrentClocksThrottleReasons", self.h, byref(v))
        return {"throttle": [label for bit, label in THROTTLE_REASONS if v.value & bit],
                "throttle_mask": v.value}

    def _proc_list(self, fn):
        n = c_uint(0)
        try:
            self.nv.call(fn, self.h, byref(n), None)
            return []
        except NvmlError as e:
            if e.rc != NVML_ERROR_INSUFFICIENT_SIZE:
                raise
        cap = n.value + 8
        arr = (_ProcessInfo * cap)()
        n = c_uint(cap)
        self.nv.call(fn, self.h, byref(n), arr)
        return [(p.pid, None if p.usedGpuMemory == NVML_VALUE_NOT_AVAILABLE else p.usedGpuMemory)
                for p in arr[:n.value]]

    def _processes(self):
        procs = {}
        for fn, kind in (("nvmlDeviceGetComputeRunningProcesses_v3", "C"),
                         ("nvmlDeviceGetGraphicsRunningProcesses_v3", "G")):
            for pid, mem in self._proc_list(fn):
                p = procs.setdefault(pid, {"pid": pid, "vram": None, "type": ""})
                p["type"] = (p["type"] + "+" + kind) if p["type"] else kind
                if mem is not None:
                    p["vram"] = max(p["vram"] or 0, mem)
        return {"procs": list(procs.values())}


def open_nvml():
    """Returns (nvml, [Gpu], error string or None)."""
    try:
        nv = Nvml()
        driver = nv.string("nvmlSystemGetDriverVersion")
        try:
            cuda = c_int()
            nv.call("nvmlSystemGetCudaDriverVersion_v2", byref(cuda))
            cuda = f"{cuda.value // 1000}.{cuda.value % 1000 // 10}"
        except NvmlError:
            cuda = None
        count = nv.uint("nvmlDeviceGetCount_v2")
        gpus = [Gpu(nv, i) for i in range(count)]
        return nv, gpus, {"driver": driver, "cuda": cuda}, None
    except (OSError, NvmlError) as e:
        return None, [], {}, str(e)


# ---------------------------------------------------------------- host metrics

def cpu_topology():
    """Logical CPUs grouped into physical cores, and cores into sensible groups:
    P/E cores on hybrid parts (different cpuinfo_max_freq), otherwise one group
    per L3 domain (AMD CCDs), otherwise a single group."""
    cpus = parse_cpulist(read_text("/sys/devices/system/cpu/online", "0"))
    base = "/sys/devices/system/cpu/cpu{}/"
    info = {}
    for c in cpus:
        b = base.format(c)
        info[c] = {
            "siblings": tuple(parse_cpulist(read_text(b + "topology/thread_siblings_list", str(c)))),
            "max_khz": int(read_text(b + "cpufreq/cpuinfo_max_freq", "0") or 0),
            "l3": read_text(b + "cache/index3/shared_cpu_list", ""),
        }
    cores = {}
    for c in cpus:
        cores.setdefault(info[c]["siblings"], []).append(c)
    maxes = sorted({info[c]["max_khz"] for c in cpus}, reverse=True)
    l3s = sorted({info[c]["l3"] for c in cpus}, key=lambda s: parse_cpulist(s)[:1] or [0])
    vendor = ""
    for line in (read_text("/proc/cpuinfo", "") or "").splitlines():
        if line.startswith("vendor_id"):
            vendor = line.split(":", 1)[1].strip()
            break
    if len(maxes) > 1:
        def key(sib):
            return maxes.index(info[sib[0]]["max_khz"])
        names = {0: "P-cores", len(maxes) - 1: "E-cores"} if len(maxes) == 2 else {}
        gname = lambda k: names.get(k, f"{maxes[k] / 1e6:.1f} GHz cores")
    elif len(l3s) > 1:
        def key(sib):
            return l3s.index(info[sib[0]]["l3"])
        label = "CCD" if vendor == "AuthenticAMD" else "L3 domain"
        gname = lambda k: f"{label} {k}"
    else:
        key = lambda sib: 0
        gname = lambda k: "Cores"
    groups = {}
    for sib in sorted(cores, key=lambda s: s[0]):
        groups.setdefault(key(sib), []).append(sib)
    smt = any(len(s) > 1 for s in cores)
    out = []
    for k in sorted(groups):
        out.append({
            "name": gname(k),
            "cores": [{"label": f"core {i}", "cpus": list(sib),
                       "max_mhz": info[sib[0]]["max_khz"] // 1000}
                      for i, sib in enumerate(groups[k], start=sum(len(groups[j]) for j in groups if j < k))],
        })
    return {"cpus": cpus, "groups": out, "physical_cores": len(cores), "smt": smt, "vendor": vendor}


def cpu_model():
    for line in (read_text("/proc/cpuinfo", "") or "").splitlines():
        if line.startswith("model name"):
            return line.split(":", 1)[1].strip()
    return os.uname().machine


class CpuCollector:
    def __init__(self, cpus):
        self.stat = SysFile("/proc/stat")
        self.ncpu = max(cpus) + 1
        self.freq = [try_sysfile(f"/sys/devices/system/cpu/cpu{c}/cpufreq/scaling_cur_freq")
                     if c in cpus else None for c in range(self.ncpu)]
        self.prev = None

    def sample(self):
        cur = {}
        for line in self.stat.read().split(b"\n"):
            if not line.startswith(b"cpu"):
                break
            f = line.split()
            v = [int(x) for x in f[1:9]]          # user nice system idle iowait irq softirq steal
            cur[f[0]] = (sum(v), v[3] + v[4], v[4])
        prev, self.prev = self.prev, cur
        cores = [None] * self.ncpu
        total = iowait = None
        if prev:
            for name, (t, idle, io) in cur.items():
                p = prev.get(name)
                if not p:
                    continue
                dt = t - p[0]
                busy = 100.0 * (dt - (idle - p[1])) / dt if dt > 0 else 0.0
                if name == b"cpu":
                    total = busy
                    iowait = 100.0 * (io - p[2]) / dt if dt > 0 else 0.0
                else:
                    i = int(name[3:])
                    if i < self.ncpu:
                        cores[i] = round(busy, 1)
        mhz = [round(f.int(0) / 1000) if f else None for f in self.freq]
        return {"total": None if total is None else round(total, 1),
                "iowait": None if iowait is None else round(iowait, 2),
                "cores": cores, "mhz": mhz}


class MemCollector:
    KEYS = {b"MemTotal:", b"MemFree:", b"MemAvailable:", b"Buffers:", b"Cached:",
            b"SReclaimable:", b"SwapTotal:", b"SwapFree:", b"Shmem:"}

    def __init__(self):
        self.f = SysFile("/proc/meminfo")

    def sample(self):
        m = {}
        for line in self.f.read().split(b"\n"):
            k, _, rest = line.partition(b" ")
            if k in self.KEYS:
                m[k[:-1].decode()] = int(rest.split()[0]) * 1024
        total = m.get("MemTotal", 0)
        avail = m.get("MemAvailable", 0)
        cached = m.get("Cached", 0) + m.get("SReclaimable", 0)
        return {"total": total, "used": total - avail, "available": avail,
                "free": m.get("MemFree", 0), "buffers": m.get("Buffers", 0), "cached": cached,
                "shmem": m.get("Shmem", 0),
                "swap_total": m.get("SwapTotal", 0),
                "swap_used": m.get("SwapTotal", 0) - m.get("SwapFree", 0)}


class DiskCollector:
    """Physical block devices (have a device/ link, or are md RAID arrays) and
    the real filesystems mounted from them."""
    SKIP_FS = {"tmpfs", "devtmpfs", "overlay", "squashfs", "proc", "sysfs", "cgroup2",
               "nsfs", "fuse.snapfuse", "ramfs", "autofs"}

    def __init__(self):
        self.stats = SysFile("/proc/diskstats")
        self.disks = {}
        for name in sorted(os.listdir("/sys/block")):
            b = f"/sys/block/{name}"
            if name.startswith(("loop", "ram", "zram", "dm-")):
                continue
            if os.path.exists(b + "/device") or os.path.isdir(b + "/md"):
                model = read_text(b + "/device/model") or (read_text(b + "/md/level") or "").upper()
                self.disks[name.encode()] = {
                    "name": name, "model": (model or "").strip(),
                    "size": int(read_text(b + "/size", "0")) * 512,
                    "rotational": read_text(b + "/queue/rotational") == "1",
                }
        self.prev = None
        self.prev_t = None
        self.mounts = []
        self.mounts_t = 0

    def _refresh_mounts(self):
        seen, out = set(), []
        for line in (read_text("/proc/self/mounts", "") or "").splitlines():
            src, target, fstype = line.split()[:3]
            if (not src.startswith("/dev/") or src.startswith("/dev/loop")
                    or fstype in self.SKIP_FS or src in seen):
                continue
            target = target.replace("\\040", " ")
            try:
                st = os.statvfs(target)
            except OSError:
                continue
            size = st.f_blocks * st.f_frsize
            if target != "/" and size < 2 * GIB:     # skip /boot/efi and other small ones
                continue
            seen.add(src)
            out.append((target, src, fstype))
        out.sort(key=lambda m: (m[0] != "/", m[0]))
        self.mounts = out
        self.mounts_t = time.monotonic()

    def sample(self):
        now = time.monotonic()
        cur = {}
        for line in self.stats.read().split(b"\n"):
            f = line.split()
            if len(f) > 12 and f[2] in self.disks:
                cur[f[2]] = (int(f[5]), int(f[9]), int(f[12]))   # sectors read, written, io_ticks ms
        disks = []
        if self.prev:
            dt = now - self.prev_t
            for name, (r, w, io) in cur.items():
                p = self.prev.get(name)
                if not p or dt <= 0:
                    continue
                d = dict(self.disks[name])
                d["read"] = (r - p[0]) * 512 / dt
                d["write"] = (w - p[1]) * 512 / dt
                d["busy"] = round(min(100.0, (io - p[2]) / (dt * 10)), 1)
                disks.append(d)
        self.prev, self.prev_t = cur, now
        if now - self.mounts_t > 60:
            self._refresh_mounts()
        fs = []
        for target, src, fstype in self.mounts:
            try:
                st = os.statvfs(target)
            except OSError:
                continue
            total = st.f_blocks * st.f_frsize
            free = st.f_bavail * st.f_frsize
            used = total - st.f_bfree * st.f_frsize
            fs.append({"mount": target, "dev": src, "fstype": fstype, "total": total,
                       "used": used, "free": free,
                       "pct": round(100.0 * used / (used + free), 1) if used + free else 0})
        return disks, fs


class NetCollector:
    """Physical NICs (have a device/ link) that are up; list rechecked every 10 s."""

    def __init__(self):
        self.files = {}
        self.prev = {}
        self.prev_t = None
        self.checked = 0

    def _refresh(self):
        up = {}
        for name in sorted(os.listdir("/sys/class/net")):
            b = f"/sys/class/net/{name}"
            if not os.path.exists(b + "/device") or read_text(b + "/operstate") != "up":
                continue
            old = self.files.get(name)
            speed = read_text(b + "/speed")
            try:
                speed = int(speed) if speed and int(speed) > 0 else None
            except ValueError:
                speed = None
            up[name] = {
                "rx": old["rx"] if old else SysFile(b + "/statistics/rx_bytes"),
                "tx": old["tx"] if old else SysFile(b + "/statistics/tx_bytes"),
                "speed": speed, "ip": iface_ipv4(name),
                "wireless": os.path.isdir(b + "/wireless"),
            }
        for name, old in self.files.items():
            if name not in up:
                os.close(old["rx"].fd)
                os.close(old["tx"].fd)
        self.files = up
        self.checked = time.monotonic()

    def sample(self):
        now = time.monotonic()
        if now - self.checked > 10:
            self._refresh()
        out, cur = [], {}
        dt = now - self.prev_t if self.prev_t else None
        for name, f in self.files.items():
            rx, tx = f["rx"].int(0), f["tx"].int(0)
            cur[name] = (rx, tx)
            p = self.prev.get(name)
            out.append({"name": name, "speed": f["speed"], "ip": f["ip"], "wireless": f["wireless"],
                        "rx": (rx - p[0]) / dt if p and dt else None,
                        "tx": (tx - p[1]) / dt if p and dt else None})
        self.prev, self.prev_t = cur, now
        return out


class TempCollector:
    """Every hwmon temperature input that reads a plausible value at start-up."""
    CPU_CHIPS = {"k10temp", "coretemp", "zenpower", "cpu_thermal"}

    def __init__(self):
        self.sensors = []
        counts = {}
        base = "/sys/class/hwmon"
        for hw in sorted(os.listdir(base), key=lambda s: int(re.sub(r"\D", "", s) or 0)):
            d = f"{base}/{hw}"
            chip = read_text(d + "/name", hw)
            inputs = sorted((f for f in os.listdir(d) if re.fullmatch(r"temp\d+_input", f)),
                            key=lambda f: int(re.sub(r"\D", "", f)))
            for inp in inputs:
                stem = inp[:-6]
                f = try_sysfile(f"{d}/{inp}")
                v = f.int() if f else None
                if v is None or not (-40000 < v < 150000):
                    continue
                label = read_text(f"{d}/{stem}_label")
                crit = None
                for lim in ("_crit", "_max"):
                    x = read_text(f"{d}/{stem}{lim}")
                    if x and x.lstrip("-").isdigit() and 30000 < int(x) < 150000:
                        crit = int(x) / 1000
                        break
                group, name = self._describe(chip, label, d, counts)
                # DIMM (SMBus) and NVMe sensors block for 1-3 ms per read: slow tier
                t = time.perf_counter()
                for _ in range(3):
                    f.int()
                slow = (time.perf_counter() - t) / 3 * 1000 > SLOW_CALL_MS
                self.sensors.append({"file": f, "chip": chip, "label": name, "group": group,
                                     "crit": crit, "slow": slow, "last": v / 1000})

    @staticmethod
    def _describe(chip, label, d, counts):
        if chip in TempCollector.CPU_CHIPS:
            if label in ("Tctl", "Package id 0"):
                return "cpu", "CPU package" + (f" ({label})" if label == "Tctl" else "")
            if label and label.startswith("Tccd"):
                return "cpu", f"CPU CCD{label[4:]}"
            return "cpu", f"CPU {label or chip}"
        if chip == "nvme":
            model = (read_text(d + "/device/model") or "").strip()
            dev = os.path.basename(os.path.realpath(d + "/device"))
            return "nvme", f"NVMe {dev}" + (f" {label}" if label and label != "Composite" else "") + (f" · {model}" if model else "")
        if chip in ("spd5118", "jc42", "ee1004"):
            counts[chip] = counts.get(chip, 0) + 1
            return "dimm", f"DIMM {counts[chip]}"
        if chip in ("amdgpu", "nouveau", "radeon", "i915", "xe"):
            return "igpu", f"{'iGPU' if chip != 'nouveau' else 'GPU'} {chip}" + (f" {label}" if label else "")
        if chip.startswith(("r8169", "igb", "ixgbe", "e1000", "mt7", "iwlwifi", "atlantic")):
            return "nic", f"NIC {chip.split('_')[0]}"
        return "other", f"{chip}" + (f" {label}" if label else "")

    def sample(self, slow_tick):
        out = []
        for s in self.sensors:
            if slow_tick or not s["slow"]:
                v = s["file"].int()
                s["last"] = None if v is None else v / 1000
            if s["last"] is not None:
                out.append({"label": s["label"], "group": s["group"], "c": s["last"],
                            "crit": s["crit"], "slow": s["slow"]})
        return out


class ProcInfo:
    """Name / RSS / command line for a PID, with the static parts cached."""

    def __init__(self):
        self.cache = {}

    @staticmethod
    def _read(path, size=4096):
        try:
            fd = os.open(path, os.O_RDONLY | os.O_CLOEXEC)
        except OSError:
            return None
        try:
            return os.read(fd, size)
        except OSError:
            return None
        finally:
            os.close(fd)

    def static(self, pid):
        c = self.cache.get(pid)
        if c is None:
            comm = (self._read(f"/proc/{pid}/comm") or b"?").decode(errors="replace").strip()
            cmd = (self._read(f"/proc/{pid}/cmdline", 1024) or b"").replace(b"\0", b" ")
            cmd = cmd.decode(errors="replace").strip()
            c = self.cache[pid] = (comm, (cmd[:300] or f"[{comm}]"))
        return c

    def rss(self, pid):
        s = self._read(f"/proc/{pid}/statm", 256)
        try:
            return int(s.split()[1]) * PAGE if s else None
        except (ValueError, IndexError):
            return None

    def prune(self, live):
        for pid in list(self.cache):
            if pid not in live:
                del self.cache[pid]


class TopProcs:
    """Top processes by CPU, from a /proc/*/stat scan (run on the slow tick)."""

    def __init__(self, procinfo, n=8):
        self.pi, self.n = procinfo, n
        self.prev = {}
        self.prev_t = None
        self.result = []
        self.proc_fd = os.open("/proc", os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)

    def scan(self):
        now = time.monotonic()
        cur = {}
        pfd, flags = self.proc_fd, os.O_RDONLY | os.O_CLOEXEC
        for name in os.listdir(pfd):
            if not name.isdigit():
                continue
            try:     # relative to an open /proc fd: no full path walk per process
                fd = os.open(name + "/stat", flags, dir_fd=pfd)
            except OSError:
                continue
            try:
                b = os.read(fd, 1024)
            except OSError:
                continue
            finally:
                os.close(fd)
            r = b.rfind(b")")
            f = b[r + 2:].split()
            try:
                # after "(comm) ": state=0, utime=11, stime=12, starttime=19, rss=21
                cur[(int(name), f[19])] = (int(f[11]) + int(f[12]), b[b.find(b"(") + 1:r], int(f[21]))
            except (IndexError, ValueError):
                continue
        if self.prev_t is not None:
            dt = now - self.prev_t
            deltas = []
            for k, (ticks, comm, rss) in cur.items():
                p = self.prev.get(k)
                if p is not None and ticks > p[0]:
                    deltas.append((ticks - p[0], k, comm, rss))
            deltas.sort(reverse=True)
            out = []
            for d, (pid, _), comm, rss in deltas[: self.n]:
                _, cmd = self.pi.static(pid)
                out.append({"pid": pid, "name": comm.decode(errors="replace"),
                            "cpu": round(100.0 * d / CLK_TCK / dt, 1), "rss": rss * PAGE, "cmd": cmd})
            self.result = out
        self.prev, self.prev_t = cur, now
        return self.result


class OllamaPoller(threading.Thread):
    """Polls /api/ps in its own thread so a slow or stopped Ollama can never
    delay a sample; the sampler just reads the latest result."""

    def __init__(self, url):
        super().__init__(daemon=True, name="ollama")
        u = urlsplit(url)
        self.url = url
        self.https = u.scheme == "https"
        self.host = u.hostname or "127.0.0.1"
        self.port = u.port or (443 if self.https else 11434)
        self.kick = threading.Event()
        self.result = {"ok": None, "models": [], "error": "not polled yet", "url": url}

    def run(self):
        while True:
            self.kick.wait()
            self.kick.clear()
            self.result = self.fetch()

    @staticmethod
    def _seconds_until(ts):
        if not ts:
            return None
        m = re.match(r"(.*T\d\d:\d\d:\d\d)(\.\d+)?(.*)$", ts)
        if not m:
            return None
        frac = (m.group(2) or "")[:7]          # Go emits nanoseconds; Python takes ≤ 6 digits
        tz = m.group(3) or "+00:00"
        tz = "+00:00" if tz == "Z" else tz
        try:
            return round(datetime.fromisoformat(m.group(1) + frac + tz).timestamp() - time.time())
        except ValueError:
            return None

    def fetch(self):
        cls = http.client.HTTPSConnection if self.https else http.client.HTTPConnection
        conn = cls(self.host, self.port, timeout=0.5)
        try:
            conn.request("GET", "/api/ps")
            r = conn.getresponse()
            body = r.read()
            if r.status != 200:
                return {"ok": False, "models": [], "error": f"HTTP {r.status}", "url": self.url}
            data = json.loads(body)
        except (OSError, ValueError, http.client.HTTPException) as e:
            return {"ok": False, "models": [], "error": str(e) or type(e).__name__, "url": self.url}
        finally:
            conn.close()
        models = []
        for m in data.get("models") or []:
            det = m.get("details") or {}
            models.append({
                "name": m.get("name") or m.get("model"),
                "size": m.get("size") or 0,
                "size_vram": m.get("size_vram") or 0,
                "ctx": m.get("context_length"),
                "params": det.get("parameter_size"),
                "quant": det.get("quantization_level"),
                "expires_in": self._seconds_until(m.get("expires_at")),
            })
        return {"ok": True, "models": models, "error": None, "url": self.url}


# ------------------------------------------------------------------ collector

class Collector:
    def __init__(self, ollama_url, interval):
        self.interval = interval
        self.topo = cpu_topology()
        self.nv, self.gpus, nvinfo, self.nvml_error = open_nvml()
        self.cpu = CpuCollector(self.topo["cpus"])
        self.mem = MemCollector()
        self.disk = DiskCollector()
        self.net = NetCollector()
        self.temps = TempCollector()
        self.procinfo = ProcInfo()
        self.top = TopProcs(self.procinfo)
        self.ollama = OllamaPoller(ollama_url)
        self.ollama.start()
        self.loadavg = SysFile("/proc/loadavg")
        self.uptime = SysFile("/proc/uptime")
        self.last_slow = 0.0
        self.static = {
            "host": os.uname().nodename,
            "kernel": os.uname().release,
            "cpu_model": cpu_model(),
            "cpu_threads": len(self.topo["cpus"]),
            "cpu_cores": self.topo["physical_cores"],
            "cpu_groups": self.topo["groups"],
            "smt": self.topo["smt"],
            "driver": nvinfo.get("driver"),
            "cuda": nvinfo.get("cuda"),
            "nvml_error": self.nvml_error,
            "gpus": [g.static for g in self.gpus],
            "interval": interval,
            "slow_period": SLOW_PERIOD,
        }

    def nvml_report(self):
        return [{"gpu": g.index, "timing_ms": g.timing_ms, "cpu_ms": g.cpu_ms,
                 "slow": sorted(g.slow), "every_other_slow_tick": sorted(g.slower),
                 "unsupported": g.disabled} for g in self.gpus]

    def sample(self):
        t0, c0 = time.perf_counter(), time.thread_time()
        now = time.monotonic()
        slow = now - self.last_slow >= SLOW_PERIOD - 0.05
        if slow:
            self.last_slow = now
            self.ollama.kick.set()
        s = {"t": round(time.time(), 3)}
        s["cpu"] = self.cpu.sample()
        la = self.loadavg.read(128).split()
        s["load"] = [float(x) for x in la[:3]]
        s["uptime"] = float(self.uptime.read(128).split()[0])
        s["mem"] = self.mem.sample()
        s["disks"], s["fs"] = self.disk.sample()
        s["net"] = self.net.sample()
        s["temps"] = self.temps.sample(slow)
        gpus = []
        live = set()
        for g in self.gpus:
            gs = g.sample(slow)
            for p in gs.get("procs", []):
                live.add(p["pid"])
                p["name"], p["cmd"] = self.procinfo.static(p["pid"])
                p["rss"] = self.procinfo.rss(p["pid"])
            gpus.append(gs)
        s["gpus"] = gpus
        s["nvml_error"] = self.nvml_error
        if slow:
            top = self.top.scan()
            self.procinfo.prune(live | {p["pid"] for p in top})
        s["top"] = self.top.result
        s["ollama"] = self.ollama.result
        s["d"] = self.dials(s)
        s["cost_ms"] = round((time.perf_counter() - t0) * 1000, 2)
        s["cpu_ms"] = round((time.thread_time() - c0) * 1000, 2)
        s["slow"] = slow
        return s

    @staticmethod
    def dials(s):
        """The percentages behind the dial row and sparklines."""
        d = {}
        for g in s["gpus"]:
            i = g["index"]
            d[f"g{i}_util"] = g.get("util")
            if g.get("vram_total"):
                d[f"g{i}_vram"] = round(100.0 * g["vram_used"] / g["vram_total"], 1)
            if g.get("power_limit_w") and g.get("power_w") is not None:
                d[f"g{i}_power"] = round(100.0 * g["power_w"] / g["power_limit_w"], 1)
            d[f"g{i}_watts"] = g.get("power_w")
        d["cpu"] = s["cpu"]["total"]
        m = s["mem"]
        d["ram"] = round(100.0 * m["used"] / m["total"], 1) if m["total"] else None
        d["disk"] = max((x["busy"] for x in s["disks"]), default=None)
        root = next((f for f in s["fs"] if f["mount"] == "/"), None)
        d["rootfs"] = root["pct"] if root else None
        # busiest NIC with a known link speed (Wi-Fi reports none); links are
        # full duplex, so each direction is measured against the full speed
        util = [100.0 * max(n["rx"] or 0, n["tx"] or 0) * 8 / (n["speed"] * 1e6)
                for n in s["net"] if n["speed"]]
        d["net"] = round(min(100.0, max(util)), 2) if util else None
        d["net_rx"] = sum(n["rx"] or 0 for n in s["net"])
        d["net_tx"] = sum(n["tx"] or 0 for n in s["net"])
        return d


# ----------------------------------------------------------------- streaming

class Hub:
    """Sampling thread + fan-out to SSE clients. Sampling runs only while at
    least one client is connected."""

    def __init__(self, collector):
        self.c = collector
        self.cond = threading.Condition()
        self.clients = 0
        self.seq = 0
        self.payload = None
        self.history = deque(maxlen=HISTORY)
        self.last_t = 0.0
        threading.Thread(target=self._loop, daemon=True, name="sampler").start()

    def add(self):
        with self.cond:
            self.clients += 1
            self.cond.notify_all()

    def remove(self):
        with self.cond:
            self.clients -= 1

    def _loop(self):
        interval = self.c.interval
        while True:
            with self.cond:
                while self.clients == 0:
                    self.cond.wait()
            now = time.monotonic()
            if now - self.last_t > 5 * interval:
                self.history.clear()        # a gap: sparklines would lie across it
            self.last_t = now
            s = self.c.sample()
            self.history.append(s["d"])
            data = json.dumps(s, separators=(",", ":")).encode()
            with self.cond:
                self.seq += 1
                self.payload = data
                self.cond.notify_all()
            next_t = now + interval
            delay = next_t - time.monotonic()
            if delay > 0:
                time.sleep(delay)

    def wait(self, seq, timeout):
        with self.cond:
            self.cond.wait_for(lambda: self.seq != seq, timeout)
            return self.seq, (self.payload if self.seq != seq else None)

    def info(self):
        return json.dumps(dict(self.c.static, history=list(self.history)), separators=(",", ":")).encode()


class Handler(BaseHTTPRequestHandler):
    server_version = "rtx-monitor/1.0"
    hub = None

    def log_message(self, fmt, *args):     # keep journald quiet
        pass

    def _send(self, code, body, ctype):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        path = urlsplit(self.path).path
        if path in ("/", "/index.html"):
            try:
                with open(os.path.join(HERE, "index.html"), "rb") as f:
                    body = f.read()
            except OSError as e:
                return self._send(500, str(e).encode(), "text/plain")
            return self._send(200, body, "text/html; charset=utf-8")
        if path == "/stream":
            return self._stream()
        if path == "/healthz":
            return self._send(200, b"ok\n", "text/plain")
        self._send(404, b"not found\n", "text/plain")

    def _stream(self):
        hub = self.hub
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()
        if self.command == "HEAD":
            return
        hub.add()
        try:
            self.wfile.write(b"retry: 2000\nevent: info\ndata: " + hub.info() + b"\n\n")
            seq = hub.seq
            while True:
                seq, data = hub.wait(seq, 15)
                self.wfile.write(b"data: " + data + b"\n\n" if data else b": ping\n\n")
        except OSError:          # BrokenPipe / ConnectionReset: the tab went away
            pass
        finally:
            hub.remove()


def main():
    ap = argparse.ArgumentParser(description="Low-overhead real-time resource dashboard")
    ap.add_argument("--host", default="0.0.0.0", help="bind address (default 0.0.0.0)")
    ap.add_argument("--port", type=int, default=8888, help="port (default 8888)")
    ap.add_argument("--interval", type=float, default=1.0, help="seconds between samples (default 1)")
    ap.add_argument("--ollama", default="http://127.0.0.1:11434", help="Ollama base URL")
    ap.add_argument("--once", action="store_true", help="print one sample as JSON and exit")
    a = ap.parse_args()
    a.interval = max(0.25, a.interval)

    c = Collector(a.ollama, a.interval)
    if a.once:
        c.sample()                            # primes the deltas and kicks the Ollama poll
        time.sleep(a.interval)
        c.last_slow = 0                       # make the second sample a full one
        s = c.sample()
        json.dump({"info": c.static, "nvml": c.nvml_report(), "sample": s}, sys.stdout, indent=1)
        print()
        return

    for r in c.nvml_report():
        print(f"GPU {r['gpu']}: NVML probe ms {r['timing_ms']}; slow tier {r['slow']}; "
              f"unsupported {list(r['unsupported'])}", flush=True)
    if c.nvml_error:
        print(f"NVML unavailable: {c.nvml_error}", flush=True)
    Handler.hub = Hub(c)
    srv = ThreadingHTTPServer((a.host, a.port), Handler)
    srv.daemon_threads = True
    print(f"rtx-monitor on http://{a.host}:{a.port}/  (interval {a.interval}s)", flush=True)
    try:
        srv.serve_forever(poll_interval=3600)   # select() wakes on connections anyway
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
