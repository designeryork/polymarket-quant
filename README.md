# Polymarket 量化交易引擎（10U 专享版）

> 从公开成交到无人值守：一套可 24 小时自动巡检的 Polymarket 量化策略引擎。
> 当前处于**纸盘（Paper Trading）验证阶段** —— 只模拟撮合，不动真钱。

[![Paper Trading](https://img.shields.io/badge/mode-paper--trading-34d399)]()
[![License](https://img.shields.io/badge/license-AGPL--3.0%20%2B%20non--commercial-blue)]()
[![Python](https://img.shields.io/badge/python-3.12%2B-3776ab)]()

---

## 这个项目在做什么

Polymarket 是一个预测市场平台，行情公开、撮合公开。绝大多数人看到的是"结果"，
少数人看到了"过程"——**每个市场的每一次成交都留下公开的脚印**。

本引擎做的事，就是把这些公开脚印变成可执行的信号：

```
公开成交数据 → Logit 空间特征工程 → 假说检验 → 风险门过滤 → 盘口真实深度撮合
```

四条硬规矩，贯穿全部代码：

1. **事实是代码算的，不是模型说的。** 任何结论都必须由脚本跑出来，可以复现。
2. **不用中间价幻觉。** 撮合严格按盘口真实买一/卖一与深度计算，中间价不存在。
3. **三元凯利 + 预授权风控信封。** 仓位由公式决定，风险门在开仓前就锁死上限。
4. **Fail-closed 优先。** 数据不可信、通道不通、对账不平 —— 一律拒绝下单。

## 核心设计

### 特征工程：Logit 空间

预测市场的价格 `p` 天然是有界概率，直接在价格空间算指标会被边界压扁。
本引擎统一在 Logit 空间 `z = ln(p / (1-p))` 里做统计：

| 指标 | 含义 |
|---|---|
| `ROC_logit` | Logit 空间的动量斜率 |
| `ATR_z` | 波动率标准化 |
| `Aroon` | 趋势新鲜度 |

### 仓位：四分之一凯利

二元事件的凯利最优仓位 `f* = (p - c) / (1 - c)`，其中 `c` 是成交成本。
为了对抗模型误差，引擎只用 **1/4 凯利**：`f = 0.25 * f*`。

### 风险门（Risk Gate）

每一笔开仓前必须通过预授权风控信封检查，任意一项不通过就跳过：

- 单笔金额上限 / 最小金额
- 最大同时持仓数
- 日亏损上限
- 资金上限
- 方向一致性校验

### 纸盘撮合

纸盘模式按**真实买一价**模拟成交，扣减真实盘口深度，不做理想化撮合。
所以纸盘净值天然低于实盘预期 —— 这是刻意的保守偏差。

## 架构

```
quant_engine.py        主引擎：扫描 → 特征 → 检验 → 风控 → 撮合 → 出面板
├── proxy_detector.py  网络通道探测（fail-closed：找不到可用通道就拒绝请求）
├── rate_limiter.py    限流感知的重试层（429 读 Retry-After 退避）
├── market_circuit.py  市场熔断
├── single_instance.py 单实例锁（防止两个引擎同时写一个账本）
├── order_ledger.py / hedge_ledger.py / reverse_ledger.py   各类账本
├── portfolio_reconciliation.py   账本对账
└── trading_error_monitor.py      交易异常监控

.github/workflows/     GitHub Actions 云端无人值守
sync_state.py          状态快照同步（单提交分支，仓库不膨胀）
```

## 云端无人值守

```
GitHub Actions（每 5 小时触发 + 自续跑）
    └── job 内部：quant_engine.py --loop --interval 60
          └── 每 60 秒一轮扫描 → 每轮更新账本与面板
                └── 结束时把状态推到 state-snapshot 分支（--force，恒 1 个提交）
                      └── 面板同步到 GitHub Pages
```

**为什么不是纯 cron？** GitHub 官方限制定时 workflow 最短间隔为 5 分钟，
`*/1 * * * *` 不会被调度。所以采用"长驻 job + 结束时自触发下一轮"的方式，
实现真正的**每分钟一轮**。

## 跟单纸盘

独立于主策略的第二套纸盘：盯 3 位交易员的公开持仓，每分钟轮询一次，
把他们**新开仓 / 加仓 / 减仓 / 清仓**按比例镜像到各自的 $10 子账户。

| 规则 | 口径 |
|---|---|
| 初始资金 | 每位交易员独立 $10 |
| 基线 | 零回填：上线时已有持仓只记录，不跟 |
| 首次开仓 | 用当时可用现金的 **50%** |
| 加仓 | 按对方加仓比例跟（维持"我方份额 / 对方份额"不变） |
| 部分减仓 | 同比例减仓 |
| 清仓 | 对方归零即卖出；市场已结算则按 1/0 结算 |
| 成交价 | 买按卖一、卖按买一（真实价差计入成本） |
| 执行硬约束 | 入场价差 >10%、出场 >15%、盘口深度不足 → 本轮跳过并记录事件 |

```
.github/workflows/copy_paper.yml   独立 workflow，每 5 小时一轮、内部 60s 轮询
copy_engine.py                     轮询 + 镜像逻辑 + 账本
copy_dashboard.py                  三栏页面（KPI / 净值走势 / 双方持仓）
copy_ledger.py                     copy-ledger 分支（只追加，永久留存供分析）
copy_runner.py                     云端常驻器：落账本 + 发布 copy.html
```

页面：<https://designeryork.github.io/polymarket-paper-dashboard/copy.html>

数据全部只读公开接口（Data API v2 / CLOB /book / Gamma），不会下任何真实订单。

## 本地运行

```bash
pip install -r requirements.txt

# 纸盘单轮
python quant_engine.py --paper

# 纸盘常驻（每 2 分钟一轮）
python quant_engine.py --loop --interval 120 --paper

# 打开监控面板
open dashboard_paper.html
```

### 环境变量

| 变量 | 默认 | 说明 |
|---|---|---|
| `PAPER` | `true` | `true` = 纸盘；`false` = 实盘（需同时满足 `ALLOW_LIVE=true`） |
| `ALLOW_LIVE` | `false` | 实盘总开关 |
| `ALLOW_DIRECT` | `false` | 允许显式直连（云端 Runner 无代理时需要） |
| `SCAN_LIMIT` | `60` | 每轮评估的市场数 |
| `ROUND_BUDGET_SECONDS` | `240` | 单轮请求时间预算 |
| `IDLE_CHECK_INTERVAL_SECONDS` | `60` | 空闲期补检持仓的间隔 |
| `MAX_CAPITAL_USDC` / `MAX_SINGLE_BET_USDC` / `MAX_OPEN_POSITIONS` / `DAILY_LOSS_LIMIT_USDC` | 见 `.env.example` | 风控上限 |

> ⚠️ **本仓库不包含任何私钥。** `.env` 已在 `.gitignore` 中。
> 真实交易请自行创建 `.env` 并妥善保管，切勿提交。

## 安全机制

| 机制 | 作用 |
|---|---|
| `STOP` 文件 | 紧急停机开关，引擎每轮开头检查，优先级最高 |
| 单实例锁 | 原子 PID 锁，防止两个引擎同时写一个账本 |
| Fail-closed | 通道不通 / 对账不平 / 数据缺失 → 拒绝下单 |
| 三重开关 | `PAPER` + `ALLOW_LIVE` + STOP 文件三者同时允许才可能下单 |
| 账本隔离 | 纸盘与实盘使用完全不同的账本文件 |
| 账本对账 | 每轮开仓前与链上/撮合端对账，不平则 fail-closed |

## 免责声明

本项目仅用于技术研究与教育目的，**不构成任何投资建议**。

预测市场交易具有极高风险，可能损失全部本金。作者不对使用本代码产生的
任何直接或间接损失承担责任。使用者需自行承担全部决策与合规责任，
并遵守所在司法辖区的相关法律法规。

## 许可协议

本项目采用 **AGPL-3.0 + 非商业附加条款** 双约束：

- **禁止商业用途** —— 任何形式的收费、盈利性部署、对客服务均需事先取得书面授权；
- **衍生作品必须同协议开源** —— 基于本项目的任何修改版本必须以相同协议公开完整源码；
- **网络服务同样触发开源义务** —— 改了代码又拿去做 SaaS，源码必须公开。

完整条款见 [LICENSE](LICENSE)（AGPL-3.0 正文）与
[LICENSE-ADDITIONAL-TERMS.md](LICENSE-ADDITIONAL-TERMS.md)（非商业附加条款）。

> 如需商业授权，或希望以其他协议（如 MIT / Apache-2.0）使用，请联系作者。

## 状态与免责声明

项目处于**试跑阶段**：

- 纸盘模拟结果不代表实盘收益，历史业绩不预示未来表现；
- 仓库开源仅供学习交流，任何人不得据此直接从事金融交易并据此索赔；
- 作者保留随时调整策略、暂停或终止项目及开源许可的权利。

## 致谢

感谢 Polymarket 公开 API 提供的透明数据。没有公开成交数据，这套方法不存在。
