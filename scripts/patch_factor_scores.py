"""
快速补丁：修复 factor_scores 中因 dividend_history 数据不足
而被错误淘汰的股票（连续分红年数=0<3年，但实际只是没有历史数据）

逻辑：
1. 找到 dividend_history 中有记录的股票代码集合
2. 对 factor_scores 中 dividend_continuity=0 但不在上述集合里的记录：
   - 将 dividend_continuity 置为 NULL（"无数据"，区别于"确认从未分红"）
   - 从 fail_reasons 中移除"连续分红年数=0<3年"相关日志
   - 根据剩余 fail_reasons + composite_score 阈值重新计算 passed_screening
3. 统计并打印修复前后的入选数对比

用法：
    python scripts/patch_factor_scores.py [YYYYMMDD]
    不传日期则自动使用最近交易日
"""
from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

DB_PATH = Path(__file__).parent.parent / "data" / "stocksentry.db"

# 与 config.py 保持一致的阈值（补丁脚本不导入 Settings 以免加载慢）
SCREEN_MIN_COMPOSITE_SCORE = 60.0


def main() -> None:
    if not DB_PATH.exists():
        print(f"❌ 找不到数据库: {DB_PATH}")
        sys.exit(1)

    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row

    # 确定交易日
    if len(sys.argv) > 1:
        td = sys.argv[1]
        if len(td) == 8 and "-" not in td:
            td = f"{td[:4]}-{td[4:6]}-{td[6:]}"
    else:
        row = conn.execute(
            "SELECT trade_date FROM factor_scores ORDER BY trade_date DESC LIMIT 1"
        ).fetchone()
        if not row:
            print("❌ factor_scores 无数据")
            conn.close()
            sys.exit(1)
        td = row[0]

    print(f"\n{'='*60}")
    print(f"  补丁目标交易日：{td}")
    print(f"{'='*60}\n")

    # ── 1. 获取有分红历史数据的股票集合 ──────────────────────────────────
    div_codes = {
        r[0] for r in conn.execute("SELECT DISTINCT ts_code FROM dividend_history").fetchall()
    }
    print(f"dividend_history 中有记录的股票数: {len(div_codes)}")

    # ── 2. 修复前统计 ──────────────────────────────────────────────────────
    before = conn.execute(
        "SELECT COUNT(*), SUM(passed_screening) FROM factor_scores WHERE trade_date=?",
        (td,),
    ).fetchone()
    total = before[0]
    passed_before = before[1] or 0
    print(f"修复前 — 总记录: {total}，入选: {passed_before}")

    # ── 3. 查询需要修复的记录 ─────────────────────────────────────────────
    # 找所有 dividend_continuity=0 且不在 dividend_history 的记录
    rows = conn.execute(
        """
        SELECT id, ts_code, composite_score, fail_reasons, passed_screening
        FROM factor_scores
        WHERE trade_date = ? AND dividend_continuity = 0
        """,
        (td,),
    ).fetchall()

    to_fix = [r for r in rows if r["ts_code"] not in div_codes]
    print(f"需修复（无分红历史数据被误淘汰）: {len(to_fix)}")

    if not to_fix:
        print("✅ 无需修复")
        conn.close()
        return

    # ── 4. 逐条更新 ──────────────────────────────────────────────────────
    fixed_pass = 0
    updated = 0

    for r in to_fix:
        row_id = r["id"]
        composite_score = r["composite_score"] or 0.0

        # 解析并移除连续分红年数相关的 fail_reason
        try:
            reasons: list[str] = json.loads(r["fail_reasons"] or "[]")
        except Exception:
            reasons = []

        original_len = len(reasons)
        reasons_cleaned = [
            reason for reason in reasons
            if "连续分红年数" not in reason
        ]
        removed_count = original_len - len(reasons_cleaned)

        if removed_count == 0:
            # 没有"连续分红年数"相关原因，不需要修
            continue

        # 重新计算 passed_screening
        new_passed = (
            len(reasons_cleaned) == 0
            and composite_score >= SCREEN_MIN_COMPOSITE_SCORE
        )
        new_reasons_json = json.dumps(reasons_cleaned, ensure_ascii=False)

        conn.execute(
            """
            UPDATE factor_scores
            SET dividend_continuity = NULL,
                fail_reasons = ?,
                passed_screening = ?
            WHERE id = ?
            """,
            (new_reasons_json, 1 if new_passed else 0, row_id),
        )
        updated += 1
        if new_passed:
            fixed_pass += 1

    conn.commit()

    # ── 5. 修复后统计 ─────────────────────────────────────────────────────
    after = conn.execute(
        "SELECT COUNT(*), SUM(passed_screening) FROM factor_scores WHERE trade_date=?",
        (td,),
    ).fetchone()
    passed_after = after[1] or 0

    print(f"\n✅ 修复完成")
    print(f"  更新记录数  : {updated}")
    print(f"  新增入选数  : {fixed_pass}")
    print(f"修复后 — 总记录: {total}，入选: {passed_after}（修复前: {passed_before}）")

    # ── 6. 打印 Top5 新入选 ────────────────────────────────────────────────
    if fixed_pass > 0:
        print(f"\n入选 Top10（综合分排序）：")
        top = conn.execute(
            """
            SELECT ts_code, ROUND(composite_score,1) score, ROUND(dv_ttm,2) dv, fail_reasons
            FROM factor_scores
            WHERE trade_date=? AND passed_screening=1
            ORDER BY composite_score DESC LIMIT 10
            """,
            (td,),
        ).fetchall()
        for row in top:
            reasons = json.loads(row["fail_reasons"] or "[]")
            print(f"  {row['ts_code']}  分={row['score']}  股息%={row['dv'] or '-'}  fail={reasons}")

    conn.close()
    print(f"\n{'='*60}\n")


if __name__ == "__main__":
    main()
