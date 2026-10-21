"""Local-only browser control API. No Cookie, password or raw token in responses."""
from __future__ import annotations

from dataclasses import asdict
from typing import Literal

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, Response
from pydantic import BaseModel, Field

from fuckclassroom.auth.webvpn import safe_history_url, validate_webvpn_url


class NavigatePayload(BaseModel):
    url: str = Field(min_length=8, max_length=2048)


class BookmarkPayload(NavigatePayload):
    title: str = Field(default="校园内网", max_length=180)


class BrowserInputPayload(BaseModel):
    type: Literal["click", "scroll", "text", "key"]
    x: float | None = None
    y: float | None = None
    text: str | None = Field(default=None, max_length=200)
    key: str | None = Field(default=None, max_length=32)


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
    gateway_status = services.get("f_browser_gateway_status")
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
                "webvpn_status": gateway_status.snapshot(),
            },
        )

    @router.get("/api/f-browser/state")
    def state():
        return {"browser": browser.state(), "webvpn": gateway_status.snapshot()}

    @router.get("/api/f-browser/frame")
    def frame(request: Request):
        # Unlike a plain <img src>, fetch() must supply the same-origin custom
        # header. Authenticated page pixels must not be cross-origin embeddable.
        _assert_same_origin_action(request)
        if not browser.state().get("running"):
            raise HTTPException(status_code=409, detail="内嵌浏览器尚未启动")
        try:
            image = browser.send("screenshot", timeout=9)
        except Exception as exc:
            raise HTTPException(status_code=409, detail="截图暂时不可用，稍后重试") from exc
        return Response(
            content=image, media_type="image/jpeg",
            headers={"Cache-Control": "no-store, private", "X-Content-Type-Options": "nosniff",
                     "Content-Security-Policy": "default-src 'none'", "X-Frame-Options": "DENY"},
        )

    @router.post("/api/f-browser/input")
    def browser_input(request: Request, payload: BrowserInputPayload):
        _assert_same_origin_action(request)
        try:
            return browser.send("input", payload.model_dump(exclude_none=True), timeout=9)
        except Exception as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @router.post("/api/f-browser/verify")
    def verify_gateway(request: Request):
        _assert_same_origin_action(request)
        def worker(progress):
            progress(10, "正在独立验证 WebVPN 网关；教务系统登录状态不代表网关登录有效")
            status = session.verify()
            progress(100, status.reason)
            return "/f-browser"
        return {"task_id": tasks.start("校验 WebVPN 网关", worker).id}

    @router.post("/api/f-browser/control/{command}")
    def control(request: Request, command: str):
        _assert_same_origin_action(request)
        if command not in {"open", "back", "forward", "reload", "close"}:
            raise HTTPException(status_code=404, detail="不支持的浏览器操作")

        def worker(progress):
            progress(10, "正在操作内嵌 Chromium 浏览器")
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
            # Reconnect is an explicit user action, not another 90-second
            # headless CAS retry. Move the challenge into the visible Chromium
            # window and let the user complete the school's own login form.
            progress(10, "正在重置旧 WebVPN 会话")
            browser.send("reset", timeout=40)
            session.authorize_login()
            progress(50, "正在打开内嵌 WebVPN 登录页")
            browser.send("open", timeout=40)
            progress(100, "内嵌登录页已打开，可使用已保存的凭据自动登录；验证码请在页面中完成")
            return "/f-browser"

        return {"task_id": tasks.start("WebVPN：打开内嵌登录页", worker).id}

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
