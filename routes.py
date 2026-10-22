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
        if command not in {"open", "back", "forward", "reload", "close", "sync"}:
            raise HTTPException(status_code=404, detail="不支持的浏览器操作")

        def worker(progress):
            progress(10, "正在操作独立 Chromium 浏览器")
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
            progress(5, "正在向 Chromium 浏览器发送导航请求")
            browser.send("navigate", target, timeout=150)
            progress(100, "网页已打开")
            return "/f-browser"

        return {"task_id": tasks.start("浏览器：打开网页", worker).id}

    @router.post("/api/f-browser/reconnect")
    def reconnect(request: Request):
        """Explicit broker-controlled WebVPN login, never plugin autofill."""
        _assert_same_origin_action(request)

        def worker(progress):
            progress(5, "正在通过主程序重新连接 WebVPN（可能需要交互验证）")
            current = session.reconnect(interactive_allowed=True)
            if not current.is_authenticated:
                raise RuntimeError(f"WebVPN 重新认证尚未完成：{current.reason}")
            progress(80, "已获取共享会话，正在同步 Chromium")
            if browser.state().get("running"):
                browser.send("sync", timeout=40)
            progress(100, "已载入共享 WebVPN 会话")
            return "/f-browser"

        return {"task_id": tasks.start("WebVPN：主程序重新连接", worker).id}

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
