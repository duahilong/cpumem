# -*- coding: utf-8 -*-
"""模拟查询演示：展示三类核心查询 + 浏览类查询的输出效果"""
import sqlite3
import os

DB_PATH = os.path.join(os.path.dirname(__file__), "cpumem.db")
conn = sqlite3.connect(DB_PATH)
conn.row_factory = sqlite3.Row
cur = conn.cursor()

def show(title, sql, params=()):
    print("=" * 70)
    print(f"【{title}】")
    print("-" * 70)
    rows = cur.execute(sql, params).fetchall()
    if not rows:
        print("  (无结果)")
        return rows
    # 表头
    keys = rows[0].keys()
    widths = [max(len(str(k)), *(len(str(r[k])) for r in rows)) for k in keys]
    print("  " + " | ".join(str(k).ljust(w) for k, w in zip(keys, widths)))
    print("  " + "-+-".join("-" * w for w in widths))
    for r in rows:
        print("  " + " | ".join(str(r[k]).ljust(w) for k, w in zip(keys, widths)))
    print(f"  ({len(rows)} 行)")
    return rows

# ---------- 浏览类 ----------
show("浏览：CPU 品牌下的所有型号（做下拉框用）", """
    SELECT vendor, product_key, display_name
    FROM products WHERE category='CPU' ORDER BY vendor, display_name
""")

show("浏览：固态硬盘品牌下的所有型号", """
    SELECT vendor, product_key, display_name
    FROM products WHERE category='固态硬盘' ORDER BY vendor, display_name
""")

# ---------- 查询①：某 CPU 一段时间价格波动 ----------
show("查询①：i5-12400F 散片 2026-09 的价格波动", """
    SELECT d.date_key AS 日期, q.price AS 散片价
    FROM quotes q
    JOIN products p ON q.product_key = p.product_key
    JOIN dates d    ON q.date_key = d.date_key
    WHERE p.display_name LIKE '%12400F%'
      AND p.vendor='Intel' AND p.category='CPU'
      AND q.price_type='散片'
      AND d.date_key BETWEEN '2026-09-01' AND '2026-09-16'
    ORDER BY d.date_key
""")

# 简易文本走势图
rows = cur.execute("""
    SELECT d.date_key, q.price FROM quotes q
    JOIN products p ON q.product_key=p.product_key
    JOIN dates d ON q.date_key=d.date_key
    WHERE p.display_name LIKE '%12400F%' AND q.price_type='散片'
    ORDER BY d.date_key
""").fetchall()
prices = [r[1] for r in rows]
lo, hi = min(prices), max(prices)
print("-" * 70)
print("【i5-12400F 散片价格走势图（文本版）】")
for (dk, p) in rows:
    bar = "█" * int((p - lo) / max(hi - lo, 1) * 40) + "▏"
    print(f"  {dk}  {p:>6}  {bar}")
print(f"  最低 {lo} / 最高 {hi} / 波动 {hi-lo}")

# ---------- 查询②：某天某 CPU 的价格 ----------
show("查询②：i5-12400F 在 2026-09-11 当天的所有价格（散片+原盒）", """
    SELECT p.display_name AS 型号, q.price_type AS 类型, q.price AS 价格, d.date_key AS 日期
    FROM quotes q
    JOIN products p ON q.product_key=p.product_key
    JOIN dates d ON q.date_key=d.date_key
    WHERE p.display_name LIKE '%12400F%' AND d.date_key='2026-09-11'
    ORDER BY q.price_type
""")

# ---------- 查询③：某硬盘价格波动 ----------
show("查询③：三星 990 PRO 1TB 固态的价格波动", """
    SELECT d.date_key AS 日期, q.price AS 价格
    FROM quotes q
    JOIN products p ON q.product_key=p.product_key
    JOIN dates d ON q.date_key=d.date_key
    WHERE p.display_name LIKE '%990 PRO%'
    ORDER BY d.date_key
""")

# ---------- 查询④：某品牌最新报价 ----------
show("查询④：金士顿全部产品最新一期报价", """
    SELECT p.display_name AS 型号, p.category AS 大类, q.price_type AS 类型, q.price AS 最新价
    FROM quotes q
    JOIN products p ON q.product_key=p.product_key
    WHERE p.vendor='金士顿'
      AND q.date_key = (SELECT MAX(date_key) FROM quotes)
    ORDER BY p.category, p.display_name
""")

conn.close()
