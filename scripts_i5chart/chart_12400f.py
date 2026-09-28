# -*- coding: utf-8 -*-
"""从 cpumem.db 读取指定 CPU 型号报价，生成零依赖 SVG 折线图（只读查询，无副作用）。

用法：python chart_12400f.py [product_key] [输出路径]
默认型号 intel-i5-12400f。
"""
import sqlite3
import datetime
import sys
from pathlib import Path

DB = Path(__file__).parent.parent / "database" / "cpumem.db"
DEFAULT_PRODUCT = "intel-i5-12400f"

product = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_PRODUCT
OUT = Path(sys.argv[2]) if len(sys.argv) > 2 else Path(__file__).parent / f"{product}_price.svg"

W, H = 1600, 680
ML, MR, MT, MB = 80, 170, 60, 80  # 边距
CW, CH = W - ML - MR, H - MT - MB  # 绘图区

con = sqlite3.connect(DB)
row = con.execute("SELECT display_name FROM products WHERE product_key = ?", (product,)).fetchone()
if row is None:
    con.close()
    raise SystemExit(f"products 表中不存在 product_key = {product!r}")
display_name = row[0]
rows = con.execute(
    """SELECT q.date_key, q.price_type, q.price FROM quotes q
       JOIN products p ON p.product_key = q.product_key
       WHERE p.product_key = ? ORDER BY q.date_key""", (product,)
).fetchall()
con.close()
if not rows:
    raise SystemExit(f"{product} 没有任何报价记录")

series: dict[str, list[tuple[datetime.date, float]]] = {"散片": [], "原盒": []}
for d, pt, price in rows:
    series[pt].append((datetime.date.fromisoformat(d), price))

all_dates = sorted({d for pts in series.values() for d, _ in pts})
d0, d1 = all_dates[0], all_dates[-1]
lo = min(p for pts in series.values() for _, p in pts)
hi = max(p for pts in series.values() for _, p in pts)
pad = (hi - lo) * 0.08 or 10
plo, phi = lo - pad, hi + pad


def X(d: datetime.date) -> float:
    return ML + (d - d0).days / max((d1 - d0).days, 1) * CW


def Y(p: float) -> float:
    return MT + (phi - p) / (phi - plo) * CH


def path(pts):
    return " ".join(f"{X(d):.1f},{Y(p):.1f}" for d, p in pts)


def yticks():
    step = 20 if (phi - plo) / 20 < 22 else 40
    t, out = round(plo / step) * step, []
    while t <= phi:
        out.append(t)
        t += step
    return out


def week_ticks():
    """每周刻度：从首个周一（不早于 d0）开始，每 7 天一个。"""
    start = d0 + datetime.timedelta(days=(7 - d0.weekday()) % 7)
    out, cur = [], start
    while cur <= d1:
        out.append(cur)
        cur += datetime.timedelta(days=7)
    return out


COL = {"散片": "#d62728", "原盒": "#1f77b4"}
parts = [
    f'<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{H}" viewBox="0 0 {W} {H}" '
    'font-family="Microsoft YaHei, PingFang SC, sans-serif">',
    f'<rect width="{W}" height="{H}" fill="#ffffff"/>',
    f'<text x="{W/2}" y="32" text-anchor="middle" font-size="24" font-weight="bold" fill="#222">'
    f'{display_name} 报价走势（{d0} ~ {d1}）</text>',
]
# 网格与 Y 轴
for t in yticks():
    y = Y(t)
    parts.append(f'<line x1="{ML}" y1="{y:.1f}" x2="{ML+CW}" y2="{y:.1f}" stroke="#e5e5e5"/>')
    parts.append(f'<text x="{ML-8}" y="{y+4:.1f}" text-anchor="end" font-size="12" fill="#555">{t}</text>')
# X 轴每周刻度（首刻度带年份，月初刻度加粗，其余只标 月-日）
wt = week_ticks()
for m in wt:
    x = X(m)
    is_month_start = m.day == 1 or m == wt[0]
    parts.append(f'<line x1="{x:.1f}" y1="{MT+CH}" x2="{x:.1f}" y2="{MT+CH+6}" stroke="{"#333" if is_month_start else "#bbb"}"/>'
                 + (f'<line x1="{x:.1f}" y1="{MT}" x2="{x:.1f}" y2="{MT+CH}" stroke="#f5f5f5"/>' if is_month_start else ''))
    label = f"{m.year}-{m.month:02d}-{m.day:02d}" if m == wt[0] else (
        f"{m.month:02d}-01" if m.day == 1 else f"{m.month:02d}-{m.day:02d}")
    parts.append(f'<text x="{x:.1f}" y="{MT+CH+24}" text-anchor="middle" font-size="{"12" if is_month_start else "10"}" '
                 f'fill="{"#444" if is_month_start else "#999"}">{label}</text>')
# 轴线
parts.append(f'<line x1="{ML}" y1="{MT}" x2="{ML}" y2="{MT+CH}" stroke="#333"/>')
parts.append(f'<line x1="{ML}" y1="{MT+CH}" x2="{ML+CW}" y2="{MT+CH}" stroke="#333"/>')
# 轴标题
parts.append(f'<text x="{ML-45}" y="{MT+CH/2}" font-size="13" fill="#444" '
             f'transform="rotate(-90 {ML-45} {MT+CH/2})" text-anchor="middle">价格（元）</text>')
parts.append(f'<text x="{ML+CW/2}" y="{H-18}" text-anchor="middle" font-size="13" fill="#444">时间</text>')
# 折线
for name in ("散片", "原盒"):
    pts = series[name]
    if not pts:
        continue
    parts.append(f'<polyline fill="none" stroke="{COL[name]}" stroke-width="2" points="{path(pts)}"/>')
    last_d, last_p = pts[-1]
    parts.append(f'<circle cx="{X(last_d):.1f}" cy="{Y(last_p):.1f}" r="3.5" fill="{COL[name]}"/>')
    parts.append(f'<text x="{X(last_d)+8:.1f}" y="{Y(last_p)+4:.1f}" font-size="12" fill="{COL[name]}">'
                 f'{last_p:.0f}</text>')

# 关键月份标注：每月首个数据点标出价格（散片标在线下方、原盒标在线上方）
month_min = {d.year * 100 + d.month: d for d in all_dates}  # 每月首个出现日期
for name in ("散片", "原盒"):
    by_date = dict(series[name])
    for key, d in sorted(month_min.items()):
        if d not in by_date:
            continue  # 该月此系列无数据
        p = by_date[d]
        x, y = X(d), Y(p)
        dy = 16 if name == "原盒" else -10
        parts.append(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="3" fill="#ffffff" '
                     f'stroke="{COL[name]}" stroke-width="1.5"/>')
        parts.append(f'<text x="{x:.1f}" y="{y+dy:.1f}" text-anchor="middle" font-size="11" '
                     f'font-weight="bold" fill="{COL[name]}">{p:.0f}</text>')
# 图例
for i, name in enumerate(("散片", "原盒")):
    lx, ly = ML + CW + 30, MT + 20 + i * 26
    parts.append(f'<line x1="{lx}" y1="{ly}" x2="{lx+26}" y2="{ly}" stroke="{COL[name]}" stroke-width="2"/>')
    parts.append(f'<text x="{lx+34}" y="{ly+4}" font-size="13" fill="#222">{name}</text>')
parts.append("</svg>")
OUT.write_text("\n".join(parts), encoding="utf-8")
print(f"已生成 {OUT}  （散片 {len(series['散片'])} 点 / 原盒 {len(series['原盒'])} 点）")
