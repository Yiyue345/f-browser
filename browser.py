"""Visible Chromium integration for FuckClassroom's shared WebVPN session.

Playwright objects only live on the dedicated browser thread. Credentials and
WebVPN authentication remain exclusively owned by the host's session broker.
"""
from __future__ import annotations

import queue
import threading
import time
from concurrent.futures import Future, TimeoutError as FutureTimeout
from urllib.parse import urlsplit, urlunsplit

from fuckclassroom.auth.webvpn import (
    GATEWAY_URL, safe_history_url, validate_webvpn_url,
)

_GATEWAY = "v.guet.edu.cn"
_CAS = "cas.guet.edu.cn"


class BrowserError(RuntimeError):
    pass


def _display_url(url: str) -> str:
    """Show a URL without exposing query-string CAS tickets or URL fragments."""
    try:
        parsed = urlsplit(str(url))
        if (parsed.scheme != "https" or parsed.hostname not in {_GATEWAY, _CAS}
                or parsed.username or parsed.password or parsed.port not in (None, 443)):
            return ""
        return urlunsplit(("https", parsed.netloc, parsed.path or "/", "", ""))
    except (ValueError, TypeError, AttributeError):
        return ""


def _login_page(url: str) -> bool:
    """Only for display; never used as an excuse to reauthenticate a site."""
    try:
        p = urlsplit(str(url))
        if p.scheme != "https" or p.hostname not in {_GATEWAY, _CAS}:
            return False
        path = p.path.lower()
        return any(token in path for token in ("/authserver/login", "/wengine-auth/login")) or (
            p.hostname == _GATEWAY and path in ("/login", "/login/")
        )
    except (ValueError, TypeError, AttributeError):
        return False


class BrowserController:
    """Single Playwright worker. Web page navigation is handled by Chromium."""

    COMMANDS = frozenset({"open", "navigate", "back", "forward", "reload", "close", "sync"})

    def __init__(self, session, store, *, launcher=None):
        self.session = session
        self.store = store
        self._launcher = launcher
        self._requests: queue.Queue = queue.Queue()
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._command_inflight = False
        self._state = {
            "running": False, "busy": False, "page_status": "closed",
            "url": "", "title": "", "can_back": False, "can_forward": False,
            "message": "浏览器尚未打开",
        }

        # Only the Playwright worker accesses these objects/fields.
        self._playwright = None
        self._browser = None
        self._context = None
        self._page = None
        self._observed_pages: set = set()
        self._positions: list[str] = []
        self._cursor = -1
        self._last_method = "GET"
        self._safe_get = GATEWAY_URL
        self._context_stamp = None
        self._last_sync_check = 0.0
        self._last_save = 0.0
        self._dirty = False
        self._logged_out = False

    def state(self) -> dict:
        with self._lock:
            return dict(self._state)

    def _set(self, **changes):
        with self._lock:
            self._state.update(changes)

    @staticmethod
    def _stamp(status) -> tuple:
        # checked_at changes after a host-initiated verification/login.
        # No cookies or credentials are read by this plugin.
        return (status.state, getattr(status, "checked_at", None))

    def send(self, command: str, value: str = "", *, timeout: int = 50) -> dict:
        if command not in self.COMMANDS:
            raise BrowserError("不支持的浏览器命令")
        with self._lock:
            if self._command_inflight:
                raise BrowserError("浏览器导航操作正在执行，请稍后再试")
            self._command_inflight = True
            self._state["busy"] = True
            try:
                if self._thread is None or not self._thread.is_alive():
                    self._thread = threading.Thread(
                        target=self._run, daemon=True, name="f-browser-visible-chromium"
                    )
                    self._thread.start()
            except Exception:
                self._command_inflight = False
                self._state["busy"] = False
                raise
        result = Future()
        self._requests.put((command, value, result))
        try:
            return result.result(timeout=timeout)
        except FutureTimeout as exc:
            raise BrowserError("浏览器响应超时；操作可能仍在运行，请查看浏览器窗口") from exc

    def stop(self):
        with self._lock:
            thread = self._thread
        if thread is None or not thread.is_alive():
            return
        result = Future()
        self._requests.put(("close", "", result))
        try:
            result.result(timeout=30)
        except (FutureTimeout, BrowserError):
            pass
        thread.join(timeout=5)

    def _run(self):
        # Sync Playwright needs its event loop pumped to deliver navigation,
        # popup, load and close events. Do not gate maintenance on queue idleness.
        next_maintenance = time.monotonic()
        try:
            while True:
                try:
                    command, value, result = self._requests.get(timeout=0.2)
                except queue.Empty:
                    command = None
                if command is not None:
                    try:
                        if command == "close":
                            self._shutdown(save=True)
                            result.set_result(self.state())
                            break
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
                        elif command == "sync":
                            self._refresh_context(force=True)
                        result.set_result(self.state())
                    except Exception as exc:
                        self._set(page_status="error", message=str(exc))
                        result.set_exception(BrowserError(str(exc)))
                    finally:
                        with self._lock:
                            self._command_inflight = False
                            self._state["busy"] = False
                if time.monotonic() >= next_maintenance:
                    next_maintenance = time.monotonic() + 1.0
                    try:
                        self._maintain()
                    except Exception:
                        self._set(message="浏览器会话同步暂不可用，网页仍可正常操作")
        finally:
            self._shutdown(save=False)
            while True:
                try:
                    _, _, result = self._requests.get_nowait()
                except queue.Empty:
                    break
                if not result.done():
                    result.set_exception(BrowserError("浏览器已关闭"))
            with self._lock:
                self._thread = None
                self._command_inflight = False
                self._state["busy"] = False

    def _open(self, requested):
        target = validate_webvpn_url(requested)
        if self._page is not None and not self._page.is_closed():
            self._page.bring_to_front()
            if target != GATEWAY_URL:
                self._navigate(target)
            return
        status = self.session.get_status()
        if status.state == "logged_out":
            raise BrowserError("WebVPN 已主动退出，请先通过主程序重新登录")
        self._build_context()
        # Record which shared session snapshot this Context was made from.
        self._context_stamp = self._stamp(status)
        self._goto(target)

    def _build_context(self):
        if self._browser is None:
            if self._launcher is not None:
                self._playwright, self._browser = self._launcher()
            else:
                from playwright.sync_api import sync_playwright
                self._playwright = sync_playwright().start()
                try:
                    self._browser = self._playwright.chromium.launch(headless=False)
                except Exception as exc:
                    self._playwright.stop()
                    self._playwright = None
                    raise BrowserError(
                        "无法启动 Chromium，请运行 python -m playwright install chromium"
                    ) from exc
        self._context = self.session.new_browser_context(self._browser)
        self._context.on("page", self._attach_page)
        self._observed_pages.clear()
        self._positions = []
        self._cursor = -1
        self._dirty = False
        page = self._context.new_page()
        self._attach_page(page)
        self._set(
            running=True, page_status="loading",
            url="", title="", can_back=False, can_forward=False,
            message="已打开独立 Chromium 窗口",
        )

    def _attach_page(self, page):
        if page in self._observed_pages:
            return
        self._observed_pages.add(page)
        self._page = page
        self._positions = []
        self._cursor = -1
        self._last_method = "GET"
        self._safe_get = GATEWAY_URL
        page.on("request", lambda request, p=page: self._request_seen(p, request))
        page.on("framenavigated", lambda frame, p=page: self._navigation_seen(p, frame))
        page.on("domcontentloaded", lambda p=page: self._document_loaded(p))
        page.on("load", lambda p=page: self._document_loaded(p))
        page.on("close", lambda p=page: self._page_closed(p))
        self._set(running=True, page_status="loading")

    def _request_seen(self, page, request):
        try:
            if page is not self._page or (
                not request.is_navigation_request() or request.frame != page.main_frame
            ):
                return
            self._last_method = request.method.upper()
            self._set(page_status="loading", message="正在打开网页")
            # Track explicit WebVPN logout, not a campus site's own logout.
            p = urlsplit(request.url)
            if p.hostname == _GATEWAY and p.path.lower() in ("/logout", "/logout/"):
                self._logged_out = True
        except Exception:
            pass

    def _navigation_seen(self, page, frame):
        if page is not self._page or frame != page.main_frame:
            return
        url = _display_url(frame.url)
        if not url:
            # A temporary redirect or about:blank is not necessarily failure.
            self._set(page_status="loading", message="正在导航")
            return
        login = _login_page(frame.url)
        self._set(
            url=url,
            page_status="login" if login else "loading",
            message="登录页已打开，可直接在 Chromium 中操作" if login else "正在加载网页",
        )
        # Login URLs are visible in the status but never saved to history.
        try:
            safe = safe_history_url(frame.url)
        except Exception:
            return
        if self._last_method == "GET":
            self._safe_get = safe
        if self._cursor >= 0 and self._positions[self._cursor] == safe:
            return
        if self._cursor > 0 and self._positions[self._cursor - 1] == safe:
            self._cursor -= 1
        elif self._cursor + 1 < len(self._positions) and self._positions[self._cursor + 1] == safe:
            self._cursor += 1
        else:
            self._positions = self._positions[:self._cursor + 1] + [safe]
            self._cursor += 1
        self._dirty = True
        self._set(can_back=self._cursor > 0,
                  can_forward=self._cursor < len(self._positions) - 1)

    def _document_loaded(self, page):
        if page is not self._page or page.is_closed():
            return
        # Authentication pages still have a genuine title even though their
        # URLs must be omitted from persisted history.
        url = _display_url(page.url)
        try:
            title = " ".join(str(page.title()).split())[:180]
        except Exception:
            title = ""
        status = "login" if _login_page(page.url) else "ready"
        self._set(
            title=title or ("学校统一身份认证" if status == "login" else "校园网页"),
            url=url,
            page_status=status,
            message="登录页可直接操作" if status == "login" else "页面已加载",
        )
        if self._dirty and status == "ready":
            try:
                self.store.record(page.url, title or "校园网页")
            except Exception:
                self._set(message="网页已加载，但历史记录保存失败")
            else:
                self._dirty = False
        # Persist a user-completed WebVPN login through the host broker;
        # never invoke any credential submission or retry logic here.
        self._last_save = 0.0

    def _page_closed(self, page):
        self._observed_pages.discard(page)
        if page is not self._page:
            return
        candidates = [
            other for other in self._observed_pages
            if not other.is_closed()
        ]
        if candidates:
            self._page = candidates[-1]
            self._document_loaded(self._page)
        else:
            self._page = None
            self._set(
                running=False, page_status="closed", url="", title="",
                can_back=False, can_forward=False,
                message="浏览器网页窗口已关闭",
            )

    def _goto(self, target):
        if self._page is None or self._page.is_closed():
            raise BrowserError("浏览器尚未打开")
        self._page.goto(target, wait_until="domcontentloaded", timeout=25000)

    def _navigate(self, value):
        target = validate_webvpn_url(value)
        if self._page is None or self._page.is_closed():
            self._open(target)
        else:
            self._goto(target)

    def _history_move(self, direction):
        page = self._page
        if page is None or page.is_closed():
            raise BrowserError("请先打开浏览器")
        # Chromium native navigation will never re-POST form data from plugin code.
        if direction < 0:
            page.go_back(wait_until="domcontentloaded", timeout=20000)
        else:
            page.go_forward(wait_until="domcontentloaded", timeout=20000)

    def _reload(self):
        page = self._page
        if page is None or page.is_closed():
            raise BrowserError("请先打开浏览器")
        if self._last_method != "GET":
            raise BrowserError("这是表单提交页面，请在 Chromium 中自行决定是否重新提交")
        page.reload(wait_until="domcontentloaded", timeout=20000)

    def _refresh_context(self, *, force=False):
        if self._page is None or self._context is None:
            return
        status = self.session.get_status()
        new_stamp = self._stamp(status)
        if status.state == "logged_out":
            self._close_context(save=False)
            self._set(
                running=False, page_status="closed", message="主程序已退出 WebVPN",
            )
            return
        if not force and (not status.is_authenticated or new_stamp == self._context_stamp):
            return
        # Only a known safe, non-login GET page is eligible for automatic reload.
        target = self._safe_get if self._last_method == "GET" else GATEWAY_URL
        try:
            safe_history_url(target)
        except Exception:
            target = GATEWAY_URL
        self._close_context(save=False)
        try:
            self._build_context()
            self._context_stamp = new_stamp
            self._goto(target)
            self._set(message="已载入主程序更新后的共享 WebVPN 会话")
        except Exception:
            self._set(page_status="error", message="共享会话载入失败，请重新打开浏览器")

    def _maintain(self):
        if self._page is None:
            return
        if self._page.is_closed():
            self._page_closed(self._page)
            return
        # Process real Playwright events from the visible browser UI without
        # screenshots or remote key/mouse forwarding.
        self._page.wait_for_timeout(60)
        if self._logged_out:
            self._logged_out = False
            self._close_context(save=False)
            self.session.clear_session()
            self._set(running=False, page_status="closed", message="WebVPN 已主动退出")
            return
        self._refresh_context()
        if self._context is not None and time.monotonic() - self._last_save >= 12:
            self._last_save = time.monotonic()
            try:
                self.session.save_browser_state(self._context)
            except Exception:
                # A generation mismatch means another module replaced the
                # session. Never overwrite its fresh cookies.
                self._close_context(save=False)
                self._set(
                    running=False, page_status="closed",
                    message="主程序登录会话已变更，旧浏览器已关闭以保护新会话",
                )

    def _close_context(self, *, save):
        context = self._context
        self._context = None
        if save and context is not None:
            try:
                self.session.save_browser_state(context)
            except Exception:
                pass
        self._page = None
        self._observed_pages.clear()
        if context is not None:
            try:
                context.close()
            except Exception:
                pass

    def _shutdown(self, *, save):
        self._close_context(save=save)
        browser, playwright = self._browser, self._playwright
        self._browser = None
        self._playwright = None
        if browser is not None:
            try:
                browser.close()
            except Exception:
                pass
        if playwright is not None:
            try:
                playwright.stop()
            except Exception:
                pass
        self._positions, self._cursor = [], -1
        self._context_stamp = None
        self._logged_out = False
        self._set(
            running=False, page_status="closed",
            url="", title="", can_back=False, can_forward=False,
            message="浏览器已关闭",
        )
