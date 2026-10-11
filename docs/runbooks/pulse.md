# Morning Agent：理解、执行与 Pulse

Morning 的核心是 Agent：理解目标、使用工具、读取结果、继续完成任务。网页驻守是其中一项
后台能力。默认界面没有示例任务、预设条件按钮、列选择器或比较运算表单。

## 启动

```bash
# apps/agent-runtime
npm ci --ignore-scripts
npm run prepare:runtime
npm test
# apps/pulse
npm ci
npm run build
# 仓库根目录
uv sync --all-packages --all-extras --group dev
uv run python scripts/pulse.py
```

打开 `http://127.0.0.1:8788`。`--workspace` 可指定编程工作区，默认是仓库根目录。
在设置 → 模型网关中添加 API 根地址、协议、模型和 API Key / Bearer Token。支持添加、修改、
删除凭据或整个配置、选择当前服务，以及真实模型连接测试。只有实际返回模型结果才记录成功。
当前安全存储实现使用 macOS 钥匙串；其他平台尚未实现系统凭据后端，会明确拒绝保存，不回退到明文。

首次启动会把已有 `.secrets/openai.key`、`.secrets/openai.base_url`（或 `OPENAI_API_KEY`、
`OPENAI_API_KEY_FILE`、`OPENAI_BASE_URL`）迁入新配置；旧密钥文件须为 0600。原文件不删除，以免
影响其他启动方式。后续以 GUI 保存的配置为准，删除后不会再次导入。`--model` 只用于首次迁移。
旧配置使用原本的 Responses `instructions` 格式；其他网关默认 Pi 原生格式，可在兼容性设置中调整。
凭据经继承的私有 socket 进入 Pi CredentialStore，不进入 argv、公共 RPC、GUI 回读或诊断日志。

没有指定 `--runtime-url` 时自动启动本地浏览器 sidecar。已有 sidecar 用
`WAKECORE_UI_RUNTIME_URL` 与 `WAKECORE_UI_RUNTIME_TOKEN` 连接。网页操作仍需要兼容的 Computer Use
模型；常规 DOM 观察不调用模型。数据接收方进入系统策略、任务授权和浏览器审批摘要。

## Pi 执行机制

默认运行 `native-bootstrap.mjs`，实际复用 Pi 0.99.1 的 `AgentSessionRuntime`、持久 `SessionManager`、
`ModelRuntime`、`DefaultResourceLoader` 与导出的 `runRpcMode`。见
[ADR 0011](../adr/0011-native-pi-and-gateway-credentials.md) 与 [来源说明](../../apps/agent-runtime/THIRD_PARTY.md)。
普通对话直接进入同一个 Pi 会话，不再经过驻守分类器，不再指定四工具白名单或自设 24 轮上限。
模型能力继承 Pi 模型目录；未知模型使用 Pi 自身的默认值，也允许填写自定义能力。

GUI 提供新建、恢复、命名、复制、历史消息分支、会话树导航、JSONL 导入与 HTML 导出；支持模型、
思考深度、图片输入、处理中补充指令、完成后再做、队列模式、停止、自动重试和上下文整理。
停止先清空等待队列再执行 Pi abort，未执行的排队文字返回编辑器。整理上下文后，原始历史仍可查看。

Skills、提示词、工作区说明、扩展、MCP 与工具发现使用 Pi 的原生资源加载器和项目信任机制。
GUI 可查看诊断、重载、选择活动工具，并通过 Pi 原生包管理器安装、更新和移除资源包。
Pi 自带提供方的 API Key / OAuth 流程接到 GUI 和相同钥匙串存储；本次真实认证验收针对现有 API Key 网关。

扩展的选择、确认、输入、多行编辑、通知、状态、文本组件和编辑器填充接入 GUI。Pi RPC 本身不支持
终端自定义组件、原始终端按键和 TUI 专属主题切换，这些不会伪装成已支持。
版本固定的 HTTP 初始化与项目信任解析各使用一处 Pi CLI 深层导出；升级 Pi 时必须重新验证。

默认使用 Pi 原生文件和终端工具，权限随运行 Morning 的本地账户，不使用旧适配器的工作区沙箱。
`runtime.mjs` / `workspace-tools.mjs` 仅保留为旧适配器回归参考；不属于默认运行路径。
WakeCore 浏览器扩展的授权、审批与提交后核验仍由内核管理，不替代 Pi 的通用工具执行模型。

Pi CLI 的发布 shrinkwrap 固定了旧 brace-expansion。`prepare:runtime` 将其指向显式锁定的
5.0.12，并同步本地 lockfile；安装使用 `--ignore-scripts`。启动前检查补丁，测试和审计需通过。

## 对话式驻守

直接说明目标和页面。Agent 会读取页面，结合上下文确定相关对象与真实等待条件。
缺链接或对象不明确时，只追问阻止任务成立的信息；不让用户手动配置业务含义。
确认简短计划后才激活任务。带否定与意义的条件使用 `semantic_watch.v1`，只在相关内容变化时
调用模型判断。确定的数值条件可编译为 `web_watch.v1` 的低成本确定性检查。

Pi 通过 `morning_inspect_page` 获取观察，再以观察 ID 调用 `morning_prepare_watch`；
WakeCore 校验后给出可确认的计划。`morning_watches` 提供已有驻守的查询和控制。
对象和成功字段依据实际页面内容选择，准备计划不会自动激活任务。
未通过、审核中、按钮存在不能冒充成功。模型不可用或判断不确定时，不假装理解或完成。

Pulse 默认收缩，点击才查看任务，工程配置在设置内。无变化时减慢检查，变化时恢复较快节奏；
故障退避并遵守 Retry-After。登录和结构失效暂停检查，保留可信记录。修改间隔保留观察基准，
暂停/恢复/改设置不能绕过服务器退避。审批绑定具体载荷、范围、数据接收方；提交后重新观察。
无法确认的动作进入对账，不重新提交。

复杂 SPA 仍需可靠的加载标记；自动内容读取覆盖主文档 DOM，尚不包括 iframe、封闭 Shadow DOM
和 canvas/OCR。这是通用机制，不意味着每个业务站点无需适配。

## 数据与验证

`.pulse/` 为私有状态目录，保存任务数据库、会话引用、对话、Pi 会话和浏览器状态。目录 0700，
会话文件 0600，被 Git 忽略。关闭网页不停止 worker；关闭启动进程后停止，重启恢复持久状态。
本地应用只监听 loopback，使用 HttpOnly/SameSite cookie 和 Host/Origin 检查；不是远程多用户账号服务。

```bash
uv run ruff check
uv run python scripts/gen_openapi.py --check
uv run pytest -q --ignore=tests/ui
uv run pytest -q tests/ui
# apps/agent-runtime
npm test
npm audit
```

测试替身验证故障和回归，不冒充真实模型证明。真实 Gateway 的连接、错误凭据、重启恢复，
以及 GUI → 原生写文件、精确编辑、读取、终端验证 → 回复的结果见
[本次验收记录](../verification/2026-10-11-native-pi-gateway.md)。
`--demo` 的课程页仅为验收夹具，不作为默认产品内容，也不证明真实业务系统选课成功。


## 扩展平台入口

Computer、Watch、Tasks、Devices 与独立服务启动方式见 [平台运行手册](platform.md)。
当前本机闭环、原生 Pi 回归和未覆盖环境见 [平台验收](../verification/2026-10-11-agent-platform.md)。
