"""
黑名单识别模块单元测试
"""
from __future__ import annotations

from datetime import date, timedelta

from app.analytics.blacklist import build_blacklist_from_df, detect_blacklist_reason

# ─────────────────────────────────────────────────────────────────────────────
# detect_blacklist_reason 测试
# ─────────────────────────────────────────────────────────────────────────────

TODAY = date(2025, 5, 21)


class TestDetectST:
    """ST / *ST / PT / 退市 识别"""

    def test_star_st_prefix(self):
        reason, expires_at = detect_blacklist_reason("*ST银行", "000001.SZ", None, TODAY)
        assert reason == "*ST"
        assert expires_at is None

    def test_st_space_prefix(self):
        reason, _ = detect_blacklist_reason("ST 某某", "000002.SZ", None, TODAY)
        assert reason == "ST"

    def test_st_nospace_prefix(self):
        reason, _ = detect_blacklist_reason("ST某某", "000003.SZ", None, TODAY)
        assert reason == "ST"

    def test_st_contains(self):
        reason, _ = detect_blacklist_reason("某ST股", "000004.SZ", None, TODAY)
        assert reason == "ST"

    def test_pt_prefix(self):
        reason, _ = detect_blacklist_reason("PT某某", "000005.SZ", None, TODAY)
        assert reason == "PT"

    def test_delist_in_name(self):
        reason, _ = detect_blacklist_reason("退市某某", "000006.SZ", None, TODAY)
        assert reason in ("delist_risk", "*ST", "ST")

    def test_normal_stock_not_blacklisted(self):
        reason, _ = detect_blacklist_reason("贵州茅台", "600519.SH", None, TODAY)
        assert reason is None

    def test_name_with_st_in_middle_no_false_positive(self):
        """名称中不含 ST 前缀的正常股票不应误判（如某些含有 str 之类的）"""
        reason, _ = detect_blacklist_reason("成都银行", "001265.SZ", None, TODAY)
        assert reason is None


class TestDetectBSE:
    """北交所识别"""

    def test_bj_suffix_excluded(self):
        reason, expires_at = detect_blacklist_reason("某某科技", "835185.BJ", None, TODAY)
        assert reason == "bse"
        assert expires_at is None

    def test_bj_suffix_allowed_when_not_excluded(self):
        reason, _ = detect_blacklist_reason(
            "某某科技", "835185.BJ", None, TODAY, exclude_bj=False
        )
        assert reason is None

    def test_sh_not_bj(self):
        reason, _ = detect_blacklist_reason("招商银行", "600036.SH", None, TODAY)
        assert reason is None

    def test_sz_not_bj(self):
        reason, _ = detect_blacklist_reason("平安银行", "000001.SZ", None, TODAY)
        assert reason is None


class TestDetectNewListing:
    """次新股识别"""

    def test_listed_6_months_ago(self):
        list_date = TODAY - timedelta(days=180)
        reason, expires_at = detect_blacklist_reason(
            "新股某某", "688001.SH", list_date, TODAY, min_list_years=1.0
        )
        assert reason == "new_listing"
        assert expires_at is not None
        # 过期时间应为 list_date + 365 天
        assert expires_at == list_date + timedelta(days=365)

    def test_listed_exactly_1_year_ago_is_ok(self):
        list_date = TODAY - timedelta(days=365)
        reason, _ = detect_blacklist_reason(
            "某某股", "688002.SH", list_date, TODAY, min_list_years=1.0
        )
        assert reason is None

    def test_listed_over_1_year_ago_is_ok(self):
        list_date = TODAY - timedelta(days=400)
        reason, _ = detect_blacklist_reason(
            "某某股", "688003.SH", list_date, TODAY, min_list_years=1.0
        )
        assert reason is None

    def test_none_list_date_skipped(self):
        reason, _ = detect_blacklist_reason("某某股", "688004.SH", None, TODAY)
        assert reason is None

    def test_custom_min_years(self):
        """min_list_years=2: 上市 20 个月仍视为次新股"""
        list_date = TODAY - timedelta(days=600)  # 约 20 个月
        reason, _ = detect_blacklist_reason(
            "新股某某", "688005.SH", list_date, TODAY, min_list_years=2.0
        )
        assert reason == "new_listing"

    def test_expires_at_in_future(self):
        """次新股 expires_at 必须大于 today"""
        list_date = TODAY - timedelta(days=100)
        _, expires_at = detect_blacklist_reason(
            "新股某某", "688006.SH", list_date, TODAY, min_list_years=1.0
        )
        assert expires_at is not None
        assert expires_at > TODAY


class TestPriorityOrder:
    """多条件命中时优先级验证：ST > 北交所 > 次新股"""

    def test_st_beats_bj(self):
        """ST + 北交所 → 返回 ST"""
        reason, _ = detect_blacklist_reason("ST某某", "835185.BJ", None, TODAY)
        assert reason == "ST"

    def test_st_beats_new_listing(self):
        """ST + 次新股 → 返回 ST"""
        list_date = TODAY - timedelta(days=30)
        reason, _ = detect_blacklist_reason("ST某某", "000001.SZ", list_date, TODAY)
        assert reason == "ST"


# ─────────────────────────────────────────────────────────────────────────────
# build_blacklist_from_df 测试
# ─────────────────────────────────────────────────────────────────────────────

class TestBuildBlacklistFromDf:
    """DataFrame 批量识别"""

    def test_mixed_df(self):
        import pandas as pd

        df = pd.DataFrame(
            {
                "ts_code": ["600519.SH", "*ST某某", "835185.BJ", "688001.SH"],
                "name": ["贵州茅台", "*ST某某", "某某科技", "新股"],
                "list_date": [
                    "19910725",       # 正常
                    "20230101",       # ST，应命中 ST
                    "20220101",       # 北交所
                    (TODAY - timedelta(days=100)).strftime("%Y%m%d"),  # 次新股
                ],
            }
        )

        records = build_blacklist_from_df(df, TODAY)
        codes = {r["ts_code"] for r in records}

        # 贵州茅台不在黑名单
        assert "600519.SH" not in codes
        # ST 股在黑名单（ts_code 字段是 *ST某某，但 name 才是关键）
        # 这里 ts_code="*ST某某" 并非标准格式，但我们通过 name 识别
        assert any(r["reason"] in ("*ST", "ST") for r in records if r["ts_code"] == "*ST某某")
        # 北交所在黑名单
        assert "835185.BJ" in codes
        # 次新股在黑名单
        assert "688001.SH" in codes

    def test_empty_df(self):
        import pandas as pd

        df = pd.DataFrame({"ts_code": [], "name": [], "list_date": []})
        records = build_blacklist_from_df(df, TODAY)
        assert records == []
