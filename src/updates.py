"""Best-effort PyPI release checks with a bounded startup wait."""

from __future__ import annotations

import json
import re
from http.client import HTTPException
from threading import Event, Thread
from urllib.request import Request, urlopen

PYPI_URL = "https://pypi.org/pypi/draintracer/json"
CHECK_TIMEOUT = 1.0
_MAX_RESPONSE_BYTES = 256 * 1024


def _release_version(value: object) -> tuple[int, int, int] | None:
    """Compare final SemVer components as integers, never decimal fractions."""
    if not isinstance(value, str):
        return None
    match = re.fullmatch(r"v?(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)", value)
    return tuple(map(int, match.groups())) if match else None


def check_for_update(current_version: str, *, timeout: float = CHECK_TIMEOUT) -> bool | None:
    """Return newer/current/unknown without holding up startup beyond timeout.

    The daemon worker never prints or changes application state. Bounding the
    caller's wait also covers DNS resolution, which socket timeouts may not.
    This function is only used by interactive startup, never monitoring.
    """
    current = _release_version(current_version)
    if current is None:
        return None
    finished = Event()
    result: list[bool | None] = [None]

    def fetch() -> None:
        try:
            request = Request(PYPI_URL, headers={
                "Accept": "application/json",
                "User-Agent": f"DrainTracer/{current_version}",
            })
            with urlopen(request, timeout=timeout) as response:
                payload = response.read(_MAX_RESPONSE_BYTES + 1)
            if len(payload) > _MAX_RESPONSE_BYTES:
                return
            metadata = json.loads(payload)
            info = metadata.get("info") if isinstance(metadata, dict) else None
            if not isinstance(info, dict) or info.get("yanked", False) is not False:
                return
            latest = _release_version(info.get("version"))
            if latest is not None:
                result[0] = latest > current
        except (OSError, ValueError, HTTPException):
            # Offline, TLS, HTTP, and malformed-response failures are expected.
            pass
        finally:
            finished.set()

    try:
        Thread(target=fetch, name="draintracer-update", daemon=True).start()
    except RuntimeError:
        return None
    return result[0] if finished.wait(timeout) else None
