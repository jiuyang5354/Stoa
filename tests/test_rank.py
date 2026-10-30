"""相对强弱排序评估（rank.py）与 point-in-time 公告/财报（pit_events.py）。全部离线。"""
import json
import math

import pandas as pd
import pytest

from stock_agent import backtest as bt
from stock_agent import pit_events as pe
from stock_agent import rank


@pytest.fixture(autouse=True)
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("STOCK_AGENT_HOME", str(tmp_path))
    yield tmp_path


def test_stats_helpers():
    assert rank.spearman([1, 2, 3, 4], [1, 3, 2, 4]) == pytest.approx(0.8)
    assert rank.spearman([1, 2, 3], [3, 2, 1]) == pytest.approx(-1)
    assert rank.rankdata([10, 20, 20, 5]) == [2, 3.5, 3.5, 1]
    assert rank.t_pvalue(2.0, 30) == pytest.approx(0.0546, abs=1e-3)
    assert rank.t_pvalue(1.96, 100000) == pytest.approx(0.05, abs=1e-3)
    assert rank.t_pvalue(0.0, 20) == pytest.approx(1.0, abs=1e-3)
    # 正相关序列的 NW 标准误比朴素的大
    x = [1, 1, 1, 1, -1, -1, -1, -1] * 5
    naive = (sum((v - 0) ** 2 for v in x) / (len(x) - 1)) ** .5 / len(x) ** .5
    assert rank.nw_se(x, 3) > naive
    s = rank.summarize([0.1, 0.2, 0.15, 0.05, 0.12, 0.3, 0.08, 0.11, 0.2, 0.1], lag=1, block=2)
    assert s["mean"] > 0 and s["p"] < 0.01 and s["ci95"][0] > 0


def _xs(n_days=30, n=12, seed=1):
    import random
    rnd = random.Random(seed)
    out = []
    for i in range(n_days):
        d = pd.Timestamp("2026-01-05") + pd.Timedelta(days=7 * i)
        syms = bt.DEFAULT_SYMBOLS[:n]
        fwd = {s: {20: rnd.gauss(0, 5), 5: rnd.gauss(0, 2)} for s in syms}
        out.append((d, syms, fwd))
    return out


def test_evaluate_perfect_and_inverse_scores():
    xs = _xs()
    perfect = [(d, {s: f[s][20] for s in syms}, f) for d, syms, f in xs]
    inverse = [(d, {s: -f[s][20] for s in syms}, f) for d, syms, f in xs]
    rp = rank.evaluate(perfect, [20], step=5)["20"]
    ri = rank.evaluate(inverse, [20], step=5)["20"]
    assert rp["ic"]["mean"] == pytest.approx(1.0) and rp["long_short%"]["mean"] > 0
    assert ri["ic"]["mean"] == pytest.approx(-1.0) and ri["long_short%"]["mean"] < 0
    assert rp["nw_lag"] == 3 and rp["block"] == 4
    pr = rank.paired(rank.evaluate(perfect, [20], 5), rank.evaluate(inverse, [20], 5), "20", 3, 4)
    assert pr["mean"] == pytest.approx(2.0)


class FakeLLM:
    ready = True

    def __init__(self, mode="good"):
        self.mode, self.calls, self.seen = mode, 0, []

    def chat(self, msgs, tools=None, temperature=0.0, timeout=0):
        self.calls += 1
        self.seen.append(msgs[-1]["content"])
        body = json.loads(msgs[-1]["content"])
        labs = list(body["股票"])
        if self.mode == "partial":
            labs = labs[:3]
        return {"content": json.dumps({"scores": {l: 50 + i for i, l in enumerate(labs)}, "reason": "测试"})}


def test_llm_ranker_blind_payload_cache_and_shuffle(home):
    closes, _ = bt.load_prices(bt.DEFAULT_SYMBOLS, "2025-06-01", "2026-03-01", synthetic=True)
    pit = bt.PointInTime(closes)
    xs = rank.cross_sections(pit, "2025-10-01", "2026-01-30", 10, [20, 5])
    assert xs and all(c.index.max() <= d for d, cs, _ in xs for c in cs.values())  # 截断，无未来行
    llm = FakeLLM()
    r = rank.LLMRanker(llm=llm, horizon=20, max_calls=50)
    d, cs, _ = xs[0]
    sc = r(d, cs)
    assert set(sc) == set(cs)
    txt = llm.seen[0]
    for s in cs:
        assert s not in txt  # 盲测：不出现代码
    assert f"{d:%Y-%m-%d}" not in txt and "2025" not in txt and "2026" not in txt
    r(d, cs)
    assert llm.calls == 1 and r.stats["cache_hits"] == 1
    lab1, lab2 = r.labels(xs[0][0], cs), r.labels(xs[1][0], cs)
    assert lab1 != lab2  # 每天打乱代号


def test_llm_ranker_partial_answer_counts_as_error(home):
    closes, _ = bt.load_prices(bt.DEFAULT_SYMBOLS, "2025-06-01", "2026-03-01", synthetic=True)
    xs = rank.cross_sections(bt.PointInTime(closes), "2025-10-01", "2025-11-30", 10, [20])
    r = rank.LLMRanker(llm=FakeLLM("partial"), horizon=20)
    assert r(xs[0][0], xs[0][1]) is None and r.stats["errors"] == 1


def test_run_rank_mock_end_to_end(home):
    res = rank.run_rank(start="2025-10-01", end="2026-03-01", step=5, horizons=[20, 5], synthetic=True, llm_kind="mock",
                        random_seeds=10, concurrency=1)
    assert res["params"]["scored_days"] > 10
    for k in ("agent", "momentum20", "reversal5", "random"):
        assert res["results"][k]["20"]["ic"]["n"] > 5
    assert (bt.bt_dir() / f"{res['id']}.json").exists()
    assert "agent_vs_momentum20" in res["paired"]["20"]


EV = {"symbol": "600519.SS", "market": "A", "announcements": [
    {"pub": "2026-03-10", "time": "2026-03-09 20:00:00", "title": "贵州茅台:贵州茅台关于回购股份的公告", "type": ""},
    {"pub": "2026-03-20", "time": "2026-03-20 16:30:00", "title": "贵州茅台:2025年年度报告", "type": ""},  # 决策日当天收盘后
    {"pub": "2026-03-25", "time": "2026-03-25", "title": "贵州茅台:未来的公告", "type": ""},
    {"pub": "2026-03-12", "time": "2026-03-12", "title": "贵州茅台:第四届董事会决议公告", "type": ""}],
    "financials": [{"pub": "2026-04-25", "period": "2026一季报", "营收同比%": 9.9},
                   {"pub": "2025-10-25", "period": "2025三季报", "营收同比%": 6.3}]}


def test_pit_asof_strictly_before_and_by_disclosure_date():
    v = pe.asof(EV, "2026-03-20", names=["贵州茅台", "茅台"])
    titles = [i["标题"] for i in v["近期公告"]]
    assert titles == ["该公司关于回购股份的公告"]  # 当天发布的、未来的、例行董事会决议都不给
    assert v["最近已披露财报"]["period"] == "2025三季报"  # 一季报 4 月才披露，按披露日而不是报告期
    assert pe.leak_check(EV, "2026-03-20") and pe.leak_check(EV, "2026-05-01")
    assert pe.asof(EV, "2026-04-26")["最近已披露财报"]["period"] == "2026一季报"


def test_relative_years_and_redact():
    t = rank.relative_years("截至2025年12月31日止年度业绩；二零二五年末期股息；2026一季报；2019年", 2026)
    assert "2025" not in t and "2026" not in t and "二零二五" not in t and "去年" in t and "今年" in t and "某年" in t
    assert pe.redact("香港上海汇丰银行 HSBC 公告", ["汇丰", "HSBC"]) == "香港上海该公司银行 该公司 公告"


def test_hk_results_date_ignores_board_meeting_notice(monkeypatch):
    rows = {"result": {"data": [{"REPORT_DATE": "2025-12-31 00:00:00", "REPORT_TYPE": "2025年年报", "OPERATE_INCOME_YOY": 3.2}]}}

    class R:
        def json(self):
            return rows
    monkeypatch.setattr(pe, "_req", lambda *a, **k: R())
    anns = [{"pub": "2026-02-10", "title": "董事会会议召开日期 - 审议2025年末期业绩"},
            {"pub": "2026-02-25", "title": "截至2025年12月31日止年度全年业绩公布"}]
    f = pe._hk_financials("0005.HK", anns)
    assert f[0]["pub"] == "2026-02-25" and f[0]["pub_basis"] == "业绩公告日"
    f = pe._hk_financials("0005.HK", anns[:1])
    assert f[0]["pub"] == "2026-03-31" and "保守" in f[0]["pub_basis"]  # 找不到业绩公告：按期限


def test_events_payload_blind(home):
    closes, _ = bt.load_prices(["600519.SS", "000858.SZ", "601318.SS", "300750.SZ", "AAPL", "MSFT"], "2025-06-01", "2026-05-01", synthetic=True)
    xs = rank.cross_sections(bt.PointInTime(closes), "2026-03-20", "2026-03-20", 1, [20])
    llm = FakeLLM()
    r = rank.LLMRanker(llm=llm, horizon=20, events=True, events_loader=lambda s: EV if s == "600519.SS" else {"market": "A"})
    r(xs[0][0], xs[0][1])
    txt = llm.seen[0]
    assert "茅台" not in txt and "回购" in txt and "未来的公告" not in txt and "2025" not in txt
    assert r.stats["events_items"] >= 2
