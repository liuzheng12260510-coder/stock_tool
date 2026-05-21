"""
诊断脚本：分析最近一次跑批 0 入选的原因
用法：python scripts/diagnose_screen.py [YYYYMMDD]
"""
from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

DB_PATH = Path(__file__).parent.parent / "data" / "stocksentry.db"


def main() -> None:
    if not DB_PATH.exists():
        print(f"❌ 找不到数据库: {DB_PATH}")
        sys.exit(1)

    # 确定要诊断的交易日
    if len(sys.argv) > 1:
        trade_date = sys.argv[1]
        # 标准化为 YYYY-MM-DD
        if len(trade_date) == 8 and "-" not in trade_date:
            trade_date = f"{trade_date[:4]}-{trade_date[4:6]}-{trade_date[6:]}"
    else:
        # 自动取最近一次有数据的日期
        conn = sqlite3.connect(str(DB_PATH))
        row = conn.execute(
            "SELECT trade_date FROM factor_scores ORDER BY trade_date DESC LIMIT 1"
        ).fetchone()
        conn.close()
        if not row:
            print("❌ factor_scores 表中没有任何数据")
            sys.exit(1)
        trade_date = row[0]

    print(f"\n{'='*60}")
    print(f"  诊断交易日：{trade_date}")
    print(f"{'='*60}\n")

    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row

    # ── ① 总量 & 综合分分布 ─────────────────────────────────────────────
    row = conn.execute(
        """
        SELECT
          COUNT(*)                                           AS total,
          SUM(passed_screening)                              AS passed,
          ROUND(AVG(composite_score), 2)                     AS avg_score,
          ROUND(MIN(composite_score), 2)                     AS min_score,
          ROUND(MAX(composite_score), 2)                     AS max_score,
          SUM(CASE WHEN composite_score IS NULL THEN 1 ELSE 0 END) AS null_score,
          SUM(CASE WHEN composite_score >= 60 THEN 1 ELSE 0 END)   AS ge_60,
          SUM(CASE WHEN composite_score >= 50 THEN 1 ELSE 0 END)   AS ge_50,
          SUM(CASE WHEN composite_score >= 40 THEN 1 ELSE 0 END)   AS ge_40
        FROM factor_scores WHERE trade_date = ?
        """,
        (trade_date,),
    ).fetchone()

    if not row or row["total"] == 0:
        print(f"❌ factor_scores 中没有 trade_date={trade_date} 的数据")
        conn.close()
        sys.exit(1)

    print("【1】综合分分布")
    print(f"  总记录数  : {row['total']}")
    print(f"  入选数    : {row['passed']}")
    print(f"  评分为NULL: {row['null_score']}")
    print(f"  平均分    : {row['avg_score']}")
    print(f"  最低分    : {row['min_score']}")
    print(f"  最高分    : {row['max_score']}")
    print(f"  ≥60 分    : {row['ge_60']}")
    print(f"  ≥50 分    : {row['ge_50']}")
    print(f"  ≥40 分    : {row['ge_40']}")

    # ── ② fail_reasons 关键词统计 ─────────────────────────────────────
    print("\n【2】fail_reasons 关键词 Top15（各原因出现次数）")
    rows = conn.execute(
        """
        SELECT fail_reasons, COUNT(*) AS cnt
        FROM factor_scores
        WHERE trade_date = ?
        GROUP BY fail_reasons
        ORDER BY cnt DESC
        LIMIT 15
        """,
        (trade_date,),
    ).fetchall()

    keyword_counter: dict[str, int] = {}
    for r in rows:
        reasons_raw = r["fail_reasons"] or "[]"
        cnt = r["cnt"]
        try:
            reasons: list[str] = json.loads(reasons_raw)
        except Exception:
            reasons = [reasons_raw]
        for reason in reasons:
            kw = reason[:60]
            keyword_counter[kw] = keyword_counter.get(kw, 0) + cnt

    # 按出现次数排序
    sorted_kws = sorted(keyword_counter.items(), key=lambda x: -x[1])
    for kw, cnt in sorted_kws[:15]:
        bar = "█" * min(30, cnt // max(1, row["total"] // 30))
        pct = cnt / row["total"] * 100
        print(f"  {cnt:5d} ({pct:5.1f}%)  {bar}  {kw}")

    # ── ③ 综合分 Top10（看"最好这几只"为什么没过） ──────────────────
    print("\n【3】综合分 Top10 详情")
    top10 = conn.execute(
        """
        SELECT
          ts_code,
          ROUND(composite_score, 1)     AS score,
          ROUND(pe_deduct_ttm, 1)       AS pe_d,
          ROUND(dv_ttm, 2)              AS dv,
          dividend_continuity           AS div_y,
          high_leverage_flag            AS lev,
          ROUND(value_score, 1)         AS v_s,
          ROUND(growth_score, 1)        AS g_s,
          ROUND(stability_score, 1)     AS s_s,
          ROUND(dividend_score, 1)      AS d_s,
          ROUND(safety_score, 1)        AS f_s,
          passed_screening              AS pass,
          fail_reasons
        FROM factor_scores
        WHERE trade_date = ?
        ORDER BY composite_score DESC
        LIMIT 10
        """,
        (trade_date,),
    ).fetchall()

    header = f"{'代码':12s} {'综合':>5} {'扣非PE':>7} {'股息%':>6} {'分红年':>5} {'杠杆':>4} {'价值':>5} {'成长':>5} {'稳定':>5} {'分红':>5} {'安全':>5}  失败原因"
    print("  " + header)
    print("  " + "-" * len(header))
    for r in top10:
        reasons_raw = r["fail_reasons"] or "[]"
        try:
            reasons_list = json.loads(reasons_raw)
            reasons_str = "; ".join(reasons_list)[:80] if reasons_list else "✅ 通过"
        except Exception:
            reasons_str = reasons_raw[:80]
        print(
            f"  {r['ts_code']:12s} "
            f"{str(r['score'] or '-'):>5} "
            f"{str(r['pe_d'] or '-'):>7} "
            f"{str(r['dv'] or '-'):>6} "
            f"{str(r['div_y'] or '-'):>5} "
            f"{str(r['lev'] or '-'):>4} "
            f"{str(r['v_s'] or '-'):>5} "
            f"{str(r['g_s'] or '-'):>5} "
            f"{str(r['s_s'] or '-'):>5} "
            f"{str(r['d_s'] or '-'):>5} "
            f"{str(r['f_s'] or '-'):>5}  "
            f"{reasons_str}"
        )

    conn.close()
    print(f"\n{'='*60}\n")


if __name__ == "__main__":
    main()
