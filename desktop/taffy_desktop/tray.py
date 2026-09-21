"""系统托盘：状态色图标 + 左键/双击唤起 + 右键菜单。纯 ctypes Shell_NotifyIconW，零额外依赖。

非 Windows / 测试环境下全部降级为 no-op，壳层功能不受影响。
状态四色映射（STATE_COLORS）与菜单动作沿用既有状态机；
菜单项由 Windows 原生 TrackPopupMenu 绘制（零 CDN 零构建）。
"""

from __future__ import annotations

import ctypes
import logging
import threading
from collections.abc import Callable
from ctypes import wintypes
from pathlib import Path
from typing import Any

_log = logging.getLogger("taffy.desktop.tray")

#: 状态 → 图标 RGB 颜色（与既有状态机一致）。
STATE_COLORS = {
    "running": (0x2E, 0xA0, 0x43),
    "starting": (0xE6, 0xA2, 0x3C),
    "stopped": (0xB0, 0xB4, 0xBA),
    "foreign_port": (0xD9, 0x3B, 0x3B),
}

_TRAY_MENU_LABELS = (
    ("open", "打开工作台"),
    ("start_bot", "启动塔菲"),
    ("stop_bot", "停止塔菲"),
    ("restart_bot", "重启塔菲"),
    ("quit", "退出桌面工作台"),
)

# Windows 托盘回调消息（自定义）：WM_USER + 1。
_TRAY_CALLBACK_MSG = 0x0400 + 1
_WM_LBUTTONUP = 0x0202
_WM_LBUTTONDBLCLK = 0x0203
_WM_RBUTTONUP = 0x0205
_NIM_ADD = 0
_NIM_MODIFY = 1
_NIM_DELETE = 2
_NIF_MESSAGE = 0x1
_NIF_ICON = 0x2
_NIF_TIP = 0x4
_NIF_INFO = 0x10

# TrackPopupMenu 选项
_TPM_RIGHTBUTTON = 0x2
_TPM_RETURNCMD = 0x100


class _NOTIFYICONDATAW(ctypes.Structure):
    """Shell_NotifyIconW 的 NOTIFYICONDATA（v6 布局，cbSize 固定）。"""

    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("hWnd", wintypes.HWND),
        ("uID", wintypes.UINT),
        ("uFlags", wintypes.UINT),
        ("uCallbackMessage", wintypes.UINT),
        ("hIcon", wintypes.HICON),
        ("szTip", wintypes.WCHAR * 128),
        ("dwState", wintypes.DWORD),
        ("dwStateMask", wintypes.DWORD),
        ("szInfo", wintypes.WCHAR * 256),
        ("uTimeoutOrVersion", wintypes.UINT),
        ("szInfoTitle", wintypes.WCHAR * 64),
        ("dwInfoFlags", wintypes.DWORD),
        ("guidItem", wintypes.BYTE * 16),
        ("hBalloonIcon", wintypes.HICON),
    ]


class _WNDCLASSW(ctypes.Structure):
    """RegisterClassW 需要的 WNDCLASS（Unicode 版）。"""

    _fields_ = [
        ("style", wintypes.UINT),
        ("lpfnWndProc", ctypes.c_void_p),
        ("cbClsExtra", ctypes.c_int),
        ("cbWndExtra", ctypes.c_int),
        ("hInstance", wintypes.HINSTANCE),
        ("hIcon", wintypes.HICON),
        ("hCursor", wintypes.HANDLE),
        ("hbrBackground", wintypes.HBRUSH),
        ("lpszMenuName", wintypes.LPCWSTR),
        ("lpszClassName", wintypes.LPCWSTR),
    ]


def _rgb_icon(rgb: tuple[int, int, int]) -> int:
    """把 (R,G,B) 画成 16×16 纯色图标 HBITMAP（托盘尺寸）。"""

    red, green, blue = rgb
    buf = bytearray()
    for _row in range(16):
        for _col in range(16):
            # BITMAPINFO 要求 BGR 顺序 + 底部朝上；简化：全部同色，方向无所谓。
            buf += bytes((blue, green, red))
        buf += b"\x00\x00"  # 每行 4 字节对齐（16*3=48 → 补齐到 52）
    bitmap = ctypes.windll.gdi32.CreateBitmap(16, 16, 1, 24, None)
    if not bitmap:
        return 0
    return int(bitmap)


class TrayIcon:
    """Owns one Shell_NotifyIconW slot on a dedicated thread; safe stub elsewhere."""

    def __init__(
        self,
        icon_path: Path | None,
        *,
        actions: dict[str, Callable[[], object]] | None = None,
    ) -> None:
        self._icon_path = icon_path
        self._actions = actions or {}
        self._state = "stopped"
        self._tooltip = "塔菲桌面工作台"
        self._thread: threading.Thread | None = None
        self._available = False
        self._hwnd: int | None = None
        self._h_icon: int | None = None
        self._menu_handles: dict[str, int] = {}
        self._callback_msg = _TRAY_CALLBACK_MSG

    @property
    def available(self) -> bool:
        return self._available

    def menu_labels(self) -> tuple[tuple[str, str], ...]:
        return _TRAY_MENU_LABELS

    def set_state(self, state: str, tooltip: str | None = None) -> bool:
        """Update status color/tooltip; returns True when the icon was updated."""

        if state not in STATE_COLORS:
            return False
        changed = state != self._state or (tooltip is not None and tooltip != self._tooltip)
        self._state = state
        if tooltip:
            self._tooltip = tooltip
        if self._available and changed:
            self._refresh_icon()
        return changed

    def dispatch(self, action: str) -> bool:
        handler = self._actions.get(action)
        if handler is None:
            return False
        try:
            handler()
        except Exception as exc:  # noqa: BLE001 - menu handlers must not kill the tray
            _log.warning("Tray action %s failed error=%s", action, type(exc).__name__)
        return True

    def start(self) -> None:
        """Create the native icon when possible; otherwise stay a stub."""

        if self._thread is not None:
            return
        try:
            ctypes.windll.shell32  # noqa: B018 - platform gate for the real tray
            self._available = True
        except Exception:  # noqa: BLE001 - non-Windows or restricted sandbox
            _log.info("Native tray unavailable; running without tray icon")
            self._available = False
            return

        self._thread = threading.Thread(target=self._run, name="taffy-tray", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        """Remove the native icon and stop the message loop."""

        if self._available and self._hwnd:
            try:
                data = self._notify_data(0)
                ctypes.windll.shell32.Shell_NotifyIconW(_NIM_DELETE, ctypes.byref(data))
            except Exception as exc:  # noqa: BLE001
                _log.warning("Tray delete failed error=%s", type(exc).__name__)
        self._hwnd = None
        self._thread = None

    # ── 原生实现 ───────────────────────────────────────────

    def _notify_data(self, flags: int) -> _NOTIFYICONDATAW:
        data = _NOTIFYICONDATAW()
        data.cbSize = ctypes.sizeof(_NOTIFYICONDATAW)
        data.hWnd = wintypes.HWND(self._hwnd or 0)
        data.uID = 1
        data.uFlags = flags
        data.uCallbackMessage = self._callback_msg
        data.hIcon = self._h_icon or 0
        data.szTip = (self._tooltip or "")[:127]
        return data

    def _create_icon(self) -> int:
        """按当前状态色建 16×16 纯色图标句柄（无资源文件时）。"""

        if self._icon_path and self._icon_path.is_file():
            try:
                return int(ctypes.windll.user32.LoadImageW(0, str(self._icon_path), 1, 16, 16, 0))
            except Exception:  # noqa: BLE001
                _log.warning("Tray icon load failed; falling back to color dot")
        rgb = STATE_COLORS.get(self._state, STATE_COLORS["stopped"])
        return _rgb_icon(rgb)

    def _refresh_icon(self) -> None:
        if not self._available or not self._hwnd:
            return
        try:
            self._h_icon = self._create_icon()
            data = self._notify_data(_NIF_ICON | _NIF_MESSAGE | _NIF_TIP)
            ctypes.windll.shell32.Shell_NotifyIconW(_NIM_MODIFY, ctypes.byref(data))
        except Exception as exc:  # noqa: BLE001
            _log.warning("Tray refresh failed error=%s", type(exc).__name__)

    def _build_menu(self) -> int:
        """创建右键菜单，返回句柄；菜单项动作 ID 与 menu_labels 下标对应。"""

        menu = ctypes.windll.user32.CreatePopupMenu()
        for index, (_action, label) in enumerate(_TRAY_MENU_LABELS):
            ctypes.windll.user32.AppendMenuW(menu, 0x0, index + 1, label)
        return int(menu)

    def _run(self) -> None:
        """托盘消息循环：注册窗口类 → 添加图标 → GetMessageW 派发。"""

        wndclass = _WNDCLASSW()
        # 64 位下 HWND/WPARAM/LPARAM 均为 8 字节；显式 c_uint64/c_int64
        # 防止 ctypes 回调按 4 字节截断指针（Win32 窗口过程的标准签名）。
        wndproc = ctypes.WINFUNCTYPE(
            ctypes.c_long, ctypes.c_uint64, ctypes.c_uint, ctypes.c_uint64, ctypes.c_int64
        )(self._wnd_proc)
        wndclass.lpfnWndProc = ctypes.cast(wndproc, ctypes.c_void_p)
        ctypes.windll.kernel32.GetModuleHandleW.restype = wintypes.HMODULE
        wndclass.hInstance = ctypes.windll.kernel32.GetModuleHandleW(None)
        wndclass.lpszClassName = "TaffyTrayWindow"
        class_atom = ctypes.windll.user32.RegisterClassW(ctypes.byref(wndclass))
        if not class_atom:
            _log.warning("Tray window class registration failed")
            self._available = False
            return
        ctypes.windll.user32.CreateWindowExW.argtypes = [
            wintypes.DWORD,
            wintypes.LPCWSTR,
            wintypes.LPCWSTR,
            wintypes.DWORD,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            wintypes.HWND,
            wintypes.HMENU,
            wintypes.HINSTANCE,
            wintypes.LPVOID,
        ]
        ctypes.windll.user32.CreateWindowExW.restype = wintypes.HWND
        hwnd = ctypes.windll.user32.CreateWindowExW(
            0,  # dwExStyle
            "TaffyTrayWindow",  # lpClassName
            "TaffyTray",  # lpWindowName
            0,  # dwStyle
            0,  # x
            0,  # y
            0,  # nWidth
            0,  # nHeight
            0,  # hWndParent
            0,  # hMenu
            wndclass.hInstance,  # hInstance
            0,  # lpParam
        )
        if not hwnd:
            _log.warning("Tray window creation failed")
            self._available = False
            return
        self._hwnd = int(hwnd)
        self._refresh_icon()

        msg = wintypes.MSG()
        while ctypes.windll.user32.GetMessageW(ctypes.byref(msg), 0, 0, 0) > 0:
            ctypes.windll.user32.TranslateMessage(ctypes.byref(msg))
            ctypes.windll.user32.DispatchMessageW(ctypes.byref(msg))
        self._hwnd = None

    def _wnd_proc(self, hwnd: int, msg: int, wparam: int, lparam: int) -> int:
        if msg == self._callback_msg:
            self._on_callback(lparam)
            return 0
        if msg == 0x0010:  # WM_DESTROY
            ctypes.windll.user32.PostQuitMessage(0)
            return 0
        ctypes.windll.user32.DefWindowProcW.argtypes = [
            wintypes.HWND,
            wintypes.UINT,
            wintypes.WPARAM,
            wintypes.LPARAM,
        ]
        ctypes.windll.user32.DefWindowProcW.restype = ctypes.c_long
        return int(ctypes.windll.user32.DefWindowProcW(hwnd, msg, wparam, lparam))

    def _on_callback(self, lparam: int) -> None:
        if lparam == _WM_LBUTTONUP:
            self.dispatch("open")
        elif lparam == _WM_LBUTTONDBLCLK:
            self.dispatch("open")
        elif lparam == _WM_RBUTTONUP:
            self._show_menu()

    def _show_menu(self) -> None:
        try:
            menu = self._build_menu()
            point = wintypes.POINT()
            ctypes.windll.user32.GetCursorPos(ctypes.byref(point))
            ctypes.windll.user32.SetForegroundWindow(self._hwnd)
            command = ctypes.windll.user32.TrackPopupMenu(
                menu,
                _TPM_RIGHTBUTTON | _TPM_RETURNCMD,
                point.x,
                point.y,
                0,
                self._hwnd,
                0,
            )
            ctypes.windll.user32.DestroyMenu(menu)
            if command >= 1 and command <= len(_TRAY_MENU_LABELS):
                action = _TRAY_MENU_LABELS[command - 1][0]
                self.dispatch(action)
        except Exception as exc:  # noqa: BLE001 - menu must never crash the loop
            _log.warning("Tray menu failed error=%s", type(exc).__name__)


def build_tray_actions(
    *,
    open_window: Callable[[], object],
    start_bot: Callable[[], object],
    stop_bot: Callable[[], object],
    restart_bot: Callable[[], object],
    quit_app: Callable[[], object],
) -> dict[str, Callable[[], object]]:
    return {
        "open": open_window,
        "start_bot": start_bot,
        "stop_bot": stop_bot,
        "restart_bot": restart_bot,
        "quit": quit_app,
    }


def win32_stub() -> dict[str, Any]:
    """Expose nothing on purpose; keeps import cost zero on non-Windows."""
    return {}
