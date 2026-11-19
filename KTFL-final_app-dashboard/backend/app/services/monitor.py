"""Structured stage logging with RAM / GPU figures (Phase 8). psutil and pynvml are optional: when they are not
installed or no NVIDIA GPU is present the figures are simply left out. Credentials are never logged."""
from __future__ import annotations

import json
import logging
import time
from contextlib import contextmanager

log = logging.getLogger("clearmatch.metrics")

try:                                    # optional, approved packages
    import psutil
except Exception:                       # pragma: no cover
    psutil = None
try:
    import pynvml
    pynvml.nvmlInit()
except Exception:                       # no GPU / no driver / package missing
    pynvml = None

_SENSITIVE = ("password", "secret", "token", "authorization", "jwt", "key")


def resources() -> dict:
    out: dict = {}
    if psutil is not None:
        try:
            out["rss_mb"] = round(psutil.Process().memory_info().rss / 1e6, 1)
            out["ram_used_pct"] = psutil.virtual_memory().percent
        except Exception:
            pass
    if pynvml is not None:
        try:
            h = pynvml.nvmlDeviceGetHandleByIndex(0)
            mem = pynvml.nvmlDeviceGetMemoryInfo(h)
            out["gpu_mem_used_mb"] = round(mem.used / 1e6)
            out["gpu_mem_total_mb"] = round(mem.total / 1e6)
            out["gpu_util_pct"] = pynvml.nvmlDeviceGetUtilizationRates(h).gpu
        except Exception:
            pass
    return out


def _safe(fields: dict) -> dict:
    return {k: ("***" if any(s in k.lower() for s in _SENSITIVE) else v) for k, v in fields.items()}


def event(stage: str, **fields) -> None:
    """One JSON log line: stage, ids, durations, counts, resources. Sensitive keys are masked."""
    log.info(json.dumps(_safe({"stage": stage, **fields, **resources()}), default=str))


@contextmanager
def timed(stage: str, **fields):
    t0 = time.perf_counter()
    err = None
    try:
        yield
    except Exception as exc:
        err = f"{type(exc).__name__}: {str(exc)[:200]}"
        raise
    finally:
        event(stage, duration_ms=round((time.perf_counter() - t0) * 1000), error_type=err and err.split(":")[0],
              error=err, **fields)
