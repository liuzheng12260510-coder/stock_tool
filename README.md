# 🛡️ StockSentry 个股哨兵

A股民营企业价值筛选系统 — 每日定时跑批，历史落库，单页只读看板

## 功能特性

- **每日自动筛选**：工作日 15:30 自动拉取全市场数据并计算评分
- **修正 TTM 计算**：严格校验季度连续性，消除原代码季度错位 Bug
- **横截面 z-score 评分**：每日全市场分位数归一化，5 个维度综合评分
- **民企识别**：多源校验（act_ent_type + act_name 关键词兜底），可解释降级日志
- **AI 深度解读**：按需触发 OpenRouter Claude 分析，5 维度结构化输出，DB 缓存
- **Excel 导出**：看板按钮一键导出，保留美化样式
- **全市场批量接口**：daily/daily_basic 一次拉全市场，日常增量 < 8 分钟

## 快速开始

### 1. 配置环境变量

```bash
cp .env.example .env
# 编辑 .env，填写：
# TUSHARE_TOKEN=your_token_here
# OPENROUTER_API_KEY=sk-or-...（可选，AI 功能）
```

### 2. 安装依赖

```bash
pip install -r requirements.txt
```

### 3. 初始化数据库

```bash
python scripts/init_db.py
```

### 4. 启动服务

**Windows 双击启动：**
```
run.bat
```

**命令行启动：**
```bash
python -m app.main
```

访问 http://127.0.0.1:8000

## 手动跑批

```bash
# 最近交易日
python scripts/run_once.py

# 指定日期
python scripts/run_once.py 20250519

# 强制重跑（即使当日已有数据）
python scripts/run_once.py 20250519 --force
```

## 项目结构

```
stock_tool/
├── app/
│   ├── main.py           # FastAPI 应用入口
│   ├── core/             # 配置、日志、异常、限速
│   ├── domain/           # 领域模型（纯 Python）
│   ├── db/               # SQLAlchemy ORM + session
│   ├── data/             # Tushare/AkShare 数据层
│   ├── analytics/        # 因子计算、TTM、评分
│   ├── jobs/             # 跑批 Pipeline + APScheduler
│   ├── ai/               # OpenRouter AI Provider
│   ├── services/         # 筛选服务、Excel 导出
│   ├── api/              # FastAPI 路由
│   └── web/              # Jinja2 模板页面
├── scripts/
│   ├── init_db.py        # 初始化数据库
│   └── run_once.py       # 命令行跑批
├── tests/
│   └── test_ttm.py       # TTM 修正验证
├── .env.example          # 环境变量模板
├── run.bat               # Windows 一键启动
└── requirements.txt
```

## API 文档

启动后访问：http://127.0.0.1:8000/api/docs

主要接口：

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/api/stocks` | 筛选结果列表 |
| GET | `/api/stocks/{code}` | 单股最新快照 |
| GET | `/api/stocks/{code}/history` | 历史评分趋势 |
| POST | `/api/stocks/{code}/insight` | 按需 AI 解读 |
| POST | `/api/jobs/run` | 手动触发跑批 |
| GET | `/api/jobs/runs` | 跑批历史 |
| GET | `/api/export/excel` | 导出 Excel |

## 筛选标准（默认值，可在 .env 调整）

| 条件 | 默认值 |
|------|--------|
| PE_TTM | ≤ 30 |
| 扣非PE_TTM | ≤ 20 |
| 股息率 | ≥ 2% |
| 总市值 | ≤ 100 亿元 |
| 排除北交所 | true |
| 排除国企/央企 | true |
| 最低综合评分 | ≥ 60 |

## 评分体系

5 个维度，每维度横截面 z-score 归一化（0-100），各占 20%：

- **价值维度**：扣非 PE TTM（越低越好）
- **成长维度**：TTM 营收同比增速
- **稳定维度**：近 60 日年化波动率（越低越好）
- **股息维度**：股息率 TTM
- **安全维度**：安全边际综合得分

## 常见问题

**Q: 首次跑批报 Tushare 权限错误？**
A: 检查 `TUSHARE_TOKEN` 是否正确，fina_indicator_vip 接口需要 2120+ 积分，系统会自动降级到 fina_indicator

**Q: AI 解读返回 503？**
A: 检查 `.env` 中 `OPENROUTER_API_KEY` 是否配置，AI 功能为可选项

**Q: 跑批超过 15 分钟？**
A: 冷启动（首次）约 30-60 分钟，日常增量约 3-8 分钟；可通过日志查看各步骤耗时

**Q: Pylance 提示"无法解析导入"？**
A: 确认已运行 `pip install -r requirements.txt`，并已激活对应的 Python 环境
