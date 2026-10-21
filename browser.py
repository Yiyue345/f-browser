"""Single-threaded Playwright Chromium controller.

Playwright objects stay on one dedicated worker thread. The host owns the
WebVPN credential store and session generation; this plugin never reads it.
"""
from __future__ import annotations

import queue
import threading
import time
from concurrent.futures import Future
from urllib.parse import urlsplit

from fuckclassroom.auth.webvpn import (
    GATEWAY_URL, safe_history_url, validate_webvpn_url,
)

_GATEWAY_HOST = "v.guet.edu.cn"
_CAS_HOST = "cas.guet.edu.cn"


class BrowserError(RuntimeError):
    pass


def _gateway_login_url(url: str) -> bool:
    """A gateway/CAS redirect is a *signal to verify*, not proof of expiry."""
    parsed = urlsplit(url)
    if parsed.hostname == _CAS_HOST:
        return True
    return parsed.hostname == _GATEWAY_HOST and (
        parsed.path.lower().startswith("/login")
        or parsed.path.lower().startswith("/wengine-auth/login")
    )


class BrowserController:
    def __init__(self, session, store, *, launcher=None) -> None:
        self.session = session
        self.store = store
        self._launcher = launcher
        self._requests: queue.Queue = queue.Queue()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._state = {
            "running": False, "url": "", "title": "", "can_back": False,
            "can_forward": False, "message": "浏览器尚未启动",
            "recovery": "idle",
        }
        # The fields below are *only* used by the controller's worker thread.
        self._playwright = None
        self._browser = None
        self._context = None
        self._page = None
        self._positions: list[str] = []
        self._cursor = -1
        self._last_document_method = "GET"
        self._safe_get_url = GATEWAY_URL
        self._needs_recovery = False
        self._recovery_attempted = False
        self._user_logout = False
        self._dirty = False
        self._last_save = 0.0

    def state(self) -> dict:
        with self._lock:
            return dict(self._state)

    def _set(self, **updates) -> None:
        with self._lock:
            self._state.update(updates)

    def send(self, command: str, value: str = "", *, timeout: int = 50) -> dict:
        if command not in {"open", "navigate", "back", "forward", "reload", "close", "reset"}:
            raise BrowserError("无效的浏览器命令")
        with self._lock:
            if self._thread is None or not self._thread.is_alive():
                self._thread = threading.Thread(
                    target=self._run, daemon=True, name="f-browser-chromium"
                )
                self._thread.start()
        future: Future = Future()
        self._requests.put((command, value, future))
        try:
            return future.result(timeout=timeout)
        except TimeoutError as exc:
            raise BrowserError("浏览器操作超时，请检查 Chromium 或 WebVPN 连接") from exc

    def stop(self) -> None:
        with self._lock:
            thread = self._thread
        if thread is not None and thread.is_alive():
            try:
                self.send("close", timeout=35)
            except (RuntimeError, TimeoutError):
                pass
            thread.join(timeout=8)

    def _run(self) -> None:
        try:
            while True:
                try:
                    command, value, future = self._requests.get(timeout=0.35)
                except queue.Empty:
                    self._idle()
                    continue
                try:
                    if command == "close":
                        self._shutdown(save=True)
                        future.set_result(self.state())
                        break
                    if command == "reset":
                        self._shutdown(save=False)
                        future.set_result(self.state())
                        continue
                    if command == "open":
                        self._open(value or GATEWAY_URL)
                    elif command == "navigate":
                        self._navigate(value)
                    elif command == "back":
                        self._history_move(-1)
                    elif command == "forward":
                        self._history_move(1)
                    elif command == "reload":
                        self._reload()
                    self._poll_page()
                    future.set_result(self.state())
                except Exception as exc:
                    self._set(message=str(exc))
                    future.set_exception(BrowserError(str(exc)))
        finally:
            self._shutdown(save=True)
            # Signal queued callers rather than leaving HTTP tasks hanging.
            while True:
                try:
                    _, _, future = self._requests.get_nowait()
                except queue.Empty:
                    break
                future.set_exception(BrowserError("浏览器已停止"))
            with self._lock:
                self._thread = None

    def _open(self, requested: str) -> None:
        url = validate_webvpn_url(requested)
        if self._page is not None and not self._page.is_closed():
            self._page.bring_to_front()
            if url != GATEWAY_URL:
                self._navigate(url)
            return
        status = self.session.ensure_authenticated(interactive_allowed=False)
        if not status.is_authenticated:
            raise BrowserError(f"WebVPN 尚未就绪：{status.reason}；请使用「重新连接」完成登录")
        self._build_context()
        self._goto(url)

    def _build_context(self) -> None:
        if self._browser is None:
            if self._launcher is not None:
                self._playwright, self._browser = self._launcher()
            else:
                from playwright.sync_api import sync_playwright
                self._playwright = sync_playwright().start()
                try:
                    self._browser = self._playwright.chromium.launch(headless=False)
                except Exception:
                    self._playwright.stop()
                    self._playwright = None
                    raise BrowserError(
                        "Chromium 启动失败。请执行 python -m playwright install chromium"
                    )
        self._context = self.session.new_browser_context(self._browser)
        self._page = self._context.new_page()
        self._page.on("request", self._request_seen)
        self._page.on("framenavigated", self._navigation_seen)
        self._positions, self._cursor = [], -1
        self._dirty = False
        self._needs_recovery = False
        self._recovery_attempted = False
        self._user_logout = False
        self._set(running=True, message="浏览器已打开", recovery="idle")

    def _request_seen(self, request) -> None:
        try:
            if request.is_navigation_request() and request.frame == self._page.main_frame:
                self._last_document_method = request.method.upper()
                parts = urlsplit(request.url)
                if parts.hostname == _GATEWAY_HOST and parts.path.lower().startswith("/logout"):
                    self._user_logout = True
        except Exception:
            pass

    def _navigation_seen(self, frame) -> None:
        if self._page is None or frame != self._page.main_frame:
            return
        url = frame.url
        if _gateway_login_url(url):
            self._needs_recovery = True
        try:
            safe = safe_history_url(url)
        except Exception:
            return
        if self._last_document_method == "GET":
            self._safe_get_url = safe
        if self._cursor >= 0 and safe == self._positions[self._cursor]:
            return
        if self._cursor > 0 and safe == self._positions[self._cursor - 1]:
            self._cursor -= 1
        elif self._cursor + 1 < len(self._positions) and safe == self._positions[self._cursor + 1]:
            self._cursor += 1
        else:
            self._positions = self._positions[:self._cursor + 1] + [safe]
            self._cursor += 1
        self._dirty = True

    def _goto(self, target: str) -> None:
        self._last_document_method = "GET"
        self._recovery_attempted = False
        self._page.goto(target, wait_until="domcontentloaded", timeout=25000)
        self._poll_page()

    def _navigate(self, url: str) -> None:
        safe_target = validate_webvpn_url(url)
        if self._page is None or self._page.is_closed():
            self._open(safe_target)
            return
        self._goto(safe_target)

    def _history_move(self, direction: int) -> None:
        if self._page is None or self._page.is_closed():
            raise BrowserError("请先打开浏览器")
        if direction < 0 and self._cursor <= 0:
            raise BrowserError("没有更早的网页")
        if direction > 0 and self._cursor >= len(self._positions) - 1:
            raise BrowserError("没有可前进的网页")
        # Use Chromium's real back/forward, not a reconstructed GET replay:
        # a form submission must never be submitted a second time.
        if direction < 0:
            self._page.go_back(wait_until="domcontentloaded", timeout=20000)
        else:
            self._page.go_forward(wait_until="domcontentloaded", timeout=20000)

    def _reload(self) -> None:
        if self._page is None or self._page.is_closed():
            raise BrowserError("请先打开浏览器")
        # Chromium native refresh may ask before resubmitting a POST. Do not
        # programmatically reload a known non-GET document.
        if self._last_document_method != "GET":
            raise BrowserError("当前页面由表单提交产生，自动刷新可能重复提交；请在浏览器窗口自行确认")
        self._page.reload(wait_until="domcontentloaded", timeout=20000)

    def _poll_page(self) -> None:
        page = self._page
        if page is None:
            return
        if page.is_closed():
            self._shutdown(save=True)
            return
        try:
            page.wait_for_timeout(20)
            title = " ".join(page.title().split())[:180]
            url = safe_history_url(page.url)
            if self._dirty:
                self.store.record(url, title or "校园内网页面")
                self._dirty = False
            self._set(
                running=True, title=title, url=url,
                can_back=self._cursor > 0,
                can_forward=self._cursor + 1 < len(self._positions),
            )
        except Exception:
            # Authentication and intermediate CAS pages must never be sent to UI.
            self._set(running=True, title="正在验证或跳转", url="", can_back=False, can_forward=False)

    def _idle(self) -> None:
        if self._page is None:
            return
        self._poll_page()
        if self._user_logout:
            # Gateway logout explicitly triggered by the user in Chromium.
            self._user_logout = False
            self._shutdown(save=False)
            self.session.clear_session()
            self._set(message="WebVPN 已主动退出，请手动重新连接", recovery="logged_out")
            return
        if self._needs_recovery and not self._recovery_attempted:
            self._needs_recovery = False
            self._recovery_attempted = True
            self._recover_if_expired()
        if time.monotonic() - self._last_save >= 12:
            self._save()

    def _recover_if_expired(self) -> None:
        # A CAS redirect might be a target application's separate CAS login.
        # Only a gateway probe is authoritative; 403 on an internal site is not.
        status = self.session.verify()
        if status.state not in ("expired", "login_required"):
            self._set(recovery=status.state if status.state != "authenticated" else "idle")
            return
        self._set(recovery="recovering", message="WebVPN 网关会话过期，正在恢复认证")
        result = self.session.ensure_authenticated(interactive_allowed=False)
        if not result.is_authenticated:
            self._set(recovery=result.state, message=result.reason)
            return
        target = self._safe_get_url if self._last_document_method == "GET" else ""
        # Discard stale context without committing old credentials.
        self._close_context(save=False)
        self._build_context()
        if target:
            try:
                self._page.goto(target, wait_until="domcontentloaded", timeout=25000)
                self._set(recovery="idle", message="WebVPN 已恢复，安全页面已重新打开")
            except Exception:
                self._set(recovery="failed", message="登录已恢复，但原网页打开失败；请手动刷新")
        else:
            self._set(recovery="idle", message="WebVPN 已恢复；表单提交不会自动重放，请手动继续")

    def _save(self) -> None:
        if self._context is None:
            return
        self._last_save = time.monotonic()
        try:
            self.session.save_browser_state(self._context)
        except Exception:
            # Account switched/logout in another plugin: NEVER overwrite its
            # freshly established shared session with this stale browser.
            self._set(message="WebVPN 账号或会话已变化；请重新打开浏览器", recovery="failed")

    def _close_context(self, *, save: bool) -> None:
        if save:
            self._save()
        context = self._context
        self._page, self._context = None, None
        if context is not None:
            try:
                context.close()
            except Exception:
                pass

    def _shutdown(self, *, save: bool) -> None:
        self._close_context(save=save)
        if self._browser is not None:
            try:
                self._browser.close()
            except Exception:
                pass
        self._browser = None
        if self._playwright is not None:
            try:
                self._playwright.stop()
            except Exception:
                pass
        self._playwright = None
        self._positions, self._cursor = [], -1
        self._set(
            running=False, url="", title="", can_back=False, can_forward=False,
            message="浏览器已关闭",
        )
