# Morning 平台实施与验收记录

日期：2026-10-11。工作目录：`wakecore-kernel`。

文中的 `app/`、`adapters/`、`kernel/`、`protocol/` 路径以 `packages/wakecore/src/wakecore/` 为根；桌面驱动位于 `packages/wakecore-ui-runtime/src/wakecore_ui_runtime/`。

本次实现的是可运行的本地平台闭环。真实验收使用现有模型网关、Pi 0.99.1、macOS 计算器、真实文件、进程与本地 HTTP 服务；验收实例的会话、驻守和授权与用户原有 `.pulse` 数据分开。没有把测试任务放进正式工作空间。

## P0：保留原生 Pi，验证真实 Gateway

继续使用 Pi 的 `AgentSessionRuntime`、`SessionManager`、`DefaultResourceLoader`、`ModelRuntime` 和原生 RPC。会话树、导入导出、模型与思考深度、停止/队列、上下文整理、Skills、Extensions、MCP 和原生工具入口保留。Settings 增加原生能力入口，Agent 输入框原有设置入口保留。

模型 Gateway 与 Pi 提供商凭据沿用现有 macOS Keychain 实现；新设备令牌使用独立钥匙串 namespace，不加入 Pi 的 provider credential snapshot。远程节点拒绝明文 HTTP（loopback / SSH 隧道除外），拒绝携带认证的重定向；更换设备地址必须重输凭据。可选浏览器/语义适配器只使用兼容的 Responses 服务，不能因此影响 Pi 的其他原生模型提供商。

实测结果：

- 当前网关 `gpt-5.6-sol` 完成真实认证和模型请求，记录延迟 4076 ms。
- Pi 用原生工具创建并读回 `pi-platform-check.txt`，内容匹配 `NATIVE_PI_OK`。
- 重启 Pi 后恢复同一持久会话。
- 修复空会话未落盘时误回旧历史的问题：只持久保存选中会话的 ID/模型/设置，恢复时使用原生 SessionManager；不伪造消息或提前写 JSONL。新增真实 SDK 回归通过。
- 正式服务已更新到 8788，原空会话 ID、0 条消息和原有暂停驻守的 ID/版本均核对一致，见 [正式服务核对](agent-platform/production-verified.json)。
- 不加载 Morning 自定义扩展时，该真实读写链路仍通过。

证据：[p0-real.json](agent-platform/p0-real.json)。相关文件：`scripts/pulse.py`、`app/pulse/server.py`、`adapters/agent_runtime/native.py`、`apps/agent-runtime/native-bootstrap.mjs`、`apps/pulse/src/Settings.tsx`。

## P1：独立 Computer 节点与工作台

新增 `morning.computer-node/1`，复用已有 `McpBridge` 和写前日志 `Journal`，直接使用已安装的 `codex-computer-use-mcp 0.5.2`。节点不运行模型，不持有模型凭据。Pi 通过增量扩展决定观察和动作。

实现应用清单、窗口截图/可读内容、显示器清单、显示器/区域截图、点击、键盘、文字、选择、滚动、拖拽和应用辅助动作。Computer 页面提供设备与授权应用选择、自动刷新预览、步骤记录、暂停/接管、恢复、自然语言任务和实际审批。显示器捕获检查 macOS 录屏权限。

操作授权与观察授权分离；所有变更动作当前都要求逐次确认。审批展示实际目标标签、参数和准备时的画面。动作绑定具体应用、最近观察、权限版本、完整参数摘要及过期时间；准备审批时刷新观察，执行前再次校验。坐标不能越过截图范围，画面变化会拒绝旧坐标。含凭据字段的窗口交给人工。暂停阻止后续操作，同时保留已授权的观察能力。

发送前先写日志；同一 key 不会再次执行，崩溃、超时等不确定结果保留为待核验。重启时未发送的审批失效，已发送但未确认的操作不会重试。失败会回到对话和 Pulse 中，不会仅藏在执行日志里。

真实验收：

1. Devices 完成节点认证，读取真实应用清单与本机显示器信息。
2. 仅在隔离验收实例中授权计算器。
3. Pi 观察 → 准备点击数字 7 → GUI 批准 → 节点单次点击 → 重新观察；Pi 读回显示由 3 变为 7。
4. 延迟批准触发观察过期保护，未点击。
5. 重新观察和批准后，只点击一次数字 8；Pi 读回显示由 7 变为 78。
6. 真实工作台切换到暂停/人工接管，操作入口随状态停用；恢复后入口重新可用。见 [接管截图](agent-platform/computer-takeover-browser.png)。

证据：[计算器运行截图](agent-platform/computer-final-browser.png)、[恢复验收](agent-platform/computer-recovery-real.json)。相关文件：`wakecore_ui_runtime/desktop/node.py`、`protocol/computer_actions.py`、`app/pulse/devices.py`、`app/pulse/platform.py`、`apps/agent-runtime/morning-extension.mjs`、`apps/pulse/src/Platform.tsx`。

## P2：Watch 适配器、独立运行与安全协作

网页继续使用原有 `web.playwright` 适配器。新增 `environment.local`，支持文件、文件夹、日志尾部、进程名称/PID、HTTP/API、网络服务、桌面可读内容、屏幕和区域视觉变化。语义判断通过现有 reasoning port 接入，保留否定、对象和完整条件；不使用关键词模板替代理解。截图语义判断绑定相应视觉证据，证据变化或缺失时不猜测。

继续复用 WakeCore 的持久调度、权限、去重、退避、任务版本和通知。文件事件用 watchdog 加速检查，定时观察负责恢复和补偿。事件被最短间隔或服务器退避阻挡时会保留待检查请求。视觉基准持久化。显式指定检查频率时采用固定间隔；默认值支持自适应节奏。

增加独立入口 `scripts/watch.py`，不创建 Pi 进程，也不提供前端页面。默认整合启动使用独立生命周期的 Watch worker；关闭浏览器不会停止它。进程退出后可重启恢复，不能把“关闭浏览器仍运行”误解成“退出所有后台进程后仍运行”。

Watch 详情有开始、暂停、恢复、即时检查、修改频率/条件/后续动作、移除与事件时间线。移除采用取消并隐藏，历史记录保留。通知、事件与唤醒意图在同一事务中提交；同一通知不会重复生成唤醒。

实测纯后台服务的文件触发、HTTP/API、网络端口、进程、文件夹和日志读取；服务重启后暂停状态和通知去重均保留。见 [headless 实测](agent-platform/watch-real-final.json)。

闭页协作验收：Pi 理解文件等待条件 → 用户启动 → 关闭整个验收网页 → 修改真实文件 → 一次持久通知 → 唤醒原会话 → Pi 观察真实计算器并准备动作 → 等待用户批准，没有自动点击。见 [闭页协作记录](agent-platform/watch-pi-computer-real.json) 和 [实际审批画面](agent-platform/watch-computer-approval-browser.png)。记录中延迟审批没有执行，随后通过重新观察完成恢复，详见 P1 的恢复记录。

后台唤醒使用 Pi 原生 `sendCustomMessage`，只临时开放已授权来源观察和准备审批；工具调用钩子阻止终端、文件写入及其他扩展绕过。后台结束后恢复原活动工具列表，不修改普通对话的原生权限。绑定原会话与模型接收方；用户换了模型服务时等待重新确认。核验已批准动作的回合不能再次准备相同动作。

真实负向验收：错误模型接收方被拒绝；后台请求写文件被阻止；后台结束后原生工具列表恢复；普通用户对话随后成功写入并读回 `ORIGINAL_TOOLS_OK`。见 [后台权限实测](agent-platform/background-policy-real.json)。

相关文件：`adapters/sources/environment.py`、`app/pulse/file_events.py`、`app/pulse/platform_store.py`、`app/pulse/platform.py`、`adapters/models/semantic_watch.py`、`apps/agent-runtime/native-bootstrap.mjs`、`scripts/watch.py`。

## P3：统一入口和视觉

六个主入口：Agent、Computer、Watch、Tasks、Devices、Settings。Agent 仍是首页；会话列表和小型 Pulse 保留。电脑审批和异常也进入 Pulse。Tasks 汇集 Pi 回合、电脑动作、后台唤醒、持久通知和事件日志，能返回原会话或驻守详情。Pi 回合结束与实际任务成功分开表示；电脑“已重新观察”不冒充目标已完成。

沿用分层日出 Logo、暖色中性色、克制边框与字体层级。所有新页共用现有 tokens，保留深浅色、窄屏导航、焦点、减少动态效果，并增加 Cmd/Ctrl + 1–6 路由快捷键。没有默认示例任务或假设备运行状态。

UI 测试使用真实本地 HTTP、SQLite 与文件事件，不替换 console API。验证了 Watch 开始、暂停、编辑后仍暂停、恢复、触发、通知、详情与移除，以及模块关闭后对话入口仍可用。1440×900 和 390×844 检查无横向溢出；浅色/深色执行 WCAG 2 A/AA 与 2.1 AA 扫描及页面错误检查。

相关文件：`apps/pulse/src/App.tsx`、`Platform.tsx`、`platform.css`、`StatusBar.tsx`、`Settings.tsx`、`Timeline.tsx`。

macOS 后台系统提醒也已接入：可在 Settings 单独启用，服务端发送，不依赖网页常驻。正式界面的测试调用获得系统请求成功反馈，用户原来的开关保持关闭。

## 可复现检查

在仓库根目录执行：

```sh
uv sync --all-packages --all-extras --group dev
uv run ruff check .
uv run python scripts/gen_openapi.py --check
uv run pytest -q --ignore=tests/ui
uv run pytest -q tests/ui
uv run python scripts/verify_watch_service.py --output /tmp/morning-watch-acceptance.json
```

分别在两个前端目录执行：

```sh
# apps/agent-runtime
npm test
# apps/pulse
npm run build
```

已通过的完整回归：753 项非 UI 测试通过，9 项需要外部环境的测试跳过；72 项 UI 测试通过；10 项 Node 测试通过。后续界面调整又单独通过 8 项关键 UI 回归。构建、Ruff 和既有 OpenAPI 检查通过。上游 Pi CLI 独立运行确认版本为 0.99.1。新增动作协议使用公共 `jsonschema_lite`，没有破坏 protocol/kernel/driver 的依赖方向。

## 视觉验收

选择延续现有工作空间的方向：复用日出标识、纸色/深色表面与字体，而非增添运维仪表盘。对同一路由、空状态、1440×900 与 390×844 进行比较；完善控件状态、窄屏排列和主题切换。渲染评估从 84.8 到 88.2/100，最后一轮增益较小，已达到本次已实现页面的收敛条件。这是内部视觉评估，不是用户满意度指标。分项和依据见 [评分记录](agent-platform/visual-review.json)、[原始评估](agent-platform/visual-scorecard.json)。未实现的跨机器和多窗口能力没有计作已完成。

## 仍然存在的边界

- 真实 Computer 验收覆盖本机 macOS 与计算器点击；键盘、拖拽、跨应用复杂工作流尚未逐项完成真机验收。驱动依赖已安装的签名 Computer Use 组件和第三方 MCP 桥接。
- 当前应用观察使用桥接返回的窗口。独立的多窗口 ID 选择尚未接入；不能把按应用选择称为完整多窗口管理。
- 多显示器/区域接口已实现，但本机只发现一个显示器，未完成多显示器实机验收；屏幕权限由用户在设备和系统两层授权。
- 远程设备的地址、独立凭据、测试与权限接口已接好；跨机器与 TLS/SSH 部署尚待真实远程设备验收，未宣称 Windows/Linux 输入驱动已完成。
- 通知包含持久 Inbox、浏览器通知和可选 macOS 后台系统提醒。系统提醒已从正式 GUI 调用并收到“已请求显示”的反馈，见 [反馈截图](agent-platform/system-notification-browser.png)；没有把这一响应当作用户已看到横幅。邮件等外部渠道未接入。
- 默认整合部署仍由同一个后台宿主组合 Pi/Watch，另有独立 Watch 运行入口；独立远程 Watch 服务的 console 代理与运维托管尚未实现。
- 原生 RPC 无法呈现 Pi 的任意终端自定义组件；这项 Pi RPC 边界沿用上次验收说明。
- 前端主包约 160 KB gzip，构建有未压缩体积超过 500 KB 的提示；没有把该提示隐藏为“零警告”。

本次证明了本机可运行闭环与已列出的恢复/授权边界。多窗口精确选择仍有实现缺口；跨机器与多显示器仍需实际环境验收，不能标为全部完成。
