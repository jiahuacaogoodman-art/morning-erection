# ADR 0004：外部副作用：effect_key、UNKNOWN 不重发、无效果才重试

- 状态：已采纳（v0.2，含随机故障测试发现的修正）
- 关联：RFC §8、T05、T06、T09、T10

## 决定
1. 每个动作有稳定的 `effect_key`，工具以它作为幂等键；同一 key 不同内容进隔离区（quarantine）。
2. 进入 `DISPATCHING` 后结果不明（崩溃、超时、租约过期）一律记为 `UNKNOWN`，**绝不直接重发**；
   由对账（`reconcile_unknown`）向工具按 effect_key 查询：
   - 查到效果 → `CONFIRMED`；
   - 证明无效果 → `FAILED_NO_EFFECT`，然后以**新的关联动作** `{base}#retry{n}` 重试
     （最多 `MAX_NO_EFFECT_RETRIES` 次，指数退避）；
   - 仍不确定 → 保持 `UNKNOWN`，等待人工 `resolve`（`RESOLVED_MANUALLY`）。
3. 需要审批的动作不自动重试，必须重新审批。
4. 任务完成判定（`internal_inbox_committed`）按 base effect_key 分组，每组必须有 `CONFIRMED` 或
   `RESOLVED_MANUALLY`，且没有待发/在途的成员。

## 修正（2026-09-29）
随机故障测试（`tests/recovery/test_randomized_faults.py`，500 个种子中 11 个失败）发现：
对账在任务 `PAUSED` 期间证明"无效果"时，旧实现因生命周期不是 ACTIVE/DRAINING 而跳过重试，
该通知永久丢失，任务在全部出分后卡在 `DRAINING`。现在 `PAUSED` 时也创建后继动作；
派发器本来就会在暂停期间推迟它，恢复后才发送。回归测试：
`test_t06_no_effect_proven_while_paused_is_still_retried_after_resume`。
修正后 SQLite 上 2000 个种子、PostgreSQL 上 200 个种子全部通过。
