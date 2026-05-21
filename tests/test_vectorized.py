"""
P3.6 — test_vectorized.py

矩阵化算子与单股算子结果一致性测试（oracle 校验）
"""
from __future__ import annotations

from datetime import date

import pandas as pd

from app.analytics.factors import compute_all_factors
from app.analytics.vectorized import batch_compute_factors, vectorized_prescreen
from app.domain.models import DividendRecord, FactorResult, QuarterRecord

# ─────────────────────────────────────────────────────────────────────────────
# Fixtures
# ─────────────────────────────────────────────────────────────────────────────

TRADE_DATE = date(2025, 3, 31)


def _make_market_df(rows: list[dict]) -> pd.DataFrame:
    """构造测试用 market_df"""
    return pd.DataFrame(rows)


def _make_quarters(ts_code: str) -> list[QuarterRecord]:
    return [
        QuarterRecord(
            ts_code=ts_code,
            end_date=date(2024, 12, 31),
            q_dtprofit=500.0,
            total_cur_assets=10000.0,
            total_liab=5000.0,
            op_revenue=8000.0,
            source="tushare",
            roe=15.0,
            npta=8.0,
            debt_to_assets=0.48,
            assets_turn=0.6,
            eqt_multiplier=2.0,
            netprofit_yoy=12.0,
            op_yoy=10.0,
            n_cashflow_act=600.0,
            cfo_to_np=1.2,
        ),
        QuarterRecord(
            ts_code=ts_code,
            end_date=date(2024, 9, 30),
            q_dtprofit=450.0,
            total_cur_assets=9500.0,
            total_liab=4800.0,
            op_revenue=7600.0,
            source="tushare",
            roe=14.0,
            npta=7.5,
            debt_to_assets=0.45,
            assets_turn=0.58,
            eqt_multiplier=1.9,
            netprofit_yoy=10.0,
            op_yoy=9.0,
            n_cashflow_act=540.0,
            cfo_to_np=1.1,
        ),
    ]


def _make_dividends(ts_code: str) -> list[DividendRecord]:
    return [
        DividendRecord(
            ts_code=ts_code,
            end_date=date(2024, 12, 31),
            div_proc="实施",
            cash_div=0.5,
            cash_div_tax=0.45,
        ),
        DividendRecord(
            ts_code=ts_code,
            end_date=date(2023, 12, 31),
            div_proc="实施",
            cash_div=0.4,
            cash_div_tax=0.36,
        ),
    ]


# ─────────────────────────────────────────────────────────────────────────────
# vectorized_prescreen 一致性测试
# ─────────────────────────────────────────────────────────────────────────────

class TestVectorizedPrescreen:
    """vectorized_prescreen 结果与 apply_prescreen_filter 逐行结果一致"""

    def test_basic_filter(self) -> None:
        """PE 和市值在阈值内的股票应通过预筛"""
        from app.analytics.filters import apply_prescreen_filter

        market_rows = [
            {"ts_code": "000001.SZ", "pe_ttm": 15.0, "total_mv": 500_0000.0},  # 50亿 → 通过
            {"ts_code": "000002.SZ", "pe_ttm": 200.0, "total_mv": 300_0000.0},  # PE过高 → 过滤
            {"ts_code": "000003.SZ", "pe_ttm": 20.0, "total_mv": 6000_0000.0}, # 市值>500亿 → 过滤
            {"ts_code": "000004.SZ", "pe_ttm": -5.0, "total_mv": 200_0000.0},  # PE负 → 过滤
            {"ts_code": "430001.BJ", "pe_ttm": 20.0, "total_mv": 100_0000.0},   # 北交所 → 过滤
        ]
        market_df = _make_market_df(market_rows)
        stocks = {
            "000001.SZ": {"exchange": "SSE", "is_private": True},
            "000002.SZ": {"exchange": "SZSE", "is_private": True},
            "000003.SZ": {"exchange": "SSE", "is_private": True},
            "000004.SZ": {"exchange": "SZSE", "is_private": True},
            "430001.BJ": {"exchange": "BSE", "is_private": True},
        }

        pe_max = 100.0
        mv_max = 500.0  # 亿
        exclude_bj = True

        eligible_set, filtered_df = vectorized_prescreen(
            market_df, stocks,
            exclude_bj=exclude_bj,
            prescreen_pe_ttm_max=pe_max,
            prescreen_total_mv_max_yi=mv_max,
        )

        # 逐行验证
        for _, row in market_df.iterrows():
            ts = str(row["ts_code"])
            stock = stocks.get(ts, {})
            total_mv = float(row["total_mv"])
            pe_ttm = float(row["pe_ttm"])
            total_mv_yi = total_mv / 10000.0

            expected, _ = apply_prescreen_filter(
                ts_code=ts,
                exchange=stock.get("exchange", ""),
                pe_ttm=pe_ttm,
                total_mv_yi=total_mv_yi,
                exclude_bj=exclude_bj,
                prescreen_pe_ttm_max=pe_max,
                prescreen_total_mv_max_yi=mv_max,
            )
            assert (ts in eligible_set) == expected, (
                f"{ts}: vectorized={ts in eligible_set}, oracle={expected}"
            )

        assert list(filtered_df["ts_code"]) == sorted(eligible_set)

    def test_empty_df(self) -> None:
        """空 DataFrame 不报错，返回空集合"""
        market_df = pd.DataFrame(columns=["ts_code", "pe_ttm", "total_mv"])
        eligible_set, filtered_df = vectorized_prescreen(
            market_df, {},
            exclude_bj=True,
            prescreen_pe_ttm_max=100.0,
            prescreen_total_mv_max_yi=500.0,
        )
        assert eligible_set == set()
        assert filtered_df.empty


# ─────────────────────────────────────────────────────────────────────────────
# batch_compute_factors 一致性测试
# ─────────────────────────────────────────────────────────────────────────────

class TestBatchComputeFactors:
    """batch_compute_factors 与 compute_all_factors 逐票结果一致"""

    def _run_oracle(
        self,
        ts_codes: list[str],
        market_df: pd.DataFrame,
        stocks: dict,
        quarters_by_code: dict,
        dividends_by_code: dict,
    ) -> dict[str, FactorResult]:
        """单股 oracle 逐票计算"""
        results = {}
        for _, row in market_df.iterrows():
            ts = str(row["ts_code"])
            total_mv = float(row.get("total_mv") or 0)
            pe_ttm = row.get("pe_ttm")
            pb = row.get("pb")
            dv_ttm = row.get("dv_ttm")

            factor = compute_all_factors(
                ts_code=ts,
                trade_date=TRADE_DATE,
                quarters=quarters_by_code.get(ts, []),
                total_mv_wan=total_mv if total_mv else None,
                pe_ttm=float(pe_ttm) if pe_ttm else None,
                pb=float(pb) if pb else None,
                dv_ttm=float(dv_ttm) if dv_ttm else None,
                dividends=dividends_by_code.get(ts, []),
            )
            results[ts] = factor
        return results

    def test_single_stock_match(self) -> None:
        """单只股票：批量结果与单股结果数值一致"""
        ts = "000001.SZ"
        market_df = _make_market_df([
            {"ts_code": ts, "pe_ttm": 12.0, "pb": 1.5, "dv_ttm": 3.5, "total_mv": 200_0000.0},
        ])
        stocks = {ts: {"exchange": "SSE", "is_private": True, "name": "平安银行", "industry": "银行"}}
        quarters_by_code = {ts: _make_quarters(ts)}
        dividends_by_code = {ts: _make_dividends(ts)}

        batch_results = batch_compute_factors(
            TRADE_DATE, market_df, stocks, quarters_by_code, dividends_by_code
        )
        oracle_results = self._run_oracle(
            [ts], market_df, stocks, quarters_by_code, dividends_by_code
        )

        assert len(batch_results) == 1
        bf = batch_results[0]
        of = oracle_results[ts]

        assert bf.ts_code == of.ts_code
        # 关键数值字段与 oracle 一致（允许浮点误差）
        _assert_close(bf.pe_ttm, of.pe_ttm, "pe_ttm")
        _assert_close(bf.pb, of.pb, "pb")
        _assert_close(bf.dv_ttm, of.dv_ttm, "dv_ttm")
        _assert_close(bf.dupont_score, of.dupont_score, "dupont_score")
        _assert_close(bf.earnings_quality_score, of.earnings_quality_score, "earnings_quality_score")
        assert bf.dividend_continuity == of.dividend_continuity

    def test_multiple_stocks_all_present(self) -> None:
        """多只股票：每只股票都在返回结果中"""
        codes = ["000001.SZ", "000002.SZ", "000003.SZ"]
        rows = [
            {"ts_code": c, "pe_ttm": 15.0 + i, "pb": 1.2 + i * 0.1,
             "dv_ttm": 2.0 + i * 0.3, "total_mv": 100_0000.0 + i * 50_0000}
            for i, c in enumerate(codes)
        ]
        market_df = _make_market_df(rows)
        stocks = {c: {"exchange": "SZSE", "is_private": True} for c in codes}
        quarters_by_code = {c: _make_quarters(c) for c in codes}
        dividends_by_code = {c: _make_dividends(c) for c in codes}

        batch_results = batch_compute_factors(
            TRADE_DATE, market_df, stocks, quarters_by_code, dividends_by_code
        )
        result_codes = {r.ts_code for r in batch_results}
        assert result_codes == set(codes)

    def test_empty_df(self) -> None:
        """空 DataFrame 返回空列表"""
        market_df = pd.DataFrame(columns=["ts_code", "pe_ttm", "pb", "dv_ttm", "total_mv"])
        results = batch_compute_factors(TRADE_DATE, market_df, {}, {}, {})
        assert results == []


def _assert_close(a: float | None, b: float | None, name: str, tol: float = 1e-6) -> None:
    """允许 None vs None，或 float 近似相等"""
    if a is None and b is None:
        return
    assert a is not None and b is not None, f"{name}: one is None ({a} vs {b})"
    assert abs(a - b) <= tol, f"{name}: {a} != {b} (tol={tol})"
