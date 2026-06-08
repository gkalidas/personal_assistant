"""System resource snapshot — CPU, RAM, disk, uptime."""

import os
import time
import psutil


def get_system_stats() -> dict:
    cpu_pct   = psutil.cpu_percent(interval=0.5)
    ram       = psutil.virtual_memory()
    disk      = psutil.disk_usage("/")
    load1, load5, load15 = os.getloadavg()
    freq      = psutil.cpu_freq()

    uptime_s  = int(time.time() - psutil.boot_time())
    h, rem    = divmod(uptime_s, 3600)
    m         = rem // 60

    return {
        "cpu_pct":      round(cpu_pct, 1),
        "cpu_cores":    psutil.cpu_count(),
        "cpu_freq_mhz": round(freq.current) if freq else 0,
        "ram_pct":      round(ram.percent, 1),
        "ram_used_gb":  round(ram.used  / 1e9, 1),
        "ram_total_gb": round(ram.total / 1e9, 1),
        "disk_pct":     round(disk.percent, 1),
        "disk_used_gb": round(disk.used  / 1e9, 1),
        "disk_total_gb":round(disk.total / 1e9, 1),
        "uptime":       f"{h}h {m}m",
        "load_avg":     f"{load1:.2f}  {load5:.2f}  {load15:.2f}",
    }
