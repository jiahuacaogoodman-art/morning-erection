# 运行手册：桌面之眼与手（macOS）

设计见 [ADR 0008](../adr/0008-desktop-runtime.md)，协议见 [docs/spec/desktop-runtime-protocol.md](../spec/desktop-runtime-protocol.md)。

## 0. 前提
- macOS，Node 20+。
- 桥接：[tmustier/codex-computer-use-mcp](https://github.com/tmustier/codex-computer-use-mcp)，
  它通过 stdio MCP 读取辅助功能树，并执行点击和输入。
```bash
git clone https://github.com/tmustier/codex-computer-use-mcp ~/src/codex-computer-use-mcp
cd ~/src/codex-computer-use-mcp && npm install && npm run build
```
- 在「系统设置 → 隐私与安全性」里，给运行桥接的程序（终端或 node）授予**辅助功能**和**录屏**权限。
  没有权限时，runtime 的 `health.bridge.ok` 为 false，观察结果是 `UNAVAILABLE mcp_unavailable`。
- 桥接自身没有权限模型。哪些 app 能读、能操作，完全由 runtime 按内核的授权（bundle ID 列表）来检查。

## 1. 启动 Desktop Runtime（sidecar，只监听 127.0.0.1）
```bash
uv run wakecore-desktop-runtime --state ~/.wakecore-desktop --port 8766 --token "$WAKECORE_DESKTOP_RUNTIME_TOKEN" \
    --mcp-command "$HOME/.local/node/bin/node $HOME/src/codex-computer-use-mcp/dist/mcp-server.js" \
    --openai-key-file .secrets/openai.key --openai-base-url "$(cat .secrets/openai.base_url)"
```
- `--mcp-command` 里的 node 建议写**绝对路径**。sidecar 不经过登录 shell，PATH 里不一定有 node。
- 只用"眼"时可以不给 key，这时 `act` 返回 `model_not_configured`。
- key 文件权限必须是 `600`，只有 sidecar 读取，内核不需要。
- 可选参数：
  - `--model`，默认值与浏览器 runtime 相同；
  - `--history auto|server|client`（环境变量 `WAKECORE_CU_HISTORY`），默认 `auto`，见第 5 节。
- 启动后打印 `LISTENING 8766`，以及模型接收方 `MODEL ... (egress ...)`。
- `--state` 目录里有 journal 和每一步的截图，权限为 `0700`。

## 2. 让内核使用它
```bash
export WAKECORE_DESKTOP_RUNTIME_URL=http://127.0.0.1:8766
export WAKECORE_DESKTOP_RUNTIME_TOKEN=...
export WAKECORE_DESKTOP_MODEL_EGRESS=model:openai-compatible:<中转主机名>   # 用官方端点时不设
```
- 设置后，`bootstrap.build()` 会注册 connector `desktop.macos` 和 tool `desktop.cua`，
  并在默认系统策略里加入 `desktop.observe`、`desktop.operate` 和上面的 egress。
- 授权时写 `resource_scope.apps`，内容是 **bundle ID** 列表，例如 `["com.apple.calculator"]`。显示名不被接受。
- egress 必须和 runtime `health.model_egress` 一致；不一致时，动作在调用模型之前就会以
  `FAILED_NO_EFFECT model_egress_mismatch` 结束。

## 3. 写 extractor（只读，确定性）
extractor 放在任务的 `source.resource_scope.extractor` 里，会进入 digest，也能回放。
元素按"是什么"来找，不按序号，因为序号每次会话都会变。下面是计算器显示区的例子，已在真机上验证，与界面语言无关：
```json
{"ready": {"id": "StandardInputView"},
 "records": [{"id": "display", "columns": [
   {"field": "value", "node": {"under": {"id": "StandardInputView"}, "head_regex": "^(text|文本) "},
    "pattern": "(-?[0-9][0-9.,]*)\\s*$", "group": 1, "type": "number"}]}]}
```
- `ready` 必须写，它证明当前窗口确实是目标窗口。另有两个可选项：`login` 选择器，命中时为 `AUTH_REQUIRED`；
  `window` 标题检查。
- 选择器字段：`id`、`description`、`value`、`help`、`head`、`head_regex`、`under`、`nth`。
  匹配到多个元素而没写 `nth` 时直接报错，不会默认取第一个。
- 列转换（`pattern`/`group`/`type`/`default`/`transform`）与浏览器 extractor 完全相同，见 [docs/extractor.md](../extractor.md)。
- 本地化：中文系统里显示节点是 `文本 ‎36`，中间有一个不可见的方向符。所以要用 `head_regex` 加数字正则，不要写死 `text`。
- 先检查再用：`POST /v1/extractors/validate`，不会读取任何 app。

## 4. 手：`desktop.cua`
- 模型每一轮拿到树和截图，只能调用函数 `desktop_actions`。
  动作包括 click、set_value、type_text、press_key、scroll、select_text、secondary_action、wait，全部用元素序号指定目标，
  没有坐标，也没有拖拽。
- 以下动作会被拒绝：
  - 目标是凭据框，或者屏幕上有凭据框时打字，结果为 `needs_human credential_field`；
  - 系统级组合键；
  - 目标是不存在或已禁用的元素。
- 结果只有经过确定性重读（`verify`）确认才是 `CONFIRMED`。
  没有发出过变更动作时是 `FAILED_NO_EFFECT`，其他情况都是 `UNKNOWN`。
  对账只重新观察，从不重发。

## 5. 模型历史与中转
- 默认用 `previous_response_id` 串联多轮。
- 实测有的中转对串联请求只返回一个笼统的 400 `invalid_request`。
  `--history auto` 遇到这种情况会用客户端历史重发一次，成功后本次会话一直这样发。
  重试是安全的，因为模型调用本身不产生副作用。
- `--history server` 关闭这个回退；`--history client` 从第一轮就发客户端历史。

## 6. 排障
| 现象 | 含义 / 处理 |
|---|---|
| `health.bridge.ok = false` / `UNAVAILABLE mcp_unavailable` | 桥接没起来：检查 `--mcp-command`（node 绝对路径、`dist/mcp-server.js` 是否已 build）、权限。 |
| `UNAVAILABLE app_mismatch` | 读回来的树不是所请求的 bundle ID（app 没开，或前台是别的 app）。什么都不会用。 |
| `SCHEMA_INVALID not_ready` | `ready` 选择器没命中：窗口不对，或界面改版。可信快照不会被覆盖。 |
| `503 desktop_busy` | 正在执行一个 act，观察等待超时后返回，可以重试。 |
| 动作 `FAILED_NO_EFFECT act_needs_human:credential_field` | 屏幕上有密码、验证码之类的输入框，工具已停手，需要人来处理。 |
| 动作 `UNKNOWN` | 发出过变更动作但没有验证到结果（包括桥接超时和 runtime 重启）。对账会重新观察，**不会重发**，也可以人工 `resolve`。 |
| 数据源 `UNAVAILABLE desktop_runtime_protocol_mismatch` | 连到了浏览器 runtime，或主版本不同。 |
| `state/journal/` | 每个 effect_key 的写前日志，只有动作类型和元素序号，不含输入的文本。 |

## 7. 测试
```bash
uv run pytest -q tests/contract -k desktop      # 假桥接 + 假模型，无需 macOS 权限
```
真机测试只会操作计算器，默认不启用：
```bash
https_proxy=http://127.0.0.1:7897 WAKECORE_REAL=1 WAKECORE_REAL_DESKTOP=1 uv run pytest -q -s tests/real_desktop
```
- **D1**：只用"眼"，不需要 key。内核 watch 通过真实桥接读取计算器显示，结果为 SUCCESS。
- **D2**：需要 key。
  1. 测试扮演用户，在内核之外输入 12。
  2. 内核观察到 12，经审批后，真实模型按 × 3 =。
  3. 确定性重读得到 36，动作 `CONFIRMED`，任务 `COMPLETED`。
- 桥接的命令可以用 `WAKECORE_DESKTOP_MCP_COMMAND` 覆盖。报告写到 `reports/real/<时间>/desktop.{json,md}`。
