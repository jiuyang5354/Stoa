"""判断事后打分：按到期日收盘结算、对照基准算超额、途中最大浮盈浮亏、下次分析同票带上旧判断、置信度校准。全部离线。"""
import pandas as pd
import pytest

from stock_agent import scorecard


@pytest.fixture(autouse=True)
def _home(tmp_path, monkeypatch):
    monkeypatch.setenv("STOCK_AGENT_HOME", str(tmp_path))
    monkeypatch.setenv("EMBEDDINGS", "off")


def mem():
    from stock_agent.memory import Memory
    return Memory()


def ser(start, vals):
    return pd.Series(vals, index=pd.bdate_range(start, periods=len(vals)))


def put(m, sym, view, price, ts, due, conf=None):
    cid = m.record_call(sym, "", view, price, 5, "测试", conf)
    m.db.execute("UPDATE calls SET ts=?, due=? WHERE id=?", (ts, due, cid))
    m.db.commit()
    return cid


def test_bench_by_market():
    assert scorecard.bench_of("600519.SS") == "000300.SS"
    assert scorecard.bench_of("0700.HK") == "^HSI"
    assert scorecard.bench_of("NVDA") == "^GSPC"
    assert scorecard.bench_of("^GSPC") is None


def test_settles_at_due_close_not_today():
    m = mem()
    # 2026-01-05 起：买入价 100，到期日 01-09 收 110，之后跌到 90 —— 复盘晚了也按 110 算
    s = ser("2026-01-05", [100, 104, 96, 108, 110, 95, 90])
    b = ser("2026-01-05", [4000, 4010, 4020, 4030, 4080, 4000, 3990])
    put(m, "600519.SS", "看涨", 100, "2026-01-05 15:10", "2026-01-09")
    notes = scorecard.settle_due(m, {"600519.SS": s, "000300.SS": b})
    assert len(notes) == 1 and "+10.00%" in notes[0] and "沪深300" in notes[0]
    c = m.recent_calls()[-1]
    assert c["status"] == "closed" and c["verdict"] == "对"
    assert c["result_pct"] == 10.0 and c["bench_pct"] == 2.0 and c["alpha_pct"] == 8.0
    assert c["mfe_pct"] == 10.0 and c["mae_pct"] == -4.0 and c["end_date"] == "2026-01-09"


def test_not_settled_before_due_data_arrives():
    m = mem()
    s = ser("2026-01-05", [100, 101, 102])
    put(m, "NVDA", "看跌", 100, "2026-01-05 10:00", "2026-01-20")
    assert scorecard.settle_due(m, {"NVDA": s}) == []


def test_bear_and_range_edge():
    assert scorecard.edge("看跌", -5.0, -3.0) == 3.0
    assert scorecard.edge("看涨", 5.0, -1.0) == -1.0  # 涨了但跑输大盘：方向分为负
    assert scorecard.edge("震荡", 1.0, None) == 2.0
    assert scorecard.verdict("震荡", 4.0) == "错"


def test_track_and_get_quotes_note():
    m = mem()
    s = ser("2026-01-05", [100, 104, 96, 108, 110])
    put(m, "0700.HK", "看涨", 100, "2026-01-05 15:10", "2026-01-09")
    scorecard.settle_due(m, {"0700.HK": s, "^HSI": ser("2026-01-05", [1, 1, 1, 1, 1])})
    put(m, "0700.HK", "看跌", 110, "2026-01-12 15:10", "2099-01-01")
    t = scorecard.track(m, "0700.HK", {"close": 99})
    assert t[0]["status"] == "已结算" and t[0]["alpha"] == 10.0
    assert t[1]["pct_so_far"] == -10.0
    note = scorecard.track_note(t)
    assert "超额 +10.00%" in note and "至今 -10.00%" in note and "什么变了" in note


def test_stats_calibration():
    rows = [{"view": "看涨", "alpha": 2, "edge": 2, "confidence": 80}] * 6 + \
           [{"view": "看涨", "alpha": -2, "edge": -2, "confidence": 55}] * 6
    st = scorecard.stats(rows)
    assert st["方向分胜率%"] == 50.0 and st["按置信度"]["高"]["胜率%"] == 100.0
    assert st["校准"] == "高置信度确实更准"


def test_call_stats_includes_alpha_and_migration():
    m = mem()
    put(m, "AAPL", "看涨", 100, "2026-01-05 10:00", "2026-01-07", 70)
    scorecard.settle_due(m, {"AAPL": ser("2026-01-05", [100, 101, 103]), "^GSPC": ser("2026-01-05", [10, 10, 10.1])})
    st = m.call_stats()
    assert st["closed"] == 1 and st["超额口径"]["超额样本"] == 1
    from stock_agent.memory import Memory
    Memory()  # 二次打开：迁移幂等
