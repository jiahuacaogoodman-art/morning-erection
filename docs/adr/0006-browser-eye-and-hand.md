# ADR 0006：浏览器之眼与手（V0.3）：Playwright 观察、Computer Use 操作、内核只做大脑

- 状态：已采纳（V0.3）
- 关联：V0.3 规格 P0–P3、约束 1–10、ADR 0001、ADR 0004

## 背景
目标链路：真实网页观察 → 变化 → WakeCore 唤醒 → 判断/计划 → 授权 + 审批 → Computer Use 操作页面
→ 重新观察验证 → `CONFIRMED`；出错时进入故障恢复。分工是：Playwright 当眼睛，OpenAI Computer Use
当手，WakeCore 负责大脑、权限、记忆和状态机。

## 决定

### 1. 进程边界：内核 ← HTTP → UI Runtime sidecar
- 内核里只有两个标准库适配器，都通过 `adapters/ui_runtime/client.py` 走 localhost HTTP：
  `PlaywrightWebSource`（connector `web.playwright`）和 `BrowserCuaTool`（tool `browser.cua`）。
- Playwright、Chromium、OpenAI Responses 协议都只在 sidecar 包 `wakecore_ui_runtime/` 里（0.3.0 起路径为
  `packages/wakecore-ui-runtime/src/wakecore_ui_runtime/`，见 ADR 0007）。
  `tests/unit/test_architecture.py` 保证 `wakecore/**` 不 import `playwright`、`openai`、`wakecore_ui_runtime`（约束 2）。
- 现有的 `ObservationPort`、`ActionPort`、lease、scheduler、approval、outbox、UNKNOWN/reconcile
  都没有重写；浏览器只是新的 connector 和新的 tool（约束 1）。
- sidecar 用 Python 写，没有按规格建议用 TypeScript，原因是开发时机器上没有 Node。
  现在已装了 Node 24，但 sidecar 还没有移植。
  HTTP 协议与实现语言无关，可以换成 TS 实现；0.3.0 起的规范是
  [docs/spec/ui-runtime-protocol.md](../spec/ui-runtime-protocol.md)（`wakecore.ui-runtime/1`，JSON Schema + OpenAPI）。

### 2. 眼：确定性读取，从不调用模型（约束 3）
- `/v1/observe` 按声明式 extractor 读页面，有两种模式：
  - **表格**：按 aria-label/caption 或 CSS 选择器定位；用表头文本定位列，列可以改名、重排。
  - **列表/卡片**：每个匹配 item 是一条记录；列用相对选择器，可读属性，`exists` 判断元素是否存在。
  - 记录 id 取自行属性 `key_attr`，或某一列 `key_field`。
  - `ready` 选择器：读之前先等它出现，应对 SPA 晚渲染。列表模式必须有，否则"空列表"和"还没渲染"分不清。
  - 登录标记：URL、标题或元素选择器；命中判为 `AUTH_REQUIRED`。
  - key 重复、单元格缺失、数值解析失败，都判为 `SCHEMA_INVALID`，不会静默返回空结果。
- 观察模式下，guard 会拦掉一切非 GET/HEAD/OPTIONS 请求。
- 结果映射为 `SourceHealth`：
  - 读不出表：`SCHEMA_INVALID`，保留原来的可信快照；
  - sidecar 不可达：`UNAVAILABLE`；
  - 部分目标缺失：默认是 `PARTIAL` 加 `coverage_gaps`。
    scope 设了 `missing_targets: "absent"`（要求有 `ready`）时，按"已读、不存在"处理。
    之后目标出现，就是一次真实变化，例如新待办、新上架商品。
- 条件判断（例如"余量 > 0"）由 `DecisionProfile generic_watch.v1` 在内核里确定性完成；
  只有 `on_met: plan` 时才会调用 planner。

### 3. 手：只有被授权、被审批、被限定 origin 的一次操作（约束 4、6、7、8）
- `browser.cua` 的能力是 `browser.operate`（EXTERNAL_WRITE），并且声明了 `required_egress = model:openai-computer-use`。
  （后续增补：使用 OpenAI 兼容中转时 egress 是 `model:openai-compatible:<主机名>`，由 `WAKECORE_UI_MODEL_EGRESS`
  配置；sidecar 对审批的 egress 与自己实际的端点不一致的动作返回 409 `model_egress_mismatch`，不调模型。）
  如果任务没有被授予这个 egress，planner 看到的工具列表里就没有它；硬塞进计划也会被拒（`data_egress_not_allowed`）。
- payload 里的 URL 字段（`start_url`、`verify.url`）必须落在任务的 `resource_scope.origins` 内，
  否则在审批之前就是 `DENIED origin_outside_scope:<field>`。
  这个检查覆盖 userinfo 技巧（`https://origin@evil/`）。
- payload、resource scope、data_egress 共同进入 action digest，审批绑定这个 digest。
- sidecar 在浏览器网络层再拦一次：
  - 越界请求一律 abort；
  - 主框架越界导航判为 `origin_escape` 并停止；
  - 弹窗和新标签直接关闭；
  - 文件选择器、对话框被拒绝；
  - 往 password/OTP 字段打字会停下来，结果是 `needs_human:credential_field`；
  - 模型返回的 `pending_safety_checks` **永不自动确认**。
    当前 GA 文档已不再提这个字段，preview 变体仍然处理它。

### 4. 凭据不进模型、不进业务表（约束 5）
- 用户用 `wakecore-ui-login` 在有界面的 Chromium 里亲手输入密码、2FA、验证码。
- 登录态只存在 sidecar 的 state 目录（权限 `0700`），包括持久化 profile 和会话 cookie 备份文件。
- 内核的 source binding 的 secret 只是一个 `session_ref` 名字（例如 `jw_alice`）。
- 测试会逐条检查所有发给模型的请求体，以及 WakeCore 数据库，确认里面没有密码、TOTP 密钥和会话 token。

### 5. 每个副作用都要有结果验证（约束 9）
- `BrowserCuaTool.execute` 分三步：
  1. **先验证**：目标已经达成，就直接 `confirmed`（`already_satisfied`），不调用模型。
  2. **执行**。
  3. **再验证**：重新确定性地读页面，条件成立才 `confirmed`。
- 结果判定：
  - 没有任何写请求发出 → `failed_no_effect`；
  - 发出过写请求但目标没达成、超时、sidecar 崩溃、5xx → `unknown`。
- `reconcile` 只重新观察页面，并查询 `/v1/act/status`，从不再次操作页面：
  - 条件成立 → `CONFIRMED`；
  - journal 证明写请求从未发出 → `FAILED_NO_EFFECT`；
  - 超过 `settle_seconds`（默认 120 s）仍未达成 → `FAILED_NO_EFFECT`；
  - 其余情况保持 `UNKNOWN`。
  - 每个 key 至少间隔 `recheck_seconds` 才重查一次，按内核时钟计。
- **客户端状态**（scope `state_location: "client"`）：站点状态存在浏览器里，例如 localStorage 里的待办、购物车。
  这时"没有写请求"证明不了"没有效果"：
  - 模型实际动过页面（`actions_executed > 0`）但验证没通过，结果是 `unknown`，不是 `failed_no_effect`；
  - 对账不走 settle 窗口，只有重新观察到目标达成才 `CONFIRMED`，否则一直保持 UNKNOWN，等人工 `resolve`；
  - 只有 journal 证明 act 根本没开始（`never_received`），或模型一个动作都没执行，才判 `failed_no_effect`。
  这是在真实站点上发现的：TodoMVC 和 saucedemo 的购物车都不发任何写请求。
- sidecar 的 journal 是写前日志：每个写请求先记账、后放行，所以能区分"没发出"和"发出了但不知道结果"。
- 同一个 effect_key 同时只能有一次 act；重复投递返回 journal 里记下的结果，不会碰页面。

### 6. 回放只重算（约束 10）
`replay_task` 只读存储。验收测试和恢复测试都会在回放前停掉 sidecar，然后断言门户命中数、写请求数、模型调用数都不变。

### 7. 时间语义
- 动作截止时间 = `min(tool.max_duration(600 s), limits.max_run_seconds)`。
- act 的 `timeout_s` = 剩余预算 − `verify_reserve`（10 s），给执行后的验证留时间。
  剩余预算不足 5 s 时直接 `deadline_too_close`，结果是 `failed_no_effect`。
- 尝试租约 = 截止时间 + 30 s。内核崩溃后，租约过期，状态走 UNKNOWN → reconcile。

### 8. 并发
- 每个 session 在 sidecar 里有一条专用浏览器线程，observe、verify、act 串行执行。
- 一个 profile 同一时间只能被一个 Chromium 进程打开。人工重新登录前，要先调用 `/v1/sessions/release`。

## 真实场景测试中发现的问题（已修复）
**Chromium 会在网络层静默重发 POST。**
- 场景：门户提交成功，随后直接断开连接（`crash_after_commit`）。
- 现象：门户收到两次相同的确认 POST，间隔 0.4 ms；guard 的 journal 却只记了 1 次。
- 原因：Chromium 在复用的 keep-alive 连接被无响应关闭时，会自动重试请求。这个重试发生在
  Playwright 路由拦截之下，guard 看不见。
- 这次没造成危害，只是因为模拟门户自己去重（"您已选过该课程"）。真实站点未必会去重。
- 修正：act 模式下，guard 不再用 `route.continue_()`，改为自己用 `route.fetch(max_redirects=0, max_retries=0)`
  发送写请求，**恰好一次**，再 `route.fulfill`。
  这个请求与浏览器上下文共享 cookie，不跟随重定向，重定向仍交给页面。
  网络错误时 abort，结果判为 unknown，交给对账。
- 回归测试：`tests/ui/test_browser_recovery.py::test_portal_drops_the_connection_after_committing`，断言恰好 1 次 POST。

## 真实站点测试（`tests/real`，需 `WAKECORE_REAL=1`）
探查过的公开测试站点，以及它们带来的改动：
| 站点 | 发现 | 改动 |
|---|---|---|
| the-internet `/tables` | 表格没有 aria-label、caption，行也没有 id | 表格可按 CSS 选择器定位；`key_field` 用某列当 id |
| books.toscrape、saucedemo | 数据是卡片、列表，不是表格 | 列表模式、`attr`、`exists` |
| saucedemo | 登录页和商品页的 URL 标题都是 "Swag Labs"，URL 也几乎一样 | 登录墙按元素 `#login-button` 识别 |
| TodoMVC（React SPA） | 列表在 DOMContentLoaded 之后渲染；目标待办一开始不存在 | `ready` 等待；`missing_targets: "absent"` |
| TodoMVC、saucedemo 购物车 | 状态只在 localStorage，零写请求 | `state_location: "client"` |

- 纯观察场景（R1、R1b、R3b）已在真实网站上通过：
  - 读取稳定，digest 可复现；
  - 不调用模型；
  - 用户在自己的 profile 里退出登录后，数据源变成 `AUTH_REQUIRED`。
- 真实 Computer Use 场景（R2 TodoMVC 勾选待办、R3 saucedemo 加购一件商品）已写好。
  不带 key 空跑过：唤醒 → 计划 → 审批 → 预验证全部走通，模型那一步干净地失败，结果是 `FAILED_NO_EFFECT`。
  带真实模型的结果在填入 key 并运行后记录到 `reports/real/`。
- 为真实运行补充了两项：
  - 回执里记录模型调用次数和 token 用量；
  - OpenAI 错误正文经脱敏（`sk-…`、`Bearer …`）后写入回执。
- 协议对齐了当前 GA 文档：默认模型 `gpt-5.6-sol`；截图带 `detail: "original"`；
  支持 `[x, y]` 形式的坐标；click、scroll 等动作的 `keys` 按住修饰键（Shift、Control、Alt、Meta）。

## 已知局限
- **GET 请求的副作用 journal 看不见。** 如果站点用 GET 链接执行操作，写前日志就无法证明"没发出"。
  这种情况下对账只能靠页面状态和 settle 窗口判断。
- 验证依赖 extractor 能读到目标字段。页面结构变到读不出时，结果只能是 UNKNOWN，不会误判为 CONFIRMED。
- 真实 OpenAI Computer Use 在 key 填入之前**未验证**。`wakecore_ui_runtime/testing/fake_openai.py` 按公开文档的
  Responses computer-use 协议（GA 和 preview 两种变体）实现，并检查协议是否用对，但它不是真实模型，
  它的"视觉"读的是模拟门户的状态和固定几何坐标。
- 完整的恢复矩阵（崩溃、重复投递、竞争者抢座）只在本地模拟教务门户上跑过（CSRF + 密码 + TOTP）。
  真实网站只跑了上面那些场景，而且都是公开的测试站点，不是真实业务系统。
- `state_location: "client"` 下，被模型动过但没验证通过的动作只能人工结案，这是有意为之，代价是需要人介入。
