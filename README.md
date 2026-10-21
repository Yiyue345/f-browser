# f-browser — FuckClassroom 校园内网浏览器

独立插件仓库：`Yiyue345/f-browser`。插件 ID：`webvpn_browser`，初始 Beta 版本 `0.1.0`。

本插件复用 FuckClassroom 主仓 **Issue #89 / `webvpn_session`** 公开接口，在本机的 Chromium 窗口里访问桂林电子科技大学 WebVPN。FuckClassroom 提供统一的控制页面，用于打开和关闭窗口、后退、前进、刷新、历史记录、收藏夹以及连接状态展示。

## 前提

- 安装支持 Plugin API v1、具有 `context.services.get("webvpn_session")` 的 FuckClassroom 主仓版本。
- 插件以可信任的 **`in_process`** 模式运行；不能改成 `process`，因为浏览器 Context 不能跨进程 RPC 传送。
- 插件管理器需安装 Python 依赖 `playwright>=1.48`，并且首次运行前在 FuckClassroom 所使用的 Python 环境中执行：

  ```bash
  python -m playwright install chromium
  ```

  Chromium 需要本机图形桌面（Windows / Linux X11 / Wayland 等）。第一版不提供远程云浏览器或 iframe 嵌入模式。

## 使用步骤

1. 从本仓库的发行包或本地目录安装并启用 `webvpn_browser`。
2. 在 FuckClassroom 侧栏打开「校园内网 → 内网浏览器」。
3. 首次访问直接点击**打开浏览器窗口**：会先展示真实 Chromium，若学校要求 CAS、验证码或短信验证，就在此窗口中操作，不会先等待长时间的后台认证。若此前主动退出，请点击**重新连接 WebVPN**，它会重新授权会话并打开可交互的登录窗口。插件不会自行保存账号密码。
4. 点击**打开浏览器窗口**，访问 WebVPN 门户，正常点击门户内的校园服务。必要时可在地址栏输入 `https://v.guet.edu.cn/` 下的完整链接。
5. 使用工具栏操作后退、前进和刷新，也可使用独立 Chromium 自己的浏览器操作。访问历史、收藏夹记录在本机 SQLite。

**使用说明：** 输入框只允许 HTTPS WebVPN 网关的安全地址；不允许输入任意外网、本机地址或者任意内网 IP。其他校园服务的网关编码链接必须从 WebVPN 门户获取，不能拼接本科教务的固定网关 URL 充当通用格式。

## 安全与恢复

- 账号、Cookie 和 CAS 验证过程完全由主仓的 WebVPN Session Broker 管理。浏览器 Context 使用 `new_browser_context` 创建，并通过 `save_browser_state` 的 session-generation CAS 差量保存；不会直接覆写 `storage_state.json`。
- 历史/收藏数据库只储存网关域名内、删去全部 URL 查询参数和 fragment 的地址；登录、退出页面不入库。
- 实际网页在单独 Chromium 窗口展示，FuckClassroom 控制页面只返回去敏 URL/标题，不传递 Cookie 和认证票据。
- **首次打开浏览器不执行阻塞式无界面重登**；登录窗口先展示给用户，用户在 Chromium 完成学校认证后插件会异步保存并核验网关状态。
- **仅在已有认证状态下出现网关/CAS 跳转，且主仓主动确认网关会话已失效后**，尝试一次自动恢复。检查和恢复在独立线程完成，不阻塞 Chromium 导航；普通校内目标网站 403 不触发重登。
- 同一时间只允许一个浏览器导航命令；重复点击会立即返回明确提示，而不是排队到长超时。
- 自动恢复后最多重新加载安全 GET 页面；不会自动重放 POST、报名、选课、评价、上传等带副作用操作。
- 主动登出或账号切换后停止写回旧 Cookie。需要人工二次验证时由主程序接管；失败需要用户手动「重新连接」，不会无限重试。

## 项目结构

```text
f-browser/
├── plugin.json                    # 插件声明 / 运行依赖
├── plugin.py                      # PluginSpec / 导航 / 生命周期
├── browser.py                     # 专属线程、Playwright、会话恢复
├── history.py                     # 本地 SQLite 历史/收藏
├── routes.py                      # 同源 API 和任务管理
├── templates/f_browser/index.html # FuckClassroom 浏览器控制页
├── static/browser.js              # 导航、状态及任务进度
├── static/browser.css             # 响应式 UI
├── tests/                         # 无账号的回归测试
└── scripts/build_registry_artifact.py
```

## 本地验证

```bash
python -m unittest discover -s tests -v
python scripts/build_registry_artifact.py 0.1.0-beta.1
```

CI 校验 manifest、Python 编译、Jinja 模板、无真实账号的导航/恢复/历史回归测试和 ZIP 校验和；主仓接口集成以及学校真实 CAS/短信认证仍需在本机进行端到端验收。

## 当前限制

- 第一版是**独立 Chromium 窗口**，不是把校内网页嵌入 FuckClassroom 页面；学校网站通常有 CSP/X-Frame-Options，iframe 并不可靠。
- 控制界面追踪当前活动网页，并跟随学校门户弹出的新页面；它不提供完整的多标签管理器。
- 没有破解、规避或代替学校登录验证，也不会在未授权网络环境中提供通用内网代理。
- Registry 网站上架与真实环境验收应在首次 Beta 发布后进行。
