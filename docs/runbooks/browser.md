# 运行手册：浏览器之眼与手（V0.3）

## 安装
```bash
uv sync --all-packages --all-extras --group dev     # 开发：整个 workspace 一个 .venv
uv run playwright install chromium
# 部署：pip install "morning-erection[ui]" && playwright install chromium
```

## 1. 启动 UI Runtime（sidecar，只监听 127.0.0.1）
推荐把 key 放在文件里（权限必须是 `600`，否则拒绝启动），只有 sidecar 读它，内核不需要：
```bash
chmod 600 .secrets/openai.key
uv run wakecore-ui-runtime --state ~/.wakecore-ui --port 8765 --token "$WAKECORE_UI_RUNTIME_TOKEN" \
    --openai-key-file .secrets/openai.key
```
- 也可以用环境变量 `OPENAI_API_KEY_FILE` 指定文件；都没有时退回 `OPENAI_API_KEY`。
- 模型默认 `gpt-5.6-sol`（GA `computer` 工具，截图以 `detail: "original"` 发送，坐标不失真）。
- OpenAI 返回的错误正文会先脱敏（`sk-…`、`Bearer …`）再写进 journal 和回执。
- 可选参数：
  - `--model` 或环境变量 `WAKECORE_CU_MODEL`；
  - `--variant ga|preview|function`，对应 `computer`、`computer_use_preview` 工具，或把同样的动作包装成普通函数工具
    `computer_actions`（中转不支持 `computer` 工具时用，见 1.1）；
  - `--history auto|server|client`（环境变量 `WAKECORE_CU_HISTORY`），默认 `auto`，见 1.1；
  - `--headed`，以有界面模式运行；
  - `--openai-base-url`，见 1.1。
- `--allow-faults` 只用于测试。
- `--state` 目录就是凭据库，权限 `0700`，里面有持久化 profile、会话 cookie、journal 和截图。
  要像对待密码库一样备份和保护它。

## 1.1 使用 OpenAI 兼容的中转 / 代理 / 自建网关
协议不变（OpenAI Responses API），只换地址和 key：
```bash
echo 'https://relay.example.com/v1' > .secrets/openai.base_url && chmod 600 .secrets/openai.base_url
uv run wakecore-ui-runtime --check-model --openai-key-file .secrets/openai.key \
    --openai-base-url "$(cat .secrets/openai.base_url)"          # 先自检，两次很短的模型调用
uv run wakecore-ui-runtime --state ~/.wakecore-ui --port 8765 --token "$WAKECORE_UI_RUNTIME_TOKEN" \
    --openai-key-file .secrets/openai.key --openai-base-url "$(cat .secrets/openai.base_url)"
```
- 也可以用环境变量 `OPENAI_BASE_URL`。地址必须是 `https://`（只有 `127.0.0.1`/`localhost`/`::1` 允许 `http://`），
  不能带用户名密码、query 或 fragment；不合规时 sidecar 拒绝启动。通常以 `/v1` 结尾，sidecar 请求 `<base>/responses`。
- **前提**：中转必须实现 Responses API（只有 Chat Completions 不够），支持 `input_image` 截图输入，并且满足下面之一：
  - 支持 `computer` 工具（`--variant ga`，默认；`--variant preview` 时是 `computer_use_preview`）；
  - 或者支持普通函数调用：`--variant function`。很多中转背后是 Codex / ChatGPT 账号，
    会回 400 `Unsupported tool type: computer`，这时用这个模式。模型通过函数 `computer_actions`
    返回同样格式的动作（click/type/scroll/keypress…），由同一个执行循环、同一套守卫执行；
    动作之后的截图以 `function_call_output` + 一条带 `input_image` 的用户消息回传。
- **续接**：默认用 `previous_response_id`。中转拒绝它时（400，报错里提到 `previous_response_id`，
  例如 `previous_response_id is not available for this user`），`--history auto` 会自动改为由 sidecar
  每轮重发整段对话（不含 reasoning 和条目 id），同一轮重试一次，之后一直这样发。
  为了控制费用，只保留最近 3 张截图，更早的换成一行文字占位。`--history server` 关掉这个回退，`client` 从第一轮就重发。
  重试是安全的：被拒绝的请求没有到达模型，回传截图也不会重复任何网页动作。
- `--check-model` 正好检查这几项（两次很短的调用，`--variant`/`--history` 与运行时相同）：
  1. 发一张白色截图，要求模型用工具点一下。只有工具能点击，所以回答必须是工具调用
     （`computer_call`，function 模式下是 `computer_actions` 的 `function_call`）；
  2. 回传一张红色截图，要求模型说出颜色，必须答出 red。改为重发对话时，输出里会多一行 `history      client`。
  有的中转返回 200，却悄悄去掉了 `tools`。这时模型会回答"没有可用的浏览器工具"，自检报 `computer_tool_unused`。
  只检查 HTTP 状态码是发现不了这种情况的。
  输出最后一行是 `MODEL CHECK OK`，或 `MODEL CHECK FAILED: <错误码> - <提示>`（退出码 1）。key 不会被打印。
- **中转能看到截图。** 所以它是另一个数据接收方，egress 也不同：
  - api.openai.com → `model:openai-computer-use`（默认）；
  - 其他地址 → `model:openai-compatible:<主机名>`（小写，不含端口），例如 `model:openai-compatible:relay.example.com`。
  sidecar 启动时打印 `MODEL <模型> at <主机> (egress ...)`，`wakecore ui health` 里也有 `model_egress`。
- 内核那边要设置同一个 egress，授权和审批才会绑定到这个中转（见第 3 节）：
  `export WAKECORE_UI_MODEL_EGRESS=model:openai-compatible:relay.example.com`。
  不一致时 `wakecore ui health` 会在 stderr 提示；真要执行时 sidecar 在调用模型、写 journal 之前就返回
  409 `model_egress_mismatch`，动作记为 `FAILED_NO_EFFECT`，截图不会发给未经批准的接收方。

## 2. 人工登录一次（密码、2FA 只在浏览器窗口里）
```bash
uv run wakecore-ui-login --state ~/.wakecore-ui --session jw_alice \
    --url https://jw.example.edu/courses --done-url-contains /courses \
    --runtime http://127.0.0.1:8765 --token "$WAKECORE_UI_RUNTIME_TOKEN"
```
- 如果 sidecar 正在运行，脚本会先让它释放这个 profile。
- 登录过期时，数据源会变成 `AUTH_REQUIRED`，不会再读页面，也不会调用模型。
  处理步骤：
  1. 重新执行上面的登录命令；
  2. 调用 `mark_source_reauthorised` 恢复数据源。

## 3. 让内核使用它
```bash
export WAKECORE_UI_RUNTIME_URL=http://127.0.0.1:8765
export WAKECORE_UI_RUNTIME_TOKEN=...
```
设置后，`bootstrap.build()` 会注册 `web.playwright` 和 `browser.cua`，并在默认系统策略里加入
`web.observe`、`browser.operate`、`model:openai-computer-use`。
用中转时设置 `WAKECORE_UI_MODEL_EGRESS=model:openai-compatible:<主机名>`（见 1.1），
`browser.cua` 的 `required_egress`、默认系统策略、授权和审批 digest 都会改用这个值。

在内核里还需要三步配置：
1. 建一个 secret，值就是 session 名，例如 `jw_alice`。
2. 注册 source binding：connector 为 `web.playwright`，`secret_ref` 指向上一步的 secret。
3. 授予权限：能力 `web.observe` 和 `browser.operate`，egress `model:openai-computer-use`
   （用中转时是 `model:openai-compatible:<主机名>`），
   `resource_scope.origins` 为站点 origin。

完整的 TaskSpec 示例见 `tests/ui/conftest.py::World.spec`：
- `decision.profile = generic_watch.v1`，条件为 `seats gt 0`；
- `on_met: plan`，`complete_when: enrolled eq true`。

## 3.1 为真实站点写 extractor
extractor 放在任务的 `source.resource_scope.extractor` 里，会进入 digest，也能回放。
完整参考（英文，含真实站点例子）见 [docs/extractor.md](../extractor.md)，实现见
`packages/wakecore-ui-runtime/src/wakecore_ui_runtime/browser/extractor.py`。
写好后先检查：`uv run wakecore ui check-extractor my_extractor.json`（设置了 `WAKECORE_UI_RUNTIME_URL` 时由 sidecar 做完整检查，否则只做 schema 检查）。

- **表格**：按标签 `{"table": {"label": "可选课程"}}`，或按选择器 `{"table": {"selector": "#table1"}}`。
  列按表头别名匹配，列改名、重排都能读。
- **列表 / 卡片**：`{"list": {"item": ".inventory_item"}, "ready": ".inventory_list", ...}`。
  - 列用相对选择器；`"selector": ""` 表示 item 本身。
  - 可选 `attr` 读属性。
  - `type: exists` 判断元素是否存在，比如"移出购物车"按钮在不在。
  - 真实站点大多是这种结构。
- **记录 id**：用行属性 `key_attr`，或用某一列的值 `key_field`（例如邮箱、商品名）。
- **登录墙**：`login` 支持 `url_contains`、`title_contains`、`selector`。
  URL 和标题都不变的站点（例如 saucedemo）用 `{"selector": "#login-button"}` 识别。
- **ready**：读之前先等这个选择器出现，用来应对 SPA 在 DOMContentLoaded 之后才渲染。
  - 列表模式必须写，否则无法证明"列表确实为空"。
  - 登录选择器会一起等，先出现哪个就按哪个判断。

另有两个 scope 选项，和 extractor 并列：
| 选项 | 含义 |
|---|---|
| `"missing_targets": "absent"` | 页面已渲染（必须配合 `extractor.ready`）但目标不在：算"已读、不存在"，不再算覆盖缺口。之后目标出现就是一次真实变化，适合"出现新待办/新商品就处理"。 |
| `"state_location": "client"` | 站点状态存在浏览器里（localStorage 等，如 TodoMVC、saucedemo 购物车）。这时"没有写请求"不能证明没有效果：模型动过页面而验证没通过，结果是 `UNKNOWN`；对账不走 settle 窗口，只有重新观察到目标达成才转 `CONFIRMED`，否则由人工 `resolve`。 |

## 4. 排障
| 现象 | 含义 / 处理 |
|---|---|
| 数据源 `UNAVAILABLE ui_runtime_unreachable` | sidecar 没运行，或端口、token 错误。 |
| 数据源 `SCHEMA_INVALID` | 页面改版导致 extractor 读不出。可信快照没有被覆盖；需要更新 extractor 的表头别名。 |
| 动作 `FAILED_NO_EFFECT auth_required` | 审批之后登录失效了。按第 2 节重新登录。 |
| 动作 `FAILED_NO_EFFECT act_needs_human:*` | 页面要求输入凭据、越出 origin，或出现安全检查。工具已停手，需要人来处理。 |
| 动作 `UNKNOWN` | 写请求已经发出，但结果未确认。对账会定期重新观察；settle 窗口过后仍不成立，就转为 `FAILED_NO_EFFECT`。**不会自动重发**，也可以人工 `resolve`。 |
| 数据源 `UNAVAILABLE ui_runtime_protocol_mismatch` | sidecar 的协议主版本与内核不同（见 `GET /v1/health` 的 `protocol`）。把两个包升级到同一版本；请求在发出之前就被拒绝，不会产生副作用。 |
| 动作 `FAILED_NO_EFFECT model_egress_mismatch` | 审批时的模型接收方和 sidecar 当前配置的不同（换过 `--openai-base-url`，或内核没设 `WAKECORE_UI_MODEL_EGRESS`）。回执里有 `runtime_model_egress` 和 `approved_model_egress`。改好配置、按新 egress 授权后重新审批；没有调用模型，也没有写请求。 |
| `--check-model` 报 `http_404` | 地址下没有 `/responses`：base URL 一般要以 `/v1` 结尾，且中转必须支持 Responses API。如果提示里写的是模型名，说明中转没有这个模型，用 `--model` 换一个。 |
| `--check-model` 报 `computer_tool_unused` | 中转接受了请求，但模型没有拿到 `computer` 工具（提示里引用了模型的原话）。这种中转无法操作浏览器，只能换一个完整转发 `tools` 的端点；"眼"（Playwright 读取）不受影响。 |
| `--check-model` 报 `image_not_seen` | 模型说不出回传截图的颜色：中转可能丢掉了图片，模型等于在盲操作。 |
| `--check-model` 报 `http_503`/`http_4xx`，提示里提到 Chat Completions | 中转在这个地址只提供 Chat Completions，没有 Responses API，重试没有用。 |
| `--check-model` 在 `start` 步报 `http_400 Unsupported tool type: computer` | 中转（常见于 Codex / ChatGPT 账号背后的中转）不支持 `computer` 工具。加 `--variant function` 再自检；sidecar 也要用同样的参数启动。 |
| `--check-model` 在 `chain` 步报 `http_400` | 用了 `--history server`，而中转不支持 `previous_response_id`。去掉该参数（默认 `auto` 会改为重发对话）。 |
| 动作 `FAILED_NO_EFFECT act_canceled:cancel_requested` | 有人取消了这个动作，且没有写请求发出。 |
| `state/journal/` | 每个 effect_key 的写前日志：发出了哪些写请求、响应码、被拦截的请求。 |
| `state/artifacts/<hash>/` | 每一步的截图和 Playwright trace（`--no-trace` 关闭）。 |

## 4.1 查询与取消（协议 v1）
完整规范见 [docs/spec/ui-runtime-protocol.md](../spec/ui-runtime-protocol.md)。常用命令：
```bash
uv run wakecore ui health            # 协议版本、模型是否配置、启动时被标为 interrupted 的 journal 数
uv run wakecore ui capabilities      # extractor 类型、act 守卫、上限
uv run wakecore ui sessions          # 各 profile：open / closed / busy，不含 cookie 和路径
curl -s -H "Authorization: Bearer $WAKECORE_UI_RUNTIME_TOKEN" http://127.0.0.1:8765/v1/acts/<effect_key>
curl -s -X POST -H "Authorization: Bearer $WAKECORE_UI_RUNTIME_TOKEN" http://127.0.0.1:8765/v1/acts/<effect_key>/cancel
```
- `GET /v1/acts/<effect_key>` 返回完整 journal 记录：写请求和被拦截请求的方法与路径（不含 query、body、header）、结果、截图列表。
- 取消是协作式的：在模型两轮之间、同一批动作之间生效，不会打断正在执行的动作。
  结果是 `canceled` 加上已发出的写请求数；内核按普通失败处理（0 次写 → 无效果，否则 `UNKNOWN` → 对账），不会重发。
- 同一个 effect_key 换了请求内容（目标、起始 URL、origin、会话或登录标记）会得到 409 `idempotency_key_reused`。

## 5. 测试
```bash
uv run pytest -q tests/ui     # 真 Chromium + sidecar 进程 + 模拟门户 + 模拟 OpenAI，约 1.5 分钟
```
没有安装 Playwright 或 Chromium 的环境会自动跳过 `tests/ui`。

真实环境（公网测试站点；有 key 时用真实 Computer Use，按步数计费）：
```bash
WAKECORE_REAL=1 uv run pytest -q -s tests/real
```
- 场景：
  - R1、R1b、R3b 只用"眼"，不需要 key；
  - R0 用 `--check-model` 检查模型端点（官方或中转）；
  - R2（TodoMVC）和 R3（saucedemo）会调用真实模型，每个动作最多 12 步。
- `WAKECORE_REAL_MODEL` 可以换模型，`WAKECORE_REAL_VARIANT=preview` 换工具。
- 用中转时把地址写进 `.secrets/openai.base_url`（或设置 `WAKECORE_REAL_BASE_URL`），
  测试会把它传给 sidecar，并按 sidecar 报告的 egress 授权和审批。
- 报告和截图写到 `reports/real/<时间>/`。
