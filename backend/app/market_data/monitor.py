import os
import shutil
from collections import deque
from pathlib import Path
from time import monotonic

from app.config import DATA_DIR

WARN_FREE = int(os.getenv("DISK_WARN_MIB", 2048)) * 1024**2
STOP_FREE = int(os.getenv("DISK_STOP_MIB", 512)) * 1024**2
MAX_DATA = int(os.getenv("DATA_MAX_MIB", 10240)) * 1024**2
HOST_PROBE = Path(os.environ["DISK_PROBE_PATH"]) if os.getenv("DISK_PROBE_PATH") else None


class RequestMetrics:
    def __init__(self):
        self.events = deque(maxlen=20000)

    def record(self, error, elapsed_ms):
        self.events.append((monotonic(), bool(error), elapsed_ms))

    def snapshot(self):
        cutoff = monotonic() - 300
        samples = [(error, elapsed) for stamp, error, elapsed in tuple(self.events) if stamp >= cutoff]
        errors = sum(error for error, _ in samples)
        return dict(requests=len(samples), errors=errors,
                    error_rate=errors / len(samples) if samples else None,
                    latency_ms=round(sum(elapsed for _, elapsed in samples) / len(samples), 2) if samples else None)


def disk_status(database_bytes, file_bytes):
    free = shutil.disk_usage(DATA_DIR).free
    host = shutil.disk_usage(HOST_PROBE).free if HOST_PROBE else None
    effective = min(free, host) if host is not None else free
    remaining = MAX_DATA - database_bytes - file_bytes
    paused = min(effective, remaining) <= STOP_FREE
    return dict(disk_free_bytes=free, host_probe_free_bytes=host, quota_remaining_bytes=remaining,
                disk_alert="critical" if paused else "warning" if min(effective, remaining) <= WARN_FREE else "ok",
                collection_paused=paused)

