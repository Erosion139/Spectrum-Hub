"""CPU, RAM and GPU numbers for the dashboard (sampled every 2 s)."""

from __future__ import annotations

import time
from collections import deque
from typing import Any

import psutil

try:
    import pynvml  # from nvidia-ml-py

    pynvml.nvmlInit()
    _gpu_ok = True
except Exception:  # noqa: BLE001 - no NVIDIA driver/runtime in the container
    pynvml = None
    _gpu_ok = False

history: deque[dict[str, Any]] = deque(maxlen=180)  # 6 minutes
_last_net = psutil.net_io_counters()
_last_t = time.time()


def gpus() -> list[dict[str, Any]]:
    if not _gpu_ok:
        return []
    out = []
    for i in range(pynvml.nvmlDeviceGetCount()):
        h = pynvml.nvmlDeviceGetHandleByIndex(i)
        mem = pynvml.nvmlDeviceGetMemoryInfo(h)
        util = pynvml.nvmlDeviceGetUtilizationRates(h)
        name = pynvml.nvmlDeviceGetName(h)
        try:
            temp = pynvml.nvmlDeviceGetTemperature(h, pynvml.NVML_TEMPERATURE_GPU)
        except Exception:  # noqa: BLE001
            temp = None
        try:
            power = pynvml.nvmlDeviceGetPowerUsage(h) / 1000
        except Exception:  # noqa: BLE001
            power = None
        try:
            procs = pynvml.nvmlDeviceGetComputeRunningProcesses(h)
            proc_mem = sum((p.usedGpuMemory or 0) for p in procs)
        except Exception:  # noqa: BLE001
            procs, proc_mem = [], 0
        out.append({
            "index": i,
            "name": name.decode() if isinstance(name, bytes) else name,
            "util": util.gpu,
            "mem_used": mem.used,
            "mem_total": mem.total,
            "temp_c": temp,
            "power_w": power,
            "processes": len(procs),
            "process_mem": proc_mem,
        })
    return out


def sample() -> dict[str, Any]:
    global _last_net, _last_t
    vm = psutil.virtual_memory()
    net = psutil.net_io_counters()
    now = time.time()
    dt = max(0.1, now - _last_t)
    s = {
        "t": now,
        "cpu": psutil.cpu_percent(interval=None),
        "cpu_count": psutil.cpu_count(),
        "load": psutil.getloadavg()[0] if hasattr(psutil, "getloadavg") else None,
        "ram_used": vm.total - vm.available,
        "ram_total": vm.total,
        "net_rx": (net.bytes_recv - _last_net.bytes_recv) / dt,
        "net_tx": (net.bytes_sent - _last_net.bytes_sent) / dt,
        "gpus": gpus(),
    }
    _last_net, _last_t = net, now
    history.append(s)
    return s
