from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import nonebot

try:
    nonebot.get_driver()
except ValueError:
    nonebot.init()

from src.plugins.chat.media import (
    image_paths,
    send_random_image,
    stop_process,
    transcode_video,
    video_encoder,
)


class _TerminableProcess:
    def __init__(self) -> None:
        self.returncode: int | None = None
        self.terminated = False
        self.killed = False

    async def communicate(self) -> tuple[bytes, bytes]:
        await asyncio.Event().wait()
        return b"", b""

    def terminate(self) -> None:
        self.terminated = True
        self.returncode = -15

    def kill(self) -> None:
        self.killed = True
        self.returncode = -9

    async def wait(self) -> int:
        return self.returncode or 0


class _StubbornProcess(_TerminableProcess):
    def __init__(self) -> None:
        super().__init__()
        self._stopped = asyncio.Event()

    def terminate(self) -> None:
        self.terminated = True

    def kill(self) -> None:
        self.killed = True
        self.returncode = -9
        self._stopped.set()

    async def wait(self) -> int:
        await self._stopped.wait()
        return self.returncode or 0


class MediaLifecycleTests(unittest.IsolatedAsyncioTestCase):
    def test_bundled_ffmpeg_uses_available_openh264_encoder(self) -> None:
        root = Path(__file__).resolve().parents[1]
        bundled = root.parent / ".local" / "bin" / "ffmpeg.exe"

        with patch("src.plugins.chat.media.runtime_setting", return_value=None):
            self.assertEqual(video_encoder(bundled), "libopenh264")

    async def test_transcode_timeout_terminates_child_and_removes_partial_file(self) -> None:
        process = _TerminableProcess()
        output: Path | None = None

        async def create_process(*args: object, **kwargs: object) -> _TerminableProcess:
            nonlocal output
            del kwargs
            output = Path(str(args[-1]))
            return process

        with patch("src.plugins.chat.media.asyncio.create_subprocess_exec", create_process):
            with self.assertRaisesRegex(RuntimeError, "timed out"):
                await transcode_video("input.mp4", "ffmpeg", timeout=0.001)

        self.assertTrue(process.terminated)
        self.assertFalse(process.killed)
        self.assertIsNotNone(output)
        self.assertFalse(output.exists() if output else True)

    async def test_stop_process_escalates_to_kill(self) -> None:
        process = _StubbornProcess()

        await stop_process(process, grace_period=0.001)

        self.assertTrue(process.terminated)
        self.assertTrue(process.killed)
        self.assertEqual(process.returncode, -9)


class ImageDeliveryTests(unittest.IsolatedAsyncioTestCase):
    def test_image_paths_defaults_to_comfyui_output(self) -> None:
        with patch("src.plugins.chat.media.runtime_setting", return_value=None):
            self.assertEqual(image_paths(), "D:\\ComfyUI\\output")

    def test_image_paths_honors_configured_directory(self) -> None:
        with patch(
            "src.plugins.chat.media.runtime_setting",
            side_effect=lambda name: "D:\\custom\\images" if name == "TAFFY_IMAGE_DIR" else None,
        ):
            self.assertEqual(image_paths(), "D:\\custom\\images")

    async def test_send_random_image_picks_only_image_extensions(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "a.png").write_bytes(b"png")
            (root / "b.JPG").write_bytes(b"jpg")
            (root / "c.txt").write_bytes(b"txt")
            bot = SimpleNamespace(send=AsyncMock())
            event = SimpleNamespace()
            with patch(
                "src.plugins.chat.media.runtime_setting",
                return_value=str(root),
            ):
                await send_random_image(bot, event)  # type: ignore[arg-type]

            bot.send.assert_awaited_once()
            message = bot.send.await_args.args[1]
            self.assertIn("file:///", str(message))

    async def test_send_random_image_empty_directory_notice(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            bot = SimpleNamespace(send=AsyncMock())
            event = SimpleNamespace()
            with patch(
                "src.plugins.chat.media.runtime_setting",
                return_value=str(Path(directory)),
            ):
                await send_random_image(bot, event)  # type: ignore[arg-type]

            message = str(bot.send.await_args.args[1])
            self.assertIn("空了", message)

    async def test_send_random_image_missing_directory_notice(self) -> None:
        bot = SimpleNamespace(send=AsyncMock())
        event = SimpleNamespace()
        with patch(
            "src.plugins.chat.media.runtime_setting",
            return_value=str(Path("Z:\\no-such-dir-please")),
        ):
            await send_random_image(bot, event)  # type: ignore[arg-type]

        message = str(bot.send.await_args.args[1])
        self.assertIn("无法访问", message)

    async def test_send_random_image_skips_oversized_files(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "big.png").write_bytes(b"x" * (9 * 1024 * 1024))
            (root / "ok.jpg").write_bytes(b"small")
            bot = SimpleNamespace(send=AsyncMock())
            event = SimpleNamespace()
            with patch(
                "src.plugins.chat.media.runtime_setting",
                return_value=str(root),
            ):
                await send_random_image(bot, event)  # type: ignore[arg-type]

            bot.send.assert_awaited_once()
            message = str(bot.send.await_args.args[1])
            self.assertIn("ok.jpg", message)

    async def test_send_random_image_times_out_and_returns_notice(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "a.png").write_bytes(b"png")
            bot = SimpleNamespace(
                send=AsyncMock(side_effect=[asyncio.TimeoutError(), None]),
            )
            event = SimpleNamespace()
            with patch(
                "src.plugins.chat.media.runtime_setting",
                return_value=str(root),
            ):
                await send_random_image(bot, event)  # type: ignore[arg-type]

            self.assertEqual(bot.send.await_count, 2)
            self.assertIn("稍等再试", str(bot.send.await_args.args[1]))


if __name__ == "__main__":
    unittest.main()
