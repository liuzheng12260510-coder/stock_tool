"""
tests/test_dupont.py — 杜邦分解模块单元测试

覆盖：
- 正常杜邦分解（含高杠杆红旗触发条件）
- ROE 缺失时的降级（None 处理）
- 评分函数正确性
"""
from __future__ import annotations

from datetime import date

import pytest

from app.analytics.dupont import DupontResult, compute_dupont, score_dupont
from app.domain.models import QuarterRecord


def _make_quarter(
    ts_code: str = "000001.SZ",
    end_date: date = date(2024, 9, 30),
    roe: float | None = None,
    npta: float | None = None,
    debt_to_assets: float | None = None,
    assets_turn: float | None = None,
    eqt_multiplier: float | None = None,
) -> QuarterRecord:
    return QuarterRecord(
        ts_code=ts_code,
        end_date=end_date,
        q_dtprofit=1_000_000.0,
        roe=roe,
        npta=npta,
        debt_to_assets=debt_to_assets,
        assets_turn=assets_turn,
        eqt_multiplier=eqt_multiplier,
    )


class TestComputeDupont:
    def test_normal_no_leverage(self) -> None:
        """正常杜邦分解，杠杆合理，不触发红旗"""
        q = _make_quarter(roe=15.0, npta=8.0, debt_to_assets=45.0,
                          assets_turn=0.8, eqt_multiplier=2.0)
        result = compute_dupont(q)

        assert result.roe == 15.0
        assert result.npta == 8.0
        assert result.debt_to_assets == 45.0
        assert result.assets_turn == 0.8
        assert result.eqt_multiplier == 2.0
        assert result.leverage_warning is False

    def test_high_leverage_eqt_multiplier(self) -> None:
        """权益乘数 > 3.0 触发高杠杆红旗"""
        q = _make_quarter(roe=20.0, eqt_multiplier=3.5, debt_to_assets=60.0)
        result = compute_dupont(q)
        assert result.leverage_warning is True

    def test_high_leverage_debt_ratio(self) -> None:
        """资产负债率 > 70% 触发高杠杆红旗"""
        q = _make_quarter(roe=18.0, eqt_multiplier=2.5, debt_to_assets=75.0)
        result = compute_dupont(q)
        assert result.leverage_warning is True

    def test_both_leverage_flags(self) -> None:
        """同时触发两个高杠杆条件"""
        q = _make_quarter(roe=25.0, eqt_multiplier=4.0, debt_to_assets=80.0)
        result = compute_dupont(q)
        assert result.leverage_warning is True

    def test_null_data_graceful(self) -> None:
        """所有杜邦字段为 None 时正常降级，不抛异常"""
        q = _make_quarter()  # 所有 P1 字段默认 None
        result = compute_dupont(q)

        assert result.roe is None
        assert result.leverage_warning is False  # 无数据时不触发红旗

    def test_boundary_eqt_exactly_three(self) -> None:
        """权益乘数恰好等于 3.0 不触发红旗（阈值是严格大于）"""
        q = _make_quarter(roe=15.0, eqt_multiplier=3.0, debt_to_assets=50.0)
        result = compute_dupont(q)
        assert result.leverage_warning is False

    def test_boundary_debt_exactly_seventy(self) -> None:
        """资产负债率恰好等于 70.0 不触发红旗（阈值是严格大于）"""
        q = _make_quarter(roe=15.0, eqt_multiplier=2.0, debt_to_assets=70.0)
        result = compute_dupont(q)
        assert result.leverage_warning is False


class TestScoreDupont:
    def test_score_null_roe_returns_none(self) -> None:
        """ROE 为 None 时 score_dupont 返回 None"""
        result = DupontResult(
            roe=None, npta=None, assets_turn=None,
            eqt_multiplier=None, debt_to_assets=None,
            leverage_warning=False,
        )
        assert score_dupont(result) is None

    def test_score_full_mark_roe_20(self) -> None:
        """ROE >= 20% → 满分 100（无杠杆惩罚）"""
        result = DupontResult(
            roe=20.0, npta=10.0, assets_turn=0.8,
            eqt_multiplier=2.0, debt_to_assets=50.0,
            leverage_warning=False,
        )
        assert score_dupont(result) == 100.0

    def test_score_roe_above_20(self) -> None:
        """ROE > 20% → 仍是满分 100"""
        result = DupontResult(
            roe=35.0, npta=15.0, assets_turn=1.2,
            eqt_multiplier=2.5, debt_to_assets=55.0,
            leverage_warning=False,
        )
        assert score_dupont(result) == 100.0

    def test_score_roe_zero(self) -> None:
        """ROE <= 0 → 0 分"""
        result = DupontResult(
            roe=0.0, npta=0.0, assets_turn=0.5,
            eqt_multiplier=1.5, debt_to_assets=30.0,
            leverage_warning=False,
        )
        assert score_dupont(result) == 0.0

    def test_score_roe_negative(self) -> None:
        """ROE < 0 → 0 分"""
        result = DupontResult(
            roe=-5.0, npta=-2.0, assets_turn=0.4,
            eqt_multiplier=1.0, debt_to_assets=20.0,
            leverage_warning=False,
        )
        assert score_dupont(result) == 0.0

    def test_score_high_leverage_penalty(self) -> None:
        """高杠杆红旗扣 30 分"""
        result = DupontResult(
            roe=20.0, npta=5.0, assets_turn=0.5,
            eqt_multiplier=4.0, debt_to_assets=75.0,
            leverage_warning=True,
        )
        score = score_dupont(result)
        assert score == 70.0

    def test_score_high_leverage_clamp_to_zero(self) -> None:
        """得分被惩罚后不低于 0"""
        result = DupontResult(
            roe=5.0, npta=2.0, assets_turn=0.2,
            eqt_multiplier=4.0, debt_to_assets=80.0,
            leverage_warning=True,
        )
        score = score_dupont(result)
        # base = 5/20*100 = 25 - 30 = -5 → clamped to 0
        assert score == 0.0

    def test_score_linear_interpolation(self) -> None:
        """ROE = 10% → 基础分 50"""
        result = DupontResult(
            roe=10.0, npta=5.0, assets_turn=0.6,
            eqt_multiplier=2.0, debt_to_assets=45.0,
            leverage_warning=False,
        )
        assert score_dupont(result) == pytest.approx(50.0, abs=0.01)
