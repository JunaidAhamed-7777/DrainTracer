"""Project settings and profiles from ``.drainrc`` and related TOML files.

Settings resolve in this order, later layers winning:

1. built-in defaults,
2. the base settings of the config file,
3. the selected profile (``--profile`` or the file's ``default_profile``),
4. command-line flags.

The file is found by ``--config`` or by searching the working directory and
its parents for ``.drainrc``, ``.drainrc.toml``, or ``draintracer.toml``; a
shared ``config.toml`` counts only when it has a ``[draintracer]`` table.
In any file the settings may sit at the top level or in ``[draintracer]``.
"""

from __future__ import annotations

import difflib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from types import MappingProxyType
from typing import Any

from .events import LeakResource, LeakSeverity
from .leak_detector import (
    DEFAULT_SAMPLE_INTERVAL_MS,
    DEFAULT_THRESHOLDS,
    DEFAULT_WINDOWS_SECONDS,
    ResourceThresholds,
)
from .report import DEFAULT_REPORT_DIR
from .units import format_duration, parse_count, parse_duration, parse_rate, parse_size

try:  # Python 3.11+
    import tomllib as _toml
except ModuleNotFoundError:  # Python 3.10 reads TOML through the tomli backport.
    try:
        import tomli as _toml  # type: ignore[no-redef]
    except ModuleNotFoundError:
        _toml = None  # type: ignore[assignment]

CONFIG_FILENAMES = (".drainrc", ".drainrc.toml", "draintracer.toml")
SHARED_CONFIG_FILENAMES = ("config.toml",)
FAIL_ON_CHOICES = ("low", "medium", "high", "critical")

MIN_SAMPLE_INTERVAL = 0.1
MAX_SAMPLE_INTERVAL = 60.0
MAX_WINDOW = 3600.0
MAX_WINDOWS = 8
MAX_WARMUP = 86400.0
MIN_SAMPLES_PER_WINDOW = 4
# A window is evaluated once its samples span this share of it.
WINDOW_COVERAGE = 0.85

_LAYER_KEYS = (
    "sample_interval",
    "windows",
    "warmup",
    "timeout",
    "fail_on",
    "report_dir",
    "thresholds",
)
_ROOT_KEYS = (*_LAYER_KEYS, "profiles", "default_profile")
_THRESHOLD_KEYS = ("critical_delta", "critical_slope")


class ConfigError(ValueError):
    """A configuration file or setting is invalid."""


@dataclass(frozen=True)
class MonitorSettings:
    """Resolved monitoring settings and where they came from."""

    sample_interval_ms: int = DEFAULT_SAMPLE_INTERVAL_MS
    windows: tuple[float, ...] = DEFAULT_WINDOWS_SECONDS
    thresholds: Mapping[LeakResource, ResourceThresholds] = DEFAULT_THRESHOLDS
    warmup: float = 0.0
    timeout: float | None = None
    fail_on: LeakSeverity = LeakSeverity.HIGH
    report_dir: str = DEFAULT_REPORT_DIR
    profile: str | None = None
    source: Path | None = None

    def describe(self) -> str:
        """Short origin line such as ``profile ci from C:\\proj\\.drainrc``."""
        if self.source is None:
            return "built-in defaults"
        if self.profile:
            return f"profile {self.profile} from {self.source}"
        return f"settings from {self.source}"

    def warnings(self) -> list[str]:
        """Settings that are valid but cannot do what was probably meant."""
        if self.timeout is None:
            return []
        detecting = self.timeout - self.warmup
        needed = self.windows[0] * WINDOW_COVERAGE
        if detecting >= needed:
            return []
        after = f" after the {format_duration(self.warmup)} warm-up" if self.warmup else ""
        return [
            f"the {format_duration(self.timeout)} time limit leaves "
            f"{format_duration(detecting)} of leak detection{after}, but even the "
            f"{format_duration(self.windows[0])} window needs {format_duration(needed)} "
            "of samples, so no leak signal can be raised"
        ]


def resolve_settings(
    *,
    config_path: str | Path | None = None,
    profile: str | None = None,
    discover: bool = True,
    start: Path | None = None,
    overrides: Mapping[str, Any] | None = None,
) -> MonitorSettings:
    """Combine defaults, a config file, a profile, and flag overrides.

    ``overrides`` uses ``MonitorSettings`` field names (``sample_interval_ms``,
    ``windows``, ``warmup``, ``timeout``, ``fail_on``, ``report_dir``). Raises
    ``ConfigError`` with the file and setting named on any problem.
    """
    settings = MonitorSettings()
    path: Path | None = None
    if config_path is not None:
        path = Path(config_path)
    elif discover:
        try:
            path = find_config(start if start is not None else Path.cwd())
        except OSError:
            path = None  # an unreadable working directory just means no config

    if path is not None:
        try:
            settings = _apply_file(settings, load_config_file(path), profile, path.parent)
        except ConfigError as exc:
            raise ConfigError(f"{path}: {exc}") from None
        settings = replace(settings, source=path)
    elif profile is not None:
        raise ConfigError(
            f"--profile {profile} needs a config file, but no "
            f"{', '.join(CONFIG_FILENAMES)} was found here or in a parent directory"
        )

    if overrides:
        settings = _apply(settings, overrides)
    _check_consistency(settings)
    return settings


def find_config(start: Path) -> Path | None:
    """Return the nearest config file in ``start`` or one of its parents."""
    directory = start.resolve()
    for folder in (directory, *directory.parents):
        for name in CONFIG_FILENAMES:
            candidate = folder / name
            if _is_file(candidate):
                return candidate
        for name in SHARED_CONFIG_FILENAMES:
            candidate = folder / name
            if _is_file(candidate) and _has_draintracer_table(candidate):
                return candidate
    return None


def load_config_file(path: Path) -> Mapping[str, Any]:
    """Parse a TOML config file and return its DrainTracer settings table."""
    if _toml is None:
        raise ConfigError(
            "reading config files needs Python 3.11+ or the tomli package (pip install tomli)"
        )
    try:
        with open(path, "rb") as handle:
            document = _toml.load(handle)
    except OSError as exc:
        raise ConfigError(f"cannot read the file: {exc.strerror or exc}") from None
    except _toml.TOMLDecodeError as exc:
        raise ConfigError(f"not valid TOML: {exc}") from None
    section = document.get("draintracer")
    if section is None:
        return document
    if not isinstance(section, dict):
        raise ConfigError("[draintracer] must be a table")
    return section


def _apply_file(
    settings: MonitorSettings, root: Mapping[str, Any], profile: str | None, folder: Path
) -> MonitorSettings:
    _reject_unknown(root, _ROOT_KEYS, "")
    base = {key: value for key, value in root.items() if key in _LAYER_KEYS}
    settings = _apply(settings, _parse_layer(base, "", folder))

    profiles = root.get("profiles", {})
    if not isinstance(profiles, dict) or not all(isinstance(v, dict) for v in profiles.values()):
        raise ConfigError("profiles must be tables, as in [profiles.ci]")
    default = root.get("default_profile")
    if default is not None and (not isinstance(default, str) or default not in profiles):
        raise ConfigError(f"default_profile {default!r} is not one of the defined profiles")

    chosen = profile if profile is not None else default
    if chosen is None:
        return settings
    if chosen not in profiles:
        available = ", ".join(sorted(profiles)) or "none are defined"
        raise ConfigError(f"profile {chosen!r} is not defined (available: {available})")
    settings = _apply(settings, _parse_layer(profiles[chosen], f"profiles.{chosen}.", folder))
    return replace(settings, profile=chosen)


def _parse_layer(table: Mapping[str, Any], where: str, folder: Path) -> dict[str, Any]:
    """Validate one settings table into ``MonitorSettings`` overrides.

    A relative ``report_dir`` is resolved against ``folder``, the directory
    of the settings file, so reports land in the same place wherever the
    command runs.
    """
    _reject_unknown(table, _LAYER_KEYS, where)
    overrides: dict[str, Any] = {}
    if "sample_interval" in table:
        seconds = _duration(table["sample_interval"], f"{where}sample_interval")
        if not MIN_SAMPLE_INTERVAL <= seconds <= MAX_SAMPLE_INTERVAL:
            raise ConfigError(
                f"{where}sample_interval must be between 100ms and 1m, "
                f"got {format_duration(seconds)}"
            )
        overrides["sample_interval_ms"] = round(seconds * 1000)
    if "windows" in table:
        raw = table["windows"]
        if not isinstance(raw, list) or not raw:
            raise ConfigError(f'{where}windows must be a list such as ["10s", "30s", "60s"]')
        windows = tuple(sorted({_duration(item, f"{where}windows") for item in raw}))
        if len(windows) > MAX_WINDOWS:
            raise ConfigError(f"{where}windows may list at most {MAX_WINDOWS} durations")
        for window in windows:
            if not 0 < window <= MAX_WINDOW:
                raise ConfigError(
                    f"{where}windows must be longer than 0s and at most 1h, "
                    f"got {format_duration(window)}"
                )
        overrides["windows"] = windows
    if "warmup" in table:
        seconds = _duration(table["warmup"], f"{where}warmup")
        if seconds > MAX_WARMUP:
            raise ConfigError(f"{where}warmup must be at most 24h, got {format_duration(seconds)}")
        overrides["warmup"] = seconds
    if "timeout" in table:
        seconds = _duration(table["timeout"], f"{where}timeout")
        if seconds <= 0:
            raise ConfigError(f"{where}timeout must be greater than zero")
        overrides["timeout"] = seconds
    if "fail_on" in table:
        value = table["fail_on"]
        if not isinstance(value, str) or value.lower() not in FAIL_ON_CHOICES:
            raise ConfigError(
                f"{where}fail_on must be one of {', '.join(FAIL_ON_CHOICES)}, got {value!r}"
            )
        overrides["fail_on"] = LeakSeverity(value.lower())
    if "report_dir" in table:
        value = table["report_dir"]
        if not isinstance(value, str) or not value.strip():
            raise ConfigError(f"{where}report_dir must be a non-empty path")
        overrides["report_dir"] = str(folder / value)
    if "thresholds" in table:
        overrides["thresholds"] = _parse_thresholds(table["thresholds"], f"{where}thresholds.")
    return overrides


def _parse_thresholds(table: Any, where: str) -> dict[LeakResource, dict[str, float]]:
    if not isinstance(table, dict):
        raise ConfigError(f"{where[:-1]} must be a table such as [thresholds.memory]")
    _reject_unknown(table, [resource.value for resource in LeakResource], where)
    parsed: dict[LeakResource, dict[str, float]] = {}
    for name, fields in table.items():
        resource = LeakResource(name)
        if not isinstance(fields, dict):
            raise ConfigError(f"{where}{name} must be a table such as [{where}{name}]")
        _reject_unknown(fields, _THRESHOLD_KEYS, f"{where}{name}.")
        amount = parse_size if resource is LeakResource.MEMORY else parse_count
        values: dict[str, float] = {}
        for key in _THRESHOLD_KEYS:
            if key not in fields:
                continue
            try:
                value = (
                    amount(fields[key])
                    if key == "critical_delta"
                    else parse_rate(fields[key], amount)
                )
            except ValueError as exc:
                raise ConfigError(f"{where}{name}.{key}: {exc}") from None
            if not value > 0:
                raise ConfigError(f"{where}{name}.{key} must be greater than zero")
            values[key] = value
        parsed[resource] = values
    return parsed


def _apply(settings: MonitorSettings, overrides: Mapping[str, Any]) -> MonitorSettings:
    values = dict(overrides)
    partial = values.pop("thresholds", None)
    if partial:
        merged = dict(settings.thresholds)
        for resource, fields in partial.items():
            merged[resource] = replace(merged[resource], **fields)
        values["thresholds"] = MappingProxyType(merged)
    return replace(settings, **values)


def _check_consistency(settings: MonitorSettings) -> None:
    if settings.timeout is not None and settings.warmup and settings.timeout <= settings.warmup:
        raise ConfigError(
            f"the {format_duration(settings.timeout)} time limit ends before the "
            f"{format_duration(settings.warmup)} warm-up does, so leak detection would never run"
        )
    interval = settings.sample_interval_ms / 1000
    shortest = settings.windows[0]
    needed = MIN_SAMPLES_PER_WINDOW * interval
    if shortest < needed:
        raise ConfigError(
            f"the {format_duration(shortest)} window is too short for a "
            f"{format_duration(interval)} sample interval; each window needs at least "
            f"{MIN_SAMPLES_PER_WINDOW} samples ({format_duration(needed)})"
        )


def _duration(value: Any, name: str) -> float:
    try:
        return parse_duration(value)
    except ValueError as exc:
        raise ConfigError(f"{name}: {exc}") from None


def _reject_unknown(table: Mapping[str, Any], allowed: Sequence[str], where: str) -> None:
    for key in table:
        if key in allowed:
            continue
        close = difflib.get_close_matches(str(key), allowed, n=1)
        hint = f" (did you mean {where}{close[0]}?)" if close else ""
        raise ConfigError(f"unknown setting {where}{key}{hint}")


def _is_file(path: Path) -> bool:
    try:
        return path.is_file()
    except OSError:
        return False


def _has_draintracer_table(path: Path) -> bool:
    if _toml is None:
        return False
    try:
        with open(path, "rb") as handle:
            return isinstance(_toml.load(handle).get("draintracer"), dict)
    except (OSError, _toml.TOMLDecodeError):
        # A shared config.toml that is not ours (or not TOML) is ignored.
        return False


__all__ = [
    "MAX_WARMUP",
    "CONFIG_FILENAMES",
    "ConfigError",
    "FAIL_ON_CHOICES",
    "MonitorSettings",
    "find_config",
    "load_config_file",
    "resolve_settings",
]
