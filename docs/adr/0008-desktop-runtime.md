# ADR 0008：桌面之眼与手：macOS 原生应用经 Computer Use MCP 桥接

- 状态：已采纳（0.3.0 之后，未发版）
- 关联：约束 1–10、ADR 0004、ADR 0006、[桌面协议](../spec/desktop-runtime-protocol.md)

## 背景
V0.3 的眼和手只覆盖浏览器，但很多要盯的东西在本地应用里，比如计算器、系统设置、各种本地客户端。
OpenAI 的 hosted `computer` 工具在我们实测的中转上不可用（Codex 账号中转会拒绝）。
另一方面，[tmustier/codex-computer-use-mcp](https://github.com/tmustier/codex-computer-use-mcp)
把 macOS 的辅助功能（Accessibility）树和点击/输入包装成了一个 stdio MCP 服务器。

## 决定

### 1. 形态：一个新的 runtime + 一个 connector + 一个 tool，内核不动（约束 1、2）
- **sidecar**：`wakecore_ui_runtime.desktop` 是独立进程 `wakecore-desktop-runtime`，协议为
  `wakecore.desktop-runtime/1`。它和浏览器 runtime 共用这些东西：
  - 错误信封、journal、send-once 语义；
  - 同 key 不同请求时返回 409；
  - 模型 egress 检查和协作式取消。
- **内核侧**只加两个标准库适配器：
  - `DesktopAppSource`：connector `desktop.macos`，能力 `desktop.observe`；
  - `DesktopCuaTool`：tool `desktop.cua`，能力 `desktop.operate`，副作用类型 `EXTERNAL_WRITE`。
- 两者共用 `DesktopRuntimeClient`，它是 `UiRuntimeClient` 的子类，只是换了协议名。
  协议族不同就互相拒绝，所以浏览器客户端连不上桌面 runtime，反过来也一样。
- bootstrap：设置 `WAKECORE_DESKTOP_RUNTIME_URL` 之后才注册这两个适配器。
  如果没有显式传入 system policy，默认策略会加上 `desktop.observe`、`desktop.operate` 和对应的模型 egress。
- 以下内容都没有改：ObservationPort、ActionPort、lease、scheduler、approval、outbox、UNKNOWN/reconcile。

### 2. 按 bundle ID 授权，不按显示名（约束 7、8）
- 授权范围是 `resource_scope.apps`，一个 bundle ID 列表；动作里的 `app` 必须在这个列表里。
  两者都进入 action digest 和审批。
- runtime 会检查每次读回来的树确实来自这个 bundle ID，不是就返回 `app_mismatch`，不读也不操作。
- 显示名随语言变化，比如 “Calculator” 和 “计算器”，还可能重名，所以不接受显示名。

### 3. 眼：确定性读取无障碍树（约束 3）
- `observe`/`verify` 读完整的树（`disableDiff: true`，从不解析 diff），再套用声明式 extractor。
- 元素按“是什么”来定位：`id`、`description`、`head_regex`、`under` 等，从不按序号，因为序号每次会话都会变。
- 一个选择器匹配到多个元素就报错，不会默认取第一个。
- extractor 必须带 `ready` 选择器，用来证明当前窗口是对的。
- 真机上的例子：计算器显示区的节点在中文系统里是 `文本 ‎36`，在英文系统里是 `text 36`。
  所以 extractor 用 `head_regex: "^(text|文本) "` 加数字正则，与界面语言无关。

### 4. 手：只按元素序号操作，不给坐标（约束 4、5）
- 模型每一轮拿到的是树（凭据类元素的值已替换为 `[hidden]`）和截图。
  它只能调用一个普通函数工具 `desktop_actions`，动作包括 click、set_value、type_text、press_key、
  scroll、select_text、secondary_action、wait，全部用元素序号指定目标。
- 序号在发送前可以检查：元素是否存在、是否可用、是否是凭据框。坐标做不到这一点，所以不提供坐标和拖拽。
- 以下动作会被拒绝：
  - 往凭据框写值；
  - 屏幕上有凭据框时打字或按非无害键（结果为 `needs_human credential_field`）；
  - 系统级组合键，比如切换应用、Spotlight、强制退出、锁屏。
- 每个动作先写入 journal 再发送（写前 journal）。journal 只记动作类型和元素序号，不记输入的文本。
- 桥接超时或进程重启时，动作可能已经到达应用，此时结果是 `unknown`，由内核重新观察来对账，绝不重做。

### 5. 应用状态在本地：发出去却没验证到，就是 unknown（约束 9、10）
- 浏览器那边有服务器可以对账，桌面应用的状态只在本机。
- 所以 `desktop.cua` 只在确定性重读确认条件成立时才返回 `confirmed`。
  没有发出任何变更动作时是 `failed_no_effect`，其余情况都是 `unknown`。
- reconcile 只做这几件事：
  - journal 显示 `never_received` 或 0 个变更动作时，返回 `no_effect`；
  - 条件成立时返回 `confirmed`；
  - 否则返回 `still_unknown`，按 `recheck_seconds` 节流，从不重发。
- 回放不连接 runtime。

### 6. 模型历史
- 默认用 `previous_response_id` 把多轮串起来。
- 实测有的中转对串联请求只返回一个笼统的 400 `invalid_request`，不会说明原因。
- 所以 `history=auto` 的做法是：串联请求一旦返回 400，就用客户端历史重发一次；
  成功后本次会话一直使用客户端历史。
- 模型调用本身没有副作用，所以重试是安全的。

## 真机验证（2026-09-30）
在 `tests/real_desktop` 下，默认不启用，需要 `WAKECORE_REAL=1 WAKECORE_REAL_DESKTOP=1`：
- **D1**：内核的 watch 通过真实桥接读取真实计算器，结果为 SUCCESS，没有任何操作。
- **D2**：
  1. 测试扮演用户，在内核之外直接点按钮，输入 12。
  2. 内核观察到 12，由脚本 planner 生成计划，审批通过后，真实模型经中转操作真实计算器：
     按了 3 个键（× 3 =），用了 1–2 轮模型调用。
  3. 确定性重读得到 36，动作为 `CONFIRMED`，任务 `COMPLETED`。
- 第一次运行时，中转对第二轮串联请求返回 400。当时内核仍然因为独立重读而正确确认；
  之后按第 6 节修复，再跑一遍就干净完成了。

## 后果
- 目前只支持 macOS，需要给桥接进程授予辅助功能和录屏权限，还需要 Node。
- 凭据判断（`has_sensitive`）是按关键词匹配的，在真实复杂应用里可能偏严，导致误报 needs_human。
  宁可多交给人，也不冒险。
- 同一时间只能进行一项桌面操作；观察遇到操作进行中会等待，超时后返回 503 `desktop_busy`。
