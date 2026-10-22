"""Offline regression tests for the real native-Chromium integration.

No real browser, credentials, school endpoints or shared cookie files required.
"""
from __future__ import annotations

import importlib.util
import sys
import tempfile
import time
import types
import unittest
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlsplit, urlunsplit

ROOT = Path(__file__).resolve().parents[1]
GATEWAY = "https://v.guet.edu.cn/"


def safe_history(raw):
    p = urlsplit(raw)
    if p.scheme != "https" or p.hostname != "v.guet.edu.cn":
        raise ValueError("not a safe gateway URL")
    if any(fragment in p.path.lower() for fragment in ("/login", "/authserver/", "/logout")):
        raise ValueError("never persist login pages")
    return urlunsplit((p.scheme, p.netloc, p.path, "", ""))


def host_stubs():
    root = types.ModuleType("fuckclassroom")
    root.__path__ = []
    auth = types.ModuleType("fuckclassroom.auth")
    auth.__path__ = []
    vpn = types.ModuleType("fuckclassroom.auth.webvpn")
    vpn.GATEWAY_URL = GATEWAY
    vpn.safe_history_url = safe_history
    vpn.validate_webvpn_url = lambda url: safe_history(url) and url
    sys.modules.update({
        "fuckclassroom": root, "fuckclassroom.auth": auth,
        "fuckclassroom.auth.webvpn": vpn,
    })


def load(path, name):
    spec = importlib.util.spec_from_file_location(name, ROOT / path)
    result = importlib.util.module_from_spec(spec)
    sys.modules[name] = result
    spec.loader.exec_module(result)
    return result


host_stubs()
browser = load("browser.py", "f_browser_native_test")
history = load("history.py", "f_browser_history_test")


class Frame:
    def __init__(self):
        self.url = ""


class Request:
    def __init__(self, frame, url, method):
        self.frame, self.url, self.method = frame, url, method

    def is_navigation_request(self):
        return True


class Page:
    def __init__(self):
        self.events = {}
        self.url = "about:blank"
        self.main_frame = Frame()
        self.closed = False
        self.visited = []
        self.cursor = -1
        self.loads = []
        self.title_text = ""

    def on(self, event, handler):
        self.events.setdefault(event, []).append(handler)

    def emit(self, event, *args):
        for callback in self.events.get(event, []):
            callback(*args)

    def navigate(self, url, method="GET", remember=True, title=None):
        self.emit("request", Request(self.main_frame, url, method))
        self.url = self.main_frame.url = url
        self.title_text = title if title is not None else (
            "桂电统一身份认证" if "/authserver/login" in url else "教务管理门户"
        )
        if remember:
            self.visited = self.visited[:self.cursor + 1] + [url]
            self.cursor += 1
        self.loads.append(url)
        self.emit("framenavigated", self.main_frame)
        self.emit("domcontentloaded")
        self.emit("load")

    def goto(self, url, **kwargs):
        self.navigate(url)

    def go_back(self, **kwargs):
        if self.cursor > 0:
            self.cursor -= 1
            self.navigate(self.visited[self.cursor], remember=False)

    def go_forward(self, **kwargs):
        if self.cursor + 1 < len(self.visited):
            self.cursor += 1
            self.navigate(self.visited[self.cursor], remember=False)

    def reload(self, **kwargs):
        self.navigate(self.url, remember=False)

    def title(self):
        return self.title_text

    def wait_for_timeout(self, milliseconds):
        return None

    def bring_to_front(self):
        pass

    def is_closed(self):
        return self.closed

    def close(self):
        if not self.closed:
            self.closed = True
            self.emit("close")


class Context:
    def __init__(self):
        self.events = {}
        self.pages = []
        self.closed = False

    def on(self, event, callback):
        self.events[event] = callback

    def new_page(self):
        page = Page()
        self.pages.append(page)
        if "page" in self.events:
            self.events["page"](page)
        return page

    def popup(self):
        return self.new_page()

    def close(self):
        self.closed = True
        for page in self.pages:
            page.close()


class Chromium:
    def __init__(self):
        self.contexts = []
        self.closed = False

    def close(self):
        self.closed = True


class Playwright:
    def stop(self):
        pass


class SharedSession:
    def __init__(self):
        self.state = "authenticated"
        self.checked_at = "initial"
        self.chromium = Chromium()
        self.new_context_calls = 0
        self.saves = 0
        self.saved_contexts = []
        self.clears = 0

    def get_status(self):
        return SimpleNamespace(
            state=self.state, is_authenticated=self.state == "authenticated",
            checked_at=self.checked_at,
        )

    def new_browser_context(self, browser):
        self.new_context_calls += 1
        context = Context()
        browser.contexts.append(context)
        return context

    def save_browser_state(self, context):
        self.saves += 1
        self.saved_contexts.append(context)

    def clear_session(self):
        self.clears += 1

    def verify(self):
        raise AssertionError("Browser must not perform its own gateway verification")

    def ensure_authenticated(self, **kwargs):
        raise AssertionError("Browser must not perform headless login")

    def submit_saved_login(self, page):
        raise AssertionError("Browser must never submit credentials")


class History:
    def __init__(self):
        self.visits = []
        self.fail = False

    def record(self, url, title):
        if self.fail:
            raise OSError("database readonly")
        self.visits.append((safe_history(url), title))


class NativeBrowserTests(unittest.TestCase):
    @staticmethod
    def until(predicate, timeout=2.5):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            if predicate():
                return True
            time.sleep(.02)
        return bool(predicate())

    def make(self):
        session = SharedSession()
        store = History()
        controller = browser.BrowserController(
            session, store,
            launcher=lambda: (Playwright(), session.chromium),
        )
        return controller, session, store

    def test_launch_real_window_and_shared_context_no_authentication(self):
        controller, session, _ = self.make()
        try:
            state = controller.send("open")
            self.assertTrue(state["running"])
            self.assertEqual(state["page_status"], "ready")
            self.assertEqual(state["url"], GATEWAY)
            self.assertEqual(session.new_context_calls, 1)
            self.assertEqual(session.clears, 0)
            self.assertFalse(hasattr(controller, "_screenshot"))
            self.assertNotIn("screenshot", controller.COMMANDS)
            self.assertNotIn("input", controller.COMMANDS)
        finally:
            controller.stop()

    def test_login_page_shows_true_title_without_saving_history(self):
        controller, session, store = self.make()
        try:
            controller.send("open")
            page = session.chromium.contexts[-1].pages[-1]
            page.navigate(
                "https://v.guet.edu.cn/https/"
                "77726476706e69737468656265737421f3f652d220256d44300d8db9d6562d"
                "/authserver/login?ticket=SECRET",
                title="学校统一认证 · 账号登录",
            )
            result = controller.state()
            self.assertEqual(result["page_status"], "login")
            self.assertEqual(result["title"], "学校统一认证 · 账号登录")
            self.assertNotIn("SECRET", str(result))
            self.assertNotIn("authserver/login", str(store.visits))
        finally:
            controller.stop()

    def test_site_login_does_not_restart_gateway_authentication(self):
        controller, session, _ = self.make()
        try:
            controller.send("open")
            page = session.chromium.contexts[-1].pages[-1]
            page.navigate("https://v.guet.edu.cn/https/academic/login", title="教务用户登录")
            self.assertEqual(controller.state()["title"], "教务用户登录")
            self.assertEqual(session.new_context_calls, 1)
            self.assertEqual(session.clears, 0)
        finally:
            controller.stop()

    def test_history_database_failure_does_not_masquerade_as_loading(self):
        controller, session, store = self.make()
        try:
            controller.send("open")
            store.fail = True
            page = session.chromium.contexts[-1].pages[-1]
            page.navigate("https://v.guet.edu.cn/https/courses", title="课程列表")
            state = controller.state()
            self.assertEqual(state["page_status"], "ready")
            self.assertEqual(state["title"], "课程列表")
            self.assertIn("历史记录保存失败", state["message"])
        finally:
            controller.stop()

    def test_native_navigation_and_popup_state_updates(self):
        controller, session, _ = self.make()
        try:
            controller.send("open")
            controller.send("navigate", "https://v.guet.edu.cn/https/one")
            controller.send("navigate", "https://v.guet.edu.cn/https/two")
            self.assertTrue(controller.state()["can_back"])
            controller.send("back")
            self.assertTrue(controller.state()["can_forward"])
            controller.send("forward")
            self.assertIn("/two", controller.state()["url"])
            popup = session.chromium.contexts[-1].popup()
            popup.navigate("https://v.guet.edu.cn/https/library", title="图书馆")
            self.assertEqual(controller.state()["title"], "图书馆")
            self.assertIn("/library", controller.state()["url"])
            popup.close()
            self.assertTrue(controller.state()["running"])
        finally:
            controller.stop()

    def test_close_window_reports_closed_without_periodic_title_probe(self):
        controller, session, _ = self.make()
        try:
            controller.send("open")
            session.chromium.contexts[-1].pages[-1].close()
            self.assertFalse(controller.state()["running"])
            self.assertEqual(controller.state()["page_status"], "closed")
            self.assertEqual(controller.send("open")["page_status"], "ready")
        finally:
            controller.stop()

    def test_broker_session_change_rebuilds_without_replaying_post(self):
        controller, session, _ = self.make()
        session.state = "unknown"
        try:
            controller.send("open")
            old_context = session.chromium.contexts[-1]
            old_page = old_context.pages[-1]
            old_page.navigate("https://v.guet.edu.cn/https/post-result", method="POST")
            controller._last_save = time.monotonic()
            old_saves_before = session.saved_contexts.count(old_context)
            session.state = "authenticated"
            session.checked_at = "new-login"
            self.assertTrue(self.until(lambda: len(session.chromium.contexts) == 2))
            fresh = session.chromium.contexts[-1]
            self.assertTrue(old_context.closed)
            self.assertEqual(fresh.pages[-1].url, GATEWAY)
            self.assertEqual(old_saves_before, session.saved_contexts.count(old_context))
        finally:
            controller.stop()

    def test_shared_session_refresh_preserves_only_safe_get(self):
        controller, session, _ = self.make()
        session.state = "unknown"
        try:
            controller.send("open")
            controller.send("navigate", "https://v.guet.edu.cn/https/course")
            old_context = session.chromium.contexts[-1]
            controller._last_save = time.monotonic()
            session.state = "authenticated"
            session.checked_at = "renewed"
            self.assertTrue(self.until(lambda: len(session.chromium.contexts) == 2))
            self.assertTrue(old_context.closed)
            self.assertEqual(session.chromium.contexts[-1].pages[-1].url,
                             "https://v.guet.edu.cn/https/course")
        finally:
            controller.stop()

    def test_only_explicit_sync_reloads_without_status_transition(self):
        controller, session, _ = self.make()
        try:
            controller.send("open")
            controller.send("sync")
            self.assertEqual(session.new_context_calls, 2)
        finally:
            controller.stop()

    def test_broker_logout_is_respected_and_does_not_reauthenticate(self):
        controller, session, _ = self.make()
        try:
            controller.send("open")
            session.state = "logged_out"
            self.assertTrue(self.until(lambda: controller.state()["running"] is False))
            self.assertEqual(session.clears, 0)
        finally:
            controller.stop()

    def test_refuse_untrusted_navigation_address(self):
        controller, _, _ = self.make()
        try:
            for url in ["https://evil.example/", "http://127.0.0.1:8765/"]:
                with self.assertRaises(browser.BrowserError):
                    controller.send("navigate", url)
        finally:
            controller.stop()

    def test_generation_change_does_not_overwrite_broker_cookies(self):
        controller, session, _ = self.make()
        try:
            controller.send("open")
            context = session.chromium.contexts[-1]
            controller._last_save = time.monotonic()
            session.state = "authenticated"
            session.checked_at = "other-generation"
            self.assertTrue(self.until(lambda: context.closed))
            # Context shutdown on broker revision does not write old cookies.
            self.assertEqual(session.saved_contexts.count(context), 1)
        finally:
            controller.stop()

    def test_no_remote_frame_or_keyboard_api_left(self):
        from pathlib import Path
        routes = (ROOT / "routes.py").read_text(encoding="utf-8")
        frontend = (ROOT / "static/browser.js").read_text(encoding="utf-8")
        template = (ROOT / "templates/f_browser/index.html").read_text(encoding="utf-8")
        self.assertNotIn("/api/f-browser/frame", routes)
        self.assertNotIn("/api/f-browser/input", routes)
        self.assertNotIn("refreshFrame", frontend)
        self.assertNotIn("queueInput", frontend)
        self.assertNotIn("fb-frame-image", template)
        self.assertIn("fb-more-menu", template)


class HistoryStoreTests(unittest.TestCase):
    def test_visited_pages_and_bookmarks_strip_query(self):
        with tempfile.TemporaryDirectory() as directory:
            store = history.HistoryStore(Path(directory) / "browser.sqlite")
            store.record("https://v.guet.edu.cn/https/a?token=SECRET", "教学")
            self.assertEqual(store.list_history()[0]["url"],
                             "https://v.guet.edu.cn/https/a")
            store.bookmark("https://v.guet.edu.cn/https/a?q=SECRET", "收藏")
            self.assertEqual(store.list_bookmarks()[0]["url"],
                             "https://v.guet.edu.cn/https/a")


if __name__ == "__main__":
    unittest.main()
