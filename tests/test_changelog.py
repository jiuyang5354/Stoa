from stock_agent import changelog


def test_parse_groups_heads_types_and_boilerplate():
    body = ("## v1.2.0 · X\n\n**小节**\n- 新增 A 功能\n- 修复 B 崩溃\n- **长程研究**：开课题\n- **修复**：角标不再错位\n"
            "\nWindows 用户下载下方 `Stoa-Windows.zip`。仅供参考。\n")
    r = changelog.parse("v1.2.0", "v1.2.0 · Probatio", body, "2026-10-30T16:13:00Z")
    assert r["version"] == "1.2.0" and r["title"] == "Probatio" and r["date"] == "2026-10-31"
    assert [i["type"] for i in r["items"]] == ["added", "fixed", "added", "fixed"]
    assert r["items"][0]["group"] == "小节" and r["items"][2]["head"] == "长程研究" and "head" not in r["items"][3]
    assert all("zip" not in i["text"] for i in r["items"])


def test_patch_default_and_bundle_and_upcoming():
    assert changelog.parse("v0.9.1", "", "- 标题栏对齐", "")["items"][0]["type"] == "fixed"
    d = changelog.load({"available": True, "latest": "99.0.0", "name": "v99.0.0 · Futura", "notes": "- 新增 C", "url": "u"})
    assert d["releases"][0]["upcoming"] and d["releases"][0]["title"] == "Futura"
    assert len(d["releases"]) > 30 and all(r["version"] for r in d["releases"])
