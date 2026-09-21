"""Transactional scheduler configuration storage and legacy migration."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import secrets
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from src.core.database import PROJECT_ROOT, StateDatabase, Transaction

from .repository import Repository

LEGACY_SOURCE_NAME = "schedule-config-json-v1"
MAX_LEGACY_FILE_BYTES = 256_000
MAX_SCHEDULE_GROUPS = 1_000
MAX_SCHEDULE_TIMES = 48
DEFAULT_TIMEZONE = "Asia/Shanghai"
_ABSENT_DIGEST = hashlib.sha256(b"").hexdigest()
_TIME_PATTERN = re.compile(r"(?:[01][0-9]|2[0-3]):[0-5][0-9]")
_TIMEZONE_PATTERN = re.compile(r"[A-Za-z0-9_+.-]+(?:/[A-Za-z0-9_+.-]+)+")
_PORTABLE_TIMEZONES = {"UTC", "Etc/UTC", DEFAULT_TIMEZONE}
_KNOWN_KEYS = {
    "enabled",
    "warm_cache_enabled",
    "alerts_enabled",
    "timezone",
    "groups",
    "times",
}


class ScheduleRepositoryError(RuntimeError):
    """Base class for scheduler persistence errors."""


class LegacyScheduleImportError(ScheduleRepositoryError):
    """The legacy schedule file exists but failed strict validation."""


class _StrictJSONError(ValueError):
    pass


class ScheduleNotInitializedError(ScheduleRepositoryError):
    """No scheduler settings row exists (usually because initialization failed)."""


class ScheduleVersionConflictError(ScheduleRepositoryError):
    """An optimistic update used a stale configuration version."""

    def __init__(self, expected_version: int, actual_version: int | None) -> None:
        self.expected_version = expected_version
        self.actual_version = actual_version
        super().__init__(
            f"scheduler version conflict: expected {expected_version}, actual {actual_version}"
        )


@dataclass(frozen=True, slots=True)
class ScheduleConfig:
    enabled: bool
    warm_cache_enabled: bool
    alerts_enabled: bool
    timezone: str
    groups: tuple[int, ...]
    times: tuple[str, ...]
    version: int
    updated_at: int

    def as_dict(self) -> dict[str, object]:
        """Return the plugin-compatible JSON representation without metadata."""

        return {
            "enabled": self.enabled,
            "warm_cache_enabled": self.warm_cache_enabled,
            "alerts_enabled": self.alerts_enabled,
            "timezone": self.timezone,
            "groups": [str(group_id) for group_id in self.groups],
            "times": list(self.times),
        }


@dataclass(frozen=True, slots=True)
class _ScheduleValues:
    enabled: bool = True
    warm_cache_enabled: bool = True
    alerts_enabled: bool = True
    timezone: str = DEFAULT_TIMEZONE
    groups: tuple[int, ...] = ()
    times: tuple[str, ...] = ("08:00", "12:00", "15:00", "17:30", "20:00")


def _strict_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise _StrictJSONError("duplicate JSON object key")
        result[key] = value
    return result


def _reject_json_constant(value: str) -> Any:
    del value
    raise _StrictJSONError("non-finite JSON number")


def _backup_invalid_source(path: Path, payload: bytes) -> None:
    """Durably preserve exact invalid bytes without disclosing the source path."""

    digest = hashlib.sha256(payload).hexdigest()
    backup = path.with_name(f"{path.name}.invalid-{digest[:16]}.bak")
    temporary = path.with_name(f".{path.name}.{secrets.token_hex(8)}.tmp")
    try:
        with temporary.open("xb") as file:
            file.write(payload)
            file.flush()
            os.fsync(file.fileno())
        os.replace(temporary, backup)
    except OSError as exc:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
        raise LegacyScheduleImportError(
            "invalid legacy schedule could not be backed up safely"
        ) from exc


def _verify_legacy_source(path: Path, source_state: str, digest: str) -> None:
    """Recheck the validated source while the database write lock is held."""

    try:
        payload = path.read_bytes()
    except FileNotFoundError:
        if source_state == "absent":
            return
        raise LegacyScheduleImportError("legacy schedule changed during migration") from None
    except OSError as exc:
        raise LegacyScheduleImportError("legacy schedule changed during migration") from exc
    if source_state == "absent" or hashlib.sha256(payload).hexdigest() != digest:
        raise LegacyScheduleImportError("legacy schedule changed during migration")


def _strict_bool(value: object, *, field: str, default: bool) -> bool:
    if value is None:
        return default
    if not isinstance(value, bool):
        raise ValueError(f"{field} must be a boolean")
    return value


def _timezone(value: object) -> str:
    if not isinstance(value, str):
        raise ValueError("timezone must be a string")
    result = value.strip()
    if not 1 <= len(result) <= 100:
        raise ValueError("timezone must contain between 1 and 100 characters")
    if result in _PORTABLE_TIMEZONES:
        return result
    if not _TIMEZONE_PATTERN.fullmatch(result) or ".." in result:
        raise ValueError("timezone must be a valid IANA timezone name")
    try:
        ZoneInfo(result)
    except ZoneInfoNotFoundError as exc:
        raise ValueError("timezone must be installed and recognized") from exc
    return result


def _groups(value: object) -> tuple[int, ...]:
    if not isinstance(value, list | tuple):
        raise ValueError("groups must be an array")
    if len(value) > MAX_SCHEDULE_GROUPS:
        raise ValueError(f"groups cannot contain more than {MAX_SCHEDULE_GROUPS} entries")
    result: list[int] = []
    seen: set[int] = set()
    for raw_group in value:
        if isinstance(raw_group, bool):
            raise ValueError("group ids must be positive integers")
        if isinstance(raw_group, int):
            group_id = raw_group
        elif isinstance(raw_group, str) and raw_group.isascii() and raw_group.isdecimal():
            group_id = int(raw_group)
        else:
            raise ValueError("group ids must be positive integers")
        if group_id <= 0:
            raise ValueError("group ids must be positive integers")
        if group_id not in seen:
            seen.add(group_id)
            result.append(group_id)
    return tuple(result)


def _times(value: object) -> tuple[str, ...]:
    if not isinstance(value, list | tuple):
        raise ValueError("times must be an array")
    if len(value) > MAX_SCHEDULE_TIMES:
        raise ValueError(f"times cannot contain more than {MAX_SCHEDULE_TIMES} entries")
    result: list[str] = []
    seen: set[str] = set()
    for raw_time in value:
        if not isinstance(raw_time, str) or _TIME_PATTERN.fullmatch(raw_time) is None:
            raise ValueError("schedule times must use strict 24-hour HH:MM format")
        if raw_time not in seen:
            seen.add(raw_time)
            result.append(raw_time)
    return tuple(result)


def _values(value: Mapping[str, object] | ScheduleConfig) -> _ScheduleValues:
    if isinstance(value, ScheduleConfig):
        mapping: Mapping[str, object] = {
            "enabled": value.enabled,
            "warm_cache_enabled": value.warm_cache_enabled,
            "alerts_enabled": value.alerts_enabled,
            "timezone": value.timezone,
            "groups": value.groups,
            "times": value.times,
        }
    elif isinstance(value, Mapping):
        mapping = value
    else:
        raise ValueError("schedule configuration must be an object")

    if any(not isinstance(key, str) for key in mapping):
        raise ValueError("schedule field names must be strings")
    unknown = {str(key) for key in mapping} - _KNOWN_KEYS
    if unknown:
        raise ValueError(f"unknown schedule fields: {', '.join(sorted(unknown))}")
    defaults = _ScheduleValues()
    return _ScheduleValues(
        enabled=_strict_bool(mapping.get("enabled"), field="enabled", default=defaults.enabled),
        warm_cache_enabled=_strict_bool(
            mapping.get("warm_cache_enabled"),
            field="warm_cache_enabled",
            default=defaults.warm_cache_enabled,
        ),
        alerts_enabled=_strict_bool(
            mapping.get("alerts_enabled"),
            field="alerts_enabled",
            default=defaults.alerts_enabled,
        ),
        timezone=_timezone(mapping.get("timezone", defaults.timezone)),
        groups=_groups(mapping.get("groups", defaults.groups)),
        times=_times(mapping.get("times", defaults.times)),
    )


def _parse_legacy(payload: bytes) -> _ScheduleValues:
    if len(payload) > MAX_LEGACY_FILE_BYTES:
        raise LegacyScheduleImportError("legacy schedule file is too large")
    try:
        decoded = payload.decode("utf-8")
        raw = json.loads(
            decoded,
            object_pairs_hook=_strict_object,
            parse_constant=_reject_json_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, _StrictJSONError) as exc:
        raise LegacyScheduleImportError("legacy schedule file is not valid UTF-8 JSON") from exc
    if not isinstance(raw, dict):
        raise LegacyScheduleImportError("legacy schedule root must be an object")
    try:
        return _values(raw)
    except ValueError as exc:
        raise LegacyScheduleImportError(str(exc)) from exc


def _config_from_rows(
    setting: Mapping[str, Any],
    group_rows: Sequence[Mapping[str, Any]],
    time_rows: Sequence[Mapping[str, Any]],
) -> ScheduleConfig:
    return ScheduleConfig(
        enabled=bool(setting["enabled"]),
        warm_cache_enabled=bool(setting["warm_cache_enabled"]),
        alerts_enabled=bool(setting["alerts_enabled"]),
        timezone=str(setting["timezone"]),
        groups=tuple(int(row["group_id"]) for row in group_rows),
        times=tuple(str(row["time_hhmm"]) for row in time_rows),
        version=int(setting["version"]),
        updated_at=int(setting["updated_at"]),
    )


def _read_config(transaction: Transaction) -> ScheduleConfig:
    settings = transaction.read(
        """
        SELECT enabled, warm_cache_enabled, alerts_enabled, timezone, version, updated_at
        FROM scheduler_settings WHERE singleton = 1
        """
    )
    if not settings:
        raise ScheduleNotInitializedError("scheduler repository has not been initialized")
    groups = transaction.read("SELECT group_id FROM scheduler_groups ORDER BY group_id")
    times = transaction.read("SELECT time_hhmm FROM scheduler_times ORDER BY time_hhmm")
    return _config_from_rows(settings[0], groups, times)


def _insert_values(transaction: Transaction, values: _ScheduleValues) -> int:
    transaction.write(
        """
        INSERT INTO scheduler_settings
            (singleton, enabled, warm_cache_enabled, alerts_enabled, timezone, version)
        VALUES (1, ?, ?, ?, ?, 1)
        """,
        (
            int(values.enabled),
            int(values.warm_cache_enabled),
            int(values.alerts_enabled),
            values.timezone,
        ),
    )
    for group_id in values.groups:
        transaction.write("INSERT INTO scheduler_groups (group_id) VALUES (?)", (group_id,))
    for time_hhmm in values.times:
        transaction.write(
            "INSERT INTO scheduler_times (time_hhmm) VALUES (?)",
            (time_hhmm,),
        )
    return 1 + len(values.groups) + len(values.times)


class ScheduleRepository(Repository):
    """Hot-reloadable schedule state with optimistic configuration updates."""

    def __init__(
        self,
        database: StateDatabase,
        *,
        legacy_path: str | Path | None = None,
    ) -> None:
        super().__init__(database)
        configured_path = (
            Path(legacy_path) if legacy_path is not None else Path("schedule_config.local.json")
        )
        if not configured_path.is_absolute():
            configured_path = PROJECT_ROOT / configured_path
        self.legacy_path = configured_path.resolve()

    async def initialize(self) -> None:
        await super().initialize()
        if await self._legacy_already_processed():
            if not await self.database.read("SELECT 1 FROM scheduler_settings WHERE singleton = 1"):
                raise ScheduleNotInitializedError(
                    "scheduler import marker exists without scheduler settings"
                )
            return

        try:
            payload = await asyncio.to_thread(self.legacy_path.read_bytes)
        except FileNotFoundError:
            values = _ScheduleValues()
            source_state = "absent"
            digest = _ABSENT_DIGEST
        except OSError as exc:
            raise LegacyScheduleImportError("legacy schedule file cannot be read") from exc
        else:
            try:
                values = _parse_legacy(payload)
            except LegacyScheduleImportError:
                await asyncio.to_thread(
                    _backup_invalid_source,
                    self.legacy_path,
                    payload,
                )
                raise
            source_state = "imported"
            digest = hashlib.sha256(payload).hexdigest()

        def migrate(transaction: Transaction) -> None:
            if transaction.read(
                "SELECT 1 FROM legacy_imports WHERE source_name = ?",
                (LEGACY_SOURCE_NAME,),
            ):
                if not transaction.read("SELECT 1 FROM scheduler_settings WHERE singleton = 1"):
                    raise ScheduleNotInitializedError(
                        "scheduler import marker exists without scheduler settings"
                    )
                return

            _verify_legacy_source(self.legacy_path, source_state, digest)
            populated = transaction.read(
                """
                SELECT (
                    EXISTS(SELECT 1 FROM scheduler_settings)
                    OR EXISTS(SELECT 1 FROM scheduler_groups)
                    OR EXISTS(SELECT 1 FROM scheduler_times)
                ) AS populated
                """
            )[0]["populated"]
            if bool(populated):
                raise LegacyScheduleImportError(
                    "existing scheduler state has no legacy import marker"
                )
            imported_rows = _insert_values(transaction, values)
            transaction.write(
                """
                INSERT INTO legacy_imports
                    (source_name, source_sha256, imported_rows, source_state)
                VALUES (?, ?, ?, ?)
                """,
                (LEGACY_SOURCE_NAME, digest, imported_rows, source_state),
            )

        await self.database.transaction(migrate)

    async def _legacy_already_processed(self) -> bool:
        rows = await self.database.read(
            "SELECT 1 FROM legacy_imports WHERE source_name = ?",
            (LEGACY_SOURCE_NAME,),
        )
        return bool(rows)

    async def get(self) -> ScheduleConfig:
        """Read settings, groups, and times from one consistent snapshot."""

        return await self.database.transaction(_read_config, immediate=False)

    async def replace(
        self,
        config: Mapping[str, object] | ScheduleConfig,
        *,
        expected_version: int,
    ) -> ScheduleConfig:
        """Replace the complete config if ``expected_version`` is still current."""

        if (
            isinstance(expected_version, bool)
            or not isinstance(expected_version, int)
            or expected_version <= 0
        ):
            raise ValueError("expected_version must be a positive integer")
        values = _values(config)

        def replace_in_transaction(transaction: Transaction) -> ScheduleConfig:
            result = transaction.write(
                """
                UPDATE scheduler_settings
                SET enabled = ?, warm_cache_enabled = ?, alerts_enabled = ?,
                    timezone = ?, version = version + 1,
                    updated_at = CAST(strftime('%s', 'now') AS INTEGER)
                WHERE singleton = 1 AND version = ?
                """,
                (
                    int(values.enabled),
                    int(values.warm_cache_enabled),
                    int(values.alerts_enabled),
                    values.timezone,
                    expected_version,
                ),
            )
            if result.rowcount != 1:
                rows = transaction.read(
                    "SELECT version FROM scheduler_settings WHERE singleton = 1"
                )
                actual = int(rows[0]["version"]) if rows else None
                raise ScheduleVersionConflictError(expected_version, actual)

            transaction.write("DELETE FROM scheduler_groups")
            transaction.write("DELETE FROM scheduler_times")
            for group_id in values.groups:
                transaction.write(
                    "INSERT INTO scheduler_groups (group_id) VALUES (?)",
                    (group_id,),
                )
            for time_hhmm in values.times:
                transaction.write(
                    "INSERT INTO scheduler_times (time_hhmm) VALUES (?)",
                    (time_hhmm,),
                )
            return _read_config(transaction)

        return await self.database.transaction(replace_in_transaction)
