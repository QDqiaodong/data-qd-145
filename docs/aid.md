# 公共用量援助计划 (aid-plan)

`aid-plan` 从一份独立 JSON 快照制定一轮用量券分配方案，不读取也不写入电表账簿；
输入是每户已确认总用量，不涉及读数修订、关账或发票调整。v1 计价与
ingest/status/close 入口完全保留。

```sh
tariff-rating aid-plan --source fixtures/aid.json --output run/aid-report.json
```

## 输入快照

顶层字段恰好为 `currency fixed_charge tiers households districts credit_budget`：

- `currency` / `fixed_charge` / `tiers`：沿用 v1 契约（整数最小货币单位、边界严格
  递增、末档无限、最多 100 档、rate 为 0..10^9）。
- `households`：最多 300 户。每户恰好 `id district quantity credit_cap target_relief`：
  - `id` 唯一；`district` 必须引用一个已定义地区（每户只属于一个地区，允许空地区）；
  - `quantity` / `credit_cap` 为 0..10^12 整数；`target_relief` 为 1..10^24 整数。
- `districts`：最多 30 个。每个恰好 `id min_served`：`id` 唯一；`min_served` 为非负整数。
- `credit_budget`：0..10000 整数，全局券数上限。

拒绝重复 JSON 键、重复户/地区 id、未定义字段、非法地区引用及布尔伪整数；
任何输入或计算失败退出 2 且保留旧报告（输出原子替换、拒绝符号链接、输入输出不得相同）。
输入 JSON 上限 8MB。

## 援助曲线

对每户定义原价 `B(q)`（固定费加阶梯变量费），抵扣 c 单位后应付 `B(q-c)`，
救助额 `S(c) = B(q) - B(q-c)`。固定费在差值中抵消，从不构成救助；跨档使用真实
增量价格，单价允许下降或为零。合法 c 为 `0..min(quantity, credit_cap)` 的整数。

报告为每户输出覆盖整个合法整数 c 域的分段仿射曲线：每段给出闭区间 `lo`/`hi`
及 `S(c) = a*c + b` 的整数 `a`/`b`；段按 `lo` 递增、无缺口无重叠，相邻斜率相同
已合并。边界点归入其右侧增量斜率所在段（域末端沿用左侧；仅 c=0 时 a=b=0）。
曲线只在档位边界处分段，计算量为 O(档数)，绝不按用量逐单位展开。

由曲线求达到 `target_relief` 的最小整数券数 `need` 与实际救助额 `S(need)`；
`S(min(quantity, credit_cap))` 仍达不到目标时该户 `ineligible`。零价平台、同档
边界或固定费都不会产生虚假的可救助户。

## 地区保障分配

只允许对 eligible 户发放恰好 `need` 张券或不发放；券不可拆分转让，不能向已选户
额外加券。全局券数不超过 `credit_budget`，且每个地区至少有 `min_served` 户被选。
可行方案按顺序优化：

1. 被选户数最大；
2. 实际救助额之和最大；
3. 所用券数最少；
4. 排序后被选 id 列表字典序最小（Python 字符串顺序）。

分配器给出全局精确最优解：以券数为下标的精确动态规划，目标向量折叠进单个
大整数（户数位图的超递增权重使最大掩码即字典序最小名单，并可由掩码直接重建
所选集合），不贪心、不浮点近似、不截断前沿、不枚举户子集。先按无约束背包
快速判定保障是否自然满足，仅当地区最低保障真正约束时才进入带地区计数的
动态规划；资源规模为 O(预算 × Σ 地区户数 × min(保障数, 地区可救助数−保障数))，
与 quantity、target_relief、货币金额的大小无关。

## 报告

`schema_version` 为 3；`source_sha256` 是规范化快照（户/地区按 id 排序后的规范
JSON）的哈希，因此同输入及打乱户/地区输入顺序产生完全相同的规范 JSON 报告。

可行时（`business_status = "feasible"`）报告含：

- 逐户 `eligible`、`need`、`selected`、`relief`（实发救助额）、`payable_after`
  （抵扣后应付）及完整 `curve`；
- 各地区 `min_served`、`eligible_count`、`served`（完成数）；
- `objectives` 目标向量 `[被选户数, 总救助额, 总券数]`、`selected_ids`（字典序）
  及 `totals`。

若地区保障与预算不能同时满足，输出合法业务报告（退出 0）：
`business_status = "infeasible"`、各地区 `eligible_count`（可救助户数）、
`min_vouchers_for_guarantees`（满足全部保障的最少总券数；某地区人数不足则为
null），且 `objectives` 为 null、`selected_ids` 为空——不输出任何部分发券名单。

## 正确性与资源代价

- **长零价平台**：零费率段在曲线上就是一个斜率为 0 的段。`need` 的求解只扫描
  段端点（每段 O(1)），跨越任意长度的零价平台都是一次斜率判断，不逐单位枚举；
  平台造成的 `need` 跃变（目标刚过平台时券数陡增）被精确捕获，绝不会把
  `target_relief` 直接当作券数。
- **地区保障**：分配动态规划以券数为下标、以“当前地区已选/已排除户数（取
  min(保障数, 可救助数−保障数) 较小者）”为第二维，逐户 0/1 转移，地区结束时
  只保留满足最低保障的状态。总代价 O(预算 × Σ 地区户数 × min(保障数,
  地区可救助数−保障数))，状态规模只随户数、地区数、档数与 0..10000 预算增长，
  与 quantity、target_relief、货币金额的大小无关；也不枚举户子集。先用一次
  无约束背包检测保障是否自然满足，仅在真正约束时才进入带地区维度的计算。
- **选择重建**：每户按 id 字典序分配一个 2 的幂位（超递增权重），与户数、救助额
  一起折叠进动态规划值。最大掩码恰好对应字典序最小的被选名单，且掩码本身即
  所选集合的位图——最优名单直接从最终掩码解码，无需额外回溯或保存中间层，
  平局规则精确可追溯。同一快照及任意打乱户/地区输入顺序都得到逐字节相同的
  规范 JSON 报告。
