"""Bounded Apple GPU statistics from ioreg: no helper to install, about 25 ms per read."""
from __future__ import annotations

import re
import subprocess
from typing import Any

_IOREG = "/usr/sbin/ioreg"
_MAX_OUTPUT = 1 << 20
_STAT = re.compile(r'"PerformanceStatistics" = \{([^}]*)\}')
_PAIR = re.compile(r'"([^"]{1,64})"=(\d{1,20})')
_MODEL = re.compile(r'"model" = "([^"]{1,64})"')
_CORES = re.compile(r'"gpu-core-count" = (\d{1,4})')


def _percent(stats: dict[str, int], key: str) -> int | None:
    value = stats.get(key)
    return value if value is not None and 0 <= value <= 100 else None


def _bytes(stats: dict[str, int], key: str) -> int | None:
    value = stats.get(key)
    return value if value is not None and 0 <= value <= 1 << 50 else None


def parse(output: str) -> dict[str, Any]:
    """The first accelerator's utilisation and memory; unknown fields stay None."""
    match = _STAT.search(output)
    if not match:
        raise ValueError("no GPU performance statistics")
    stats = {key: int(value) for key, value in _PAIR.findall(match.group(1))}
    model = _MODEL.search(output)
    cores = _CORES.search(output)
    return {
        "model": model.group(1) if model else None,
        "cores": int(cores.group(1)) if cores and 0 < int(cores.group(1)) <= 1024 else None,
        "utilizationPercent": _percent(stats, "Device Utilization %"),
        "rendererPercent": _percent(stats, "Renderer Utilization %"),
        "tilerPercent": _percent(stats, "Tiler Utilization %"),
        "allocatedBytes": _bytes(stats, "Alloc system memory"),
        "inUseBytes": _bytes(stats, "In use system memory"),
    }


def mac_gpu(runner=subprocess.run) -> tuple[dict[str, Any], dict[str, Any]]:
    """(gpu, source) for the snapshot; the GPU is None whenever the read fails or looks wrong."""
    try:
        result = runner([_IOREG, "-r", "-d", "1", "-w", "0", "-c", "IOAccelerator"],
                        capture_output=True, text=True, timeout=1.5, check=False)
        if result.returncode != 0 or len(result.stdout) > _MAX_OUTPUT:
            raise ValueError("ioreg unavailable")
        gpu = parse(result.stdout)
        gpu["ageSeconds"] = 0.0
        return gpu, {"id": "mac-gpu", "label": "Mac GPU", "state": "live", "ageSeconds": 0.0,
                     "detail": "GPU utilisation and memory sampled from ioreg"}
    except Exception:
        return None, {"id": "mac-gpu", "label": "Mac GPU", "state": "unavailable", "ageSeconds": None,
                      "detail": "GPU statistics unavailable"}
