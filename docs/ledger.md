# Meter reading verification & monthly invoice ledger (v2)

在 v1 单期阶梯计价之上增加电表读数核验与月度发票账簿。v1 的 `--source/--output`
调用方式保持不变；新增三个子命令，库文件路径一律由显式 `--ledger` 给出：

```sh
tariff-rating ingest --batch fixtures/batch.json --ledger run/ledger.json
tariff-rating status --ledger run/ledger.json [--account A]
tariff-rating close  --ledger run/ledger.json --account A --period 2025-01
```

对同一 `--ledger` 路径重复提交同一批次，只观察到同一已提交账簿。

## 配置（仅 seq=1 的首批可携带）

- `accounts` / `meters`：账户与表计归属（meter -> account）。
- `period_start` / `period_end`：连续 UTC 月度账期边界，均必须是 UTC 月首时刻；
  每账户最多 120 个账期。
- `max_interval_seconds`：每表计允许的最大相邻读数间隔，正整数且不得超过最短账期秒数。
- `max_rate_kwh_per_hour`：最大速率，正有理数（整数或分数字符串），单位 kWh/小时。
- `exchanges`：换表声明，仅允许同账户 `out_meter`/`in_meter` 在同一边界 UTC 时刻成对。
- 价目表沿用 v1 校验（整数最小货币单位、末档无限、边界严格递增）。

## 读数与区间核验

- 用量按半开区间 `(left, right]` 累计到右端所在账期；共享边界读数不重复计量。
- 账期起止必须各有精确落在边界时刻的锚点；表计区间触及的每个账期都要求该表在两侧
  边界有锚点，缺锚点只使关联账期 `held`。
- 跨越非相邻边界的区间使其覆盖的全部账期 `held`。
- 换表边界：两表终值/起值在同一边界成对出现时，两个读数均为锚点且该边界不产生用量；
  未配对换表使相邻账期 `held`。其他累计下降为 `held` 异常。
- 超速判定（严格大于）：`delta_kWh * 3600 > max_rate * elapsed_seconds`，全程分数精确。
- 缺锚点、超间隔、未配对换表、超速或非整数用量只 `held` 关联账期，不拒绝整批；
  `held` 账期不可 `close`，也不生成发票。完整账期以核准的整数差调用既有阶梯计价。

## 批次身份与修订

- 持久库批次序号从 1 连续递增；每批记录 `batch_id`、`seq` 与规范 JSON 内容 SHA256。
- 同 id+seq+SHA 重放返回已存结果（`replayed: true`）；id 或 seq 任一复用但身份或 SHA
  不同均为冲突；缺序号拒绝。结构冲突、缺序号、非法身份整批拒绝，库与报告不变。
- `reading_id` 在 meter 内唯一，`revision` 从 1 开始：
  - 低于已知最高 revision 的到达被忽略并记为 `stale`；
  - 最高 revision 内容相同为幂等；同 revision 内容不同拒绝整批；
  - 较高 revision 可改时间和值，但不得改 meter/account 身份。
- 每表计最多 20 万有效读数（超限为结构性拒绝）。

## 关账与迟到更正

- `close` 只接受完整且 released（非 held、无阻断、无未分配余额）的账期；关账后发票字节
  （含 SHA256）与冻结价目表永久不可变，固定费只收取一次。
- 迟到更正改变已关账期应收时不重发旧发票：按该期冻结价目表重算更正后应收，减去原发票
  金额与此前已关账的净调整，正/负余额作为该源期目标 adjustment，分配到同账户最早的开放且
  released 账期；开放期暂配是可替换状态，不永久冲减源期目标净额。
- 源期再次更正先撤销其全部未关账暂配再按新目标重分配；随接收期关账的调整才永久计入
  “已关账净调整”。无开放 released 接收期时保留未分配余额并使账户 held。
- 已关账源期因后续读数异常变 held 时原发票仍冻结：撤回暂配、目标标 `pending`，并阻断该
  账户所有接收调整的开放期关账；恢复后重算目标、替换暂配并释放阻断。同一源期/版本 SHA
  的计算结果幂等。开放期自身的更正直接重算该期 quantity 与应收。

## 提交代与持久化

- 账簿为单个 JSON 文件，每次成功 ingest/close 原子替换并递增 `generation`；派生视图
  （区间、账期、暂配）不入库，加载时由持久事实确定性重算。
- 提交失败（测试可用环境变量 `TARIFF_LEDGER_FAULT=1` 模拟）不发布文件并回滚内存状态，
  重放同批只能观察到旧代或完整新代。
- `status` 返回最新代、批次水位、各账期状态、冻结发票金额与未分配调整。
- 全量重算成本为 O(有效读数 + 账户数×账期数)，在 20 万读数/表、120 期/账户上限内可评估；
  不以放宽发票不变性或延迟账期状态更新换取性能。

## 只读冻结发票取数（供 cash-reconcile）

- `ledger.load_snapshot(path)` 只读解析账簿（≤64MB、拒绝符号链接），不触发读数链
  重算、不写回；返回状态与账簿原始字节 SHA256。
- `ledger.frozen_invoice(state, account, period, invoice_sha256, currency)` 仅从
  `closed` 的 invoice 原文取数：移去 `invoice_sha256` 字段后按 canonical/SHA 规则
  重算并与存储值及请求值双重核对（可检测所选冻结发票被篡改），再核对请求的
  account/period/currency。真实应收为已含调整的 `invoice.total`；即使当前读数令
  该源期 held 或 pending，冻结发票仍可收款。详见 [cash.md](cash.md)。
