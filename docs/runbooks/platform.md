# Morning 平台运行

## 安装与本地启动

```sh
uv sync --all-packages --all-extras --group dev
cd apps/agent-runtime
npm ci --ignore-scripts
npm run prepare:runtime
cd ../pulse
npm ci
npm run build
cd ../..
uv run python scripts/pulse.py
```

入口：`http://127.0.0.1:8788`。Agent 是首页。Devices 配置模型 Gateway 和设备连接，Settings 打开 Pi 原生模型提供商认证、Skills、Extensions、MCP、工具与上下文管理。

数据目录默认 `.pulse`，只允许一个宿主占用同一数据目录。服务在创建 Pi 和 worker 前先占用端口，避免重复启动影响正在运行的会话。不要在运行中复制数据库用于另一个宿主。

## 独立运行与模块开关

下列命令是可选启动模式；先停止占用同一数据目录和端口的服务。独立并行实例请使用不同的 `--state` 和端口。

```sh
# 只有原生 Pi GUI，不加载 Morning 自定义工具，也不启动 Watch worker
uv run python scripts/pulse.py --no-custom-modules

# 独立 Watch：没有 Pi 进程、没有前端页面，通过同一 HTTP API 管理
uv run python scripts/watch.py --state /absolute/private/watch-state --port 8789

# 标准平台，但关闭某个增量模块
uv run python scripts/pulse.py --no-computer
uv run python scripts/pulse.py --no-watch

# 上游 Pi CLI，保留其原生终端体验、设置与认证行为
cd apps/agent-runtime
npm start
```

Settings 中也可以单独启停 Computer 和 Watch。停止 Watch 后不再领取后续步骤；正在进行的一次读取需要结束后才能退出。浏览器页面关闭不停止后台服务；电脑休眠、断网或退出服务时不能继续采集，重启服务后依据持久状态恢复。

`npm start` 指向上游 Pi CLI；旧桥接仅保留为 `npm run start:legacy` 的兼容验证入口。GUI 的默认路径始终是 `native-bootstrap.mjs` 和 Pi 原生 RPC。

## Computer 节点

本机节点按需启动。需要已安装 `codex-computer-use-mcp`，或现有源码目录 `~/src/codex-computer-use-mcp/dist/mcp-server.js`。也可通过 `WAKECORE_DESKTOP_MCP_COMMAND` 指定受信的 stdio MCP 启动命令。

它依赖已安装的签名 Computer Use 组件及 macOS 辅助功能权限。显示器捕获另外需要运行节点程序的录屏权限。Devices → 权限逐项允许应用观察、应用操作和显示器观察；初始均未授权。

节点可以单独启动。认证令牌通过环境变量 `MORNING_DEVICE_TOKEN` 提供，不放进命令行参数：

```sh
uv run morning-computer --state /absolute/private/computer-state --port 8767
```

`MORNING_DEVICE_TOKEN` 应来自操作者的安全存储。远程 Mac 运行同一节点，再通过受信的 HTTPS 代理或 SSH 隧道访问。在 Devices 添加地址与令牌；只允许 HTTPS 或 loopback HTTP，拒绝认证重定向。远程节点本身只监听 loopback，不自动暴露到公网。

设备令牌与 Pi/Gateway 凭据分别保存。节点不运行模型，也不继承模型认证环境变量。`GET /health` 返回 `morning.computer-node/1`；`/inventory`、`/observe`、`/screen` 为观察接口；`/act` 处理有唯一 key 的单步操作；`/permissions` 管理应用、显示器与暂停状态。

Computer Use 的授权是增量工具的边界，不是普通 Pi 文件/终端工具的系统沙箱。普通用户会话保持 Pi 原生权限；后台事件回合只开放受限观察与准备审批，完成后恢复原工具集合。

## Watch 来源与协作

在 Agent 或 Watch 输入目标。Pi 读取真实来源并给出待确认计划，不需要手工把自然语言翻译成运算符。支持网页、文件、当前层文件夹、日志尾部、进程/PID、HTTP GET/API、端口服务、应用可读内容、屏幕与区域。

HTTP 内容错误不冒充空结果；状态码模式可以观察 HTTP 状态。服务端 `Retry-After` 同样约束手动检查。文件正文上限 64 KB、日志读尾部、文件夹最多 500 个直接项目；超出时要求缩小来源。屏幕视觉变化使用持久基准及阈值，语义判断仅使用与事件匹配的证据。

API Bearer 凭据可通过受保护的 `source_credential` 管理操作写入独立钥匙串项，再以 `credential_ref` 引用；不会复用模型密钥，不接受携带凭据的 URL。这个高级配置目前没有专用图形表单。

后台通知默认持久保存在 Tasks → 通知。Settings → 后台系统提醒可让 macOS 后台直接请求提醒，页面关闭仍可发送；横幅是否出现取决于系统通知权限和专注模式。测试按钮明确区分“已请求系统显示”与“用户已看到”。不会在重启后重发结果不明的系统提醒，Inbox 仍保留。

唤醒 Pi 的任务绑定原会话与确认的模型接收方。用户切换到其他会话时，事件留在 Tasks 等待原会话；更换模型接收方时要求明确确认。后台只观察、报告和准备动作，不能直接使用终端、写文件或其他扩展绕过审批。需要原生编码操作时，在普通对话中继续处理。

## 事件与排障

统一平台事件格式：`version`、`seq`、`type`、`entity`、`at`、`payload`。`GET /api/pulse/events?after=<seq>` 返回下一批和游标；`has_more` 表示仍有未读取事件。Pi 的模型流式输出继续使用原生 RPC，平台记录其执行状态，不另造模型循环。

| 状态 | 处理 |
| --- | --- |
| 设备认证失败 | 在 Devices 更新该节点令牌；不要修改模型提供商密钥 |
| 桥接不可用 | 检查 MCP 安装路径、签名组件及系统权限 |
| 观察/审批过期 | 重新观察，按新画面准备操作；旧 key 不重发 |
| 操作结果未知 | 只重新观察和核验，不能直接重复提交 |
| 来源认证失效 | 更新来源凭据或人工登录，再显式检查/恢复 |
| 数据目录已占用 | 连接现有服务，或为另一个独立服务指定不同的 `--state` |
| Pi 空会话重启 | GUI 只保存选中空会话的 ID/设置；通过原生 SessionManager 恢复，不写入虚假消息 |

完整改动、真实验收与尚未覆盖的环境见 [验收记录](../verification/2026-10-11-agent-platform.md)。

接口依据为安装的 Pi 0.99.1 SDK/RPC 声明、[Pi 官方项目](https://pi.dev/) 和 [Computer Use MCP 的公开接口](https://github.com/tmustier/codex-computer-use-mcp)。
