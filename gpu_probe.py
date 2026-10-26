"""Bounded Apple GPU statistics from ioreg: no helper to install, about 25 ms per read.

The unified-memory ceiling (Metal's recommendedMaxWorkingSetSize: how much of the shared memory the
GPU may use) is only readable from a Metal client, so the app shell reads it once at launch and passes
it in AGIW_GPU_WORKING_SET_BYTES. Run without the shell, or with a bad value, it is unknown, not 0."""
from __future__ import annotations

import os
import re
import subprocess
from typing import Any, Mapping

_IOREG = "/usr/sbin/ioreg"
CEILING_ENV = "AGIW_GPU_WORKING_SET_BYTES"
CEILING_SOURCE = "Metal recommendedMaxWorkingSetSize (read by the app shell)"
_CEILING_MIN = 1 << 30   # every Apple-silicon Mac reports several GiB; less means a wrong value
_CEILING_MAX = 1 << 50
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


def working_set_ceiling(environ: Mapping[str, str] | None = None) -> tuple[int | None, str]:
    """(bytes, source) for the unified-memory ceiling; (None, why) when the shell did not supply a valid one."""
    raw = (os.environ if environ is None else environ).get(CEILING_ENV)
    if raw is None:
        return None, "unknown: not passed in by the app shell"
    if not re.fullmatch(r"[1-9][0-9]{0,15}", raw) or not _CEILING_MIN <= int(raw) <= _CEILING_MAX:
        return None, "unknown: the app shell passed an invalid value"
    return int(raw), CEILING_SOURCE


def mac_gpu(runner=subprocess.run, environ: Mapping[str, str] | None = None) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    """(gpu, source) for the snapshot; a known Metal ceiling survives an unavailable ioreg read."""
    ceiling, ceiling_source = working_set_ceiling(environ)
    try:
        result = runner([_IOREG, "-r", "-d", "1", "-w", "0", "-c", "IOAccelerator"],
                        capture_output=True, text=True, timeout=1.5, check=False)
        if result.returncode != 0 or len(result.stdout) > _MAX_OUTPUT:
            raise ValueError("ioreg unavailable")
        gpu = parse(result.stdout)
        gpu["ageSeconds"] = 0.0
        gpu["unifiedMemoryCeilingBytes"] = ceiling
        gpu["unifiedMemoryCeilingSource"] = ceiling_source
        return gpu, {"id": "mac-gpu", "label": "Mac GPU", "state": "live", "ageSeconds": 0.0,
                     "detail": "GPU utilisation and memory sampled from ioreg; unified-memory ceiling "
                     + ("from Metal (app shell)" if ceiling is not None else ceiling_source)}
    except Exception:
        if ceiling is not None:
            gpu = {"model": None, "cores": None, "utilizationPercent": None, "rendererPercent": None,
                   "tilerPercent": None, "allocatedBytes": None, "inUseBytes": None, "ageSeconds": None,
                   "unifiedMemoryCeilingBytes": ceiling, "unifiedMemoryCeilingSource": ceiling_source}
            return gpu, {"id": "mac-gpu", "label": "Mac GPU", "state": "unavailable", "ageSeconds": None,
                         "detail": "GPU statistics from ioreg unavailable; unified-memory ceiling from Metal (app shell)"}
        return None, {"id": "mac-gpu", "label": "Mac GPU", "state": "unavailable", "ageSeconds": None,
                      "detail": "GPU statistics unavailable"}
