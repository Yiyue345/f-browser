# f-browser

FuckClassroom 的独立校园 WebVPN 浏览器插件（插件 ID：`webvpn_browser`）。

通过主仓 `webvpn_session` 服务共享统一认证，在独立 Chromium 窗口中访问桂电 WebVPN，并在 FuckClassroom 中管理会话状态、浏览历史、收藏和导航。

开发分支：`plugin-management`。依赖主仓已提供的 WebVPN Session Broker（Issue #89），不单独读取或保存学校账号密码。

> 当前仍在开发中；实际 GUET/CAS 登录、短信验证和在线浏览需要在本机环境验收。
