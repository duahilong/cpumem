# -*- coding: utf-8 -*-
"""三型号 CPU 报价对比图（零依赖 SVG，只读查询 cpumem.db）。

上面三块面板各自独立纵轴（避免量级差异压扁低价型号），
最下面一块为“归一化走势”面板（各型号散片首价=100），用于横向对比涨跌幅。
横轴共用，每周一个刻度，月初刻度加粗。
"""
import sqlite3
import datetime
import math
from pathlib import Path

DB = Path(__file__).parent.parent / "database" / "cpumem.db"
OUT = Path(__file__).parent / "cpu_compare.svg"

PRODUCTS = [
    "intel-i5-12400f",
    "intel-i5-13600kf",
    "amd-ryzen-7-9800x-3d",
]
# 归一化面板中每个型号的线条颜色（与“散片/原盒”配色错开）
IDX_COLORS = ["#1f77b4", "#d62728", "#2ca02c"]
COL = {"散片": "#d62728", "原盒": "#1f77b4"}

W, H = 1600, 1420
ML, MR, MT, MB = 110, 200, 80, 70
PH, GAP = 265, 58          # 面板高度 / 面板间距
CW = W - ML - MR

# ---------------- 读数据 ----------------
con = sqlite3.connect(DB)
data: dict[str, dict] = {}
for key in PRODUCTS:
    row = con.execute("SELECT display_name FROM products WHERE product_key = ?", (key,)).fetchone()
    if row is None:
        con.close()
        raise SystemExit(f"products 表中不存在 product_key = {key!r}")
    series: dict[str, list[tuple[datetime.date, float]]] = {"散片": [], "原盒": []}
    for d, pt, price in con.execute(
        """SELECT q.date_key, q.price_type, q.price FROM quotes q
           JOIN products p ON p.product_key = q.product_key
           WHERE p.product_key = ? ORDER BY q.date_key""", (key,)
    ):
        series.setdefault(pt, []).append((datetime.date.fromisoformat(d), price))
    data[key] = {"name": row[0], "series": {k: v for k, v in series.items() if v}}
con.close()

all_dates = sorted({d for info in data.values() for pts in info["series"].values() for d, _ in pts})
d0, d1 = all_dates[0], all_dates[-1]
span = max((d1 - d0).days, 1)


def X(d: datetime.date) -> float:
    return ML + (d - d0).days / span * CW


def week_ticks():
    """每周刻度：首个周一（不早于 d0）起每 7 天一个。"""
    cur = d0 + datetime.timedelta(days=(7 - d0.weekday()) % 7)
    out = []
    while cur <= d1:
        out.append(cur)
        cur += datetime.timedelta(days=7)
    return out


def nice_ticks(lo, hi, target=5):
    """在 [lo, hi] 内生成数量接近 target 的“整齐”刻度值。"""
    cands = [10, 20, 25, 50, 100, 200, 250, 500, 1000, 2000]
    raw = (hi - lo) / max(target, 1)
    step = next((c for c in cands if c >= raw), cands[-1])
    t = math.floor(lo / step) * step
    out = []
    while t <= hi + 1e-9:
        if t >= lo:
            out.append(t)
        t += step
    return out


def text_w(s: str, size: float) -> float:
    """粗估字符串像素宽度（CJK 按全宽、ASCII 按半宽），用于避免标签重叠。"""
    return sum(size if ord(c) > 127 else size * 0.55 for c in s)


P = []  # SVG 片段
P.append(f'<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{H}" viewBox="0 0 {W} {H}" '
         'font-family="Microsoft YaHei, PingFang SC, sans-serif">')
P.append(f'<rect width="{W}" height="{H}" fill="#ffffff"/>')
P.append(f'<text x="{W/2}" y="36" text-anchor="middle" font-size="26" font-weight="bold" fill="#222">'
         f'CPU 报价对比（{d0} ~ {d1}）</text>')
P.append(f'<text x="{W/2}" y="60" text-anchor="middle" font-size="13" fill="#777">'
         '上三块：各自独立纵轴的实际价格　|　最下块：归一化走势（各型号散片首价 = 100）</text>')

wt = week_ticks()
month_starts = [d for d in wt if d.day == 1 or d == wt[0]]


def draw_frame(top: float, yticks: list[float], ylo: float, yhi: float, ylabel: str):
    """画一块面板的坐标轴、网格、横向刻度标签。"""
    def Y(p):
        return top + (yhi - p) / (yhi - ylo) * PH

    # 月初竖网格（横跨整块面板）
    for m in month_starts:
        x = X(m)
        P.append(f'<line x1="{x:.1f}" y1="{top:.1f}" x2="{x:.1f}" y2="{top+PH:.1f}" stroke="#f2f2f2"/>')
    # 水平网格 + 纵轴标签
    for t in yticks:
        y = Y(t)
        if top - 0.5 <= y <= top + PH + 0.5:
            P.append(f'<line x1="{ML}" y1="{y:.1f}" x2="{ML+CW}" y2="{y:.1f}" stroke="#e8e8e8"/>')
            P.append(f'<text x="{ML-10}" y="{y+4:.1f}" text-anchor="end" font-size="12" fill="#666">'
                     f'{t:g}</text>')
    # 轴线
    P.append(f'<line x1="{ML}" y1="{top:.1f}" x2="{ML}" y2="{top+PH:.1f}" stroke="#333"/>')
    P.append(f'<line x1="{ML}" y1="{top+PH:.1f}" x2="{ML+CW}" y2="{top+PH:.1f}" stroke="#333"/>')
    P.append(f'<text x="{ML-58}" y="{top+PH/2:.1f}" font-size="13" fill="#444" text-anchor="middle" '
             f'transform="rotate(-90 {ML-58} {top+PH/2:.1f})">{ylabel}</text>')
    return Y


def draw_series(top: float, Y, series: dict, label_last=True):
    """在面板上画折线、端点与末尾价格标签。"""
    for name, pts in series.items():
        if not pts:
            continue
        color = COL.get(name, "#555")
        path = " ".join(f"{X(d):.1f},{Y(p):.1f}" for d, p in pts)
        P.append(f'<polyline fill="none" stroke="{color}" stroke-width="2" points="{path}"/>')
        if label_last:
            ld, lp = pts[-1]
            P.append(f'<circle cx="{X(ld):.1f}" cy="{Y(lp):.1f}" r="3.5" fill="{color}"/>')
            P.append(f'<text x="{X(ld)+7:.1f}" y="{Y(lp)+4:.1f}" font-size="12" font-weight="bold" '
                     f'fill="{color}">{lp:.0f}</text>')
    # 图例
    lx, ly = ML + CW + 26, top + 16
    for i, (name, pts) in enumerate(series.items()):
        if not pts:
            continue
        color = COL.get(name, "#555")
        yy = ly + i * 22
        P.append(f'<line x1="{lx}" y1="{yy}" x2="{lx+22}" y2="{yy}" stroke="{color}" stroke-width="2"/>')
        P.append(f'<text x="{lx+28}" y="{yy+4}" font-size="12" fill="#333">{name}</text>')


# ---------------- 三块价格面板 ----------------
panels = []
for i, key in enumerate(PRODUCTS):
    info = data[key]
    top = MT + i * (PH + GAP)
    prices = [p for pts in info["series"].values() for _, p in pts]
    lo, hi = min(prices), max(prices)
    pad = (hi - lo) * 0.12 or 10
    ylo, yhi = lo - pad, hi + pad
    # 面板标题（型号名 + 区间，区间位置按标题宽度偏移避免重叠）
    P.append(f'<text x="{ML}" y="{top-12:.1f}" font-size="16" font-weight="bold" fill="#222">'
             f'{info["name"]}</text>')
    rx = ML + text_w(info["name"], 16) + 28
    P.append(f'<text x="{rx:.1f}" y="{top-12:.1f}" font-size="12" fill="#888">'
             f'价格区间 {lo:g} ~ {hi:g} 元</text>')
    Y = draw_frame(top, nice_ticks(ylo, yhi), ylo, yhi, "价格（元）")
    draw_series(top, Y, info["series"])
    panels.append((key, top, Y))

# ---------------- 归一化走势面板 ----------------
top = MT + 3 * (PH + GAP)
P.append(f'<text x="{ML}" y="{top-12:.1f}" font-size="16" font-weight="bold" fill="#222">'
         '归一化走势对比（散片首价 = 100，越低越便宜）</text>')
idx_series = {}
for key, color in zip(PRODUCTS, IDX_COLORS):
    pts = data[key]["series"].get("散片") or next(iter(data[key]["series"].values()))
    base = pts[0][1]
    idx_series[key] = [(d, p / base * 100) for d, p in pts]
all_idx = [v for pts in idx_series.values() for _, v in pts]
ilo, ihi = min(all_idx), max(all_idx)
ipad = (ihi - ilo) * 0.12 or 2
Y = draw_frame(top, nice_ticks(ilo - ipad, ihi + ipad), ilo - ipad, ihi + ipad, "指数（首价=100）")
for (key, color) in zip(PRODUCTS, IDX_COLORS):
    pts = idx_series[key]
    path = " ".join(f"{X(d):.1f},{Y(v):.1f}" for d, v in pts)
    P.append(f'<polyline fill="none" stroke="{color}" stroke-width="2.2" points="{path}"/>')
    ld, lv = pts[-1]
    P.append(f'<circle cx="{X(ld):.1f}" cy="{Y(lv):.1f}" r="3.5" fill="{color}"/>')
    P.append(f'<text x="{X(ld)+7:.1f}" y="{Y(lv)+4:.1f}" font-size="12" font-weight="bold" '
             f'fill="{color}">{lv:.1f}</text>')
# 基准线 100
y100 = Y(100)
P.append(f'<line x1="{ML}" y1="{y100:.1f}" x2="{ML+CW}" y2="{y100:.1f}" stroke="#999" '
         'stroke-dasharray="5,4"/>')
P.append(f'<text x="{ML+6}" y="{y100-5:.1f}" font-size="11" fill="#999">基准 100</text>')
# 图例（统放在面板内左上角，避免右侧溢出；含末端涨跌幅）
idx_legend = [(data[k]["name"].split()[-1] if k != "intel-i5-13600kf" else "13600KF", c)
              for k, c in zip(PRODUCTS, IDX_COLORS)]
idx_legend = ["12400F", "13600KF", "9800X3D"]
for i, (key, color) in enumerate(zip(PRODUCTS, IDX_COLORS)):
    pts = idx_series[key]
    yy = top + 20 + i * 20
    P.append(f'<line x1="{ML+14}" y1="{yy}" x2="{ML+36}" y2="{yy}" stroke="{color}" stroke-width="2.2"/>')
    P.append(f'<text x="{ML+42}" y="{yy+4}" font-size="12" fill="#333">'
             f'{idx_legend[i]}　{pts[-1][1]-100:+.1f}%</text>')

# ---------------- 共用横轴刻度（画在最下面板下方） ----------------
axis_y = top + PH
for m in wt:
    x = X(m)
    is_ms = m.day == 1 or m == wt[0]
    P.append(f'<line x1="{x:.1f}" y1="{axis_y:.1f}" x2="{x:.1f}" y2="{axis_y+6:.1f}" '
             f'stroke="{"#333" if is_ms else "#bbb"}"/>')
    label = f"{m.year}-{m.month:02d}-{m.day:02d}" if m == wt[0] else (
        f"{m.month:02d}-01" if m.day == 1 else f"{m.month:02d}-{m.day:02d}")
    P.append(f'<text x="{x:.1f}" y="{axis_y+24:.1f}" text-anchor="middle" '
             f'font-size="{"12" if is_ms else "10"}" fill="{"#444" if is_ms else "#999"}">{label}</text>')
P.append(f'<text x="{ML+CW/2:.1f}" y="{axis_y+50:.1f}" text-anchor="middle" font-size="14" '
         'fill="#444">时间（每周一个刻度，月初加粗）</text>')
P.append("</svg>")

OUT.write_text("\n".join(P), encoding="utf-8")
summary = " / ".join(f'{data[k]["name"]}:{sum(len(v) for v in data[k]["series"].values())}点' for k in PRODUCTS)
print(f"已生成 {OUT}\n{summary}")
