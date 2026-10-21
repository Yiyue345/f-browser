"""Offline tests for asynchronous WebVPN status synchronization."""
from __future__ import annotations

import importlib.util
import threading
import time
import unittest
from dataclasses import dataclass
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("f_browser_gateway_status", ROOT / "gateway_status.py")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
GatewayStatusChecker = module.GatewayStatusChecker


@dataclass
class Status:
    state: str
    reason: str
    checked_at: str | None = None
    retry_after: int = 0

    @property
    def is_authenticated(self) -> bool:
        return self.state == "authenticated"


class FakeGateway:
    def __init__(self, state="unknown"):
        self.state = state
        self.calls = 0
        self.entered = threading.Event()
        self.release = threading.Event()

    def get_status(self):
        return Status(self.state, self.state)

    def verify(self):
        self.calls += 1
        self.entered.set()
        self.release.wait(timeout=2)
        self.state = "authenticated"
        return self.get_status()


class GatewayStatusTests(unittest.TestCase):
    def test_unknown_cookie_state_probes_in_background(self):
        gateway = FakeGateway()
        checker = GatewayStatusChecker(gateway, retry_seconds=20)
        initial = checker.snapshot()
        self.assertEqual(initial["state"], "checking")
        self.assertTrue(gateway.entered.wait(timeout=1))
        for _ in range(8):
            self.assertEqual(checker.snapshot()["state"], "checking")
        self.assertEqual(gateway.calls, 1)
        gateway.release.set()
        deadline = time.monotonic() + 1
        while time.monotonic() < deadline and checker.snapshot()["state"] != "authenticated":
            time.sleep(.01)
        self.assertEqual(checker.snapshot()["state"], "authenticated")
        self.assertEqual(gateway.calls, 1)

    def test_do_not_automatically_login_after_logout(self):
        for state in ("logged_out", "login_required", "authenticated", "expired"):
            with self.subTest(state=state):
                gateway = FakeGateway(state)
                checker = GatewayStatusChecker(gateway)
                result = checker.snapshot()
                self.assertEqual(result["state"], state)
                self.assertEqual(gateway.calls, 0)

    def test_concurrent_status_polling_only_starts_one_probe(self):
        gateway = FakeGateway()
        checker = GatewayStatusChecker(gateway)
        results = []
        try:
            threads = [
                threading.Thread(target=lambda: results.append(checker.snapshot()["state"]))
                for _ in range(15)
            ]
            for thread in threads: thread.start()
            for thread in threads: thread.join(1)
            self.assertTrue(gateway.entered.wait(1))
            self.assertEqual(gateway.calls, 1)
            self.assertEqual(results, ["checking"] * 15)
        finally:
            gateway.release.set()


if __name__ == "__main__":
    unittest.main()
