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
        self._command_inflight = False
        self._state = {
            "running": False, "busy": False, "url": "", "title": "", "can_back": False,
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
        self._manual_login_seen = False
        self._manual_verify_pending = False
        self._autofill_pending = False
        self._autofill_submitted = False
        self._autofill_started = 0.0
        self._autofill_next_try = 0.0
        self._allow_auto_recovery = False
        self._user_logout = False
        self._dirty = False
        self._observed_pages: set = set()
        self._recovery_future: Future | None = None
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
            if self._command_inflight:
                raise BrowserError("已有浏览器操作正在执行，请等待当前任务结束，不要重复点击")
            if self._state.get("recovery") in {"checking", "recovering"} and command in {
                "open", "navigate", "back", "forward", "reload"
            }:
                raise BrowserError("WebVPN 正在后台验证或恢复登录，请等待完成后再导航")
            self._command_inflight = True
            self._state["busy"] = True
            try:
                if self._thread is None or not self._thread.is_alive():
                    self._thread = threading.Thread(
                        target=self._run, daemon=True, name="f-browser-chromium"
                    )
                    self._thread.start()
            except Exception:
                self._command_inflight = False
                self._state["busy"] = False
                raise
        future: Future = Future()
        self._requests.put((command, value, future))
        try:
            return future.result(timeout=timeout)
        except TimeoutError as exc:
            # The worker may still be finishing the operation. Do not enqueue
            # duplicate requests or pretend the underlying command was cancelled.
            raise BrowserError("当前浏览器操作等待超时，但后台仍可能正在处理；请先查看连接状态再重试") from exc

    def stop(self) -> None:
        # Runtime shutdown must always be allowed to queue a close, even
        # when an HTTP request is currently awaiting a navigation.
        with self._lock:
            thread = self._thread
        if thread is not None and thread.is_alive():
            done: Future = Future()
            self._requests.put(("close", "", done))
            try:
                done.result(timeout=40)
            except TimeoutError:
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
                    with self._lock:
                        self._command_inflight = False
                        self._state["busy"] = False
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
        # Do NOT require headless authentication before showing a window.
        # CAS/OTP/captcha may require human interaction. Open the real
        # Chromium first so the user can complete the school's own flow.
        status = self.session.get_status()
        if status.state == "logged_out":
            raise BrowserError("WebVPN 已主动退出，请点击「重新连接」后再打开浏览器")
        self._allow_auto_recovery = bool(status.is_authenticated)
        self._build_context()
        self._goto(url)
        if not status.is_authenticated:
            self._set(
                message="已打开 Chromium：请在新窗口内完成学校 WebVPN/CAS 登录；如曾主动退出，请点击重新连接",
                recovery="login_required",
            )

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
        # Target=_blank is common on WebVPN portals. Follow the newest
        # Chromium page instead of silently ignoring a newly opened tab.
        self._context.on("page", self._attach_page)
        page = self._context.new_page()
        self._attach_page(page)
        self._positions, self._cursor = [], -1
        self._dirty = False
        self._needs_recovery = False
        self._recovery_attempted = False
        self._manual_login_seen = False
        self._manual_verify_pending = False
        self._autofill_pending = False
        self._autofill_submitted = False
        self._autofill_started = 0.0
        self._autofill_next_try = 0.0
        self._user_logout = False
        self._set(running=True, message="浏览器已打开", recovery="idle")

    def _attach_page(self, page) -> None:
        if self._page is not page:
            self._page = page
            self._positions, self._cursor = [], -1
            self._safe_get_url = GATEWAY_URL
            self._last_document_method = "GET"
            self._recovery_attempted = False
        if page in self._observed_pages:
            return
        self._observed_pages.add(page)
        page.on("request", self._request_seen)
        page.on("framenavigated", self._navigation_seen)

    def _request_seen(self, request) -> None:
        try:
            if request.is_navigation_request() and request.frame == self._page.main_frame:
                self._last_document_method = request.method.upper()
                parts = urlsplit(request.url)
                gateway_logout = parts.path.lower() in {"/logout", "/logout/"} or (
                    parts.path.lower().startswith("/wengine-auth/logout")
                )
                if parts.hostname == _GATEWAY_HOST and gateway_logout:
                    self._user_logout = True
        except Exception:
            pass

    def _navigation_seen(self, frame) -> None:
        if self._page is None or frame != self._page.main_frame:
            return
        url = frame.url
        if _gateway_login_url(url):
            self._manual_login_seen = True
            self._needs_recovery = True
            if not self._autofill_submitted:
                self._autofill_pending = True
                if not self._autofill_started:
                    self._autofill_started = time.monotonic()
        try:
            safe = safe_history_url(url)
        except Exception:
            return
        if self._manual_login_seen and not self._allow_auto_recovery:
            # After manual CAS login, verify in a separate thread so the
            # broker learns the newly saved cookies and later expiry can
            # recover automatically. Never probe on the Playwright thread.
            self._manual_login_seen = False
            self._manual_verify_pending = True
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
        if self._autofill_pending:
            self._maybe_submit_saved_login()
        if self._user_logout:
            # Gateway logout explicitly triggered by the user in Chromium.
            self._user_logout = False
            self._shutdown(save=False)
            self.session.clear_session()
            self._set(message="WebVPN 已主动退出，请手动重新连接", recovery="logged_out")
            return
        if self._needs_recovery:
            self._needs_recovery = False
            if self._allow_auto_recovery and not self._recovery_attempted:
                self._recovery_attempted = True
                self._recover_if_expired()
            elif not self._allow_auto_recovery:
                self._set(
                    recovery="login_required",
                    message="请在 Chromium 窗口中完成学校统一身份认证，页面不会被后台登录任务阻塞",
                )
        self._finish_recovery()
        if self._manual_verify_pending and self._recovery_future is None:
            self._manual_verify_pending = False
            if self._save():
                self._verify_manual_login()
        if time.monotonic() - self._last_save >= 12:
            if not self._save():
                self._shutdown(save=False)
                self._set(message="WebVPN 会话或账号已切换，已关闭旧窗口以保护新的登录状态", recovery="failed")

    def _maybe_submit_saved_login(self) -> None:
        """Use the host's saved credentials in the *visible* CAS window.

        Only the trusted WebVPN broker can read the credential store; this
        plugin receives an outcome string, never the username or password.
        Called exclusively on the owning Playwright thread.
        """
        if self._page is None or self._page.is_closed():
            self._autofill_pending = False
            return
        if not _gateway_login_url(self._page.url):
            self._autofill_pending = False
            return
        now = time.monotonic()
        if now < self._autofill_next_try:
            return
        if now - self._autofill_started > 15:
            self._autofill_pending = False
            self._set(message="统一登录表单尚未就绪，请在 Chromium 中手动登录", recovery="verification_required")
            return
        submit = getattr(self.session, "submit_saved_login", None)
        if not callable(submit):
            self._autofill_pending = False
            self._set(
                message="当前主仓尚未提供 WebVPN 自动填写接口，请更新主仓，或在 Chromium 中手动登录",
                recovery="login_required",
            )
            return
        try:
            outcome = submit(self._page)
        except Exception:
            self._autofill_pending = False
            self._set(message="自动填写登录信息失败，请在 Chromium 中手动登录", recovery="failed")
            return
        if outcome == "submitted":
            self._autofill_pending = False
            self._autofill_submitted = True
            self._set(
                message="已使用主仓保存的账号提交统一身份认证；如要求短信或验证码请在 Chromium 完成",
                recovery="login_required",
            )
        elif outcome in {"busy", "not_ready"}:
            self._autofill_next_try = now + 1
        else:
            self._autofill_pending = False
            hints = {
                "verification_required": "学校要求验证码或人工验证，请在 Chromium 中继续",
                "no_credentials": "主仓尚未保存统一账号，请先在账户设置中保存后重新连接",
                "credentials_unavailable": "主仓已保存的统一账号无法读取，请在账户设置中重新保存",
                "logged_out": "WebVPN 已主动退出，请先点击重新连接",
                "not_cas": "当前不是允许自动填写的 CAS 登录页，请手动完成登录",
            }
            self._set(message=hints.get(outcome, "无法自动填写统一账号，请手动登录"), recovery="login_required")

    def _recover_if_expired(self) -> None:
        """Start gateway validation off the Playwright controller thread.

        A broker login can wait for CAS/OTP for over a minute. It must not
        monopolize this controller or make subsequent browser commands queue.
        Only the controller thread itself ever touches Playwright page objects.
        """
        self._set(recovery="checking", message="检测到登录跳转，正在独立验证 WebVPN 网关")
        result: Future = Future()
        self._recovery_future = result

        def work() -> None:
            try:
                status = self.session.verify()
                if status.state not in {"expired", "login_required"}:
                    result.set_result((False, status))
                    return
                recovered = self.session.ensure_authenticated(interactive_allowed=False)
                result.set_result((True, recovered))
            except Exception:
                # Do not include sensitive URL, cookies or CAS data in errors.
                result.set_exception(BrowserError("WebVPN 网关会话验证或恢复出错，请尝试手动重新连接"))

        threading.Thread(
            target=work, daemon=True, name="f-browser-webvpn-recovery"
        ).start()

    def _verify_manual_login(self) -> None:
        self._set(recovery="checking", message="已返回 WebVPN 页面，正在验证浏览器登录状态")
        result: Future = Future()
        self._recovery_future = result

        def work() -> None:
            try:
                result.set_result((False, self.session.verify()))
            except Exception:
                result.set_exception(BrowserError("无法确认浏览器登录状态"))

        threading.Thread(
            target=work, daemon=True, name="f-browser-manual-login-check"
        ).start()

    def _finish_recovery(self) -> None:
        future = self._recovery_future
        if future is None or not future.done():
            return
        self._recovery_future = None
        try:
            expired, result = future.result()
        except Exception:
            self._set(recovery="failed", message="WebVPN 恢复失败，请手动重新连接")
            return
        if not expired:
            if result.is_authenticated:
                self._allow_auto_recovery = True
            self._set(
                recovery="idle" if result.is_authenticated else result.state,
                message="WebVPN 网关已认证，可以继续浏览" if result.is_authenticated else result.reason,
            )
            return
        if not result.is_authenticated:
            self._set(recovery=result.state, message=(
                f"WebVPN 自动恢复未完成：{result.reason}。"
                "请在浏览器中完成学校认证，或点击「重新连接」"
            ))
            return
        target = self._safe_get_url if self._last_document_method == "GET" else ""
        # The new Context acquires the freshly committed generation/cookies.
        # Never save the expired context after account switching.
        self._close_context(save=False)
        self._build_context()
        # A second CAS redirect must not initiate an infinite retry loop.
        self._recovery_attempted = True
        if target:
            try:
                self._page.goto(target, wait_until="domcontentloaded", timeout=25000)
                self._set(recovery="idle", message="WebVPN 会话已恢复，安全 GET 页面已重新打开")
            except Exception:
                self._set(recovery="failed", message="会话已恢复，但原网页无法打开，请手动刷新")
        else:
            self._set(recovery="idle", message="WebVPN 会话已恢复；不会自动重新提交表单")

    def _save(self) -> bool:
        if self._context is None:
            return True
        self._last_save = time.monotonic()
        try:
            self.session.save_browser_state(self._context)
            return True
        except Exception:
            # Account switched/logout in another plugin: NEVER overwrite its
            # freshly established shared session with this stale browser.
            self._set(message="WebVPN 账号或会话已变化；请重新打开浏览器", recovery="failed")
            return False

    def _close_context(self, *, save: bool) -> None:
        if save:
            self._save()
        context = self._context
        self._page, self._context = None, None
        self._observed_pages.clear()
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
        self._needs_recovery = False
        self._recovery_attempted = False
        self._manual_login_seen = False
        self._manual_verify_pending = False
        self._autofill_pending = False
        self._autofill_submitted = False
        self._autofill_started = 0.0
        self._autofill_next_try = 0.0
        self._recovery_future = None
        self._set(
            running=False, url="", title="", can_back=False, can_forward=False,
            message="浏览器已关闭", recovery="idle",
        )
