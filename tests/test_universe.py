"""股票池：沪深 300 成分解析、Yahoo 代码、确定性抽样、历史/当前成分选择。离线。"""
import io

import pandas as pd
import pytest

from stock_agent import universe as U


@pytest.fixture(autouse=True)
def _home(tmp_path, monkeypatch):
    monkeypatch.setenv("STOCK_AGENT_HOME", str(tmp_path))


def xls(date, codes):
    df = pd.DataFrame({"日期Date": [date] * len(codes), "指数代码 Index Code": [300] * len(codes),
                       "成份券代码Constituent Code": codes, "成份券名称Constituent Name": [f"名{c}" for c in codes],
                       "成份券英文名称Constituent Name(Eng)": ["x"] * len(codes)})
    b = io.BytesIO()
    df.to_excel(b, index=False)
    return b.getvalue()


def test_yahoo_symbol():
    assert U.yahoo_symbol(1) == "000001.SZ" and U.yahoo_symbol("600519") == "600519.SS" and U.yahoo_symbol(300750) == "300750.SZ"
    assert U.yahoo_symbol(688981) == "688981.SS"


def test_members_prefers_historical_cache_and_flags_current():
    (U.udir() / "000300cons_20250620.xls").write_bytes(xls(20250620, [1, 600519, 300750]))
    (U.udir() / "000300cons_20261029.xls").write_bytes(xls(20261029, [1, 600519, 688981]))
    m = U.members("2025-09-24", online=False)
    assert m["survivorship"] == "historical" and m["date"] == "20250620" and {x["symbol"] for x in m["members"]} == {"000001.SZ", "600519.SS", "300750.SZ"}
    m2 = U.members("2024-01-02", online=False)  # 只有更晚的 → 当前成分，标注幸存者偏差
    assert m2["survivorship"] == "current"


def test_sample_deterministic_and_independent_of_order():
    mem = [{"symbol": f"{600000 + i}.SS", "name": str(i)} for i in range(300)]
    a, b = U.sample(mem, 100, 7), U.sample(list(reversed(mem)), 100, 7)
    assert a == b and len(a) == 100 and U.sample(mem, 100, 8) != a
