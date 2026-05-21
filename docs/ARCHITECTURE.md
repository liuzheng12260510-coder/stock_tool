# StockSentry 架构设计文档

> 版本：P3（2025-05）

---

## 1. 分层架构

```
┌─────────────────────────────────────────────────────────────────┐
│  HTTP 层（FastAPI）                                              │
│  app/api/routes/       — JSON API（stocks/insight/jobs/export）  │
│  app/web/routes.py     — Jinja2 页面（dashboard/detail）         │
└─────────────────────────────────────────────────────────────────┘
            ↓ 只调用 services/domain
┌─────────────────────────────────────────────────────────────────┐
│  服务编排层                                                       │
│  app/services/screening.py      — 筛选结果查询                   │
│  app/services/analytics_service.py — 财务深度报告（P3 新增）     │
│  app/services/export.py         — CSV 导出                       │
└─────────────────────────────────────────────────────────────────┘
            ↓ 只调用 analytics/domain
┌─────────────────────────────────────────────────────────────────┐
│  计算层（纯函数，无 IO）                                          │
│  app/analytics/vectorized.py    — P3 矩阵化算子（批量预筛/因子） │
│  app/analytics/factors.py       — 单股因子计算（兼容旧版）        │
│  app/analytics/scoring.py       — 横截面 Z-score 评分            │
│  app/analytics/ttm.py           — TTM 季度聚合                   │
│  app/analytics/dupont.py        — 杜邦三因子                     │
│  app/analytics/earnings_quality.py — CFO/NP 盈余质量             │
│  app/analytics/dividend_quality.py — 分红连续性                  │
│  app/analytics/filters.py       — 预筛/硬性过滤                  │
│  app/analytics/blacklist.py     — 黑名单构建/过滤                │
└─────────────────────────────────────────────────────────────────┘
            ↓
┌─────────────────────────────────────────────────────────────────┐
│  数据层                                                           │
│  app/data/composite.py       — CompositeProvider（主入口）       │
│  app/data/tushare_provider.py — Tushare 适配器                   │
│  app/data/akshare_provider.py — AkShare 适配器                   │
│  app/data/trade_calendar.py  — 交易日历                          │
│  app/data/breaker.py         — 熔断器（指数退避 + 半开放）        │
│  app/core/ratelimit.py       — 令牌桶限速                        │
└─────────────────────────────────────────────────────────────────┘
            ↓
┌─────────────────────────────────────────────────────────────────┐
│  持久层                                                           │
│  app/db/models.py   — SQLAlchemy ORM（SQLite）                   │
│  app/db/session.py  — db_session() 上下文管理器                  │
└─────────────────────────────────────────────────────────────────┘
```

### 依赖方向规则（强约束）

```
api/web → services → analytics/data → domain/core（单向，禁止反向）
jobs    → 任意层（跑批 Pipeline 可引入所有模块）
ai      → services/db（缓存），禁止 import api/web
```

---

## 2. Checkpoint 状态机

跑批 Pipeline 的每个 Step 均通过 `job_checkpoints` 表记录状态：

```
pending ──→ running ──→ done
                   └──→ failed
```

**Job Run 整体状态机**：
```
(新建) running ──→ success
              └──→ failed
              └──→ [心跳超时 1h] → recover_unfinished() → running (resume)
```

### Step 顺序与断点策略

| Step | 名称 | skip_if_done | 说明 |
|------|------|-------------|------|
| 1 | stock_basic | ✓ | 幂等 upsert |
| 2 | blacklist | ✓ | 幂等 upsert |
| 3 | daily | ✗ | 始终拉取（内存数据） |
| 3.5 | daily_basic | ✗ | 始终拉取 |
| 4 | prescreen | ✗ | 内存计算（P3: 矩阵化） |
| 5 | snapshot_save | ✓ | INSERT OR IGNORE |
| 6 | fina_q1~q4 | ✓ | 季报独立断点 |
| 6.5 | dividend | 年度级别 ✓ | payload 记录已完成年度 |
| 7 | load_inputs | ✗ | 内存加载 |
| 8 | factor_compute | ✗ | 内存计算（P3: 矩阵化） |
| 9 | scoring | ✗ | 内存计算 |
| 10 | hard_filter | ✗ | 无 IO |
| 11 | save_scores | ✗ | DELETE + INSERT |

---

## 3. 熔断器状态机（CircuitBreaker）

```
CLOSED ──[连续失败 N 次]──→ OPEN
  ↑                           │
  └──[半开探测成功]──── HALF_OPEN ←──[等待 timeout 秒]
```

- **CLOSED**：正常调用
- **OPEN**：快速失败（不发起网络请求）
- **HALF_OPEN**：允许一次探测调用

实现见 `app/data/breaker.py`。

---

## 4. P3 矩阵化优化

### 优化路径

| 步骤 | 优化前 | 优化后 | 加速比 |
|------|--------|--------|--------|
| 预筛（Step 4） | `iterrows` 逐票 | pandas 向量化布尔运算 | ~20-50× |
| 因子计算（Step 8） | `iterrows` + `compute_all_factors` | `batch_compute_factors` | ~5-8× |

### 灰度开关

```python
# app/jobs/pipeline.py
USE_VECTORIZED_PIPELINE: bool = True  # False = 回退到 iterrows（调试用）
```

### 矩阵化算子位置

- `app/analytics/vectorized.py`
  - `vectorized_prescreen()` — 向量化预筛（pandas 布尔索引）
  - `batch_compute_factors()` — 批量因子（复用单股 `compute_all_factors`）

---

## 5. AI 模块

```
StockSnapshot → build_stock_analysis_prompt()
             +  _build_dupont_context()     ← P3 新增
             +  _build_cfo_context()        ← P3 新增
             +  _build_dividend_context()   ← P3 新增
             ↓
OpenRouter API (JSON Schema 强制结构化输出)
             ↓
AIInsight (5维度 + financial_red_flags)   ← P3 新增
             ↓
ai_insights 表（缓存，版本化 cache_key）
```

**缓存 key** 格式（P3 更新）：
```
{ts_code}:{trade_date}:{composite_score_hash}
```
财务因子更新后 `composite_score` 变化 → 加入 hash → 缓存自动失效。

---

## 6. DB Schema 版本历史

| 版本 | 内容 |
|------|------|
| v1 | 初始建表（ORM create_all） |
| v2 | P0: stocks 黑名单列；blacklist 表 |
| v3 | P1: financial_quarters 杜邦/CFO字段；dividend_history 表；factor_scores P1列 |
| v4 | P2: job_runs 断点续传列；job_checkpoints 表 |
| v5 | P3: ai_insights.financial_red_flags 列 |

迁移入口：`python scripts/init_db.py`
