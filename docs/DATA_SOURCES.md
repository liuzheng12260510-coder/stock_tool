# StockSentry · 取数层接口对照表与推荐蓝图

> **文档版本**：v1.0 · 2026-05-21  
> **作者/维护**：参与数据层开发的工程师均可更新，每次变更须同步更新变更日志（§0.3）  
> **配套文档**：[ARCHITECTURE.md](./ARCHITECTURE.md) · [UPGRADE.md](./UPGRADE.md)

---

## 目录

- [§0 文档定位](#0-文档定位)
- [§1 现状速览：已接入接口清单](#1-现状速览已接入接口清单)
- [§2 数据质量诊断](#2-数据质量诊断)
- [§3 推荐接入清单](#3-推荐接入清单)
  - [P0 · 立即修复（止当前血）](#p0--立即修复止当前血)
  - [P1 · 数据深度增强（民企价值场景）](#p1--数据深度增强民企价值场景)
  - [P2 · 信号丰富备选](#p2--信号丰富备选)
- [§4 字段命名与单位约定](#4-字段命名与单位约定)
- [§5 接入路线图](#5-接入路线图)
- [§6 风险与积分预算](#6-风险与积分预算)
- [§7 附录](#7-附录)

---

## §0 文档定位

### 0.1 这份文档是什么

本文档是 StockSentry 取数层的 **"接口清单 + 推荐蓝图"**。它的作用是：

1. **防止重复造轮子**：新增取数需求时，先查本文档确认 Tushare / AkShare 是否有现成接口。
2. **管理技术债**：当前数据质量缺陷（§2）是有根因的，本文档精确记录原因与对应的修复接口。
3. **控制积分消耗**：Tushare Pro 按积分收费，每次新增接口前需在 §6 评估预算。

### 0.2 不是什么

- 本文档不是代码规范，代码规范见 `.clinerules`。
- 本文档不记录"应该怎么算"（因子/评分逻辑），那是 `docs/ARCHITECTURE.md` 的职责。
- 本文档不是 Tushare 官方文档的翻版，字段描述以官方文档为准，本文只记录"我们用什么、为什么用、怎么接"。

### 0.3 维护规则

- 新增任何 Tushare/AkShare 接口调用，**必须**同步更新 §1（已接入）或 §3（推荐）。
- 废弃/替换接口时，在对应行注明废弃日期和替换接口名，不直接删除行。
- 积分估算数字以官网最新为准，每季度检查一次并更新 §6。

---

## §1 现状速览：已接入接口清单

> 统计时间：2026-05-21  
> 调用位置：`app/data/tushare_provider.py`（主）+ `app/data/composite.py`（路由层）

### 1.1 Tushare Pro 接口（已接入）

| # | 接口名 | 调用函数 | 当前 fields | 调用频率 | Pipeline 步骤 | 熔断保护 | AkShare 兜底 | 备注 |
|---|--------|----------|-------------|----------|----------------|----------|--------------|------|
| 1 | `stock_basic` | `get_stock_basic()` | ts_code, symbol, name, area, industry, market, exchange, list_date, is_hs | 每日（Step 1，但 **`skip_if_done=True` 存在漏刷风险**） | `stock_basic` | ❌ | ✅（AkShare 版名称不带 ST 前缀，有隐患） | ⚠️ **见 §2 根因 1** |
| 2 | `stock_company` | `get_company_info_batch()` | ts_code, act_ent_type, act_name | 每日（Step 1 内） | `stock_basic` | ❌ | ❌ | 民企识别用，AkShare 无批量接口 |
| 3 | `trade_cal` | `get_trade_calendar()` `get_latest_trade_date()` | cal_date | 按需（交易日历查询） | 非 pipeline 步骤，由 `trade_calendar.py` 调用 | ❌ | ✅ | 每次查询 14–730 天范围 |
| 4 | `daily` | `get_daily(trade_date)` | ts_code, trade_date, open, high, low, close, pct_chg, vol, amount | 每日（Step 3） | `daily` | ✅ | ✅ | 全市场一次拉取 |
| 5 | `daily_basic` | `get_daily_basic(trade_date)` | ts_code, trade_date, pe_ttm, pb, dv_ttm, total_mv, circ_mv | 每日（Step 4） | `daily_basic` | ✅ | ❌（AkShare spot 无 dv_ttm）| ⚠️ **dv_ttm 已拉但导出层存在映射断链，见 §2 根因 3** |
| 6 | `fina_indicator_vip` | `get_financial_quarterly()` | 见 `_FINA_FIELDS`（18 字段，含杜邦/盈余质量/同比） | 每季度（分 4 个报告期，Steps fina_q1~q4） | `fina_q1` … `fina_q4` | ✅ | ❌ | VIP 接口，积分 ≥5000；失败降级到 `fina_indicator` |
| 7 | `fina_indicator` | `get_financial_quarterly()` (降级) `_loop_fina_indicator()` (逐只兜底) | 同上 `_FINA_FIELDS` | 降级时按需调用 | 同 Step fina_q1~q4 | ❌（降级路径不走熔断） | ❌ | 逐只串行需约 30 分钟/报告期 |
| 8 | `dividend` | `get_dividend_batch(end_date)` `get_dividend(ts_code)` | ts_code, ann_date, end_date, div_proc, stk_div, cash_div, cash_div_tax, base_date, pay_date, record_date, ex_date | 每年度（5 年 × 5 次，Step div_*）/ 单股按需 | `div_*` | ❌ | ❌ | ⚠️ **div_proc 未过滤"实施"状态，含预案数据，见 §3.P1.5** |

### 1.2 已接入字段的 `fina_indicator` 明细

当前 `_FINA_FIELDS` 常量包含以下 18 个字段（`app/data/tushare_provider.py`）：

| 字段 | 含义 | 主要用途 |
|------|------|---------|
| `ts_code` | 股票代码 | 关联键 |
| `ann_date` | 公告日期 | TTM 连续性检查 |
| `end_date` | 报告期 | TTM 季度定位 |
| `q_dtprofit` | 单季扣非净利润 | TTM 聚合 |
| `op_revenue` | 单季营业收入 | TTM 聚合 |
| `total_cur_assets` | 流动资产合计 | 流动比率分母 |
| `total_liab` | 总负债 | 负债率 |
| `roe` | 加权净资产收益率 | 盈利因子 |
| `npta` | 总资产净利润率 | 资产效率 |
| `debt_to_assets` | 资产负债率 | 安全维度 |
| `assets_turn` | 总资产周转率 | 杜邦分解 |
| `eqt_multiplier` | 权益乘数 | 杜邦分解 |
| `netprofit_yoy` | 净利润同比增速 | 成长维度 |
| `op_yoy` | 营收同比增速 | 成长维度 |
| `n_cashflow_act` | 经营活动现金流净额 | 盈余质量 |
| `ocf_to_profit` | 经营现金流/净利润 | 盈余质量 |
| `eps` | 基本每股收益 | 估值辅助 |
| `dt_eps` | 扣非每股收益 | 估值辅助 |

**⚠️ 缺口**：毛利率、净利润率、ROA、ROIC、流动比率、速动比率、FCFF 等均未接入，见 §3.P1.3。

---

## §2 数据质量诊断

> 本节基于 `StockSentry_2026-05-19.xlsx` 的 205 只筛选结果进行分析。

| # | 表现 | 根因 | 对应修复接口 / 操作 |
|---|------|------|---------------------|
| 1 | \*ST 明德排全市场第 1；ST文峰、退市观典等 30+ 只 ST/退市股进入结果 | `pipeline.py` Step 1 的 `skip_if_done=True` 导致 `stock_basic.name` 不刷新；一旦某股后来被 ST 化，名称不会更新，黑名单正则匹配失效。此外退市股 `list_status="D"` 完全未拉入 stocks 表，无法被匹配 | P0-1：`namechange` 接口 + P0-2：`stock_basic` 扩 `list_status=L,D,P` + Step 1 强制日刷新 name |
| 2 | `行业 = nan`（退市观典等） | `list_status="D"` 股票从未入库，industry 字段空；行业为 nan 的股票应直接剔除 | P0-2 修复入库 + P1-4：`bak_basic` 行业兜底 |
| 3 | 股息率 % 整列为空（205 只全空） | `daily_basic.dv_ttm` 字段已拉取，但 `factors.py` → `vectorized.py` → `export.py` 的字段重命名链中存在断链（字段别名不一致），导致 `股息率%` 列输出为空 | 代码排查（非 Tushare 接口问题）；同时参考 P1-5 确保 `div_proc='实施'` 过滤 |
| 4 | 约 60 只股票同分 65.6 / 88.5%；另 30 只同分 73.8 / 95.9% | `scoring.py` 对缺失维度（无 PE / 无股息率）统一填同一默认值参与 z-score，导致大批"只有 PB 有值"的股票输出相同分数 | 代码修改（纯算法问题，非 Tushare 缺口）：缺失维度该股不参与该维度排名，权重按有效维度数归一 |
| 5 | 涨停板股（+20% 尚品宅配）、接近涨停（+9.97% 大亚圣象）混入结果 | `filters.py` 未设涨跌幅阈值过滤；涨跌停识别依赖 pct_chg 阈值不准（各板块涨跌幅限制不同） | P0-4：`limit_list_d` 接口精确识别涨跌停 |
| 6 | 多只股票涨跌幅 % 为空（000717、002030、000517 等） | 停牌股当日无成交，`daily` 接口返回 pct_chg / amount 均为 NULL，但未在筛选前剔除 | P0-3：`suspend_d` 停牌识别接口 |
| 7 | 总市值最小 1.7 亿（退市观典）、6.8 亿（\*ST 清越）进入结果 | `filters.py` 未设市值下限 | 代码修改：在 `settings` 中添加 `min_total_mv_yi`（默认 30 亿，可配置） |

---

## §3 推荐接入清单

> **格式说明**：每项接口均按统一模板描述。积分门槛参考 Tushare 官网，以官网最新数据为准。  
> **优先级图例**：⭐⭐⭐ = 修当前 Bug / ⭐⭐ = 强化核心场景 / ⭐ = 备选增强

---

### P0 · 立即修复（止当前血）

---

#### P0-1　`namechange`　⭐⭐⭐

| 项 | 内容 |
|----|------|
| Tushare 路径 | `pro.namechange` |
| 官方文档 | https://tushare.pro/document/2?doc_id=100 |
| 关键字段 | `ts_code`, `name`（新名称）, `start_date`, `end_date`, `change_reason`, `ann_date` |
| 调用频率 | 每日 1 次，拉取最近 30 天的名称变更记录（增量） |
| 积分门槛 | 2000（以官网为准） |
| 解决问题 | 对应 §2 根因 1：ST/\*ST/退市股因 `stock_basic.name` 不刷新而漏检 |

**接入位置**

| 文件 | 改动 |
|------|------|
| `app/data/tushare_provider.py` | 新增 `get_name_changes(start_date: str, end_date: str) -> pd.DataFrame`；拉取字段：`ts_code, name, start_date, end_date, change_reason, ann_date` |
| `app/data/composite.py` | 注册 `get_name_changes`，标记 AkShare **无兜底**（返回空 DataFrame，降级时 log.warning） |
| `app/db/models.py` | 新增 `NameChange` 表：`ts_code(String 10) / name(String 20) / effective_date(String 8) / change_reason(String 64)` + 唯一索引 `(ts_code, effective_date)` |
| `app/jobs/pipeline.py` | Step 1.5（在 `stock_basic` 与 `blacklist` 之间）：调用 `get_name_changes`，upsert `NameChange` 表，然后对最新名称含 ST/退市的 ts_code **立即更新 stock_basic.name 并触发黑名单重写**；Step 1.5 **不走 `skip_if_done`**，每日强制跑 |
| `app/analytics/blacklist.py` | `build_blacklist_from_df` 无需改动；确保传入的 `stock_df.name` 字段使用 `NameChange` 覆盖后的最新名称 |

**降级路径**

- Tushare 失败：log.warning，fallback 到当日 `stock_basic` 的 name 字段（过期名，但不崩溃）。
- AkShare 无此接口，无需 fallback 适配。

**风险与注意事项**

- `change_reason` 字段编码不统一（"证监会审核通过"/"实施"/"申请注册"等），不要依赖此字段来判断是否 ST；ST 识别依赖 **name 字段本身**，不依赖 `change_reason`。
- Tushare 名称变更接口返回的是**历史全量**（不支持增量），每次请求需指定 `start_date / end_date` 控制范围，建议拉近 30 天滚动窗口。
- `end_date` 为空表示"当前仍使用此名"，逻辑上等同于"name 仍有效"，处理时需注意 NULL 判断。

---

#### P0-2　`stock_basic` 扩展 `list_status`　⭐⭐⭐

| 项 | 内容 |
|----|------|
| Tushare 路径 | `pro.stock_basic`（已接入；扩展参数） |
| 官方文档 | https://tushare.pro/document/2?doc_id=25 |
| 新增字段 | `list_status`（L=上市 / D=退市 / P=暂停上市） |
| 改动类型 | 修改已有接口参数，不新增 API 调用 |
| 解决问题 | 对应 §2 根因 1，2：退市股 `list_status="D"` 从未入库，无法被黑名单匹配 |

**接入位置**

| 文件 | 改动 |
|------|------|
| `app/data/tushare_provider.py` | `get_stock_basic()` 的 `list_status` 参数从 `"L"` 改为 `"L,D,P"`；`fields` 追加 `list_status` |
| `app/db/models.py` | `Stock` 表新增 `list_status: Mapped[str]`（String 1，含索引）；已有 `delist_date` 字段可延用 |
| `app/jobs/pipeline.py` | `_update_stock_basic`：upsert 时同步写入 `list_status`；`_update_blacklist` 中增加规则：`list_status in ('D', 'P')` → 黑名单原因 `"delist"` / `"suspend"`，永久有效 |
| `app/analytics/blacklist.py` | `detect_blacklist_reason` 增加参数 `list_status: str`；当值不为 `"L"` 时直接返回 `("delist_or_suspend", None)` |

**降级路径**

- AkShare `get_stock_basic` 也有列表状态字段，但编码与 Tushare 不同；在 composite.py 的 `get_stock_basic` 兜底路径中，对 AkShare 返回值做字段映射后补充 list_status 字段（默认 `"L"`，因为 AkShare 通常只返回上市状态）。
- 降级场景将丢失 D/P 状态识别，已有 `namechange` 作为第二道防线。

**风险与注意事项**

- 一次拉 L+D+P 约返回 6000+ 条（相比原来的约 5300 条），upsert 逻辑需幂等，不会重复插入。
- P（暂停上市）的股票不应出现在评分排名中，但不应永久删除，与 D（退市）的黑名单 `expires_at` 策略区分：D 永久黑名单，P 黑名单跟踪 `list_status` 变化（恢复上市时需清除）。
- Step 1 的 `_update_stock_basic` 必须改为**强制日刷新**（去掉或配置化 `skip_if_done`），否则新 ST 化股票的 `list_status` 更新仍会被跳过。

---

#### P0-3　`suspend_d`　⭐⭐⭐

| 项 | 内容 |
|----|------|
| Tushare 路径 | `pro.suspend_d` |
| 官方文档 | https://tushare.pro/document/2?doc_id=214 |
| 关键字段 | `ts_code`, `trade_date`, `suspend_timing`（停牌时点：S=开盘前/E=收盘后等）, `suspend_type`（停牌原因类型） |
| 调用频率 | 每日 1 次（Step daily 之后，当日停牌情况） |
| 积分门槛 | 2000（以官网为准） |
| 解决问题 | 对应 §2 根因 6：停牌股 pct_chg/amount 为空，静默混入筛选结果 |

**接入位置**

| 文件 | 改动 |
|------|------|
| `app/data/tushare_provider.py` | 新增 `get_suspend(trade_date: str) -> pd.DataFrame` |
| `app/data/composite.py` | 注册 `get_suspend`；AkShare **有兜底**（`ak.stock_zh_a_stop_em()` 可获取停牌列表，字段映射需适配） |
| `app/jobs/pipeline.py` | Step 3.5（daily 之后，prescreen 之前）：拉取当日停牌列表，将停牌 ts_code 存入临时集合，在 Step 4（prescreen/过滤）时用 `filters.py` 剔除 |
| `app/analytics/filters.py` | 增加 `filter_suspended(df, suspended_codes: set[str]) -> pd.DataFrame`，剔除停牌股 |

**降级路径**

- AkShare `ak.stock_zh_a_stop_em()` 可返回当日停牌/复牌列表，字段不同但可映射。
- 双路均失败时：fallback 到"pct_chg 为空 AND amount 为空 → 视为停牌"的规则兜底，log.warning 记录。

**风险与注意事项**

- `suspend_timing` 字段含义：S=全天停牌，E=午后停牌，其他值含义参考官方文档。
- 停牌不等于退市，停牌股在复牌后应重新进入评估范围，不入黑名单表（临时排除即可）。
- 当日行情里已无数据的股票，`pct_chg` 和 `amount` 均为空，可作为兜底识别依据（无需依赖 `suspend_d`），但 `suspend_d` 可区分"全天停牌"和"午后停牌"等更细粒度情况。

---

#### P0-4　`limit_list_d`　⭐⭐⭐

| 项 | 内容 |
|----|------|
| Tushare 路径 | `pro.limit_list_d` |
| 官方文档 | https://tushare.pro/document/2?doc_id=198 |
| 关键字段 | `ts_code`, `trade_date`, `limit_type`（U=涨停/D=跌停/Z=炸板）, `close`, `pct_chg`, `fd_amount`（封单金额）, `first_time`（首次封板时间）, `last_time`（最后封板时间）, `open_times`（开板次数）, `strth`（封板强度） |
| 调用频率 | 每日 1 次，获取当日涨跌停股票列表 |
| 积分门槛 | 5000（以官网为准，属于中高权限接口） |
| 解决问题 | 对应 §2 根因 5：精确识别当日涨/跌停股，各板块涨跌幅限制不同（主板 ±10%、创业板科创板 ±20%、ST ±5%），不能用固定 pct_chg 阈值 |

**接入位置**

| 文件 | 改动 |
|------|------|
| `app/data/tushare_provider.py` | 新增 `get_limit_list(trade_date: str) -> pd.DataFrame`；fields 包含上表关键字段 |
| `app/data/composite.py` | 注册 `get_limit_list`；AkShare **有兜底**（`ak.stock_limit_up_em()` + `ak.stock_limit_down_em()`，需分两次拉取后合并） |
| `app/jobs/pipeline.py` | Step 3.5（与 suspend_d 同步获取）：拉取涨跌停列表，传给 filters |
| `app/analytics/filters.py` | 新增 `filter_limit_stocks(df, limit_codes: set[str]) -> pd.DataFrame`；规则：`limit_type` 为 `U`（涨停）或 `D`（跌停）的股票剔除；炸板 `Z` 可保留（已有真实成交） |
| `app/core/config.py` | 新增 `filter_limit_stocks: bool = True`（可配置，默认开启） |

**降级路径**

- AkShare `ak.stock_limit_up_em()` / `ak.stock_limit_down_em()` 可拉当日涨跌停，但缺少 `limit_type` 细分，拼合逻辑由 composite.py 处理。
- 双路失败时：fallback 到 `abs(pct_chg) >= 9.5` 的粗粒度过滤（误伤率高，作为最后一道防线），并 log.warning。

**风险与注意事项**

- `limit_list_d` 是中高积分接口（约 5000 积分），使用前确认账户积分充足。
- 若积分不足，优先用 AkShare 兜底，延迟约 10–30 秒。
- **为什么不用 `pct_chg >= 9.5%` 做涨跌停判断？** A股各板块涨跌幅限制不同：主板 ±10%、创业板/科创板 ±20%、ST 股 ±5%；固定阈值会同时漏掉 20% 涨停的注册制股和误伤没有涨停但 pct_chg 接近 10% 的股票。`limit_list_d` 基于交易所数据，结果精确。

---

### P1 · 数据深度增强（民企价值场景）

---

#### P1-1　`adj_factor`　⭐⭐

| 项 | 内容 |
|----|------|
| Tushare 路径 | `pro.adj_factor` |
| 官方文档 | https://tushare.pro/document/2?doc_id=28 |
| 关键字段 | `ts_code`, `trade_date`, `adj_factor`（前复权因子） |
| 调用频率 | 每日（按需，针对需要计算历史波动率的股票，不需要全市场批量） |
| 积分门槛 | 2000（以官网为准） |
| 解决问题 | 当前 `daily.pct_chg` 是不复权日涨跌幅，用于计算历史波动率、区间收益时存在除权偏差 |

**接入位置**

| 文件 | 改动 |
|------|------|
| `app/data/tushare_provider.py` | 新增 `get_adj_factor(ts_code: str, start_date: str, end_date: str) -> pd.DataFrame` |
| `app/analytics/factors.py` 或 `app/analytics/vectorized.py` | 在计算历史波动率（σ）、区间收益时，先用 `adj_factor` 对 `close` 做前复权，再计算 returns |
| pipeline 层 | 此接口为按需调用（不批量），不新增 Pipeline Step；在调用 volatility 因子时引用 |

**降级路径**

- Tushare 失败时：直接用原始 close 计算，log.warning 记录"未复权，波动率存在偏差"。
- AkShare 无完全等价的批量复权因子接口（`ak.stock_zh_a_hist` 含复权参数，但字段逻辑不同）。

**风险与注意事项**

- 只在**历史区间计算**时需要复权，当日横截面的 PE_TTM / PB 估值数据来自 `daily_basic`，已经是市值除权后的实时值，不需要额外乘以复权因子。
- 前复权因子会随时间推移调整（每次除权后全历史倒推），需注意缓存失效：落库时加 `updated_at` 字段，每次除权日后对应股票的因子数据需全量刷新。

---

#### P1-2　`pledge_stat`　⭐⭐

| 项 | 内容 |
|----|------|
| Tushare 路径 | `pro.pledge_stat` |
| 官方文档 | https://tushare.pro/document/2?doc_id=208 |
| 关键字段 | `ts_code`, `end_date`, `pledge_count`（质押次数）, `unrest_pledge`（无限售股质押数）, `rest_pledge`（限售股质押数）, `total_share`（总股本）, `pledge_ratio`（质押比例 %） |
| 调用频率 | 每季度（财报披露后更新，约 4 次/年） |
| 积分门槛 | 2000（以官网为准） |
| 解决问题 | 民企控股股东高质押是典型暴雷先兆，当前评分体系完全未覆盖此风险维度 |

**接入位置**

| 文件 | 改动 |
|------|------|
| `app/data/tushare_provider.py` | 新增 `get_pledge_stat_batch(end_date: str) -> pd.DataFrame`；全市场一次拉，不逐票 |
| `app/db/models.py` | 新增 `PledgeStat` 表：`ts_code / end_date / pledge_ratio / pledge_count`；同时在 `Stock` 表懒加载 `latest_pledge_ratio` 冗余列加速筛选 |
| `app/jobs/pipeline.py` | 在 fina 系列步骤后新增 Step `pledge`，每季度强制刷新；日常跑批跳过（仅在季度末跑批时触发） |
| `app/analytics/scoring.py` | 在安全维度（Safety Score）中引入 `pledge_ratio`：`pledge_ratio > 50%` 大幅减分；`pledge_ratio > 70%` 进风险提示列表（不强制剔除，以保留用户判断权） |
| `app/services/export.py` | Excel 输出新增"控股股东质押率 %"列 |

**降级路径**

- AkShare 无全市场质押统计批量接口；降级时对应字段输出空，安全评分该维度不参与计算（与 §2 根因 4 的 z-score 缺失处理一致）。

**风险与注意事项**

- 质押数据更新有延迟（公告日到 Tushare 入库约有 1–3 个交易日延迟），需在 score 展示层注明"数据截止 end_date"。
- `pledge_ratio > 60%` 在 A 股民企实际上是非常高风险，但不少优质公司也存在一定质押，建议在评分中做**连续值扣分**而非阈值剔除。

---

#### P1-3　`fina_indicator` 字段扩充　⭐⭐

| 项 | 内容 |
|----|------|
| Tushare 路径 | `pro.fina_indicator` / `pro.fina_indicator_vip`（已接入，扩字段） |
| 官方文档 | https://tushare.pro/document/2?doc_id=79 |
| 新增字段 | 见下方字段表 |
| 改动类型 | 修改已有接口的 `_FINA_FIELDS` 常量，不新增 API 调用 |
| 解决问题 | 当前 5 维评分中安全/盈利/现金流维度因子不足，导致同类型股票区分度低 |

**建议新增的 10 个字段**

| 字段名 | 含义 | 主要用途 | 维度 |
|--------|------|---------|------|
| `grossprofit_margin` | 销售毛利率（%） | 定价能力，高护城河企业标志 | 价值 |
| `netprofit_margin` | 销售净利润率（%） | 盈利效率 | 价值 |
| `roa` | 总资产报酬率（%） | 资产回报 | 价值 |
| `update_flag` | 更新标志 | 过滤补充公告（只取"1"正式报） | 数据质量 |
| `current_ratio` | 流动比率 | 短期偿债能力 | 安全 |
| `quick_ratio` | 速动比率 | 短期偿债能力（去库存） | 安全 |
| `debt_to_eqt` | 产权比率（负债/股东权益） | 资本结构风险 | 安全 |
| `ocfps` | 每股经营活动净现金流（元） | 现金流质量，配合 EPS 交叉验证 | 稳定 |
| `q_roe` | 单季度 ROE 环比 | 识别 ROE 趋势（上升 vs 见顶回落） | 成长 |
| `fcff` | 企业自由现金流（万元） | 内在价值估算核心投入变量 | 价值 |

**接入位置**

| 文件 | 改动 |
|------|------|
| `app/data/tushare_provider.py` | `_FINA_FIELDS` 常量追加上表 10 个字段（注意逗号分隔的字符串格式） |
| `app/db/models.py` | `FinancialQuarter` 表新增 10 个 Mapped 字段（Float，nullable=True） |
| `app/analytics/factors.py` | 使用新字段丰富各维度因子计算：毛利率进"价值"、流动比率/速动比率进"安全"、q_roe 进"成长" |
| `scripts/init_db.py` | 运行 `alembic upgrade head` 或重建 schema（视迁移策略） |

**降级路径**

- 新字段全部在 `fina_indicator` 接口内（不新增接口），积分消耗不增加。
- 若某字段 Tushare 返回 NULL（部分公司不披露），在 `scoring.py` 中视为缺失，用 §2 根因 4 的修复方案处理（权重重分配）。

**风险与注意事项**

- `update_flag` 字段非常重要：Tushare 财务数据含"补充公告"版本（update_flag=2/3），每个 (ts_code, end_date) 组合应只保留 `update_flag=1`（首次发布）以保证时序一致性，或取最新 flag（各有利弊，需统一策略并写入 `ttm.py` 的注释）。
- `fcff` 部分公司可能无该字段（如金融企业不适用），需 nullable。

---

#### P1-4　`bak_basic`　⭐⭐

| 项 | 内容 |
|----|------|
| Tushare 路径 | `pro.bak_basic` |
| 官方文档 | https://tushare.pro/document/2?doc_id=262 |
| 关键字段 | `ts_code`, `trade_date`, `name`, `industry`（中信一级行业）, `area`（省份）, `pe`, `float_share`, `total_share`, `total_assets`, `liquid_assets`, `fixed_assets`, `reserved`, `reserved_pershare`, `eps`, `bvps`, `pb`, `list_date`, `undp`, `per_undp`, `rev_yoy`, `profit_yoy`, `gpr`, `npr`, `holder_num` |
| 调用频率 | 每日 1 次（行情截面，全市场一次） |
| 积分门槛 | 5000（以官网为准） |
| 解决问题 | 对应 §2 根因 2：行业字段为 nan 的兜底；同时可获取 `holder_num`（股东户数）、`rev_yoy`（营收增速）等补充字段 |

**接入位置**

| 文件 | 改动 |
|------|------|
| `app/data/tushare_provider.py` | 新增 `get_bak_basic(trade_date: str) -> pd.DataFrame`，fields 选取 `ts_code, name, industry, area, holder_num, rev_yoy, profit_yoy` |
| `app/jobs/pipeline.py` | Step 4.5（prescreen 之后）：对 `industry` 为 nan 的股票用 `bak_basic.industry` 回填行业字段 |
| composite.py | 注册但无 AkShare 兜底 |

**降级路径**

- `bak_basic` 失败时：行业 nan 的股票在 Excel 中标注"行业待确认"输出，不直接剔除（避免漏掉真实民企）。

**风险与注意事项**

- `bak_basic` 是快照接口，当日可能比 `stock_basic` 更新更及时，但字段不如 `daily_basic` 丰富，**不能** 替代估值数据（PE / PB 来自 `daily_basic`）。
- `holder_num` 可作为流动性风险辅助指标，考虑与 `stk_holdernumber`（P2 级）在功能上有重叠，先接 `bak_basic.holder_num` 即可满足基本需求。

---

#### P1-5　`dividend` 过滤优化（`div_proc` 等于实施）　⭐⭐

| 项 | 内容 |
|----|------|
| Tushare 路径 | `pro.dividend`（已接入，优化过滤逻辑） |
| 改动类型 | 修改数据处理逻辑，不新增 API 调用 |
| 关键字段 | `div_proc`（分红进度：预案/股东大会通过/实施等） |
| 解决问题 | 当前 `cash_div` 混入"预案"状态数据，导致股息率高估 |

**接入位置**

| 文件 | 改动 |
|------|------|
| `app/analytics/dividend_quality.py` | 在计算股息率时过滤 `div_proc == '实施'`（或含"实施"的字符串，注意 Tushare 编码可能含空格）；若过滤后该年无分红记录，股息率计为 0 而非 NULL |
| 导出层 | "实施过滤"逻辑写入注释，避免下次误删 |

**降级路径**

- 直接修改已有逻辑，无需降级路径。

**风险与注意事项**

- `div_proc` 字段编码因上市公司而异，常见值为：`"预案"` / `"股东大会通过"` / `"实施"` / `"董事会预案"`。
- 建议用包含匹配 `"实施" in div_proc` 而非精确相等，防止编码变体（如"已实施"）漏检。
- 股票本年未实施分红（股息率= 0）与股票数据缺失（股息率= NULL）在 scoring.py 中处理方式不同：前者正常参与 z-score，后者触发缺失值重分配。

---

### P2 · 信号丰富备选

---

#### P2-1　`stk_holdernumber`　⭐

| 项 | 内容 |
|----|------|
| Tushare 路径 | `pro.stk_holdernumber` |
| 官方文档 | https://tushare.pro/document/2?doc_id=166 |
| 关键字段 | `ts_code`, `ann_date`, `end_date`, `holder_num`（股东户数）, `holder_num_ratio`（较上期增减比例 %） |
| 调用频率 | 每季度（随季报披露） |
| 积分门槛 | 2000（以官网为准） |
| 使用场景 | 股东户数**减少**通常意味着筹码集中（机构建仓 or 主力吸收，看多信号）；持续增加则散户化，警惕风险 |
| 注意 | `bak_basic.holder_num` 已有此字段日频版本，P1-4 接入后可先用该字段评估效果，再决定是否额外接 `stk_holdernumber` |

---

#### P2-2　`hsgt_top10` / `hk_hold`　⭐

| 项 | 内容 |
|----|------|
| Tushare 路径 | `pro.hsgt_top10` / `pro.hk_hold` |
| 官方文档 | doc_id=47 / doc_id=59 |
| 关键字段 | `hsgt_top10`：ts_code, trade_date, close, change（净买卖金额）, rank；`hk_hold`：ts_code, trade_date, vol, ratio（沪深港通持股占A股比例 %） |
| 调用频率 | 每日 |
| 积分门槛 | 5000/4000（以官网为准） |
| 使用场景 | 外资持股比例上升作为"价值认可"信号；受外资政策影响大，建议仅作辅助参考 |

---

#### P2-3　`disclosure_date`　⭐

| 项 | 内容 |
|----|------|
| Tushare 路径 | `pro.disclosure_date` |
| 官方文档 | https://tushare.pro/document/2?doc_id=162 |
| 关键字段 | `ts_code`, `ann_date`, `end_date`, `pre_date`（预披露日期）, `actual_date`（实际披露日期）, `modify_date` |
| 调用频率 | 每季度或按需 |
| 使用场景 | 确认某季报是否已实际披露，避免在 TTM 计算中使用"未来季度"数据（在测试数据或模拟跑批中易发生） |

---

#### P2-4　`stk_factor_pro`　⭐

| 项 | 内容 |
|----|------|
| Tushare 路径 | `pro.stk_factor_pro` |
| 官方文档 | https://tushare.pro/document/2?doc_id=267 |
| 关键字段 | 包含真复权 EPS_TTM、BVPS、各类估值、动量因子等 |
| 积分门槛 | 5000+（高权限接口）|
| 使用场景 | 可作为对账参照，验证自计算 TTM 因子的准确性；若积分充足可直接替代部分自计算逻辑 |
| 注意 | 接入前需确认字段定义与项目内部 `ttm.py` 计算口径一致，避免双口径并存 |

---

#### P2-5　`broker_recommend`　⭐

| 项 | 内容 |
|----|------|
| Tushare 路径 | `pro.broker_recommend` |
| 官方文档 | https://tushare.pro/document/2?doc_id=96 |
| 关键字段 | `ts_code`, `report_date`, `rating`（买入/增持/中性/减持/卖出）, `rating_change`（评级变化方向） |
| 积分门槛 | 5000（以官网为准） |
| 使用场景 | 券商一致预期作为外部信号补充；注意：此类数据存在明显滞后和利益冲突，仅作"市场观点参考"，不纳入核心评分 |

---

## §4 字段命名与单位约定

> 以下约定与 `.clinerules` 保持完全一致，在此固化为数据层的参考表。

### 4.1 日期字段

| 系统层级 | 格式 | 类型 | 示例 |
|----------|------|------|------|
| Tushare 接口传参/接收 | `YYYYMMDD` 字符串 | `str` | `"20260519"` |
| Python 内部运算 | 标准日期对象 | `datetime.date` | `date(2026, 5, 19)` |
| 数据库存储 | 8 位字符串 | `String(8)` | `"20260519"` |
| API 响应（JSON）| ISO 8601 | `str` | `"2026-05-19"` |
| Excel 导出 | 可读字符串 | `str` | `"2026-05-19"` |

### 4.2 金额与数量单位

| 字段类型 | 单位 | 说明 |
|----------|------|------|
| Tushare 财务数据（利润/资产/负债） | **万元** | 原始字段，不做换算直接存库 |
| 股价（close / open / high / low） | **元/股** | |
| 总市值 / 流通市值（`total_mv / circ_mv`） | **万元**（Tushare 原始） | Excel 导出时转换为**亿元** |
| 分红（`cash_div`） | **元/股**（税后） | |
| 成交量（`vol`） | **手**（100 股/手） | |
| 成交额（`amount`） | **千元** | Tushare 原始；内部可换算为万元 |
| 换手率（`turnover_rate`） | **%** | |
| 各类比率（PS / PE / PB / ROE 等） | **倍 / %**（按字段文档） | |

### 4.3 关键对齐

- **TTM 计算**：所有 TTM 值必须通过 `app/analytics/ttm.py` 输出，禁止在 factors / scoring / export 层自行计算，原因见 `.clinerules` §5。
- **复权**：日行情的 `pct_chg` 字段是不复权涨跌幅；用于计算**历史波动率、区间收益**时必须接 `adj_factor` 做前复权。
- **PE/PB**：来自 `daily_basic.pe_ttm / pb`，是行情数据（实时）；不要与 `fina_indicator.roe`（财务数据）混用时间截面。
- **股息率**：优先用 `daily_basic.dv_ttm`（Tushare 官方 TTM 口径）；二次验证用自计算 `dividend` 历史 5 年实施分红除以当前市值，两者差异 > 2% 时输出 warning 日志。

---

## §5 接入路线图

### Sprint 1 — 修当前 Bug（对应 P0）

**目标**：让 `StockSentry_2026-05-19.xlsx` 中暴露的 4 个核心问题不再复现。

| 任务 | 接口/改动 | 预估工作量 | 验收标准 |
|------|-----------|------------|---------|
| S1-T1 | `namechange` 接入 + pipeline Step 1.5 | 1 天 | 新增测试：手动注入一只名称从"正常"变为"\*ST"的 mock 数据，黑名单在下次跑批后正确剔除 |
| S1-T2 | `stock_basic list_status=L,D,P` + Step 1 强制日刷新 | 0.5 天 | 测试：退市观典（688287.SH）不再出现在结果表 |
| S1-T3 | `suspend_d` 接入 + filters 停牌剔除 | 0.5 天 | 测试：当日 pct_chg/amount 全为空的股票被剔除，且日志输出"suspend_filtered"字样 |
| S1-T4 | `limit_list_d` 接入 + filters 涨跌停剔除 | 0.5 天 | 测试：尚品宅配（当日+20%）不进结果；300616 当日涨停，被剔除并在日志输出原因 |
| S1-T5 | `scoring.py` 缺失维度权重重分配 | 1 天 | 测试：65 只"只有 PB 值、其余缺失"的股票不再同分；分数标准差 > 5 |
| S1-T6 | `dv_ttm` 字段链路排查修复 | 0.5 天 | Excel `股息率%` 列不再为全空 |

**Sprint 1 完成后预期**：结果表不再含 ST/退市/停牌/涨停股；评分同分聚集问题消失；股息率列正常输出。

---

### Sprint 2 — 数据深度（对应 P1）

**目标**：让民企价值筛选的 5 维评分真正有充足的因子支撑，并引入质押风险维度。

| 任务 | 接口/改动 | 预估工作量 | 验收标准 |
|------|-----------|------------|---------|
| S2-T1 | `pledge_stat` 接入 + 安全评分 | 1.5 天 | 控股股东质押率 > 50% 的股票在安全维度得分显著低于质押率 < 10% 的同类股票 |
| S2-T2 | `adj_factor` 接入 + 波动率修正 | 1 天 | `tests/test_vectorized.py` 中新增复权/未复权波动率对比 case，验证差异 |
| S2-T3 | `fina_indicator` 扩 10 字段 | 1 天 | 新增字段落库；`tests/test_analytics_service.py` 新增毛利率/流动比率断言 |
| S2-T4 | `bak_basic` 行业兜底 | 0.5 天 | Excel 中行业列不再出现 nan（对退市/停牌剔除后的在库股票） |
| S2-T5 | `dividend.div_proc` 过滤 | 0.5 天 | 测试：预案分红被排除，同一股票股息率低于修改前 |

---

### Sprint 3 — 信号丰富（对应 P2）

**目标**：补充增量信息密度，让 AI 深度解读有更多维度可以"讲故事"。

| 任务 | 接口/改动 | 备注 |
|------|-----------|------|
| S3-T1 | `stk_holdernumber` 季度股东户数 | 评估 P1-4 `bak_basic.holder_num` 效果后决定是否接 |
| S3-T2 | `hsgt_top10 / hk_hold` 北向资金 | 建议先评估信号有效性（回测）再上线 |
| S3-T3 | `disclosure_date` 披露日管理 | 用于修复测试用"未来财报数据"问题 |
| S3-T4 | Excel 多 Sheet（筛选结果/5 维分项/剔除日志/元信息） | 纯 export.py 改动，无新接口 |
| S3-T5 | broker_recommend 券商评级 | 低优先级，仅作运营参考 |

---

## §6 风险与积分预算

### 6.1 Tushare 积分门槛参考

> ⚠️ 以下数据为参考值，以 [Tushare 官网积分政策](https://tushare.pro/register?reg=1) 最新公告为准。

| 接口 | 估计积分门槛 | 调用次数/天 | 积分消耗/天（估算） |
|------|--------------|-------------|---------------------|
| `stock_basic` | 120 | 1 | 低 |
| `stock_company` | 2000 | 1 | 低 |
| `daily` | 120 | 1 | 低 |
| `daily_basic` | 120 | 1 | 低 |
| `fina_indicator_vip` | 5000 | 4/季（约 0.07/天）| 中（季度集中） |
| `dividend` | 2000 | 5/季（约 0.08/天）| 低（季度集中） |
| **`namechange`**（P0-1 新增）| 2000 | 1 | 低 |
| **`suspend_d`**（P0-3 新增）| 2000 | 1 | 低 |
| **`limit_list_d`**（P0-4 新增）| 5000 | 1 | **中**（门槛较高）|
| **`adj_factor`**（P1-1 新增）| 2000 | 按需（约 20–50 只/天）| 低 |
| **`pledge_stat`**（P1-2 新增）| 2000 | 4/季（约 0.07/天）| 低（季度集中）|
| **`bak_basic`**（P1-4 新增）| 5000 | 1 | **中**（门槛较高）|
| **`stk_holdernumber`**（P2-1）| 2000 | 4/季 | 低 |
| **`hsgt_top10`**（P2-2）| 5000 | 1 | 中 |

### 6.2 积分预算建议

- **当前已用接口所需门槛**：约 5000 积分（`fina_indicator_vip` 要求最高）。
- **Sprint 1 完成后最低门槛**：`limit_list_d` 需要 5000 积分，与现状持平。
- **Sprint 2 完成后最低门槛**：`bak_basic` 也需要 5000，仍与现状持平。
- **综合建议**：账户保持 **5000 积分以上**，即可覆盖 P0 + P1 全部接口；P2 中 `hsgt_top10` 同为 5000，无额外门槛压力。

### 6.3 限速与熔断策略

所有新增 Tushare 接口均需遵守以下规则（与 `.clinerules` §1.4 一致）：

1. **调用频率控制**：通过 `app/core/ratelimit.py` 的 `get_tushare_limiter()` 令牌桶。
2. **重试策略**：使用 `tenacity` 指数退避重试，最少 3 次（`_call_with_retry`）。
3. **熔断器保护**：高频/高消耗接口（建议：每日全市场批量接口）通过 `_call_with_breaker` 调用，加入 `_BREAKERED_ENDPOINTS`。
4. **降级优先于崩溃**：任何新接口失败时，优先返回空 DataFrame + log.warning，不允许因单一数据源失败导致整个 pipeline 中断。

---

## §7 附录

### A. 字段映射对照表（Tushare → 内部变量名）

> 各层传递时的字段归一化参考，避免不同文件用不同别名导致映射断链。

| Tushare 字段 | 内部变量名 | 数据库列名 | Excel 导出列名 | 单位 |
|-------------|-----------|-----------|----------------|------|
| `pe_ttm` | `pe_ttm` | `pe_ttm` | `PE_TTM` | 倍 |
| `pb` | `pb` | `pb` | `市净率PB` | 倍 |
| `dv_ttm` | `dv_ttm` | `dv_ttm` | `股息率%` | % |
| `total_mv` | `total_mv` | `total_mv` | `总市值(亿)` | 万元（存库）→ 亿元（展示） |
| `pct_chg` | `pct_chg` | `pct_chg` | `涨跌幅%` | % |
| `close` | `close` | `close` | `收盘价` | 元/股 |
| `q_dtprofit` | `q_dtprofit` | `q_dtprofit` | — | 万元 |
| `dt_eps` | `dt_eps` | `dt_eps` | `扣非PE_TTM`（计算基础）| 元/股 |
| `netprofit_yoy` | `netprofit_yoy` | `netprofit_yoy` | — | % |
| `debt_to_assets` | `debt_to_assets` | `debt_to_assets` | — | % |
| `ocf_to_profit` | `ocf_to_profit` | `ocf_to_profit` | — | 倍 |
| `act_ent_type` | `act_ent_type` | `act_ent_type` | — | 枚举字符串 |
| `pledge_ratio` | `pledge_ratio` | `pledge_ratio` | `控股股东质押率%` | % |

### B. AkShare 兜底矩阵

| Tushare 接口 | AkShare 等价接口 | 兜底可用性 | 主要差异 |
|-------------|-----------------|-----------|---------|
| `stock_basic` | `ak.stock_info_a_code_name()` + 手动拼装 | ✅ 可用但有损 | AkShare 名称无 ST 前缀（隐患）；无 `list_status` 字段 |
| `daily` | `ak.stock_zh_a_spot_em()` | ✅ 可用 | 字段名不同，需 `_AKSHARE_TO_TUSHARE_FIELDS` 映射 |
| `daily_basic` | ❌ 无等价接口 | ❌ | AkShare spot 无 PE_TTM / dv_ttm 字段 |
| `trade_cal` | `ak.tool_trade_date_hist_sina()` | ✅ 可用 | 字段格式不同（YYYY-MM-DD vs YYYYMMDD） |
| `fina_indicator_vip` | ❌ 无等价接口 | ❌ | AkShare 无全市场财务批量接口 |
| `dividend` | ❌ 无等价接口 | ❌ | — |
| `namechange` | ❌ 无等价接口 | ❌ | — |
| `suspend_d` | `ak.stock_zh_a_stop_em()` | ✅ 有限可用 | 字端不同，仅含 ts_code，无 suspend_timing |
| `limit_list_d` | `ak.stock_limit_up_em()` + `ak.stock_limit_down_em()` | ✅ 有限可用 | 需分两次调用后合并；无 `limit_type` 细分字段 |
| `pledge_stat` | ❌ 无等价接口 | ❌ | — |
| `adj_factor` | 内含于 `ak.stock_zh_a_hist(adjust='qfq')` | ⚠️ 间接可用 | 需反推因子，不如直接用 Tushare 原始因子 |
| `bak_basic` | ❌ 无等价接口 | ❌ | — |

### C. 参考链接

| 资源 | URL |
|------|-----|
| Tushare Pro 接口文档（总目录） | https://tushare.pro/document/2 |
| Tushare Pro 积分/套餐说明 | https://tushare.pro/register?reg=1 |
| waditu/tushare GitHub | https://github.com/waditu/tushare |
| AkShare 文档 | https://akshare.akfamily.xyz |
| 本项目接口请求封装 | `app/data/tushare_provider.py` |
| 本项目限速配置 | `app/core/ratelimit.py` + `app/core/config.py` |
| 本项目熔断器实现 | `app/data/breaker.py` |
| 架构设计文档 | `docs/ARCHITECTURE.md` |
| 升级变更日志 | `docs/UPGRADE.md` |

---

### D. 变更日志

| 版本 | 日期 | 变更内容 | 作者 |
|------|------|---------|------|
| v1.0 | 2026-05-21 | 初版创建，基于 2026-05-19 筛选结果 Excel 分析倒推 | StockSentry 项目组 |

---

*最后更新：2026-05-21 · StockSentry 个股哨兵 A 股民营企业价值筛选系统*
