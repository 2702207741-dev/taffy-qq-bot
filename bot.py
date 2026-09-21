"""QQ 群机器人 — NoneBot2 + DeepSeek"""

import warnings
from pathlib import Path

import nonebot
from nonebot.adapters.onebot.v11 import Adapter as OneBotV11Adapter

from src.core.config import (
    audit_project_env_keys,
    resolve_ingress_security,
    runtime_env_files,
)
from src.core.logging_privacy import configure_privacy_logging
from src.services.resource_governor import start_resource_governor, stop_resource_governor
from src.state.runtime import initialize_state, shutdown_state

_PROJECT_ROOT = Path(__file__).resolve().parent
_ingress = resolve_ingress_security(_PROJECT_ROOT)
if _ingress.downgraded_to_loopback:
    warnings.warn(
        "OneBot V11 ingress credentials are incomplete or trusted TLS "
        "termination was not confirmed; forcing the server "
        "listener to 127.0.0.1. "
        "Forward WebSocket connections remain available.",
        RuntimeWarning,
        stacklevel=1,
    )

nonebot.init(_env_file=runtime_env_files(_PROJECT_ROOT), host=_ingress.host)
configure_privacy_logging(_PROJECT_ROOT)
# 配置防呆：审计 .env/.env.<env> 键名拼写（只告警不阻断；仅看 dotenv 层，
# 不看进程环境——否则 PATH 等系统键会制造海量误报）。
audit_project_env_keys(_PROJECT_ROOT)
driver = nonebot.get_driver()
driver.register_adapter(OneBotV11Adapter)

_PLUGINS = (
    # Chat is loaded first because it owns the shared chat helpers and skin data.
    "src.plugins.chat",
    "src.plugins.alert",
    "src.plugins.inventory_monitor",
    "src.plugins.compare",
    "src.plugins.daily",
    "src.plugins.news",
    "src.plugins.reddit_news",
    "src.plugins.admin_status",
    "src.plugins.scheduler",
    "src.plugins.watchlist",
    "src.plugins.admin_api",
    "src.plugins.desktop_api",
)
for _plugin in _PLUGINS:
    if nonebot.load_plugin(_plugin) is None:
        raise RuntimeError(f"Required plugin failed to load: {_plugin}")


async def _startup_sequence() -> None:
    """One explicit startup orchestration with real sequential awaits.

    Phase order is guaranteed by this single function, not by hook
    registration order: the state store and resource governor come up
    first, then the QQ-independent application-level scheduler loops that
    depend on them.  A QQ connection is not required.
    """

    await initialize_state()
    await start_resource_governor()

    from src.plugins.scheduler import start_application_tasks

    await start_application_tasks()


async def _shutdown_sequence() -> None:
    """One explicit shutdown orchestration with real sequential awaits.

    All registered scheduler tasks (connection-bound and application-level)
    are cancelled and awaited first; only then are the resource governor
    and state they depend on closed.  The scheduler plugin no longer
    registers its own driver shutdown hook, so this runs exactly once.
    """

    from src.plugins.scheduler import shutdown_scheduler

    await shutdown_scheduler()
    await stop_resource_governor()
    await shutdown_state()


# Startup and shutdown are each a single hook whose body sequences the
# phases; no behaviour depends on the order NoneBot invokes separate hooks.
driver.on_startup(_startup_sequence)
driver.on_shutdown(_shutdown_sequence)

if __name__ == "__main__":
    nonebot.run()
