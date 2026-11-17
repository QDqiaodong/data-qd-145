本目录为原创虚构合成数据，使用MIT许可。没有真实个人、线上实验、生产日志、交易、项目进度或搜索用户信息；不来自外部平台。

- `input.json`：v1 单期阶梯计价样例（A/B/C 三户，总额 1100）。
- `batch.json`：v2 账簿首批样例（含配置与一条完整读数链）。
- `storage.json`：storage-plan 样例（便宜先充与高峰放电、非空遥测前缀、
  需求费与磨损费），用于独立读取报告与联动验收。
- `aid.json`：aid-plan 联动验收样例（两地区、含零价平台与固定费的价目表，
  一户 target_relief 可在平台前/平台后/不可达之间调整）。
- `cash_ledger.json`：cash-reconcile 样例账簿（由本仓 ledger 接口实际 ingest
  三批并 close 三期产生；二月发票含 +250 正调整、三月发票同时含 -310 与 +250
  调整，用于核对冻结应收即 invoice.total）。
- `cash.json`：cash-reconcile 样例输入（三份冻结发票、两笔来款、两次 settle、
  一笔部分退票，覆盖撤销后重开欠款再核销的联动）。
