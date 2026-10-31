from stock_agent.memory import Memory, position_state


def test_hypothetical_and_historical_positions_are_not_current(tmp_path):
    mem = Memory(tmp_path)
    mem.add("user", "假如我买入腾讯 0700.HK 200 股，成本 400")
    mem.add("user", "已经清仓茅台 600519.SS，以前持有 100 股")
    assert [position_state(x) for x in mem.entries("user")] == ["planned", "historical"]
    from stock_agent.debate import holdings_check
    result = holdings_check(mem.entries("user"), "0700.HK", {"腾讯"}, None)
    assert not any("你已持有" in x for x in result["findings"])


def test_agent_position_requires_confirmation(tmp_path):
    mem = Memory(tmp_path)
    mem.add("user", "持有腾讯 0700.HK 200 股", source="agent")
    assert position_state(mem.entries("user")[0]) == "pending"
    mem.replace("user", "持有腾讯", "【已确认持仓】持有腾讯 0700.HK 200 股", source="user")
    assert position_state(mem.entries("user")[0]) == "current"


def test_change_history_and_undo_survive_reopen(tmp_path):
    mem = Memory(tmp_path)
    mem.add("memory", "第一条笔记")
    mem.add("memory", "第二条笔记")
    change = mem.changes()[0]
    assert change["target"] == "memory" and change["source"] == "user"
    mem = Memory(tmp_path)
    assert "已撤销" in mem.undo_change(change["id"])
    assert len(mem.entries("memory")) == 1 and "第一条" in mem.entries("memory")[0]


def test_undo_does_not_overwrite_newer_edits(tmp_path):
    mem = Memory(tmp_path)
    mem.add("memory", "第一条笔记")
    old = mem.changes()[0]["id"]
    mem.add("memory", "第二条笔记")
    assert "后续修改" in mem.undo_change(old)
    assert len(mem.entries("memory")) == 2
