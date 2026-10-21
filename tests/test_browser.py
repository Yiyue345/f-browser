"""Offline smoke tests for real f-browser modules; no campus credentials needed."""
from __future__ import annotations

import importlib.util
import sqlite3
import sys
import threading
import time
import tempfile
import types
import unittest
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlsplit, urlunsplit

ROOT = Path(__file__).resolve().parents[1]


def safe_url(raw):
    parts = urlsplit(raw)
    if parts.scheme != "https" or parts.netloc != "v.guet.edu.cn":
        raise ValueError("not a gateway URL")
    if parts.path.startswith(("/login", "/logout", "/wengine-auth/login")):
        raise ValueError("auth page")
    return urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))


def install_host_stubs():
    root = types.ModuleType("fuckclassroom")
    root.__path__ = []
    auth = types.ModuleType("fuckclassroom.auth")
    auth.__path__ = []
    vpn = types.ModuleType("fuckclassroom.auth.webvpn")
    vpn.GATEWAY_URL = "https://v.guet.edu.cn/"
    vpn.safe_history_url = safe_url
    vpn.validate_webvpn_url = lambda url: (safe_url(url) and url)
    sys.modules.update({
        "fuckclassroom": root, "fuckclassroom.auth": auth,
        "fuckclassroom.auth.webvpn": vpn,
    })


def load(relative, name):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    assert spec and spec.loader
    obj = importlib.util.module_from_spec(spec)
    sys.modules[name] = obj
    spec.loader.exec_module(obj)
    return obj


install_host_stubs()
history = load("history.py", "test_f_browser_history")
browser = load("browser.py", "test_f_browser_browser")


class FakeFrame:
    def __init__(self):
        self.url = ""


class FakeRequest:
    def __init__(self, frame, method, url):
        self.frame, self.method, self.url = frame, method, url

    def is_navigation_request(self):
        return True


class FakePage:
    def __init__(self):
        self.events = {}
        self.url = "about:blank"
        self.main_frame = FakeFrame()
        self._closed = False
        self._trail, self._cursor = [], -1
        self.loads = []

    def on(self, event, callback):
        self.events[event] = callback

    def _travel(self, url, *, remember=True, method="GET"):
        self.events["request"](FakeRequest(self.main_frame, method, url))
        self.url = url
        self.main_frame.url = url
        self.loads.append(url)
        if remember:
            self._trail = self._trail[:self._cursor + 1] + [url]
            self._cursor += 1
        self.events["framenavigated"](self.main_frame)

    def goto(self, url, **kwargs):
        self._travel(url)

    def go_back(self, **kwargs):
        if self._cursor > 0:
            self._cursor -= 1
            self._travel(self._trail[self._cursor], remember=False)

    def go_forward(self, **kwargs):
        if self._cursor + 1 < len(self._trail):
            self._cursor += 1
            self._travel(self._trail[self._cursor], remember=False)

    def reload(self, **kwargs):
        self._travel(self.url, remember=False)

    def title(self):
        return "校园网页"

    def wait_for_timeout(self, ms):
        return None

    def is_closed(self):
        return self._closed

    def bring_to_front(self):
        return None

    def close(self):
        self._closed = True


class FakeContext:
    def __init__(self):
        self.page = FakePage()
        self.events = {}

    def on(self, event, callback):
        self.events[event] = callback

    def new_page(self):
        callback = self.events.get("page")
        if callback:
            callback(self.page)
        return self.page

    def popup(self):
        popup = FakePage()
        self.events["page"](popup)
        return popup

    def close(self):
        self.page.close()


class FakeBrowser:
    def __init__(self):
        self.contexts = []

    def new_context(self, **kwargs):
        ctx = FakeContext()
        self.contexts.append(ctx)
        return ctx

    def close(self):
        return None


class FakePlaywright:
    def stop(self):
        pass


class FakeSession:
    def __init__(self):
        self.state = "authenticated"
        self.verify_state = "authenticated"
        self.browser = FakeBrowser()
        self.auth_calls = 0
        self.verify_calls = 0
        self.saves = 0
        self.logouts = 0

    def get_status(self):
        return SimpleNamespace(
            is_authenticated=self.state == "authenticated",
            state=self.state, reason=self.state,
        )

    def ensure_authenticated(self, interactive_allowed=False):
        self.auth_calls += 1
        return SimpleNamespace(
            is_authenticated=self.state == "authenticated",
            state=self.state, reason=self.state,
        )

    def verify(self):
        self.verify_calls += 1
        return SimpleNamespace(
            state=self.verify_state,
            is_authenticated=self.verify_state == "authenticated",
        )

    def new_browser_context(self, fake_browser):
        return fake_browser.new_context()

    def save_browser_state(self, context):
        self.saves += 1

    def clear_session(self):
        self.logouts += 1


class FakeStore:
    def __init__(self):
        self.visits = []

    def record(self, url, title):
        self.visits.append((url, title))


class BrowserControllerTests(unittest.TestCase):
    @staticmethod
    def wait_until(predicate, timeout=2):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return True
            time.sleep(.01)
        return bool(predicate())

    def make(self):
        session = FakeSession()
        store = FakeStore()
        worker = browser.BrowserController(
            session, store, launcher=lambda: (FakePlaywright(), session.browser),
        )
        return worker, session, store

    def test_real_navigation_commands_share_one_context(self):
        worker, session, store = self.make()
        try:
            opened = worker.send("open")
            self.assertTrue(opened["running"])
            worker.send("navigate", "https://v.guet.edu.cn/https/first?ticket=SECRET")
            second = worker.send("navigate", "https://v.guet.edu.cn/https/second")
            self.assertEqual(second["url"], "https://v.guet.edu.cn/https/second")
            self.assertTrue(second["can_back"])
            back = worker.send("back")
            self.assertEqual(back["url"], "https://v.guet.edu.cn/https/first")
            self.assertTrue(back["can_forward"])
            forward = worker.send("forward")
            self.assertEqual(forward["url"], "https://v.guet.edu.cn/https/second")
            self.assertNotIn("SECRET", str(store.visits))
            self.assertEqual(len(session.browser.contexts), 1)
        finally:
            worker.stop()
        self.assertGreaterEqual(session.saves, 1)

    def test_rejects_unsafe_user_navigation(self):
        worker, _, _ = self.make()
        try:
            with self.assertRaises(browser.BrowserError):
                worker.send("navigate", "http://127.0.0.1:8888/private")
            with self.assertRaises(browser.BrowserError):
                worker.send("navigate", "https://not-guet.example/")
        finally:
            worker.stop()

    def test_cas_redirect_alone_does_not_trigger_relogin(self):
        worker, session, _ = self.make()
        try:
            worker.send("open")
            page = session.browser.contexts[-1].page
            page.goto("https://cas.guet.edu.cn/authserver/login")
            worker._idle()
            self.assertEqual(session.verify_calls, 1)
            self.assertEqual(session.auth_calls, 0)
            self.assertEqual(len(session.browser.contexts), 1)
        finally:
            worker.stop()

    def test_confirmed_gateway_expiry_recovers_only_get_page(self):
        worker, session, _ = self.make()
        try:
            worker.send("open")
            worker.send("navigate", "https://v.guet.edu.cn/https/course?token=PRIVATE")
            session.verify_state = "expired"
            page = session.browser.contexts[-1].page
            # Event processing in this offline fake is synchronous.
            page.goto("https://v.guet.edu.cn/login")
            worker._idle()
            # Worker polls the future itself; it may already be consumed
            # before the test thread observes it.
            self.assertTrue(self.wait_until(
                lambda: session.auth_calls == 1 and len(session.browser.contexts) == 2
            ))
            self.assertEqual(session.verify_calls, 1)
            self.assertEqual(session.auth_calls, 1)
            self.assertEqual(len(session.browser.contexts), 2)
            self.assertEqual(session.browser.contexts[-1].page.url,
                             "https://v.guet.edu.cn/https/course")
        finally:
            worker.stop()

    def test_non_get_document_is_never_replayed(self):
        worker, session, _ = self.make()
        try:
            worker.send("open")
            page = session.browser.contexts[-1].page
            page._travel("https://v.guet.edu.cn/https/post-result",
                         method="POST")
            with self.assertRaises(browser.BrowserError):
                worker.send("reload")
            session.verify_state = "expired"
            page.goto("https://v.guet.edu.cn/login")
            worker._last_document_method = "POST"
            worker._idle()
            self.assertTrue(self.wait_until(
                lambda: session.auth_calls == 1 and len(session.browser.contexts) == 2
            ))
            self.assertEqual(session.auth_calls, 1)
            self.assertEqual(session.browser.contexts[-1].page.loads, [])
        finally:
            worker.stop()

    def test_new_window_from_portal_follows_popup_navigation(self):
        worker, session, store = self.make()
        try:
            worker.send("open")
            context = session.browser.contexts[-1]
            popup = context.popup()
            popup.goto("https://v.guet.edu.cn/https/library?token=PRIVATE")
            worker.send("reload")
            self.assertEqual(worker.state()["url"],
                             "https://v.guet.edu.cn/https/library")
            self.assertNotIn("PRIVATE", str(store.visits))
        finally:
            worker.stop()

    def test_stale_session_is_closed_instead_of_writing_old_cookies(self):
        worker, session, _ = self.make()
        try:
            worker.send("open")
            def revoked(context):
                raise RuntimeError("WebVPN session generation changed")
            session.save_browser_state = revoked
            worker._last_save = 0
            worker._idle()
            self.assertFalse(worker.state()["running"])
            self.assertIn("账号已切换", worker.state()["message"])
        finally:
            worker.stop()

    def test_initial_login_required_opens_visible_browser_without_headless_auth(self):
        worker, session, _ = self.make()
        session.state = "login_required"
        try:
            response = worker.send("open")
            self.assertTrue(response["running"])
            self.assertEqual(session.auth_calls, 0)
            self.assertIn("完成学校", response["message"])
            self.assertEqual(len(session.browser.contexts), 1)
        finally:
            worker.stop()

    def test_manual_cas_completion_updates_broker_without_headless_login(self):
        worker, session, _ = self.make()
        session.state = "login_required"
        session.verify_state = "authenticated"
        try:
            worker.send("open")
            page = session.browser.contexts[-1].page
            # School redirects to CAS, user types SMS/CAPTCHA in Chromium,
            # then returns to the WebVPN gateway.
            page.goto("https://cas.guet.edu.cn/authserver/login")
            page.goto("https://v.guet.edu.cn/")
            self.assertTrue(self.wait_until(
                lambda: session.verify_calls >= 1 and worker._allow_auto_recovery
            ))
            self.assertEqual(session.auth_calls, 0)
            self.assertGreaterEqual(session.saves, 1)
            self.assertEqual(worker.state()["recovery"], "idle")
        finally:
            worker.stop()

    def test_close_cancels_stale_gateway_recovery_state(self):
        worker, session, _ = self.make()
        started = threading.Event()
        release = threading.Event()
        original = session.verify

        def stuck_probe():
            started.set()
            release.wait(3)
            return original()

        session.verify = stuck_probe
        try:
            worker.send("open")
            session.browser.contexts[-1].page.goto("https://v.guet.edu.cn/login")
            self.assertTrue(started.wait(2))
            closed = worker.send("close")
            self.assertFalse(closed["running"])
            self.assertEqual(closed["recovery"], "idle")
            # A second explicit open must not be rejected as stale "checking".
            response = worker.send("open")
            self.assertTrue(response["running"])
        finally:
            release.set()
            worker.stop()

    def test_explicit_logged_out_requires_user_reconnect(self):
        worker, session, _ = self.make()
        session.state = "logged_out"
        try:
            with self.assertRaisesRegex(browser.BrowserError, "重新连接"):
                worker.send("open")
            self.assertEqual(len(session.browser.contexts), 0)
        finally:
            worker.stop()

    def test_overlapping_open_returns_immediately_instead_of_queueing(self):
        worker, session, _ = self.make()
        entered = threading.Event()
        release = threading.Event()
        original = session.get_status

        def slow_status():
            entered.set()
            release.wait(3)
            return original()

        session.get_status = slow_status
        outcome = []

        def caller():
            try:
                outcome.append(worker.send("open", timeout=5))
            except Exception as exc:
                outcome.append(exc)

        running = threading.Thread(target=caller)
        running.start()
        try:
            self.assertTrue(entered.wait(1))
            started = time.monotonic()
            with self.assertRaisesRegex(browser.BrowserError, "正在执行"):
                worker.send("navigate", "https://v.guet.edu.cn/https/other", timeout=5)
            self.assertLess(time.monotonic() - started, .5)
        finally:
            release.set()
            running.join(timeout=3)
            worker.stop()
        self.assertEqual(len(outcome), 1)
        self.assertIsInstance(outcome[0], dict)

    def test_gateway_recovery_does_not_block_browser_commands(self):
        worker, session, _ = self.make()
        entered, release = threading.Event(), threading.Event()
        original = session.verify

        def slow_verify():
            entered.set()
            release.wait(3)
            return original()

        session.verify = slow_verify
        try:
            worker.send("open")
            session.browser.contexts[-1].page.goto("https://v.guet.edu.cn/login")
            worker._idle()
            self.assertTrue(entered.wait(1))
            started = time.monotonic()
            with self.assertRaisesRegex(browser.BrowserError, "恢复登录"):
                worker.send("navigate", "https://v.guet.edu.cn/")
            self.assertLess(time.monotonic() - started, .5)
            # Browser shutdown is still independent of a slow WebVPN probe.
            started = time.monotonic()
            worker.send("close", timeout=2)
            self.assertLess(time.monotonic() - started, 1)
        finally:
            release.set()
            worker.stop()

    def test_gateway_logout_in_browser_invalidates_shared_session(self):
        worker, session, _ = self.make()
        try:
            worker.send("open")
            session.browser.contexts[-1].page.goto(
                "https://v.guet.edu.cn/logout"
            )
            worker._idle()
            self.assertEqual(session.logouts, 1)
            self.assertFalse(worker.state()["running"])
        finally:
            worker.stop()


class SqliteStoreTests(unittest.TestCase):
    def test_visits_are_sanitized_and_deduplicated(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = history.HistoryStore(Path(tmp) / "history.sqlite3")
            db.record("https://v.guet.edu.cn/a?ticket=SECRET", "第一个页面")
            db.record("https://v.guet.edu.cn/a?session=SECRET2", "第二个页面")
            rows = db.list_history()
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["visit_count"], 2)
            self.assertEqual(rows[0]["url"], "https://v.guet.edu.cn/a")
            self.assertNotIn("SECRET", str(rows))
            db.bookmark("https://v.guet.edu.cn/a?key=TOKEN", "收藏")
            self.assertEqual(db.list_bookmarks()[0]["url"],
                             "https://v.guet.edu.cn/a")
            db.delete_visit(rows[0]["id"])
            self.assertEqual(db.list_history(), [])
            self.assertEqual(len(db.list_bookmarks()), 1)
            db.clear_history()

    def test_auth_page_and_external_address_are_not_persisted(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = history.HistoryStore(Path(tmp) / "history.sqlite3")
            for url in ("https://v.guet.edu.cn/logout",
                        "https://attacker.test/index",
                        "http://127.0.0.1/"):
                with self.assertRaises(ValueError):
                    db.bookmark(url, "unsafe")
            self.assertEqual(db.list_bookmarks(), [])


if __name__ == "__main__":
    unittest.main()
