"""One-time lifecycle wiring for application service clients."""

from __future__ import annotations

import asyncio
import logging

logger = logging.getLogger("taffy.services.lifecycle")

_shutdown_registered = False


async def close_services() -> None:
    """Close every shared service resource, attempting all cleanup paths."""

    from .http_client import close_http_client
    from .llm import close_llm_client
    from .market_catalog_store import close_market_catalog_store
    from .market_temperature import close_market_temperature_service
    from .steamdt_kline import close_steamdt_kline_service

    results = await asyncio.gather(
        close_http_client(),
        close_llm_client(),
        close_market_catalog_store(),
        close_market_temperature_service(),
        close_steamdt_kline_service(),
        return_exceptions=True,
    )
    for result in results:
        if isinstance(result, BaseException):
            logger.error("Service client shutdown failed error=%s", type(result).__name__)


def register_shutdown_hook() -> bool:
    """Register service cleanup once, returning whether a hook is installed."""

    global _shutdown_registered
    if _shutdown_registered:
        return True
    try:
        from nonebot import get_driver

        driver = get_driver()
    except ValueError:
        # Unit tests may import the facade before NoneBot has been initialized.
        return False
    driver.on_shutdown(close_services)
    _shutdown_registered = True
    return True


__all__ = ["close_services", "register_shutdown_hook"]
