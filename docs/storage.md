# 稳健储能充放电计划 (storage-plan)

`storage-plan` 从一份独立 JSON 快照把站点负荷与缺测储能遥测转成一条稳健充放电
计划。它不读取也不写入电表账簿或援助计划数据：没有读数修订、关账、发票调整或
券分配，输入输出都由显式 `--source` / `--output` 给出。v1 单期调用与
`ingest` / `status` / `close` / `aid-plan` 入口完全保留。

```sh
tariff-rating storage-plan --source fixtures/storage.json --output run/storage-report.json
```

## 输入快照

顶层字段恰好为
`currency fixed_charge tariffs slots battery grid_limit wear_rate demand_rate prefix`。

- `currency`：沿用 v1 标识规则（非空白、≤128 字符、无控制字符）。
- `fixed_charge` / `wear_rate` / `demand_rate`：`0..10^9` 整数最小货币单位。
- `grid_limit`：`0..24` 整数。
- `tariffs`：最多 24 份，每份恰好 `id tiers`。`id` 唯一；`tiers` 完全复用 v1
  增量阶梯语义——1..10 档、累计上界严格递增且为 `1..10^12`、末档必须 `null`
  无限、费率 `0..10^9`（允许跨档下降或为零）。
- `slots`：按时间顺序的 1..24 项，每项恰好 `id load tariff_id`。`id` 唯一；
  `load` 为 `0..24` 整数；`tariff_id` 必须引用已定义价目表。
- `battery`：恰好 `capacity initial_soc reserve charge_limit discharge_limit`。
  `capacity` 为 `1..12`；其余均为 `0..capacity` 整数。
- `prefix`：`0..min(8, 时段数)` 条遥测。每项恰好
  `slot_id import_lo import_hi soc_after`：`slot_id` 唯一且必须恰好覆盖
  `slots` 的连续前 k 段（可乱序给出）；`import_lo`/`import_hi` 为
  `0..grid_limit` 整数且 `lo<=hi`；`soc_after` 为 `null` 或
  `0..capacity` 整数。

拒绝重复 JSON 键、重复身份、未知引用、未定义字段、布尔伪整数和越界值；
任何输入或计算失败退出 2 并保留旧报告（原子替换、拒绝符号链接、输入输出不得
相同）。输入 JSON 上限 8MB。

## 物理与计费模型

一个时段动作 `a` 为整数且 `-discharge_limit <= a <= charge_limit`：

- `a > 0`：向电池存入 `a` 单位，但从电网取 `2a`（存储损耗在线侧）；
- `a < 0`：从电池释放 `-a` 单位，直接供负荷 `-a`；
- `a = 0`：仅由电网供负荷。

同一时段只能有一个方向的动作（不存在同时充电与放电）。状态与购电为

```text
s'  = s + a                                   必须 0 <= s' <= capacity
g   = load + 2*max(a,0) - max(-a,0)           必须 0 <= g  <= grid_limit
```

不允许售电。每时段能源费是 `g` 在该段 `tariff` 下的 `variable_charge`（增量阶梯
计价，不在档位越界后改价全部用量）；`fixed_charge` 全程只收一次。另收
`wear_rate * sum(abs(a))` 与 `demand_rate * P`，其中

```text
P = max(g[t-1] + g[t])，首段之前 g[-1] = 0
```

需求费按相邻两段购电之和收费且**不除以 2**；两段是指完整时间序列上的相邻段，
跨规划边界不清零。

## 前缀历史重建

遥测只限定实际 `g` 落在闭区间 `[import_lo, import_hi]`，非 `null` 的 `soc_after`
是该段末的精确锚点；实际动作未知。重建职责是保留**全部**满足物理规则、购电观测
与锚点的合法历史，并保留跨边界 SOC、最后购电、已发生需量峰值和前缀费用之间的
真实关联与见证——不得用区间中点补值，也不得挑一条历史代表全部。

实现以可达边界三元组 `(SOC, 最后购电, 前缀峰值)` 为状态做精确前向传播：每个三元组
保留前缀能源+磨损费的最大值以及取得该最大值时字典序最小的动作列表（作为完整
见证）。`prefix.reachable` 逐项输出三元组、`max_energy_wear` 和 `actions`；
`prefix.soc_range` 给出可达边界 SOC 的上下界。空前缀退化为唯一空三元组
`(initial_soc, 0, 0)`。

前缀不可达（无任何合法历史）时输出
`business_status = "infeasible"`、`reason = "inconsistent_prefix"`，退出 0。

## 稳健开环后缀

为剩余时段输出**同一条**开环动作序列。该序列必须对每一个合法前缀历史都满足物理
规则，且最终 SOC `>= reserve`；绝不会在未知历史之间切换动作。对每条合法历史拼接
同一后缀，按完整时间序列计算总账单。在全部安全后缀中依次最小化：

1. 最坏总账单（对所有合法前缀历史取最大）；
2. 后缀 `sum(abs(a))`；
3. 按时间排列的后缀动作整数列表（字典序，`-` 小于 `0`）。

最坏费用必须来自**同一条**合法历史：报告的总账单由见证历史自己的能源费、磨损费、
峰值和 SOC 共同实现，绝不把不同历史的能源费、磨损费、峰值拼成虚构场景。前缀
最坏平局选前缀动作列表字典序最小者。

后缀 DP 的状态是
`(后缀首段购电, 累计 SOC 偏移, 后缀末段购电, 后缀峰值)`，每个状态保留字典序最小的
`(费用, sum_abs, packed_actions)`。前缀可达 SOC 的区间 `[smin, smax]` 使“对全部历史
安全”精确等价于累计偏移落在 `[-smin, capacity-smax]`，于是全称安全被压缩成一次
区间约束。需要量收费的关键连接是：最终峰值取

```text
max(前缀峰值, 前缀最后购电 + 后缀首段购电, 后缀峰值)
```

即跨边界的两段在规划边界处重新接合，不在边界重置峰值。

若前缀存在但没有对所有历史共同安全的后缀，输出
`business_status = "infeasible"`、`reason = "no_safe_schedule"`，无计划，退出 0。
当 `k` 等于总时段数时后缀为空，仍检查全部历史的终端储备并计算最坏账单。

## 报告

`schema_version` 为 4。`source_sha256` 是规范化快照（价目表按 id 排序、前缀记录
按时段顺序后的规范 JSON）的哈希，因此同语义输入——包括调换 `tariffs` 顺序或
`prefix` 记录顺序——产生完全相同的规范 JSON 报告和语义输入 SHA。

可行时报告含：

- `prefix`：`slots`（k）、`soc_range` 以及 `reachable` 三元组列表（含每项的
  `max_energy_wear` 与 `actions`）；
- `suffix`：逐段 `slot_id`、`action`、`import`、`soc_after_lo`、`soc_after_hi`，
  以及整体 `actions`、`sum_abs`、`soc_range_after`；
- `worst_bill`：`total` 及 `energy_charge` / `wear_charge` / `demand_charge` /
  `fixed_charge` / `peak` 拆分；
- `objectives`：`[最坏总账单, 后缀 sum_abs, 后缀动作列表]`；
- `witness`：实现该最坏账单的完整历史——`prefix_actions`、`prefix_imports`、
  `prefix_soc_after`、`suffix_actions`、`suffix_imports`、`suffix_soc_after`、
  `prefix_energy_wear`、`peak`。

## 正确性与资源代价

- **不枚举历史或后缀**。前缀前沿最多
  `(capacity+1) * (grid_limit+1) * (2*grid_limit+1)` 个三元组（上限约
  13 × 25 × 49）；后缀 DP 的状态数只随时段数、SOC 容量、电网上限与动作幅度增长，
  与货币金额和负荷绝对值无关。绝不按货币值或逐单位展开状态，也不截断可达集合。
- **表示选择**：所选表示按 `(峰值, 最后购电)` 把前缀三元组分成等价类，每类只需
  保留前缀费用最大的那条见证——因为后续费用只通过类峰值、类最后购电和类内 SOC
  影响结果，类内其他历史被精确支配。另一种可行表示是按每个三元组逐一与全部后缀
  状态做笛卡尔积；它同样正确，但状态数为两集合之积，且仍须为每个类保留单独见证
  才能恢复最坏历史。所选类聚合把这些义务降为“每类一条见证 + 一次区间安全约束”，
  正确性义务包括：类内最大值见证必须是真实可拼接的历史（不是拼出的虚构场景）、
  全称安全须覆盖类内所有 SOC（由区间约束保证）、跨边界峰值须使用真实前缀最后购电。
- **费用相关性**：前缀峰值、最后购电与 SOC 来自同一条见证；后缀首段购电与后缀
  动作来自同一条后缀；最终峰值由这三者按物理时间顺序接合，不混合不同历史的极值。
- **见证恢复**：动作列表以
  `charge_limit + discharge_limit + 1` 进制定长打包进 DP 值；字典序最小动作即
  打包后的最小整数，因此最优见证直接从 DP 值解码，无需额外回溯表。

## 验收测试

`tests/test_storage.py` 用独立枚举对短时域核对：所有动作的独立枚举、可达边界
三元组集合、每组三元组内最大前缀能源+磨损费及字典序最小动作见证；空前缀、锚点
排除与无合法历史；以及全部安全后缀下三层目标与最坏见证。覆盖便宜电先充的贪心
失败、下降档价、终端储备、动作平局、撤去锚点使原指令不安全、前缀能源费最高与
需量最高来自不同历史、以及跨边界两段峰值改变最优首动作。上限规模（24 时段、
24 档价目表、12 容量、24 电网上限、8 条遥测）在时限内完成。
