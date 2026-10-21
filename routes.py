"""Local-only browser control API. No Cookie, password or raw token in responses."""
from __future__ import annotations

from dataclasses import asdict

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

from fuckclassroom.auth.webvpn import safe_history_url, validate_webvpn_url


class NavigatePayload(BaseModel):
    url: str = Field(min_length=8, max_length=2048)


class BookmarkPayload(NavigatePayload):
    title: str = Field(default="校园内网", max_length=180)


def _assert_same_origin_action(request: Request) -> None:
    # Required custom header defeats simple cross-site form CSRF; the host
    # does not enable third-party CORS for arbitrary origins.
    if request.headers.get("x-f-browser-action") != "1":
        raise HTTPException(status_code=403, detail="请从本机浏览器页面操作")
    origin = request.headers.get("origin")
    expected = f"{request.url.scheme}://{request.url.netloc}"
    if origin and origin != expected:
        raise HTTPException(status_code=403, detail="跨站浏览器操作已拒绝")


def build_router(context) -> APIRouter:
    router = APIRouter()
    services = context.services
    browser = services.get("f_browser")
    store = services.get("f_browser_store")
    session = services.get("webvpn_session")
    tasks = services.get("task_manager")
    templates = services.get("templates")
    auth = services.get("auth_service")

    @router.get("/f-browser", response_class=HTMLResponse)
    def home(request: Request):
        return templates.TemplateResponse(
            request,
            "f_browser/index.html",
            {
                "session": auth.get_session_status(),
                "webvpn_status": session.get_status(),
            },
        )

    @router.get("/api/f-browser/state")
    def state():
        return {"browser": browser.state(), "webvpn": asdict(session.get_status())}

    @router.post("/api/f-browser/control/{command}")
    def control(request: Request, command: str):
        _assert_same_origin_action(request)
        if command not in {"open", "back", "forward", "reload", "close"}:
            raise HTTPException(status_code=404, detail="不支持的浏览器操作")

        def worker(progress):
            progress(10, "正在操作 Chromium 浏览器")
            browser.send(command, timeout=150 if command == "open" else 50)
            progress(100, "操作完成")
            return "/f-browser"

        task = tasks.start(f"浏览器：{command}", worker)
        return {"task_id": task.id}

    @router.post("/api/f-browser/navigate")
    def navigate(request: Request, data: NavigatePayload):
        _assert_same_origin_action(request)
        try:
            target = validate_webvpn_url(data.url)
        except Exception:
            raise HTTPException(status_code=400, detail="只允许 HTTPS WebVPN 网关地址；请先在网关门户中获取入口")

        def worker(progress):
            progress(5, "正在安全访问 WebVPN 页面")
            browser.send("navigate", target, timeout=150)
            progress(100, "网页已打开")
            return "/f-browser"

        return {"task_id": tasks.start("浏览器：打开网页", worker).id}

    @router.post("/api/f-browser/reconnect")
    def reconnect(request: Request):
        _assert_same_origin_action(request)

        def worker(progress):
            progress(5, "正在使用主程序的统一账号恢复 WebVPN")
            current = session.reconnect(interactive_allowed=True)
            if not current.is_authenticated:
                raise RuntimeError(f"无法完成学校统一认证：{current.reason}")
            browser.send("reset", timeout=40)
            progress(100, "WebVPN 已重新连接")
            return "/f-browser"

        return {"task_id": tasks.start("WebVPN：交互式重新连接", worker).id}

    @router.get("/api/f-browser/history")
    def history():
        return {"items": store.list_history()}

    @router.delete("/api/f-browser/history")
    def clear_history(request: Request):
        _assert_same_origin_action(request)
        store.clear_history()
        return {"ok": True}

    @router.delete("/api/f-browser/history/{visit_id}")
    def delete_visit(request: Request, visit_id: int):
        _assert_same_origin_action(request)
        store.delete_visit(visit_id)
        return {"ok": True}

    @router.get("/api/f-browser/bookmarks")
    def bookmarks():
        return {"items": store.list_bookmarks()}

    @router.post("/api/f-browser/bookmarks")
    def add_bookmark(request: Request, data: BookmarkPayload):
        _assert_same_origin_action(request)
        try:
            safe = safe_history_url(data.url)
        except Exception:
            raise HTTPException(status_code=400, detail="不能收藏登录、退出页面或非 WebVPN 链接")
        store.bookmark(safe, data.title)
        return {"ok": True}

    @router.delete("/api/f-browser/bookmarks/{bookmark_id}")
    def delete_bookmark(request: Request, bookmark_id: int):
        _assert_same_origin_action(request)
        store.delete_bookmark(bookmark_id)
        return {"ok": True}

    return router
