# -*- coding: utf-8 -*-
"""AMD Ryzen 5 5600 全部价格走势折线图（按日期排序）。

- 只取 CPU 大类的 AMD Ryzen 5 5600（排除 5600X / 5600GT / 内存 5600MHz）
- 按 price_type 分线：散片 / 原盒 / 默认
- 排除模拟数据（source_image='mock_01.png'）
输出：charts/r5-5600_all.png
"""
import sqlite3
import os

import matplotlib
matplotlib.use("Agg")  # 无界面环境，直接出图
import matplotlib.pyplot as plt
from matplotlib import font_manager

# ===== 中文字体设置（Windows）=====
for f in ["Microsoft YaHei", "SimHei", "DengXian"]:
    if any(f.lower() in ft.name.lower() for ft in font_manager.fontManager.ttflist):
        plt.rcParams["font.family"] = f
        break
plt.rcParams["axes.unicode_minus"] = False

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "cpumem.db")
OUT_DIR = os.path.join(BASE_DIR, "charts")
os.makedirs(OUT_DIR, exist_ok=True)

conn = sqlite3.connect(DB_PATH)
cur = conn.cursor()

# ===== 1. 查询 AMD Ryzen 5 5600 全部真实价格（按日期排序）=====
rows = cur.execute("""
    SELECT q.date_key, q.price_type, q.price, q.price_raw, q.source_image
    FROM quotes q
    JOIN products p ON q.product_key = p.product_key
    WHERE p.category = 'CPU'
      AND p.product_key = 'amd-ryzen-5-5600'
      AND q.price_raw NOT LIKE '%(模拟)%'
    ORDER BY q.date_key, q.price_type
""").fetchall()

print("=" * 78)
print("【AMD Ryzen 5 5600 全部真实价格记录（按日期排序）】")
print("-" * 78)
print(f"  {'日期':<12}{'类型':<8}{'价格':>8}   原文")
print("-" * 78)
for dk, pt, pr, raw, src in rows:
    print(f"  {dk:<12}{pt:<8}{pr:>8.0f}   {raw}")
print(f"  共 {len(rows)} 条")

# 按 price_type 分组（保持理想顺序：散片 / 原盒 / 默认）
order = ["散片", "原盒", "默认"]
groups = {}
for dk, pt, pr, raw, src in rows:
    groups.setdefault(pt, []).append((dk, pr))
# 未列在 order 里的类型追加在后面
for pt in groups:
    if pt not in order:
        order.append(pt)

# ===== 2. 画折线图：一条线 = 一个 price_type =====
colors = {"散片": "#1f77b4", "原盒": "#d62728", "默认": "#2a9e4f"}
fig, ax = plt.subplots(figsize=(12, 6))

for pt in order:
    if pt not in groups:
        continue
    pts = sorted(groups[pt])                       # 按日期
    dates = [d[5:] for d, _ in pts]                # '2026-08-13' -> '08-13'
    prices = [p for _, p in pts]
    ax.plot(dates, prices, marker="o", linewidth=2,
            color=colors.get(pt, "#888888"), label=pt)
    for x, y in zip(dates, prices):
        ax.annotate(f"{y:.0f}", (x, y), textcoords="offset points",
                    xytext=(0, 8), ha="center", fontsize=8)

# 散片均值参考线
if "散片" in groups:
    sp = [p for _, p in groups["散片"]]
    avg = sum(sp) / len(sp)
    ax.axhline(avg, color="#1f77b4", linestyle="--", linewidth=1,
               label=f"散片均值 {avg:.0f}")

ax.set_title("AMD Ryzen 5 5600 价格走势（全部价格，按日期排序）", fontsize=14)
ax.set_xlabel("日期")
ax.set_ylabel("价格 (元)")
ax.grid(True, alpha=0.3)
ax.legend()
fig.tight_layout()
path = os.path.join(OUT_DIR, "r5-5600_all.png")
fig.savefig(path, dpi=120)
plt.close(fig)
print("=" * 78)
print(f"折线图已生成: {path}")

conn.close()
