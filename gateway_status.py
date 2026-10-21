"""Nonblocking gateway status verification for the browser control page.

The broker's get_status() intentionally does not probe the network.
A previously authenticated academic WebVPN session may therefore report
"unknown" until the browser explicitly verifies the shared gateway cookie.
"""
from __future__ import annotations

import threading
import time
from dataclasses import asdict


class GatewayStatusChecker:
    def __init__(self, session, *, retry_seconds: float = 30.0) -> None:
        self.session = session
        self.retry_seconds = retry_seconds
        self._lock = threading.Lock()
        self._checking = False
        self._next_check = 0.0

    def snapshot(self) -> dict:
        status = self.session.get_status()
        # Do not accidentally authenticate after an explicit logout,
        # or force network calls on every 2.5-second UI poll.
        with self._lock:
            if (
                status.state == "unknown"
                and not self._checking
                and time.monotonic() >= self._next_check
            ):
                self._checking = True
                self._next_check = time.monotonic() + self.retry_seconds
                threading.Thread(
                    target=self._verify_background,
                    daemon=True,
                    name="f-browser-gateway-status",
                ).start()
            checking = self._checking

        if checking and status.state == "unknown":
            return {
                "state": "checking",
                "reason": "正在验证已保存的 WebVPN 网关会话",
                "checked_at": status.checked_at,
                "retry_after": 0,
            }
        return asdict(status)

    def _verify_background(self) -> None:
        try:
            self.session.verify()
        except Exception:
            # The broker is responsible for classifying network failures.
            # Never expose raw login URLs or credentials in the UI.
            pass
        finally:
            with self._lock:
                self._checking = False
