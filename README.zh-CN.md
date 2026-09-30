# WakeCore

[English](README.md) · [协议规范](docs/spec/ui-runtime-protocol.md) · [Extractor](docs/extractor.md) ·
[桌面协议](docs/spec/desktop-runtime-protocol.md) ·
[同类对比](docs/comparison.md) · [ADR](docs/adr) · [变更日志](CHANGELOG.md)

**持久化、带权限控制的长期自主任务运行时。**
按计划或事件观察数据源，用确定性规则判断是否"值得醒来"，必要时才调用模型；所有外部动作都要经过
授权、绑定到具体载荷的审批、预算与幂等控制。崩溃后可恢复，不能证明没发生过的副作用绝不重发，
任何任务都能只读回放而不触碰外部世界。

> 状态：**alpha（0.3）**。内核的保证在 SQLite 和 PostgreSQL 上都有测试覆盖。
> 浏览器的眼和手已在公开测试站点上跑过，macOS 桌面的眼和手已在真实计算器上跑过，
> 两者都通过 OpenAI 兼容中转调用了真实模型。见[已知限制](#已知限制)。

内核（`wakecore`，PyPI 名 `morning-erection`）只用 Python 3.12 标准库；PostgreSQL 驱动是可选依赖。
浏览器 sidecar（`wakecore_ui_runtime`，PyPI 名 `morning-erection-ui-runtime`）是独立进程，
通过 [UI Runtime 协议 v1](docs/spec/ui-runtime-protocol.md)（[OpenAPI](spec/ui-runtime/openapi.v1.json)）与内核通信：
Playwright 是"眼"（确定性读取，不调模型），OpenAI Computer Use 是"手"（只用于需要交互的操作），
浏览器登录态只在 sidecar 里，密码和 Cookie 不进内核、不进模型。

## 快速开始
```bash
git clone https://github.com/jiahuacaogoodman-art/morning-erection && cd morning-erection
uv sync --all-packages --all-extras --group dev
uv run wakecore demo                     # 离线成绩演示：假时钟、无模型、无外部发送
uv run pytest -q --ignore=tests/ui       # 默认跑在 SQLite 上；PG 测试会显示 skipped
```
PostgreSQL 部署与 M2 并发门禁见 [docs/runbooks/postgres.md](docs/runbooks/postgres.md)，
日常运维见 [docs/runbooks/operations.md](docs/runbooks/operations.md)，设计决定见 [docs/adr](docs/adr)。
网页"插眼 + 代操作"（Playwright 观察、Computer Use 操作、内核负责权限与状态机）见
[ADR 0006](docs/adr/0006-browser-eye-and-hand.md) 和 [docs/runbooks/browser.md](docs/runbooks/browser.md)；
完整示例见 [examples/web_watch_todomvc](examples/web_watch_todomvc)。

**OpenAI 兼容中转。** sidecar 使用 OpenAI Responses 协议，只换 `--openai-base-url` 和 key 就能接中转、代理或自建网关，
前提是它支持图片输入，并且支持 `computer` 工具或普通函数调用（`--variant function`，用于回 `Unsupported tool type: computer` 的中转）；
不支持 `previous_response_id` 的中转由 sidecar 重发对话。`wakecore-ui-runtime --check-model` 用两次很短的调用检查这几项。
中转能看到截图，所以它是单独的 egress `model:openai-compatible:<主机名>`，需要授权，并在内核里设置
`WAKECORE_UI_MODEL_EGRESS`。见 [docs/runbooks/browser.md](docs/runbooks/browser.md) 第 1.1 节。

**macOS 桌面应用。** 第二个 sidecar `wakecore-desktop-runtime` 通过 Computer Use MCP 桥接
（[tmustier/codex-computer-use-mcp](https://github.com/tmustier/codex-computer-use-mcp)）读取和操作本地应用。
内核设置 `WAKECORE_DESKTOP_RUNTIME_URL` 后，会注册 `desktop.macos`（眼）和 `desktop.cua`（手）：
- 眼用声明式 extractor 读辅助功能树，不调用模型；
- 手按元素序号操作，不用坐标；
- 两者都限定在授权里的 bundle ID（`resource_scope.apps`）；
- 屏幕上有密码或验证码框时不打字，转交给人。

见 [docs/runbooks/desktop.md](docs/runbooks/desktop.md) 和 [ADR 0008](docs/adr/0008-desktop-runtime.md)。

## 模块
内核路径都在 `packages/wakecore/src/wakecore/` 下，sidecar 在 `packages/wakecore-ui-runtime/src/wakecore_ui_runtime/` 下。

| 路径 | 职责（RFC 章节） |
|---|---|
| `kernel/domain` | 枚举、记录、状态机、TaskSpec 解析、错误码（§4、§11、§15） |
| `kernel/ports` | 存储/时钟/工具/模型/数据源协议，表目录 `tables.py`（§11、§13）；`annotations.py` 导出描述符与 MCP 工具形式 |
| `kernel/scheduling` | 触发器、occurrence、catch-up、等待（§6、T13、T14） |
| `kernel/events` | 接入签名校验、去重、投递，纯 reducer 变化检测（§6、§14） |
| `kernel/decision` | 路由规则、模型结果准入、通知模板（§5）；`profiles.py` 可插拔 DecisionProfile（`grades.v1`、`generic_watch.v1`） |
| `kernel/policy` | EffectiveAuthority 计算与动作授权（§7、§9） |
| `kernel/actions` | 动作提案、审批、派发许可、对账、无效果重试（§8） |
| `kernel/execution` | 步骤执行、租约、执行槽、恢复（§10、§12） |
| `kernel/budget` | 预算账户与预留（§9.3、T15、T16） |
| `kernel/commands`, `kernel/service.py` | 控制命令、幂等、版本并发控制（§15） |
| `kernel/replay.py` | 只读确定性回放（§14、T22） |
| `adapters/*` | SQLite（开发）、PostgreSQL（生产）、离线数据源、站内信/邮件工具、脚本化模型、密钥 |
| `adapters/postgres/sql/*.sql` | 由表目录生成的迁移，测试保证不漂移 |
| `adapters/sources/playwright_web.py`, `adapters/tools/browser_cua.py`, `adapters/ui_runtime/client.py` | 浏览器之眼/手，经 HTTP 调用 sidecar，只用标准库 |
| `protocol/` | 共享契约：`schemas/`（TaskSpec、接入事件、API 错误、UI Runtime 各端点）、`jsonschema_lite.py` 校验器、`openapi.py` 生成器 |
| `plugins.py` | 第三方数据源/工具的 entry point 发现，只加载 `WAKECORE_PLUGINS` 白名单 |
| `testing/conformance.py` | 适配器一致性测试套件 |
| `app/api`, `app/cli`, `app/worker` | WSGI API、命令行、worker 循环 |
| `wakecore_ui_runtime/` | sidecar：会话、观察、extractor、验证、Computer Use 循环、写前 journal、请求守卫 |
| `wakecore_ui_runtime/desktop/` | macOS 桌面 sidecar：MCP 桥接、辅助功能树 extractor、带守卫的动作循环 |
| `adapters/sources/desktop_app.py`, `adapters/tools/desktop_cua.py` | 桌面之眼/手的内核侧适配器，只用标准库 |
| `wakecore_ui_runtime/testing/` | 测试用：本地教务门户（CSRF+密码+TOTP、注入公告、各类故障）、OpenAI computer-use 协议模拟 |
| `spec/` | 由路由表生成的 OpenAPI 3.1 文档，测试保证不漂移 |

## 测试
| 目录 | 内容 |
|---|---|
| `tests/unit` | 纯函数核心、架构约束（内核不依赖适配器）、插件、一致性套件、MCP 映射 |
| `tests/contract` | 观察流程、接入事件、HTTP API、迁移文件、JSON Schema、UI Runtime 协议、OpenAPI 漂移 |
| `tests/security` | 审批绑定（T11）、模型越权（T12）、后续任务限制、租户隔离（T20） |
| `tests/recovery` | 崩溃/租约/对账/预算（T05–T10、T14–T16）与**随机故障测试** |
| `tests/replay` | 确定性回放（T22） |
| `tests/integration_postgres` | M2 并发门禁，需要 `WAKECORE_PG_DSN` |
| `tests/ui` | 真浏览器场景：验收、越权/注入/会话/改版、崩溃恢复、extractor 各模式；需要 Playwright + Chromium，否则跳过 |
| `tests/real` | 真实公网测试站点（the-internet、books.toscrape、TodoMVC、saucedemo）+ 真实 Computer Use；需 `WAKECORE_REAL=1`，模型场景还需 `.secrets/openai.key`；报告写到 `reports/real/` |
| `tests/real_desktop` | 真实 macOS 计算器 + 真实 MCP 桥接（D1 只用眼；D2 用真实模型操作）；需 `WAKECORE_REAL=1 WAKECORE_REAL_DESKTOP=1` |

验收矩阵 T01–T22 每项都有以 `test_tNN_` 开头的用例。随机故障测试的种子范围可调：
`WAKECORE_FUZZ_FIRST=1 WAKECORE_FUZZ_SEEDS=500`。

## 已知限制
- SQLite 测试只证明逻辑，不证明并发；并发以 PostgreSQL 门禁为准。已在单机嵌入式 PG 16.2、
  非超级用户角色下验证，未做多机、故障切换或**任何容量测试**。
- 真实模型场景已于 2026-09-30 运行，走的是一个 OpenAI 兼容中转，不是 api.openai.com：
  - `tests/real` R2（TodoMVC）和 R3（saucedemo），使用 `--variant function`；
  - `tests/real_desktop` D1/D2，在 macOS 计算器上。
  这些都是演示目标上的单次运行，不代表可靠性。planner 在所有测试中都是脚本化的。
- 桌面 runtime 目前有这些限制：
  - 只支持 macOS，需要 Node，以及辅助功能和录屏权限；
  - 同一时间只能做一项操作；
  - 凭据框是按关键词判断的，宁可多交给人。
- **目前没有随包提供的生产 planner 模型适配器**：内置推理适配器是脚本化的，所以部署后 `"on_met": "plan"`
  的任务不会提出任何动作。`"on_met": "notify"`（盯 + 通知）不需要模型，已经端到端跑通，
  见 [examples/web_watch_todomvc](examples/web_watch_todomvc/)（附真实运行记录）。
- 站内信会写入并在时间线里显示为已确认的动作，但还没有列出站内信的 API。
- "眼"已在真实公网测试站点上验证（`tests/real` R1、R1b、R3b）；这些都是公开的演示/测试站点，不是真实业务系统。
- 除上述浏览器路径外只接入了离线/脚本化适配器：Gmail 及其他第三方服务**未验证**。
- 模型判断质量（召回、误报、提示注入）未评测；状态机测试通过不能说明模型判断可靠（RFC §18）。
- 事件匹配型等待只有单元级覆盖。
- 同租户非所有者：读接口返回 NOT_FOUND，控制命令返回 POLICY_DENIED（都不泄露内容，但不一致）。
- 以 GET 执行的站点副作用不在写前日志中，只能靠重新观察判断。
- sidecar 用 Python 实现，协议与语言无关，可以换成其他语言实现。

路线图见 [docs/roadmap.md](docs/roadmap.md)。

## 参与与安全
[CONTRIBUTING.zh-CN.md](CONTRIBUTING.zh-CN.md)（DCO 签名、测试分层、不可违反的约束）·
[SECURITY.md](SECURITY.md)（私密报告、威胁模型）· [行为准则](CODE_OF_CONDUCT.md)。

## 许可
[MIT](LICENSE)。
