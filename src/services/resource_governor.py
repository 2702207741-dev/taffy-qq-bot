"""Game-aware cooperative scheduling for background work."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable

import psutil

logger = logging.getLogger("taffy.services.resource_governor")


def _cs2_is_running() -> bool:
    try:
        for process in psutil.process_iter(("name",)):
            try:
                name = process.info.get("name")
            except (psutil.AccessDenied, psutil.NoSuchProcess, psutil.ZombieProcess):
                continue
            if isinstance(name, str) and name.casefold() == "cs2.exe":
                return True
    except (psutil.Error, OSError):
        return False
    return False


class ResourceGovernor:
    def __init__(
        self,
        *,
        detector: Callable[[], bool] = _cs2_is_running,
        poll_seconds: float = 15.0,
    ) -> None:
        self._detector = detector
        self.poll_seconds = max(1.0, float(poll_seconds))
        self._game_running = False
        self._task: asyncio.Task[None] | None = None

    @property
    def game_running(self) -> bool:
        return self._game_running

    async def poll_once(self) -> bool:
        detected = await asyncio.to_thread(self._detector)
        if detected != self._game_running:
            self._game_running = detected
            logger.info("Game resource mode changed active=%s", detected)
        return detected

    async def run(self) -> None:
        while True:
            await self.poll_once()
            await asyncio.sleep(self.poll_seconds)

    async def start(self) -> None:
        if self._task is None or self._task.done():
            await self.poll_once()
            self._task = asyncio.create_task(self.run(), name="resource-governor")

    async def stop(self) -> None:
        task, self._task = self._task, None
        if task is None:
            return
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


resource_governor = ResourceGovernor()


async def start_resource_governor() -> None:
    await resource_governor.start()


async def stop_resource_governor() -> None:
    await resource_governor.stop()


__all__ = [
    "ResourceGovernor",
    "resource_governor",
    "start_resource_governor",
    "stop_resource_governor",
]
