"""战报长图：把早报 / 晚报排成一张 1080×1920 的竖版图（四角题签，先看风险，再看指数、自选和判断账本）。

- data()：从行情快照 + AI 点评 + 判断账本算出版面要的字段，全是确定性计算，不再调模型。
- html()：生成单页 HTML（字体走 /assets/）；网页里点「存为图片」在浏览器本地转成 PNG，不上传任何东西。
- png()：命令行可选，用 Playwright 截图；没装就只写 HTML。
"""
from __future__ import annotations

import html as _h
import re
from datetime import datetime

from .report import CN_TZ

KIND = {"morning": ("Matutina", "早报", "Prima luce"), "evening": ("Vesper", "晚报", "Notitia diei"),
        "adhoc": ("Instantia", "快照", "Hic et nunc")}
WEEK = "一二三四五六日"
BENCH = {"A股": ["000300.SS", "000001.SS"], "港股": ["^HSI"], "美股": ["^GSPC"]}
RISKY = ("单日大跌", "跌破 20 日线", "创 52 周新低", "RSI 超买", "RSI 超卖", "单日大涨")


def roman(n: int) -> str:
    out = ""
    for v, s in ((1000, "M"), (900, "CM"), (500, "D"), (400, "CD"), (100, "C"), (90, "XC"), (50, "L"), (40, "XL"),
                 (10, "X"), (9, "IX"), (5, "V"), (4, "IV"), (1, "I")):
        while n >= v:
            out, n = out + s, n - v
    return out


def _num(v, d=2):
    return "—" if v is None else f"{v:,.{d}f}"


def _pct(v):
    return "—" if v is None else f"{v:+.2f}%".replace("-", "−")


def _pick_indices(snap: dict) -> list[dict]:
    out = []
    for m, rows in (snap.get("indices") or {}).items():
        rows = [r for r in rows if r.get("data")]
        if not rows:
            continue
        pref = BENCH.get(m, [])
        r = next((r for s in pref for r in rows if r["symbol"] == s), rows[0])
        out.append({"market": m, "name": r["name"], "close": r["data"].get("close"), "chg": r["data"].get("chg_pct")})
    return out[:3]


def _headline(ix: list[dict], ai: str | None, risks: list[dict]) -> str:
    if ai:  # 点评第一句，去掉 markdown 和引用编号
        t = re.sub(r"\[\d+(?:[,，]\s*\d+)*\]|[#*_`>\[\]]|〔[^〕]*〕", "", ai).strip()
        s = re.split(r"(?<=[。！？!?])", t, maxsplit=1)[0].strip()
        if 6 <= len(s) <= 60:
            return s
    ch = [i for i in ix if i["chg"] is not None]
    if not ch:
        return "今天没有拿到行情。"
    up = [i for i in ch if i["chg"] > 0]
    tone = "三地齐涨" if len(up) == len(ch) == 3 else "三地齐跌" if not up and len(ch) == 3 else "涨跌互现"
    lead = max(ch, key=lambda i: abs(i["chg"]))
    s = f"{tone}，{lead['market']}{'领涨' if lead['chg'] > 0 else '领跌'}。"
    if not risks:
        return s + "自选里没有触发风险信号。"
    if len(risks) == 1:
        r = risks[0]
        return s + f"先看风险：{r['name']}{r['text'].split('，')[0]}。"
    return s + f"先看风险：{'、'.join(r['name'] for r in risks[:2])}{'等 ' + str(len(risks)) + ' 只' if len(risks) > 2 else ''}触发信号。"


def _risks(snap: dict) -> list[dict]:
    out = []
    for r in snap.get("watchlist") or []:
        d = r.get("data") or {}
        sig = [s for s in d.get("signals", []) if s in RISKY]
        if not sig:
            continue
        out.append({"name": r["name"], "text": f"{'、'.join(sig)}，收 {_num(d.get('close'))}，当日 {_pct(d.get('chg_pct'))}，20 日 {_pct(d.get('chg_20d'))}。",
                    "w": abs(d.get("chg_pct") or 0) + 10 * ("跌" in "".join(sig))})
    return sorted(out, key=lambda x: -x["w"])[:3]


def data(snap: dict, session: str = "evening", ai: str | None = None, stats: dict | None = None,
         when: datetime | None = None, sources: str = "腾讯证券 · 东方财富 · Yahoo") -> dict:
    now = when or datetime.now(CN_TZ)
    ix = _pick_indices(snap)
    risks = _risks(snap)
    wl = [{"name": r["name"], "symbol": r["symbol"], "close": (r.get("data") or {}).get("close"),
           "chg": (r.get("data") or {}).get("chg_pct")} for r in (snap.get("watchlist") or []) if r.get("data")]
    wl.sort(key=lambda x: -abs(x["chg"] or 0))
    st = stats or {}
    sc = st.get("超额口径") or {}
    asof = max([(r.get("data") or {}).get("date", "") for rows in (snap.get("indices") or {}).values() for r in rows] or [""])
    return {"session": session, "kind": KIND.get(session, KIND["adhoc"]),
            "roman": f"{roman(now.day)} · {roman(now.month)} · {roman(now.year)}",
            "date": f"{now.year} 年 {now.month} 月 {now.day} 日 · 星期{WEEK[now.weekday()]} · 北京时间 {now:%H:%M}",
            "headline": _headline(ix, ai, risks), "risks": risks, "indices": ix, "watch": wl[:5],
            "calls": {"rate": st.get("准确率%"), "closed": st.get("closed", 0), "open": st.get("open", 0),
                      "alpha": sc.get("看涨平均超额%", sc.get("平均方向分%"))},
            "asof": asof, "sources": sources}


def _hl(s: str) -> str:
    s = _h.escape(s)
    return s.replace("先看风险", "<em>先看风险</em>")


def html(d: dict, save: bool = True) -> str:
    e = _h.escape
    k = d["kind"]
    risks = "".join(f"<li><i>{roman(i + 1)}</i><span><b>{e(r['name'])}</b> {e(r['text'])}</span></li>" for i, r in enumerate(d["risks"])) \
        or '<li class="calm"><i>—</i><span>自选里今天没有触发跌破均线、大跌或极端 RSI。</span></li>'
    cls = lambda v: "" if v is None else ("up" if v > 0 else "dn" if v < 0 else "")
    ix = "".join(f'<div><small>{e(i["name"])}</small><b>{_num(i["close"])}</b><span class="{cls(i["chg"])}">{_pct(i["chg"])}</span></div>' for i in d["indices"])
    wl = "".join(f'<li><span>{e(w["name"])}<small>{e(w["symbol"])}</small></span><em>{_num(w["close"])}</em><span class="{cls(w["chg"])}">{_pct(w["chg"])}</span></li>' for w in d["watch"])
    c = d["calls"]
    calls = (f'<span><b>{c["rate"]:.0f}%</b>命中率 · 已结算 {c["closed"]}</span>' if c["closed"] else '<span><b>—</b>还没有到期结算的判断</span>') \
        + (f'<span><b>{_pct(c["alpha"]).replace(".00", "")}</b>平均超额</span>' if c["alpha"] is not None else "") + f'<span><b>{c["open"]}</b>待复盘</span>'
    sec = lambda n, t, body: f'<div class="lbl"><i>{n}</i>{t}</div>{body}'
    body = sec("I", "PERICULA · 风险", f'<ol class="rk">{risks}</ol>')
    if ix:
        body += sec("II", "INDICES · 指数", f'<div class="ix">{ix}</div>')
    if wl:
        body += sec("III", "ELECTA · 自选", f'<ol class="wl">{wl}</ol>')
    body += sec("IV", "RATIONES · 判断账本", f'<div class="calls">{calls}</div>')
    return TEMPLATE.replace("{{BODY}}", body).replace("{{HEAD}}", _hl(d["headline"])).replace("{{ROMAN}}", e(d["roman"])) \
        .replace("{{DATE}}", e(d["date"])).replace("{{K0}}", e(k[0])).replace("{{K1}}", e(k[1])).replace("{{K2}}", e(k[2])) \
        .replace("{{ASOF}}", e(d["asof"] or "—")).replace("{{SRC}}", e(d["sources"])) \
        .replace("{{SAVE}}", SAVE if save else "").replace("{{NAME}}", e(f"stoa-{d['session']}-{d['asof'] or 'today'}"))


def png(page_html: str, out) -> bool:
    """命令行出图：有 Playwright 就截 PNG，返回 True；否则 False（调用方只留 HTML）。"""
    try:
        from playwright.sync_api import sync_playwright
    except Exception:
        return False
    from pathlib import Path
    from .memory import home  # noqa: F401  确保数据目录存在
    assets = (Path(__file__).parent / "assets").as_uri()
    src = page_html.replace("url(/assets/", f"url({assets}/")
    tmp = Path(out).with_suffix(".html")
    tmp.write_text(src, encoding="utf-8")
    try:
        with sync_playwright() as p:
            b = p.chromium.launch()
            pg = b.new_page(viewport={"width": 1080, "height": 1920})
            pg.goto(tmp.as_uri())
            pg.wait_for_timeout(800)
            pg.locator(".p").screenshot(path=str(out))
            b.close()
        return True
    except Exception:
        return False


TEMPLATE = """<!doctype html>
<html lang="zh"><meta charset="utf-8"><meta name="viewport" content="width=1080"><title>Stoa · {{K1}}</title>
<style>
@font-face{font-family:"Optimus Princeps";src:url(/assets/optimus-princeps.woff2) format("woff2")}
@font-face{font-family:"Linux Libertine";src:url(/assets/libertine-regular.woff2) format("woff2")}
@font-face{font-family:"Linux Libertine";src:url(/assets/libertine-italic.woff2) format("woff2");font-style:italic}
@font-face{font-family:"Alte Haas Grotesk";src:url(/assets/alte-haas-grotesk.woff2) format("woff2")}
:root{--ink:#181715;--ink-2:#4A4640;--mute:#8A847A;--gold-rgb:176,146,89;--gold:#B09259;--up:#B3543A;--down:#56684F;
  --serif:"Linux Libertine","Noto Serif CJK SC","Noto Serif SC","Songti SC","SimSun",serif;--display:"Optimus Princeps","Noto Serif CJK SC",serif;--sans:"Alte Haas Grotesk","Noto Sans CJK SC","Microsoft YaHei",sans-serif}
*{box-sizing:border-box}
body{margin:0;background:#E9E6DF;display:flex;flex-direction:column;align-items:center;gap:20px;padding:24px 0}
.p{position:relative;width:1080px;height:1920px;flex:none;background:#F5F2EB url(/assets/travertine009_color.jpg) center/1400px;color:var(--ink);font-family:var(--sans);font-variant-numeric:lining-nums tabular-nums;overflow:hidden}
.p::before{content:"";position:absolute;inset:0;background:rgba(245,242,235,.86)}
main,.c{z-index:1}
.c{position:absolute;font:500 15px/1.4 var(--sans);letter-spacing:.2em;text-transform:uppercase;color:var(--mute)}
.c b{color:var(--ink);font-weight:500}
.tl{top:64px;left:80px}.tr{top:64px;right:80px;text-align:right}.bl{bottom:64px;left:80px}.br{bottom:64px;right:80px;text-align:right}
.tl b{font:500 20px/1 var(--display);letter-spacing:.42em}
.tr i,.br i{font:italic 17px var(--serif);letter-spacing:.02em;text-transform:none;color:var(--gold)}
main{position:absolute;left:80px;right:80px;top:200px}
.date{font:400 30px/1 var(--display);letter-spacing:.3em;color:var(--ink-2)}
.date small{display:block;margin-top:14px;font:400 18px var(--serif);letter-spacing:.04em;color:var(--mute)}
h1{margin:72px 0 0;font:400 58px/1.38 var(--serif);letter-spacing:.01em}
h1 em{font-style:normal;color:var(--up)}
.rule{height:1px;background:var(--gold);margin:72px 0 0;width:120px}
.lbl{margin:56px 0 22px;font:500 14px var(--sans);letter-spacing:.32em;color:var(--mute);display:flex;gap:18px;align-items:baseline}
.lbl::after{content:"";flex:1;border-top:1px solid rgba(var(--gold-rgb),.55)}
.lbl i{font:400 18px var(--display);font-style:normal;color:var(--gold);letter-spacing:.1em}
.ix{display:grid;grid-template-columns:repeat(3,1fr)}
.ix div{padding:6px 0 0 24px;border-left:1px solid rgba(var(--gold-rgb),.25)}.ix div:first-child{padding-left:0;border-left:0}
.ix small{font:400 19px var(--serif);color:var(--ink-2)}
.ix b{display:block;margin:12px 0 6px;font:400 46px/1 var(--serif)}
.ix span{font:500 19px var(--sans)}
.up{color:var(--up)}.dn{color:var(--down)}
ol{list-style:none;margin:0;padding:0}
.rk li{display:grid;grid-template-columns:60px 1fr;padding:18px 0;border-top:1px solid rgba(24,23,21,.1);font:400 25px/1.55 var(--serif);color:var(--ink-2)}
.rk li:first-child{border-top:0}.rk li.calm{color:var(--mute)}
.rk li i{font:400 21px/1.55 var(--display);font-style:normal;color:var(--gold)}
.rk li b{color:var(--ink);font-weight:400}
.wl li{display:grid;grid-template-columns:1fr 170px 130px;align-items:baseline;padding:15px 0;border-top:1px solid rgba(24,23,21,.1);font:400 25px var(--serif)}
.wl li:first-child{border-top:0}
.wl small{font:400 16px var(--sans);letter-spacing:.06em;color:var(--mute);margin-left:12px}
.wl em{font-style:normal;text-align:right}.wl li>span:last-child{text-align:right;font:500 21px var(--sans)}
.calls{display:flex;gap:56px;font:400 21px var(--serif);color:var(--ink-2)}
.calls b{display:block;font:400 44px/1.1 var(--serif);color:var(--ink);font-weight:400}
.bar{display:flex;gap:18px;font:500 13px var(--sans);letter-spacing:.2em;color:#8A847A}
.bar button{border:1px solid #181715;background:#181715;color:#F5F2EB;font:inherit;letter-spacing:.2em;padding:10px 18px;cursor:pointer}
.bar button:disabled{opacity:.5;cursor:wait}
@media print{.bar{display:none}body{padding:0;background:none}}
</style>
<div class="p" id="poster">
  <div class="c tl"><b>STOA</b></div>
  <div class="c tr">{{K0}} · {{K1}}<br><i>{{K2}}</i></div>
  <main>
    <div class="date">{{ROMAN}}<small>{{DATE}}</small></div>
    <h1>{{HEAD}}</h1>
    <div class="rule"></div>
    {{BODY}}
  </main>
  <div class="c bl">数据 · {{SRC}}<br>行情日 {{ASOF}}</div>
  <div class="c br">仅供参考 · 不构成投资建议<br><i>Ad usum cotidianum</i></div>
</div>
{{SAVE}}
</html>"""

# 本地存图：把字体、底图内联成 data URL，放进 SVG foreignObject 画到 canvas，再导出 PNG。全程在浏览器里，不上传。
SAVE = """<div class="bar"><button id="sv" type="button">存为图片 · PNG</button><span>1080 × 1920 · 在本机生成</span></div>
<script>
const toData = async u => { const b = await (await fetch(u)).blob(); return await new Promise(r => { const f = new FileReader(); f.onload = () => r(f.result); f.readAsDataURL(b); }); };
document.getElementById('sv').onclick = async ev => {
  const btn = ev.currentTarget; btn.disabled = true; btn.textContent = '生成中…';
  try {
    let css = document.querySelector('style').textContent.replace(/\\.bar[^}]*}/g, '');
    for (const u of [...new Set(css.match(/\\/assets\\/[\\w.-]+/g) || [])]) css = css.split(u).join(await toData(u));
    const node = new XMLSerializer().serializeToString(document.getElementById('poster'));  // 必须是 XHTML（<br/>），否则 SVG 解不开
    const svg = `<svg xmlns="http://www.w3.org/2000/svg" width="1080" height="1920"><foreignObject width="100%" height="100%"><div xmlns="http://www.w3.org/1999/xhtml"><style>${css.replace(/</g, "&lt;").replace(/&(?!lt;)/g, "&amp;")}</style>${node}</div></foreignObject></svg>`;
    const img = new Image(); img.src = 'data:image/svg+xml;charset=utf-8,' + encodeURIComponent(svg);
    await img.decode(); await new Promise(r => setTimeout(r, 300));
    const cv = document.createElement('canvas'); cv.width = 1080 * 2; cv.height = 1920 * 2;
    const g = cv.getContext('2d'); g.scale(2, 2); g.drawImage(img, 0, 0);
    const a = document.createElement('a'); a.download = '{{NAME}}.png'; a.href = cv.toDataURL('image/png'); a.click();
    btn.textContent = '已保存';
  } catch (e) { btn.textContent = '没存成：' + (e.message || e); }
  setTimeout(() => { btn.disabled = false; btn.textContent = '存为图片 · PNG'; }, 2400);
};
</script>"""
