"""Failure Scout diagrams in the style of Jam with AI's Job Scout figures (hand-written SVG).
Shared drawing helpers for docs/make_main_figure.py. Needs PyMuPDF: uv pip install -e '.[docs]'."""
import os
import pymupdf as fitz
IMG = os.path.join(os.path.dirname(os.path.abspath(__file__)), "img")
F = "Helvetica"
OR, ORS = "#fcc131", "#c9910a"      # orange node
GRP, GRPS = "#fff4d6", "#c9a24a"    # orange group
GR, GRS = "#d9ead3", "#82a96f"      # green
PU, PUS, PUB = "#ecdcf3", "#9a73b0", "#cf98e3"
BL, BLS, BLB = "#e3eefc", "#7a9bd0", "#7ea6e0"
PK, PKS, PKB = "#fbe3ea", "#d48a9b", "#eb9a9a"
GY, GYS, GYB = "#f4f4f4", "#9e9e9e", "#e3e3e3"
RED = "#e1653a"

def rect(x, y, w, h, fill, stroke, r=10, dash=None, sw=1.5, shadow=True):
    s = ""
    if shadow:
        s += f'<rect x="{x+3}" y="{y+3}" width="{w}" height="{h}" rx="{r}" fill="#00000018"/>'
    d = f' stroke-dasharray="{dash}"' if dash else ""
    return s + f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="{r}" fill="{fill}" stroke="{stroke}" stroke-width="{sw}"{d}/>'

def text(x, y, s, size=22, weight="bold", color="#111", anchor="middle", italic=False):
    st = ' font-style="italic"' if italic else ""
    return f'<text x="{x}" y="{y}" font-family="{F}" font-size="{size}" font-weight="{weight}" fill="{color}" text-anchor="{anchor}"{st}>{s}</text>'

def node(x, y, w, h, label, fill=OR, stroke=ORS, size=22, sub=None):
    s = rect(x, y, w, h, fill, stroke, r=8)
    if sub:
        s += text(x + w/2, y + h/2 - 2, label, size) + text(x + w/2, y + h/2 + 20, sub, 15, "normal", "#333")
    else:
        s += text(x + w/2, y + h/2 + size*0.35, label, size)
    return s

def group(x, y, w, h, title, fill, stroke, size=24):
    return rect(x, y, w, h, fill, stroke, r=22, dash="4 4", sw=1.4, shadow=False) + text(x + w/2, y + 42, title, size)

import math
def _seg_dashes(p, q, dash):
    on, off = dash
    (x1, y1), (x2, y2) = p, q
    L = math.hypot(x2 - x1, y2 - y1)
    if L == 0:
        return ""
    ux, uy = (x2 - x1) / L, (y2 - y1) / L
    out, t = "", 0.0
    while t < L:
        e = min(t + on, L)
        out += f"M{x1+ux*t:.1f},{y1+uy*t:.1f} L{x1+ux*e:.1f},{y1+uy*e:.1f} "
        t = e + off
    return out

def _head(p, q, color, size=13):
    (x1, y1), (x2, y2) = p, q
    a = math.atan2(y2 - y1, x2 - x1)
    pts = [(x2, y2), (x2 - size*math.cos(a) + size*0.5*math.sin(a), y2 - size*math.sin(a) - size*0.5*math.cos(a)),
           (x2 - size*math.cos(a) - size*0.5*math.sin(a), y2 - size*math.sin(a) + size*0.5*math.cos(a))]
    return '<path d="M' + " L".join(f"{x:.1f},{y:.1f}" for x, y in pts) + f' z" fill="{color}"/>'

def arrow(pts, color="#222", dash=None, w=2.2, both=False, label=None, lpos=None, lcolor=None, lsize=19, head=True):
    pts = list(pts)
    hs = 9 + w * 2
    # shorten the last segment so the line ends under the head
    def shorten(a, b, by):
        L = math.hypot(b[0]-a[0], b[1]-a[1]) or 1
        return (b[0] - (b[0]-a[0])/L*by, b[1] - (b[1]-a[1])/L*by)
    draw = pts[:]
    if head:
        draw[-1] = shorten(pts[-2], pts[-1], hs*0.8)
    if both:
        draw[0] = shorten(pts[1], pts[0], hs*0.8)
    if dash:
        on, off = (float(v) for v in dash.split())
        d = "".join(_seg_dashes(a, b, (on, off)) for a, b in zip(draw, draw[1:]))
    else:
        d = " ".join(f"{'M' if i == 0 else 'L'}{x:.1f},{y:.1f}" for i, (x, y) in enumerate(draw))
    s = f'<path d="{d}" fill="none" stroke="{color}" stroke-width="{w}" stroke-linecap="round"/>'
    if head:
        s += _head(pts[-2], pts[-1], color, hs)
    if both:
        s += _head(pts[1], pts[0], color, hs)
    if label:
        lx, ly = lpos
        s += text(lx, ly, label, lsize, "bold", lcolor or color, "start")
    return s

def markers(colors):
    m = ""
    for c in colors:
        k = c.replace("#", "")
        m += (f'<marker id="a{k}" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto">'
              f'<path d="M0,0 L10,5 L0,10 z" fill="{c}"/></marker>'
              f'<marker id="s{k}" viewBox="0 0 10 10" refX="1" refY="5" markerWidth="7" markerHeight="7" orient="auto">'
              f'<path d="M10,0 L0,5 L10,10 z" fill="{c}"/></marker>')
    return f"<defs>{m}</defs>"

def svg(w, h, body, border="#e8c96a", bg="#fffdf6"):
    return (f'<svg xmlns="http://www.w3.org/2000/svg" width="{w}" height="{h}" viewBox="0 0 {w} {h}">'
            + markers(["#222222", RED, "#6f8fd0", "#d06a7f", "#8a8a8a"])
            + f'<rect x="1" y="1" width="{w-2}" height="{h-2}" fill="{bg}" stroke="{border}" stroke-width="3"/>' + body + "</svg>")
