# P0 → P3 迁移指南

> 如果你是从 P0/P1/P2 升级到 P3，请按本文档操作。

---

## 概览

| 版本 | 主要变更 |
|------|---------|
| P0 | 基础筛选：民企识别、PE/市值过滤、综合评分 |
| P1 | 杜邦三因子、盈余质量(CFO/NP)、分红连续性、新增阈值 |
| P2 | Checkpoint 断点续传、熔断器、AkShare 降级、心跳恢复 |
| P3 | **矩阵化 Pipeline**、财务深度 API、AI 增强(红旗)、文档/CI |

---

## 1. DB Schema 迁移

**P3 新增字段**：`ai_insights.financial_red_flags`（JSON 数组）

### 自动迁移（推荐）

```bash
python scripts/init_db.py
```

脚本会自动检测当前版本并执行 v5 迁移（幂等，可重复运行）。

### 手动迁移（仅紧急情况）

```sql
ALTER TABLE ai_insights ADD COLUMN financial_red_flags TEXT NOT NULL DEFAULT '[]';
```

---

## 2. 代码变更摘要

### 新增文件

| 文件 | 说明 |
|------|------|
| `app/analytics/vectorized.py` | 矩阵化算子：`vectorized_prescreen` / `batch_compute_factors` |
| `app/services/analytics_service.py` | 财务深度报告服务 + AI Prompt 上下文构建 |
| `tests/test_vectorized.py` | 矩阵化算子一致性测试 |
| `tests/test_analytics_service.py` | 财务深度报告测试 |
| `tests/test_api_financial_deep.py` | `/financial-deep` 端点测试 |
| `tests/test_pipeline_end2end.py` | 全 mock pipeline 耗时测试 |
| `.github/workflows/ci.yml` | GitHub Actions CI（ruff + pytest）|
| `docs/ARCHITECTURE.md` | 分层依赖图 + 状态机文档 |

### 修改文件

| 文件 | 变更摘要 |
|------|---------|
| `app/jobs/pipeline.py` | 引入 `USE_VECTORIZED_PIPELINE` 开关；`_run_prescreen` / `_compute_factors` 矩阵化 |
| `app/api/routes/stocks.py` | 新增 `GET /api/stocks/{ts_code}/financial-deep`；`_snapshot_to_dict` 补充 P3 字段 |
| `app/ai/prompts.py` | `build_stock_analysis_prompt` 接受杜邦/CFO/分红上下文参数 |
| `app/ai/openrouter.py` | `analyze_stock` 接受并传入 P3 上下文；解析 `financial_red_flags` |
| `app/ai/service.py` | 缓存 key 加入 `composite_score` hash；注入 P3 上下文 |
| `app/domain/models.py` | `AIInsight` 增 `financial_red_flags: list[str]` 字段 |
| `app/db/models.py` | `AIInsightCache` 增 `financial_red_flags: Text` 列 |
| `app/services/screening.py` | 修复 `snapshots.append` 缩进 bug；补充 P3 字段 |
| `scripts/init_db.py` | 新增 v5 迁移（`ai_insights.financial_red_flags`）|
| `app/web/templates/dashboard.html` | 新增列：连续分红年数、杜邦评分 |
| `app/web/templates/stock_detail.html` | 新增"财务体检"卡片 |

---

## 3. 环境变量变更

P3 无新增必填环境变量。以下为可选配置：

```dotenv
# .env（无变化，供参考）
USE_VECTORIZED_PIPELINE=true  # 由代码常量控制，非环境变量
```

---

## 4. API 变更

### 新增端点

```
GET /api/stocks/{ts_code}/financial-deep
```

**响应结构**：
```json
{
  "ts_code": "600519.SH",
  "dupont_timeseries": [{"period": "2024-12-31", "roe": 35.2, ...}],
  "cfo_np_timeseries": [{"period": "2024-12-31", "cfo_to_np": 1.05, ...}],
  "dividend_history": [{"year": 2024, "cash_div": 27.59, ...}],
  "latest_factor": {"composite_score": 88.5, ...},
  "red_flags": [],
  "dupont_percentile": {"roe_pct": 95.0, "industry": "白酒", ...}
}
```

### 变更端点（向后兼容）

`GET /api/stocks` 与 `GET /api/stocks/{ts_code}` 响应新增字段：
- `dupont_score`
- `dividend_continuity`
- `high_leverage_flag`
- `earnings_quality_score`

旧版客户端可安全忽略新字段。

---

## 5. AI 模块变更（`OPENROUTER_API_KEY` 已配置时生效）

AI 分析 JSON 输出新增 `financial_red_flags` 数组字段：

```json
{
  "financial_red_flags": [
    "高杠杆：权益乘数>3 或资产负债率>70%，ROE 含水分",
    "低盈余质量：CFO/NP 评分=25/100"
  ]
}
```

- 旧缓存不受影响（cache_key 含 `composite_score` hash，因子更新后自动失效）
- `financial_red_flags` 也由规则引擎在 `analytics_service._detect_red_flags()` 中计算，无需 AI 也可展示

---

## 6. 回滚说明

如需回滚到 P2：

1. `git checkout <P2-tag>`
2. DB schema 无需回滚（v5 新列有默认值，旧代码可忽略）
3. 或手动删除 `financial_red_flags` 列（SQLite 不支持 DROP COLUMN，需重建表）

---

## 7. 性能预期

| 环境 | P2 跑批耗时 | P3 跑批耗时 | 提升 |
|------|------------|------------|------|
| 全市场 ~5000 票 | 5-15 分钟 | 1-3 分钟 | 5-10× |
| 单次预筛 | ~10s | <0.5s | >20× |
| 因子计算 | ~300s | ~50s | ~6× |

> 以上数据不含网络 IO（Tushare/AkShare 请求时间）。
