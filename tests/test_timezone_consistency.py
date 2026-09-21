"""D1 regression: user-facing timestamps render in the Asia/Shanghai calendar."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from src.services.tz import configured_timezone

_MARKET_TZ = configured_timezone()

# 2026-08-22 03:04:05 UTC == 2026-08-22 11:04:05 Asia/Shanghai; a host on any
# other timezone would produce a different wall clock without the fix.
_EPOCH = int(datetime(2026, 8, 22, 3, 4, 5, tzinfo=timezone.utc).timestamp())
_LOCAL_TEXT = "2026-08-22 11:04:05"


def test_market_time_helper_is_eight_hours_ahead_of_utc() -> None:
    local = datetime.fromtimestamp(_EPOCH, tz=_MARKET_TZ)
    assert local.utcoffset() == timedelta(hours=8)
    assert local.strftime("%Y-%m-%d %H:%M:%S") == _LOCAL_TEXT


def test_format_data_time_renders_market_wall_clock() -> None:
    from src.services.market_validation import format_data_time

    assert format_data_time(float(_EPOCH)) == _LOCAL_TEXT


def test_kline_renderers_share_the_market_timezone() -> None:
    from src.services import kline_fast_renderer, steamdt_kline_worker

    assert kline_fast_renderer._MARKET_TZ is not None
    label = datetime.fromtimestamp(_EPOCH, tz=kline_fast_renderer._MARKET_TZ)
    worker_label = datetime.fromtimestamp(_EPOCH, tz=steamdt_kline_worker._MARKET_TZ)
    assert label.strftime("%Y-%m-%d %H:%M") == "2026-08-22 11:04"
    assert worker_label.strftime("%Y-%m-%d %H:%M") == "2026-08-22 11:04"


def test_inventory_display_times_share_the_market_timezone() -> None:
    from src.plugins import inventory_monitor as plugin_module
    from src.services import inventory_monitor as service_module
    from src.services import steam_inventory

    for module in (plugin_module, service_module, steam_inventory):
        rendered = datetime.fromtimestamp(_EPOCH, tz=module._MARKET_TZ)
        assert rendered.strftime("%Y-%m-%d %H:%M:%S") == _LOCAL_TEXT


def test_backup_slot_names_follow_the_market_calendar() -> None:
    from src.services import backup_service, config_backup

    slot_day = datetime.fromtimestamp(_EPOCH, tz=backup_service._MARKET_TZ)
    config_day = datetime.fromtimestamp(_EPOCH, tz=config_backup._MARKET_TZ)
    assert f"{slot_day:%Y-%m-%d}" == "2026-08-22"
    assert f"{config_day:%Y-%m-%d}" == "2026-08-22"


def test_official_cs2_local_time_is_market_offset() -> None:
    from src.services.official_cs2 import _local_time

    value = datetime(2026, 8, 22, 3, 4, tzinfo=timezone.utc)
    assert _local_time(value) == "08-22 11:04"
