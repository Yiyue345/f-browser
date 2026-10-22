# f-browser：校园 WebVPN 浏览器

FuckClassroom 独立插件，插件 ID：`webvpn_browser`，开发分支 `dev`。

**当前使用真正的可见 Chromium 窗口。** FuckClassroom 内只保留启动、导航状态、历史记录与收藏夹管理；学校网页由 Chromium 直接处理输入、滚动、文字选择、右键、复制粘贴、下载和文件上传。不再截屏回传网页，也不再转发鼠标键盘事件。

## 依赖

- 支持 Plugin API v1 和 `context.services.get("webvpn_session")` 的 FuckClassroom 主程序。
- 可信的 `in_process` 插件运行方式；Playwright BrowserContext 不能通过跨进程 RPC 提供。
- 主程序所在 Python 环境需要 `playwright`，并安装 Chromium：

```powershell
python -m playwright install chromium
```

需要有图形桌面的 Windows 或 Linux 环境。网页展示在独立 Chromium 窗口中，**不是嵌在 FuckClassroom 页面 DOM 中**。

## 使用

1. 安装并启用插件，打开侧栏「校园内网 → 内网浏览器」。
2. 点击「打开浏览器」，Chromium 直接加载 `https://v.guet.edu.cn/`。
3. 主仓已保存的 WebVPN Cookie 会经公开接口 `new_browser_context` 被载入。若还需认证，请在**主程序**完成 WebVPN 登录；如某个校园网站有自己的登录页，则在 Chromium 中正常操作。
4. 主程序会话更新后，浏览器会按宿主认证状态变化创建新的 Context 并重新打开安全 GET 页面；也可在「更多 → 同步共享会话」主动同步。
5. 控制页提供后退、前进、刷新、地址栏及当前页状态；历史与收藏只保存去除 query/fragment 的 WebVPN 安全地址。
6. 「更多」中还有「校验 WebVPN 网关」「通过主程序重新连接」和「关闭浏览器」。重新连接调用主仓 WebVPN Session Broker，可能需要短信/验证码。

WebVPN 网关登录和校内网站登录是不同阶段。**校内网站登录页面不是 WebVPN 网关过期的证据**；插件不会根据校园网站的登录页进行自动恢复或再次提交账号密码。

## 架构

- `browser.py`：单专用线程、可见 Chromium、主程序共享 Session Broker 接入、导航和生命周期事件。
- `gateway_status.py`：对主仓 `get_status()` 的未验证状态在后台执行一次 `verify()`，不会负责登录。
- `routes.py`：同源控制 API 与主程序 TaskManager，**没有截图和远程输入 API**。
- `templates/f_browser/index.html`、`static/browser.js`、`static/browser.css`：精简控制 UI。
- `history.py`：本地 SQLite 历史记录和收藏。
- `tests/`：模拟真实导航事件、登录页标题、窗口弹出/关闭、共享会话更新与 POST 安全限制。
- `scripts/build_registry_artifact.py`：Registry v1 ZIP 和 SHA-256 文件。

Playwright 的 `framenavigated`、`domcontentloaded`、`load` 和 `close` 事件负责即时更新标题、地址及页面状态。宿主会话检查和自动保存按较低频率进行；控制页仅低频读取最新状态，不传输网页画面。

## 测试与版本发布

```powershell
python -m unittest discover -s tests -p "test_*.py" -v
```

- `dev` 的普通提交只触发 **Test plugin**，不自动生成 Alpha。
- 需要测试包时，进入 [Actions → Publish alpha](https://github.com/Yiyue345/f-browser/actions/workflows/alpha.yml)，手动勾选 `publish` 后生成一个 Alpha。
- `main` 保持独立 Beta 发布流程，**不会自动合并 dev 到 main**。
- 本机学校网络、CAS/短信、目标网站登录及文件上传仍需实机回归。

## 安全边界

- 插件不读取、导出、保存主仓的账号密码，不再调用 CAS 自动填表或 `ensure_authenticated()` 登录流程。
- WebVPN Cookie 生命周期由宿主 Session Broker 管理，创建 Context 用 `new_browser_context()`，保存用 `save_browser_state()`。
- 检测到宿主认证变化后，丢弃旧 BrowserContext 时**不保存过期 Cookie**；同步只允许重新加载安全 GET，不重放 POST、选课或上传提交。
- 校内登录页的网址可在控制页脱敏显示，但不会纳入持久化浏览历史。
