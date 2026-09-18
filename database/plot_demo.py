# -*- coding: utf-8 -*-
"""价格波动折线图演示：matplotlib 输出 PNG"""
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

DB_PATH = os.path.join(os.path.dirname(__file__), "cpumem.db")
OUT_DIR = os.path.join(os.path.dirname(__file__), "charts")
os.makedirs(OUT_DIR, exist_ok=True)

conn = sqlite3.connect(DB_PATH)
cur = conn.cursor()

def fetch_series(product_name, price_type=None, start=None, end=None):
    """查某型号的价格时间序列"""
    sql = """
        SELECT d.date_key, q.price, q.price_type
        FROM quotes q
        JOIN products p ON q.product_key = p.product_key
        JOIN dates d ON q.date_key = d.date_key
        WHERE p.display_name LIKE ?
    """
    params = [f"%{product_name}%"]
    if price_type:
        sql += " AND q.price_type = ?"
        params.append(price_type)
    if start:
        sql += " AND d.date_key >= ?"
        params.append(start)
    if end:
        sql += " AND d.date_key <= ?"
        params.append(end)
    sql += " ORDER BY d.date_key"
    return cur.execute(sql, params).fetchall()

def plot_series(title, rows, fname, price_type_label=""):
    """画折线图"""
    dates = [r[0][5:] for r in rows]      # '2026-09-01' -> '09-01'
    prices = [r[1] for r in rows]
    avg = sum(prices) / len(prices)

    fig, ax = plt.subplots(figsize=(10, 5.5))
    ax.plot(dates, prices, marker="o", linewidth=2, color="#1f77b4", label=price_type_label or "价格")
    ax.axhline(avg, color="gray", linestyle="--", linewidth=1, label=f"均值 {avg:.0f}")

    # 标注每个点
    for x, y in zip(dates, prices):
        ax.annotate(f"{y:.0f}", (x, y), textcoords="offset points",
                    xytext=(0, 8), ha="center", fontsize=9)

    ax.set_title(title, fontsize=14)
    ax.set_xlabel("日期")
    ax.set_ylabel("价格 (元)")
    ax.grid(True, alpha=0.3)
    ax.legend()
    fig.tight_layout()
    path = os.path.join(OUT_DIR, fname)
    fig.savefig(path, dpi=120)
    plt.close(fig)
    print(f"已生成: {path}")
    return path

# ===== 图1：i5-12400F 散片价格波动 =====
rows = fetch_series("i5-12400F", price_type="散片")
plot_series("Intel i5-12400F 散片价格走势 (2026-09)", rows, "i5-12400f_sanpian.png", "散片价")

# ===== 图2：三星 990 PRO 1TB 固态价格波动 =====
rows = fetch_series("990 PRO")
plot_series("三星 990 PRO 1TB 价格走势 (2026-09)", rows, "samsung_990pro.png", "价格")

# ===== 图3：双线对比 —— i5-12400F 散片 vs 原盒 =====
r1 = fetch_series("i5-12400F", price_type="散片")
r2 = fetch_series("i5-12400F", price_type="原盒")
fig, ax = plt.subplots(figsize=(10, 5.5))
for rows, label, color in [(r1, "散片", "#1f77b4"), (r2, "原盒", "#d62728")]:
    dates = [r[0][5:] for r in rows]
    prices = [r[1] for r in rows]
    ax.plot(dates, prices, marker="o", linewidth=2, color=color, label=label)
    for x, y in zip(dates, prices):
        ax.annotate(f"{y:.0f}", (x, y), textcoords="offset points", xytext=(0, 8), ha="center", fontsize=9)
ax.set_title("Intel i5-12400F 散片 vs 原盒 价格对比 (2026-09)", fontsize=14)
ax.set_xlabel("日期"); ax.set_ylabel("价格 (元)")
ax.grid(True, alpha=0.3); ax.legend()
fig.tight_layout()
p = os.path.join(OUT_DIR, "i5-12400f_compare.png")
fig.savefig(p, dpi=120); plt.close(fig)
print(f"已生成: {p}")

conn.close()
