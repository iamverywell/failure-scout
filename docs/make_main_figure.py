"""Framework figure (docs/img/framework.png). Usage: uv pip install -e '.[docs]'; python docs/make_main_figure.py"""
import os
HERE = os.path.dirname(os.path.abspath(__file__))
exec(open(os.path.join(HERE, "draw.py")).read())
# each point: (bold keyword, [detail lines]); two rows of four boxes
row1 = [
 ("WatchProfile", GR, GRS, [
   ("Failure types", ["trial failure, clinical hold,", "CRL, business discontinuation"]),
   ("Scope set in the prompts", ["drugs and biologics, all areas", "and phases, date window"])]),
 ("Fetch", PUB, PUS, [
   ("Sources", ["ClinicalTrials.gov, RSS feeds,", "5 fixed Google News queries"]),
   ("Agent adds searches", ["for thin event types,", "≤ 2 with results"]),
   ("Empty result", ["new words, ≤ 3 empty per source"])]),
 ("Dedupe", OR, ORS, [
   ("Same URL kept once", []),
   ("Seen URLs skipped", ["from all earlier runs"]),
   ("≤ 500 new items per run", ["the rest wait for the next run"])]),
 ("Classify", OR, ORS, [
   ("5 items per call", ["agent"]),
   ("Structured output", ["failure type, fields,", "verbatim quote, confidence"])]),
]
row2 = [
 ("Verify", OR, ORS, [
   ("Quotes the source", ["not verbatim → confidence ≤ 40"]),
   ("Verify with ClinicalTrials.gov", ["status, why stopped;", "conflict flagged"])]),
 ("Store", GR, GRS, [
   ("SQLite", ["runs, seen items, events, edits"]),
   ("Same key → one row", ["NCT id, else drug + sponsor", "+ type, else URL"]),
   ("Higher confidence wins", ["a later report replaces fields", "only if more confident"])]),
 ("Merge", OR, ORS, [
   ("Rule merge", ["same drug + company,", "within 30 days"]),
   ("Agent merge", ["groups reports in", "14-day windows"]),
   ("Different NCT ids", ["never merged; duplicates kept", "with merged_into"])]),
 ("Dashboard", GR, GRS, [
   ("Google Sheet", ["pull human edits first, then", "push events, companies, monthly"]),
   ("Dashboard tab", ["KPIs, monthly chart,", "company chart, latest 30 events"])]),
]
bw, gap, x0, y0 = 380, 70, 40, 150
hh, lb, ld, pgap = 74, 30, 26, 22
rgap = 120                                   # vertical space between the rows
def body_h(pts): return 30 + sum(lb + ld*len(d) + pgap for _, d in pts)
body = max(body_h(bx[3]) for bx in row1 + row2)
bh = hh + body
W = x0*2 + 4*bw + 3*gap
y1 = y0 + bh + rgap
H = y1 + bh + 110
b = text(W/2, 90, "Trial Failure Scout: framework", 48)
def draw_box(x, y, name, fill, stroke, pts):
    out = rect(x, y, bw, hh, fill, stroke, r=12)
    out += text(x + bw/2, y + 49, name, 32)
    out += rect(x, y + hh, bw, body, "#ffffff", "#bbbbbb", r=0, sw=1.2, shadow=False)
    yy = y + hh + 46
    for kw, det in pts:
        out += text(x + 18, yy, "• " + kw, 22, "bold", "#111", anchor="start")
        for k, ln in enumerate(det):
            out += text(x + 38, yy + ld*(k + 1), ln, 19, "normal", "#555", anchor="start")
        yy += lb + ld*len(det) + pgap
    return out
xs = [x0 + i*(bw + gap) for i in range(4)]
for i, bx in enumerate(row1):
    b += draw_box(xs[i], y0, *bx)
for i, bx in enumerate(row2):
    b += draw_box(xs[i], y1, *bx)
m1, m2 = y0 + bh/2, y1 + bh/2
for i in range(3):
    b += arrow([(xs[i] + bw + 4, m1), (xs[i + 1] - 4, m1)], w=3.6)
    b += arrow([(xs[i] + bw + 4, m2), (xs[i + 1] - 4, m2)], w=3.6)
# Classify (end of row 1) -> Verify (start of row 2)
ymid = y0 + bh + rgap/2
b += arrow([(xs[3] + bw/2, y0 + bh + 4), (xs[3] + bw/2, ymid), (xs[0] + bw/2, ymid), (xs[0] + bw/2, y1 - 4)], w=3.6)
# human edits flow back from the sheet into SQLite
yb = y1 + bh + 45
b += arrow([(xs[3] + bw/2, y1 + bh + 4), (xs[3] + bw/2, yb), (xs[1] + bw/2, yb), (xs[1] + bw/2, y1 + bh + 6)],
           RED, "10 7", w=3)
b += text((xs[1] + xs[3] + bw)/2, yb + 34, "human edits pulled back into SQLite", 21, "bold", RED)
s = svg(W, H, b, border="#d89a00", bg="#fffdf8")
out = os.path.join(HERE, "img", "framework")
open(out + ".svg", "w").write(s)
d = fitz.open(out + ".svg"); d[0].get_pixmap(dpi=110).save(out + ".png"); print(d[0].rect)
