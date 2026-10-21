"""FuckClassroom WebVPN Browser, powered by the host WebVPN Session Broker."""
from __future__ import annotations

import asyncio
from pathlib import Path

from fuckclassroom.core.plugins import (
    NavigationGroup, NavigationItem, PluginContext, PluginSpec,
)

ROOT = Path(__file__).resolve().parent


def setup_services(context: PluginContext) -> None:
    from .browser import BrowserController
    from .history import HistoryStore

    store = HistoryStore(Path(context.config.data_dir) / "plugins" / "webvpn_browser" / "browser.sqlite3")
    session = context.services.get("webvpn_session")
    context.services.add("f_browser_store", store)
    context.services.add("f_browser", BrowserController(session, store))


def build_routes(context: PluginContext):
    from .routes import build_router
    return build_router(context)


async def shutdown(context: PluginContext) -> None:
    controller = context.services.maybe("f_browser")
    if controller is not None:
        await asyncio.to_thread(controller.stop)


def build_plugin() -> PluginSpec:
    return PluginSpec(
        id="webvpn_browser",
        name="校园内网浏览器",
        order=35,
        service_factory=setup_services,
        route_factory=build_routes,
        shutdown=shutdown,
        template_dir=ROOT / "templates",
        static_dir=ROOT / "static",
        stylesheets=("/plugins/webvpn_browser/static/browser.css?v=1",),
        navigation_groups=(
            NavigationGroup(
                key="webvpn_browser",
                label="校园内网",
                aria_label="校园内网浏览器",
                system="webvpn_browser",
                order=35,
                items=(
                    NavigationItem(
                        key="webvpn_browser", label="内网浏览器",
                        href="/f-browser", icon="globe",
                        active_keys=("webvpn_browser",),
                    ),
                ),
            ),
        ),
        system_labels=(("webvpn_browser", "校园内网"),),
    )
