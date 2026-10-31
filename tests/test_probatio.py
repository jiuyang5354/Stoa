from stock_agent import probatio as pb


def test_wilson_and_overlap():
    lo, hi = pb.wilson(27, 51)
    assert lo < 50 < hi  # 51 次 53% 和抛硬币分不开
    assert pb.overlap_factor(20, 5) == 4
    assert pb.p_hit_eff(60, 100, 20, 5) > pb.p_hit_eff(60, 100, 5, 5)  # 重叠越多越不显著
    rows = [{"symbol": "A", "date": f"2026-01-{d:02d}"} for d in range(1, 13)]
    assert len(pb.nonoverlap(rows, 20, 5)) == 3


def test_holm_and_dsr():
    assert pb.holm([0.01, 0.04, 0.5]) == [0.03, 0.08, 0.5]
    assert pb.expected_max_sharpe(1, 0.04) == 0 and pb.expected_max_sharpe(20, 0.04) > pb.expected_max_sharpe(5, 0.04)
    one = pb.deflated_sharpe(0.2, 100, 1, 0.01)
    many = pb.deflated_sharpe(0.2, 100, 50, 0.01)
    assert one > 0.95 > many


def test_trials_dedup_and_filters():
    def run(i, p, agent="llm", curve=None, **params):
        return {"id": f"r{i}", "status": "done", "mode": "timing", "params": {"agent": agent, "horizons": [5], "step": 5, **params},
                "results": {"agent": {"全部": {"horizons": {"5": {"p_binom": p, "hit_dir%": 50 + i}}, "curve": curve or []}}}}
    runs = [run(1, 0.04), run(1, 0.04), run(2, 0.3), run(3, 0.01, agent="baseline"), run(4, 0.001, llm_kind="mock")]
    t = pb.trials(runs)
    assert t["n"] == 2 and t["best"]["p_holm"] == 0.08 and not t["any_significant"]
    assert "不再显著" in t["verdict"]
