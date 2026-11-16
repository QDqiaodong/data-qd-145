# data-qd-145 · 阶梯用量计费核算

Exact progressive tariff rating with itemized tier charges。业务输入、计算和当前边界详见 [数据契约](docs/contract.md)。

## 安装与演示

Python >=3.11，运行和测试仅用标准库；先确认 `python3 --version`。
每仓建立独立环境，构建工具锁版本。首次安装无公共缓存时可能联网；业务运行不访问网络。

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements-build.lock
.venv/bin/python -m pip install --no-build-isolation --no-deps .
.venv/bin/python -m pip check
.venv/bin/tariff-rating --source fixtures/input.json --output run/report.json
.venv/bin/python -m unittest discover -s tests -v
```

本地已有wheel时，安装构建工具可加 `--no-index --find-links <公共wheel缓存目录>`。
修改源码后重新安装。本仓不共享其他仓环境，不依赖Docker、外部账号或持续服务。

输入JSON最多8MB，拒绝重复键、非有限数值和未定义字段；源字节SHA256随结果保留。
输出是可独立读取的JSON；成功后原子替换，输入/计算失败退出2并保留旧报告。
输入和输出不得相同，输出符号链接拒绝。单操作者批处理，不承诺并发发布或断电恢复。
同输入重跑不累加。测试使用临时目录，运行产物在忽略的run/。

src/tariff_rating/model.py 为业务模型，相关算法模块独立组织；common.py 提供受限JSON及报告发布，
cli.py 衔接数据链路。fixtures 为明确自制样例，代码与样例采用MIT许可。

## 电表读数核验与月度发票账簿 (v2)

v1 单期 `--source/--output` 调用完全保留；新增基于显式 `--ledger` 文件的三个子命令：

```sh
tariff-rating ingest --batch fixtures/batch.json --ledger run/ledger.json
tariff-rating status --ledger run/ledger.json [--account A]
tariff-rating close  --ledger run/ledger.json --account A --period 2025-01
```

读数链模块 `readings.py` 负责修订链、区间核验与更正影响闭包（相邻区间与异常状态传播到
边界或恢复为止），不触碰发票；账期账簿 `periods.py` 消费该闭包重算开放期、冻结已关账
发票、核算差额分配并更新 held 状态；`ledger.py` 负责批次身份、提交代与原子持久化。
业务规则（锚点、半开区间 `(left,right]`、换表配对、超速严格不等式、stale/冲突修订、
迟到更正净额、暂配替换、pending 阻断、失败重放）详见 [账期账簿契约](docs/ledger.md)。

## 公共用量援助计划 (aid-plan)

新增 `aid-plan --source INPUT --output REPORT`：从独立 JSON 快照（每户已确认总用量、
地区保障与全局券预算）制定一轮用量券分配方案，不读写电表账簿。`aid.py` 负责每户
分段仿射救助曲线（固定费不减免、跨档真实增量价、O(档数) 而非逐单位）与地区保障
精确分配（券不可拆分、全局预算与地区最低保障、四级字典序目标精确最优）。
契约与资源代价说明见 [援助计划契约](docs/aid.md)。

```sh
tariff-rating aid-plan --source fixtures/aid.json --output run/aid-report.json
```


## 当前业务与样例
# Progressive usage rating v1

为公用量数据整理者把每账户一份计费期总用量与显式价目表转成可核算账单。currency为调用方币种标识，fixed_charge/rate为0..10^9整数最小货币单位，quantity为0..10^12整数用量。accounts最多20000且id唯一，非事件流不重复累计。
tiers为1..100个up_to/rate；累计上界严格递增、有限上界1..10^12，末项必须null无限且只能末项。阶梯计费为每段增量用量×该段rate，而不是达到档位后全部用量改价；不强制单价递增。每个账户即使0用量也收fixed_charge。输出逐段起止累计边界、用量、费率及费用，账户总额与全体总额整数精确。
样例A250=100×2+100×3+50×5+50=800，B0=50，C100=250，总额1100。不存在货币舍入、税率、优惠、跨期累计、缺失表计插补或真实账单发送；纯单期用量计价，不是支付结算对账。

