from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.core import logging_privacy
from src.services import lifecycle


@pytest.mark.asyncio
async def test_close_services_attempts_every_cleanup_when_one_fails() -> None:
    with (
        patch("src.services.http_client.close_http_client", AsyncMock(side_effect=RuntimeError)),
        patch("src.services.llm.close_llm_client", AsyncMock()) as llm,
        patch(
            "src.services.market_catalog_store.close_market_catalog_store",
            AsyncMock(),
        ) as catalog,
        patch(
            "src.services.market_temperature.close_market_temperature_service",
            AsyncMock(),
        ) as temperature,
        patch("src.services.steamdt_kline.close_steamdt_kline_service", AsyncMock()) as kline,
    ):
        await lifecycle.close_services()
    llm.assert_awaited_once()
    catalog.assert_awaited_once()
    temperature.assert_awaited_once()
    kline.assert_awaited_once()


def test_register_shutdown_hook_returns_false_before_nonebot_initialization() -> None:
    previous = lifecycle._shutdown_registered
    lifecycle._shutdown_registered = False
    try:
        with patch("nonebot.get_driver", side_effect=ValueError):
            assert not lifecycle.register_shutdown_hook()
    finally:
        lifecycle._shutdown_registered = previous


def test_privacy_logging_configures_rotating_sanitized_sinks_once(tmp_path) -> None:
    previous = logging_privacy._configured
    logging_privacy._configured = False
    fake_logger = SimpleNamespace(remove=MagicMock(), add=MagicMock())
    try:
        with patch.object(logging_privacy, "logger", fake_logger):
            logging_privacy.configure_privacy_logging(tmp_path)
            logging_privacy.configure_privacy_logging(tmp_path)
        fake_logger.remove.assert_called_once_with(logging_privacy.logger_id)
        assert fake_logger.add.call_count == 2
        file_call = fake_logger.add.call_args_list[1]
        assert file_call.kwargs["rotation"] == "10 MB"
        assert file_call.kwargs["retention"] == "7 days"
        assert file_call.kwargs["compression"] == "gz"
    finally:
        logging_privacy._configured = previous
