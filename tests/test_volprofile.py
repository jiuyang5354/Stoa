import math

from stock_agent import volprofile as vp


def _bars(closes, spread=1.0, vol=1000.0):
    out = []
    for i, c in enumerate(closes):
        out.append({"t": f"2026-01-{i + 1:02d}", "o": c, "h": c + spread, "l": c - spread, "c": c, "v": vol})
    return out


def test_balance_range_is_balance():
    closes = [100 + 3 * math.sin(i / 2.0) for i in range(60)]
    closes[-1] = 100.2
    r = vp.profile(_bars(closes))
    assert r["state"] == "平衡" and r["regime"] == "震荡"
    assert r["val"] <= r["poc"] <= r["vah"]
    assert r["val"] < 100.2 < r["vah"]
    assert abs(sum(p["vol_pct"] for p in r["profile"]) - 100) < 0.1


def test_value_area_holds_70pct():
    closes = [100 + (i % 7) for i in range(50)]
    r = vp.profile(_bars(closes, spread=0.5))
    inside = sum(p["vol_pct"] for p in r["profile"] if r["val"] - 1e-9 <= p["price"] <= r["vah"] + 1e-9)
    assert inside >= 70 - 1e-6
    assert inside < 100  # 不是整个区间


def test_trend_breakout_is_imbalance():
    closes = [100 + 0.5 * math.sin(i) for i in range(30)] + [100 + 1.0 * k for k in range(1, 31)]
    r = vp.profile(_bars(closes))
    assert r["state"] == "向上失衡"
    assert r["regime"] == "趋势"
    assert r["poc_shift_pct"] > 0
    assert r["last5"].count("↑") >= 3


def test_downtrend():
    closes = [100 - 0.8 * k for k in range(60)]
    r = vp.profile(_bars(closes, vol=500))
    assert r["state"] in ("向下失衡", "平衡")
    assert r["poc_shift_pct"] < 0


def test_bimodal_has_hvn_and_lvn():
    closes = [90 + 0.3 * math.sin(i) for i in range(30)] + [110 + 0.3 * math.sin(i) for i in range(30)]
    r = vp.profile(_bars(closes, spread=0.6))
    assert len(r["hvn"]) >= 2
    assert r["lvn"], r
    assert any(r["hvn"][0]["price"] < l["price"] < r["hvn"][-1]["price"] for l in r["lvn"])


def test_volume_weighting_moves_poc():
    closes = [100 + (i % 10) for i in range(40)]
    bars = _bars(closes, spread=0.4)
    for b in bars:
        if b["c"] == 107:
            b["v"] = 50000
    r = vp.profile(bars)
    assert abs(r["poc"] - 107) < 1.0


def test_too_few_and_flat():
    assert "error" in vp.profile(_bars([1, 2, 3]))
    assert "error" in vp.profile([{"t": str(i), "o": 5, "h": 5, "l": 5, "c": 5, "v": 1} for i in range(10)])


def test_tool_uses_fetcher(monkeypatch):
    closes = [50 + math.sin(i / 3) for i in range(60)]
    monkeypatch.setattr(vp, "fetch_bars", lambda s, src, d, i: (_bars(closes, 0.3), {"source": "测试源", "url": "u", "as_of": "2026-01-60", "granularity": "日线"}))
    r = vp.run_tool("volume_profile", {"symbol": "600519.SS"})
    assert r["source"] == "测试源" and "profile" not in r and r["_profile_bins"] == 40
    assert r["state"] in ("平衡", "向上失衡", "向下失衡") and "近似口径" in r["note"]


def test_tool_error_when_no_data(monkeypatch):
    def boom(*a):
        raise LookupError("日线都取不到")
    monkeypatch.setattr(vp, "fetch_bars", boom)
    assert "error" in vp.run_tool("volume_profile", {"symbol": "X"})


def test_registered_in_agent():
    from stock_agent import agent
    assert "volume_profile" in {t["name"] for t in agent.TOOLS}
    assert agent.TOOL_LABELS["volume_profile"]
