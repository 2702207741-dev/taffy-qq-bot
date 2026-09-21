"""应用入口：单实例锁 → 凭据 → 托盘 → 窗口；--self-test；--minimized。

js bridge（window.pywebview.api）暴露：
- ``http_request(method, path, body)``：统一 HTTP 代理（令牌只留在 Python 侧，
  绝不进 DOM/localStorage——比任务单"前端内存持有令牌"更强的安全边界）；
- ``get_state()``：supervisor 状态快照（离线页数据源）；
- ``open_external(url)``：仅放行 https:// 链接。
所有 bridge 方法吞异常并返回类型名，绝不向页面泄漏内部细节。
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import shutil
import threading
from dataclasses import asdict
from pathlib import Path
from typing import Any

import httpx

from . import __version__
from .bot_api import BotApiClient
from .config import (
    DEFAULT_BOT_PORT,
    DEFAULT_NAPCAT_PORT,
    AppSettings,
    appdata_dir,
    find_repo_root,
    load_settings,
    normalize_base_url,
    resolve_base_url,
    save_settings,
)
from .credentials import clear_token, load_token, save_token
from .single_instance import SingleInstanceLock
from .supervisor import BOT_STATE_FOREIGN_PORT, Supervisor, _default_port_is_open
from .tray import TrayIcon, build_tray_actions
from .tunnel import TunnelManager

_log = logging.getLogger("taffy.desktop.app")

FRONTEND_INDEX = Path(__file__).resolve().parent.parent / "frontend" / "index.html"


class Bridge:
    """Everything the frontend can call: window.pywebview.api.*"""

    def __init__(
        self,
        supervisor: Supervisor | None,
        client: BotApiClient,
        lock: SingleInstanceLock,
    ) -> None:
        self._supervisor = supervisor
        self._client = client
        self._lock = lock

    # ── HTTP 代理 ────────────────────────────────────────

    def http_request(self, method: str, path: str, body: str | None = None) -> dict[str, Any]:
        try:
            if not path.startswith("/"):
                raise ValueError("path must start with /")
            # 每次请求即时解析远程地址（settings 变化无需重启生效）。
            base = resolve_base_url(load_settings())
            url = f"{base}{path}"
            headers = {"Content-Type": "application/json"} if body else {}
            token = load_token(origin=base)
            if token:
                headers["Authorization"] = f"Bearer {token}"
            timeout = httpx.Timeout(4.0, read=120.0)
            response = httpx.request(
                method.upper(),
                url,
                content=body.encode("utf-8") if body else None,
                headers=headers,
                timeout=timeout,
                follow_redirects=False,
            )
            try:
                payload: Any = response.json()
            except ValueError:
                payload = {"raw": response.text}
            return {"status": response.status_code, "body": payload}
        except Exception as exc:
            return {"status": 0, "body": {"ok": False, "error": type(exc).__name__, "message": ""}}

    # ── 凭据 ────────────────────────────────────────────

    def has_token(self) -> bool:
        return bool(load_token())

    def verify_and_save_token(self, candidate: str) -> dict[str, Any]:
        try:
            cleaned = str(candidate).strip()
            base = self._client.base_url
            ok = bool(cleaned) and self._client.verify_token(cleaned, origin=base)
            if not ok:
                return {"saved": False, "reason": "invalid"}
            sink = save_token(cleaned, origin=base)
            return {"saved": True, "sink": sink}
        except Exception as exc:
            return {"saved": False, "reason": type(exc).__name__}

    def import_token_from_bot_env(self) -> dict[str, Any]:
        """从仓库 .env 读取 ADMIN_API_TOKEN：校验通过后直接入 DPAPI（免手动复制）。"""

        try:
            root = find_repo_root(Path(__file__).resolve())
            if root is None:
                return {"saved": False, "reason": "no_repo"}
            token = _read_env_key(root / ".env", "ADMIN_API_TOKEN")
            if not token:
                return {"saved": False, "reason": "not_in_env"}
            base = self._client.base_url
            if not self._client.verify_token(token, origin=base):
                return {"saved": False, "reason": "invalid"}
            sink = save_token(token, origin=base)
            return {"saved": True, "sink": sink, "source": "bot_env"}
        except Exception as exc:
            return {"saved": False, "reason": type(exc).__name__}

    def verify_token_only(self, candidate: str) -> bool:
        try:
            return self._client.verify_token(candidate.strip())
        except Exception:
            return False

    def clear_saved_token(self) -> bool:
        try:
            return clear_token()
        except Exception:
            return False

    # ── 状态与看护 ───────────────────────────────────────

    def get_state(self) -> dict[str, Any]:
        try:
            state: dict[str, Any] = {
                "version": __version__,
                "repo_found": self._supervisor is not None,
                "bot_port": DEFAULT_BOT_PORT,
                "napcat_port": DEFAULT_NAPCAT_PORT,
            }
            if self._supervisor is not None:
                state["supervisor"] = self._supervisor.status()
            health = self._client.health_ready()
            state["online"] = health is not None
            return state
        except Exception as exc:
            return {"online": False, "error": type(exc).__name__}

    def start_bot(self) -> str:
        if self._supervisor is None:
            return "no_repo"
        return self._supervisor.start_bot()

    def stop_bot(self) -> str:
        if self._supervisor is None:
            return "no_repo"
        return self._supervisor.stop_bot()

    def restart_bot(self) -> str:
        if self._supervisor is None:
            return "no_repo"
        return self._supervisor.restart_bot()

    def set_watchdog(self, enabled: bool) -> bool:
        if self._supervisor is None:
            return False
        self._supervisor.set_watchdog(bool(enabled))
        return True

    def tail_logs(self, lines: int = 30) -> str | None:
        base = self._client.base_url
        token = load_token(origin=base)
        if not token:
            return None
        return self._client.tail_logs(token, lines, origin=base)

    # ── 客户端设置（A2：个性化数据只存 %APPDATA%，不进 bot 库） ──

    def load_settings(self) -> str:
        try:
            return json.dumps(asdict(load_settings()), ensure_ascii=False)
        except Exception as exc:
            _log.warning("load_settings failed error=%s", type(exc).__name__)
            return "{}"

    def save_settings(self, payload: str) -> bool:
        try:
            raw = json.loads(payload)
            if not isinstance(raw, dict):
                raise ValueError("settings payload must be an object")
            # 防御：整体保存路径也把非法远程地址收敛为空（回退本地默认）。
            if "remote_base_url" in raw:
                raw["remote_base_url"] = normalize_base_url(str(raw["remote_base_url"])) or ""
            return save_settings(AppSettings.from_dict(raw))
        except Exception as exc:
            _log.warning("save_settings failed error=%s", type(exc).__name__)
            return False

    # ── 远程地址（V0.3：直连云端塔菲，仅经 SSH 隧道） ────

    def get_remote_base_url(self) -> str:
        """当前生效的 API 地址（含本地默认回退），供设置页回显。"""

        try:
            return resolve_base_url(load_settings())
        except Exception as exc:
            return type(exc).__name__

    def set_remote_base_url(self, value: str) -> dict[str, Any]:
        """写入远程地址；非法值拒绝并回报 reason，绝不静默改默认。"""

        try:
            cleaned = str(value or "").strip()
            normalized = normalize_base_url(cleaned)
            if cleaned and normalized is None:
                return {"ok": False, "reason": "invalid", "base_url": ""}
            settings = load_settings()
            settings.remote_base_url = normalized or ""
            saved = save_settings(settings)
            return {
                "ok": saved,
                "reason": "cleared" if not normalized else "set",
                "base_url": normalized or "",
            }
        except Exception as exc:
            return {"ok": False, "reason": type(exc).__name__, "base_url": ""}

    # ── 杂项 ────────────────────────────────────────────

    def open_external(self, url: str) -> str:
        try:
            if not isinstance(url, str) or not url.startswith("https://"):
                raise ValueError("only https:// links may be opened externally")
            import webbrowser

            webbrowser.open(url)
            return "ok"
        except Exception as exc:
            return type(exc).__name__

    def consume_activation(self) -> bool:
        try:
            return self._lock.consume_activation()
        except Exception:
            return False


class EventPoller:
    """轮询 /api/admin/events → 通知中心规则（toast/免打扰/聚合）。

    去重键使用 events 的稳定 ``key`` 字段（F5）；令牌缺失时静默跳过。
    """

    def __init__(self, client: BotApiClient, center: Any) -> None:
        self._client = client
        self._center = center
        self._seen: set[str] = set()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._first_batch = True

    def start(self) -> None:
        if self._thread is not None:
            return

        def loop() -> None:
            while not self._stop.is_set():
                try:
                    self.poll_once()
                except Exception as exc:  # noqa: BLE001 - poller must survive anything
                    _log.warning("Event poll failed error=%s", type(exc).__name__)
                self._stop.wait(60.0)

        self._thread = threading.Thread(target=loop, name="taffy-notify-poll", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _publish(self, kind: str, summary: str) -> None:
        titles = {
            "alert_delivered": "🔔 预警触发",
            "alert_failed": "⚠️ 预警失败",
            "job_failed": "🛠️ 后台任务失败",
            "usage": None,
        }
        title = titles.get(kind)
        if title is None:
            return
        self._center.publish(kind, title, summary)

    def poll_once(self) -> int:
        base = self._client.base_url
        token = load_token(origin=base)
        if not token:
            return 0
        import httpx as httpx_mod

        headers = {"Authorization": f"Bearer {token}"}
        response = httpx_mod.get(
            f"{base}/api/admin/events?limit=50",
            headers=headers,
            timeout=6.0,
            follow_redirects=False,
        )
        payload = response.json() if response.status_code == 200 else {}
        events = payload.get("events") or []
        fresh = 0
        for event in events:
            key = str(event.get("key") or "")
            if not key or key in self._seen:
                continue
            self._seen.add(key)
            fresh += 1
            # 首轮只建立基线，不轰炸历史通知。
            if not self._first_batch:
                self._publish(str(event.get("kind") or ""), str(event.get("summary") or ""))
        if self._first_batch and events:
            self._first_batch = False
        # 有界去重集合，防长期运行内存增长。
        if len(self._seen) > 2000:
            keep = list(self._seen)[-1000:]
            self._seen = set(keep)
        return fresh


def _build_supervisor(repo_root: Path) -> Supervisor:
    return Supervisor(repo_root)


def _read_env_key(env_path: Path, key: str) -> str | None:
    try:
        for raw in env_path.read_text("utf-8").splitlines():
            line = raw.strip()
            if line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            if k.strip() == key and v.strip():
                return v.strip()
    except OSError:
        return None
    return None


def _selftest_step(lines: list[str], ok: bool, name: str, detail: str) -> None:
    lines.append(f"[{'OK' if ok else 'WARN'}] {name}: {detail}")


def run_self_test(repo_root: Path | None = None) -> int:
    """七步自检（T1.5）：输出在线/离线两种状态的事实。"""

    lines: list[str] = []
    root = find_repo_root(repo_root or Path.cwd())
    _selftest_step(
        lines, root is not None, "①仓库定位", str(root) if root else "未找到 bot.py 仓库根"
    )
    client = BotApiClient()

    if root is None:
        for name in ("②端口探测", "③进程发现", "④health+token 探活", "⑤desktop API 探测"):
            _selftest_step(lines, False, name, "跳过（无仓库根）")
    else:
        supervisor = _build_supervisor(root)
        snapshot = supervisor.refresh().as_dict()
        port_open = bool(snapshot["port_open"])
        owners = snapshot["port_owner_pids"]
        napcat_open = _default_port_is_open(DEFAULT_NAPCAT_PORT)
        foreign = snapshot["bot_state"] == BOT_STATE_FOREIGN_PORT
        _selftest_step(
            lines,
            not foreign,
            "②端口探测",
            f"8080 {'开放' if port_open else '关闭'}（归属 pid {owners or '—'}）；"
            f"3001 {'开放' if napcat_open else '关闭'}"
            + ("；⚠️ 端口被无关进程占用" if foreign else ""),
        )
        _selftest_step(
            lines,
            True,
            "③进程发现",
            f"bot 实例={snapshot.get('bot_instances', 0)}（进程树 pids={snapshot['bot_pids']}）"
            f" state={snapshot['bot_state']}；napcat pids={snapshot['napcat_pids']}",
        )
        base = client.base_url
        token = load_token(origin=base) or ""
        online = client.health_ready() is not None
        _selftest_step(
            lines,
            online,
            "④health+token 探活",
            f"health={'ready' if online else '不可达（离线）'}；token={'已配置' if token else '未配置'}",
        )
        try:
            status, payload = client.desktop_summary(token or None, origin=base)
        except ConnectionError:
            status, payload = 0, None
        if status == 200 and isinstance(payload, dict) and payload.get("ok") is True:
            _selftest_step(lines, True, "⑤desktop API 探测", "已开启且可访问")
        elif status == 401:
            _selftest_step(lines, False, "⑤desktop API 探测", "已开启但令牌无效/缺失")
        elif status == 404:
            _selftest_step(
                lines, False, "⑤desktop API 探测", "未开启（TAFFY_DESKTOP_API_ENABLED=false）"
            )
        elif status == 0:
            _selftest_step(lines, False, "⑤desktop API 探测", "Bot 离线，开关状态未知")
        else:
            _selftest_step(lines, False, "⑤desktop API 探测", f"不可达（status={status}）")

    vendor = FRONTEND_INDEX.parent / "vendor"
    try:
        vue_ok = (vendor / "vue.global.prod.js").stat().st_size > 1000
        echarts_ok = (vendor / "echarts.min.js").stat().st_size > 10_000
    except OSError:
        vue_ok = echarts_ok = False
    _selftest_step(
        lines,
        vue_ok and echarts_ok,
        "⑥vendor 库完整性",
        f"vue={'OK' if vue_ok else '缺失'} echarts={'OK' if echarts_ok else '缺失'}",
    )

    probe_lock = SingleInstanceLock(appdata_dir())
    acquired = probe_lock.acquire()
    probe_lock.release()
    _selftest_step(lines, acquired, "⑦单实例锁", "可用" if acquired else "被其他实例占用")

    print("\n".join(lines))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="taffy-desktop-studio")
    parser.add_argument("--self-test", action="store_true", help="运行七步自检后退出")
    parser.add_argument("--minimized", action="store_true", help="启动即最小化到托盘")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")

    repo_root = find_repo_root(Path(__file__).resolve())
    supervisor = _build_supervisor(repo_root) if repo_root is not None else None
    client = BotApiClient()
    appdata = appdata_dir()
    lock = SingleInstanceLock(appdata)
    bridge = Bridge(supervisor, client, lock)

    if args.self_test:
        return run_self_test(repo_root)

    if not lock.acquire():
        lock.activate_existing()
        print("塔菲桌面工作台已在运行；已请求激活现有窗口。")
        return 0

    quit_requested = threading.Event()
    window_holder: dict[str, Any] = {}

    from .notify import NotificationCenter

    center = NotificationCenter(rules=load_settings().notify_rules)
    poller = EventPoller(client, center)
    poller.start()

    # 远程隧道托管：软件开着 = 隧道开着（外部已开则接管），退出时统一关闭。
    tunnel = TunnelManager(
        remote_host=os.environ.get("TAFFY_SSH_HOST", ""),
        remote_user=os.environ.get("TAFFY_SSH_USER", ""),
        key_file=Path(os.environ["TAFFY_SSH_KEY"]) if os.environ.get("TAFFY_SSH_KEY") else None,
    )
    tunnel.start()

    tray = TrayIcon(
        None,  # Use the native status icon; no third-party character artwork.
        actions=build_tray_actions(
            open_window=lambda: _show_window(window_holder),
            start_bot=lambda: supervisor.start_bot() if supervisor else None,
            stop_bot=lambda: supervisor.stop_bot() if supervisor else None,
            restart_bot=lambda: supervisor.restart_bot() if supervisor else None,
            quit_app=_request_quit,
        ),
    )

    def poll_activation() -> None:
        while not quit_requested.is_set():
            if bridge.consume_activation():
                _show_window(window_holder)
            quit_requested.wait(2.0)

    threading.Thread(target=poll_activation, name="taffy-activate", daemon=True).start()

    if supervisor is not None:
        supervisor.start()
    tray.start()

    try:
        import webview

        # file:// 页面的 ES module 加载受 CORS 限制（origin null），必须放行
        # 同盘符文件访问；WebView2 在创建环境时读取该变量。
        if FRONTEND_INDEX.is_file():
            os.environ.setdefault(
                "WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS",
                "--allow-file-access-from-files",
            )
        url = FRONTEND_INDEX.as_uri() if FRONTEND_INDEX.is_file() else "https://example.invalid/"
        window = webview.create_window(
            "塔菲桌面工作台",
            url,
            js_api=bridge,
            width=1440,
            height=900,
            min_size=(1100, 700),
            hidden=bool(args.minimized),
        )
        window_holder["window"] = window
        _window_holder_ref.update(window_holder)
        current_window = _window_holder_ref.get("window")

        # 关窗事件 → 读取 minimized_to_tray_on_close：true 时隐藏进托盘
        # （进程保活，托盘可唤起）；false 保持现行为（关窗即退出）。
        def _on_closing() -> None:
            try:
                minimize_to_tray = load_settings().minimized_to_tray_on_close
            except Exception:  # noqa: BLE001 - settings read must never block closing
                minimize_to_tray = False
            current = current_window
            if minimize_to_tray and tray.available and current is not None:
                try:
                    current.hide()
                    _log.info("Window hidden to tray (minimized_to_tray_on_close)")
                    return
                except Exception as exc:  # noqa: BLE001 - fall through to real close
                    _log.warning("Hide-to-tray failed error=%s", type(exc).__name__)
            quit_requested.set()

        try:
            if current_window is not None:
                current_window.events.closing += _on_closing
        except Exception as exc:  # noqa: BLE001 - event wiring must not break startup
            _log.warning("Closing event wiring failed error=%s", type(exc).__name__)

        webview.start(storage_path=str(_webview_storage_dir()))
    except Exception as exc:
        _log.error("Window loop failed error=%s", type(exc).__name__)
        print(f"无法创建窗口：{type(exc).__name__}（需要 WebView2 运行时）")
        return 1
    finally:
        quit_requested.set()
        poller.stop()
        tunnel.stop()
        if supervisor is not None:
            # F7：关软件不停 bot —— 只停止看护线程。
            supervisor.shutdown()
        tray.stop()
        lock.release()
    return 0


_window_holder_ref: dict[str, Any] = {}


def _webview_storage_dir() -> Path:
    """WebView2 数据目录固定到应用目录；软件版本变更时清空重建。

    防止旧版本页面缓存串扰（典型症状：升级后设置页仍显示旧卡片/旧 UI）。
    """

    target = appdata_dir() / "webview-data"
    version_file = appdata_dir() / "webview-version.txt"
    try:
        if version_file.exists():
            if version_file.read_text("utf-8").strip() != __version__:
                shutil.rmtree(target, ignore_errors=True)
        version_file.parent.mkdir(parents=True, exist_ok=True)
        version_file.write_text(__version__, encoding="utf-8")
    except OSError:
        pass
    return target


def _show_window(holder: dict[str, Any]) -> None:
    window = holder.get("window") or _window_holder_ref.get("window")
    if window is None:
        return
    try:
        window.show()
    except Exception as exc:  # noqa: BLE001
        _log.warning("Window show failed error=%s", type(exc).__name__)


def _request_quit() -> None:
    """托盘退出：先尝试优雅关闭窗口（走 main 的 finally 清理链，
    含隧道/看护/托盘/锁），失败才强退兜底。"""

    import os

    window = _window_holder_ref.get("window")
    if window is not None:
        try:
            window.destroy()
            return
        except Exception as exc:  # noqa: BLE001 - fall through to hard exit
            _log.warning("Graceful quit failed error=%s", type(exc).__name__)
    os._exit(0)


if __name__ == "__main__":
    raise SystemExit(main())
