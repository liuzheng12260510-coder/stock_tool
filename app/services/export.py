"""
导出服务 — 生成 Excel 多 Sheet 筛选报告

v1: 单 Sheet 基础版
v2 (T6): 多 Sheet 重构
  - Sheet 1：筛选结果（行情+综合评分，核心查阅）
  - Sheet 2：因子明细（5 维分项 + 原始因子，便于诊断）
  - Sheet 3：筛选参数（本次跑批所用的阈值快照）
  - Sheet 4：数据说明（列定义/来源）

Bug Fix (T2)：股息率 dv_ttm 从 snap.dv_ttm → 使用 StockSnapshot.dv_ttm
              修复 `if snap.dv_ttm` 0.0 被误判为空的问题
"""
from __future__ import annotations

from datetime import date
from pathlib import Path

from app.core.logging import get_logger
from app.domain.models import StockSnapshot

logger = get_logger("export")


def _v(val, decimals: int = 2) -> str | float:
    """将 None 转为空字符串，float 保留指定小数位"""
    if val is None:
        return ""
    if isinstance(val, float):
        return round(val, decimals)
    return val


def _pct(val) -> str | float:
    """百分比格式：None→空，否则保留2位"""
    return _v(val, 2)


def export_to_excel(
    snapshots: list[StockSnapshot],
    trade_date: date,
    output_dir: Path | None = None,
) -> Path:
    """
    生成多 Sheet Excel 报告

    Args:
        snapshots:   筛选结果列表（StockSnapshot）
        trade_date:  交易日
        output_dir:  输出目录，None 时使用 settings.exports_dir

    Returns:
        生成的 .xlsx 文件路径
    """
    try:
        import pandas as pd
    except ImportError as e:
        raise RuntimeError("pandas 未安装，无法导出 Excel") from e

    from app.core.config import settings

    out_dir = output_dir or settings.exports_dir
    Path(out_dir).mkdir(parents=True, exist_ok=True)
    filename = f"StockSentry_{trade_date}.xlsx"
    filepath = Path(out_dir) / filename

    # ── Sheet 1: 筛选结果 ────────────────────────────────────────────────────
    sheet1_rows = []
    for idx, s in enumerate(snapshots, 1):
        sheet1_rows.append({
            "排名": idx,
            "股票代码": s.ts_code,
            "公司名称": s.name,
            "行业": s.industry or "",
            "收盘价": _v(s.close),
            "涨跌幅%": _pct(s.pct_chg),
            "PE_TTM": _v(s.pe_ttm),
            "扣非PE_TTM": _v(s.pe_deduct_ttm),
            "市净率PB": _v(s.pb),
            # T2 fix: 用 is not None 替代 if dv_ttm（防止 0.0 被当空值）
            "股息率%": _v(s.dv_ttm) if s.dv_ttm is not None else "",
            "总市值(亿)": _v(s.total_mv_yi, 1),
            "综合评分": _v(s.composite_score, 1),
            "全市场排名%": _v(s.composite_rank, 1),
        })

    df1 = pd.DataFrame(sheet1_rows)

    # ── Sheet 2: 因子明细 ────────────────────────────────────────────────────
    sheet2_rows = []
    for s in snapshots:
        sheet2_rows.append({
            "股票代码": s.ts_code,
            "公司名称": s.name,
            "行业": s.industry or "",
            "综合评分": _v(s.composite_score, 1),
            # 5 维分项（z-score 归一化，0-100）
            "价值分": _v(s.value_score, 1),
            "成长分": _v(s.growth_score, 1),
            "稳定分": _v(s.stability_score, 1),
            "股息分": _v(s.dividend_score, 1),
            "安全分": _v(s.safety_score, 1),
            # 支撑因子（原始值）
            "扣非PE_TTM": _v(s.pe_deduct_ttm),
            "股息率%": _v(s.dv_ttm) if s.dv_ttm is not None else "",
            "连续分红年": _v(s.dividend_continuity, 0),
            "杜邦综合分": _v(s.dupont_score, 1),
            "盈余质量分": _v(s.earnings_quality_score, 1),
            "高杠杆红旗": "⚠️是" if s.high_leverage_flag else "",
            "剔除原因": "; ".join(s.fail_reasons) if s.fail_reasons else "",
        })

    df2 = pd.DataFrame(sheet2_rows)

    # ── Sheet 3: 筛选参数 ────────────────────────────────────────────────────
    try:
        from app.core.config import settings as cfg
        params = [
            ("交易日", str(trade_date)),
            ("通过筛选", f"{len(snapshots)} 只"),
            ("", ""),
            ("── 估值阈值", ""),
            ("PE_TTM 上限", cfg.screen_pe_ttm_max),
            ("扣非PE_TTM 上限", cfg.screen_pe_deduct_max),
            ("股息率% 下限", cfg.screen_dv_ttm_min),
            ("总市值上限(亿)", cfg.screen_total_mv_max_yi),
            ("总市值下限(亿)", cfg.screen_min_total_mv_yi),
            ("", ""),
            ("── 价格/异动阈值", ""),
            ("股价下限(元)", cfg.screen_min_close_price),
            ("|涨跌幅%|上限", cfg.screen_max_abs_pct_chg),
            ("剔除行业空股", cfg.screen_exclude_industry_nan),
            ("剔除停牌股", cfg.screen_exclude_no_volume),
            ("", ""),
            ("── 民企/黑名单阈值", ""),
            ("排除国企/央企", cfg.screen_exclude_soe),
            ("排除北交所", cfg.screen_exclude_bj),
            ("次新股年限", cfg.screen_min_list_years),
            ("", ""),
            ("── 基本面阈值", ""),
            ("最低综合分", cfg.screen_min_composite_score),
            ("最高权益乘数", cfg.screen_max_eqt_multiplier),
            ("最低CFO/NP", cfg.screen_min_cfo_to_np),
            ("最低连续分红年", cfg.screen_min_div_continuity_years),
            ("剔除清仓式分红", cfg.screen_reject_clearance_div),
        ]
    except Exception:  # noqa: BLE001
        params = [("交易日", str(trade_date)), ("通过筛选", len(snapshots))]

    df3 = pd.DataFrame(params, columns=["参数", "值"])

    # ── Sheet 4: 数据说明 ────────────────────────────────────────────────────
    explanations = [
        ("综合评分", "横截面 z-score 归一化，5 维各 20% 加权，0-100；≥3 个有效维度才输出"),
        ("全市场排名%", "本交易日全市场百分位，100% = 排名最高；同分取最低排名"),
        ("价值分", "扣非PE_TTM 越低越高分（z-score）"),
        ("成长分", "营收/利润增速（growth_rate）越高越高分"),
        ("稳定分", "近60日波动率越低越高分"),
        ("股息分", "股息率TTM (dv_ttm) 越高越高分"),
        ("安全分", "安全边际（PB 折价）越高越高分"),
        ("杜邦综合分", "ROE 质量评分，含高杠杆粉饰罚分"),
        ("盈余质量分", "CFO/净利润比 + 现金含量"),
        ("连续分红年", "统计截止交易日的连续历年分红次数"),
        ("高杠杆红旗", "权益乘数 > 阈值时标注，ROE 含水分"),
        ("PE_TTM", "市盈率 TTM（行情快照，亏损股显示为空）"),
        ("扣非PE_TTM", "剔除非经常性损益后的 PE（自算 TTM）"),
        ("市净率PB", "价格/净资产（行情快照）"),
        ("股息率%", "近12月分红/当前股价（Tushare daily_basic）"),
        ("总市值(亿)", "流通市值（万元）/ 10000，四舍五入到1位"),
        ("剔除原因", "本周期硬过滤失败的具体原因，通过筛选的股票此列为空"),
    ]
    df4 = pd.DataFrame(explanations, columns=["列名/指标", "说明"])

    # ── 写入 Excel ────────────────────────────────────────────────────────────
    with pd.ExcelWriter(str(filepath), engine="openpyxl") as writer:  # type: ignore[abstract]
        sheet_name1 = f"筛选结果_{trade_date}"
        df1.to_excel(writer, sheet_name=sheet_name1, index=False)
        df2.to_excel(writer, sheet_name="因子明细", index=False)
        df3.to_excel(writer, sheet_name="筛选参数", index=False)
        df4.to_excel(writer, sheet_name="数据说明", index=False)

        # ── 自动列宽 ─────────────────────────────────────────────────────────
        for sheet_df, sheet_name in [
            (df1, sheet_name1),
            (df2, "因子明细"),
            (df3, "筛选参数"),
            (df4, "数据说明"),
        ]:
            ws = writer.sheets[sheet_name]
            for col_cells in ws.columns:
                max_len = max(
                    len(str(cell.value)) if cell.value is not None else 0
                    for cell in col_cells
                )
                # openpyxl 列宽单位约等于字符宽度
                ws.column_dimensions[col_cells[0].column_letter].width = min(max_len + 4, 40)

        # ── Sheet 1 标题行加粗 + 冻结首行 ────────────────────────────────────
        try:
            from openpyxl.styles import Font, PatternFill
            ws1 = writer.sheets[sheet_name1]
            header_fill = PatternFill(start_color="1F65B7", end_color="1F65B7", fill_type="solid")
            header_font = Font(bold=True, color="FFFFFF")
            for cell in ws1[1]:
                cell.font = header_font
                cell.fill = header_fill
            ws1.freeze_panes = "A2"

            ws2 = writer.sheets["因子明细"]
            for cell in ws2[1]:
                cell.font = Font(bold=True)
            ws2.freeze_panes = "A2"
        except ImportError:
            pass

    logger.info(
        "export.excel.done",
        filepath=str(filepath),
        stocks=len(snapshots),
        trade_date=str(trade_date),
    )
    return filepath


def get_latest_export(exports_dir: Path | None = None) -> Path | None:
    """返回最新的 xlsx 文件路径，不存在则返回 None"""
    from app.core.config import settings

    out_dir = exports_dir or settings.exports_dir
    files = sorted(Path(out_dir).glob("StockSentry_*.xlsx"), reverse=True)
    return files[0] if files else None
