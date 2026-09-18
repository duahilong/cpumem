# -*- coding: utf-8 -*-
"""建库脚本：创建 SQLite 数据库 + 三张表 + 模拟数据"""
import sqlite3
import os

DB_PATH = os.path.join(os.path.dirname(__file__), "cpumem.db")

def main():
    if os.path.exists(DB_PATH):
        os.remove(DB_PATH)
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()

    # ============ 建表 ============
    cur.executescript("""
    -- 维表1：型号表（分类体系核心：大类 + 品牌 + 型号）
    CREATE TABLE products (
        product_key   TEXT PRIMARY KEY,  -- 规范化键，如 'i5-12400f'
        display_name  TEXT NOT NULL,     -- 展示名，如 'Intel i5-12400F'
        category      TEXT NOT NULL,     -- 大类：CPU/内存/固态硬盘/机械硬盘/外设
        vendor        TEXT NOT NULL      -- 品牌：Intel/AMD/金士顿/三星...
    );

    -- 维表2：日期表
    CREATE TABLE dates (
        date_key   TEXT PRIMARY KEY,     -- 'YYYY-MM-DD'
        year       INTEGER,
        month      INTEGER,
        day        INTEGER,
        weekday    INTEGER               -- 0=周一
    );

    -- 事实表：价格记录（一行 = 某天某型号某价格类型的一个价格点）
    CREATE TABLE quotes (
        id            INTEGER PRIMARY KEY AUTOINCREMENT,
        product_key   TEXT NOT NULL REFERENCES products(product_key),
        date_key      TEXT NOT NULL REFERENCES dates(date_key),
        price         REAL NOT NULL,
        price_type    TEXT DEFAULT '默认',  -- 散片/原盒/单条/套装/默认
        price_raw     TEXT,                 -- 原始文本，溯源用
        source_image  TEXT                  -- 来源图片
    );

    CREATE INDEX idx_q_prod_date ON quotes(product_key, date_key);
    CREATE INDEX idx_p_cat_vendor ON products(category, vendor);
    """)

    # ============ 模拟数据：型号 ============
    products = [
        # CPU - Intel
        ("i5-12400f",  "Intel i5-12400F",     "CPU",  "Intel"),
        ("i5-13400f",  "Intel i5-13400F",     "CPU",  "Intel"),
        ("i7-13700k",  "Intel i7-13700K",     "CPU",  "Intel"),
        # CPU - AMD
        ("r5-5600",    "AMD Ryzen 5 5600",    "CPU",  "AMD"),
        ("r7-7800x3d", "AMD Ryzen 7 7800X3D", "CPU",  "AMD"),
        # 内存 - 金士顿/金百达
        ("kingston-fury-16g-5600", "金士顿 FURY 16G 5600",   "内存", "金士顿"),
        ("kingston-nv3-1tb",       "金士顿 NV3 1TB NVMe",    "固态硬盘", "金士顿"),
        ("kingston-nv2-2tb",       "金士顿 NV2 2TB NVMe",    "固态硬盘", "金士顿"),
        ("kingston-a400-240g",     "金士顿 A400 240G SATA",  "固态硬盘", "金士顿"),
        ("kingbank-16g-6000",      "金百达 银爵 16G 6000",   "内存", "金百达"),
        # 固态 - 三星/西数
        ("samsung-990pro-1tb",     "三星 990 PRO 1TB",       "固态硬盘", "三星"),
        ("samsung-980-1tb",        "三星 980 1TB",           "固态硬盘", "三星"),
        ("wd-sn770-2tb",           "西数 SN770 2TB",         "固态硬盘", "西数"),
        # 机械 - 西数/希捷
        ("wd10ezex",               "西数 WD10EZEX 1TB 蓝盘", "机械硬盘", "西数"),
        ("st1000vx009",            "希捷 ST1000VX009 1TB",   "机械硬盘", "希捷"),
    ]
    cur.executemany("INSERT INTO products VALUES (?,?,?,?)", products)

    # ============ 模拟数据：日期（模拟报价周期 09-01 ~ 09-16 每两天一期）============
    import datetime
    dates = []
    for d in range(1, 17, 2):
        dt = datetime.date(2026, 9, d)
        dates.append((dt.isoformat(), dt.year, dt.month, dt.day, dt.weekday()))
    cur.executemany("INSERT INTO dates VALUES (?,?,?,?,?)", dates)
    date_keys = [d[0] for d in dates]

    # ============ 模拟数据：价格记录 ============
    # 模拟价格走势：每天在基准价上随机波动
    import random
    random.seed(42)

    # (product_key, price_type, 基准价, 每期波动幅度)
    price_series = [
        ("i5-12400f",  "散片", 613, 8),
        ("i5-12400f",  "原盒", 720, 10),
        ("i5-13400f",  "散片", 775, 10),
        ("i7-13700k",  "散片", 1320, 20),
        ("r5-5600",    "散片", 620, 8),
        ("r5-5600",    "原盒", 680, 10),
        ("r7-7800x3d", "散片", 2650, 30),
        ("kingston-fury-16g-5600", "单条", 1720, 20),
        ("kingston-nv3-1tb",  "默认", 533, 10),
        ("kingston-nv2-2tb",  "默认", 780, 15),
        ("kingston-a400-240g","默认", 137, 5),
        ("kingbank-16g-6000", "单条", 1900, 25),
        ("samsung-990pro-1tb","默认", 1440, 20),
        ("samsung-980-1tb",   "默认", 650, 15),
        ("wd-sn770-2tb",      "默认", 850, 15),
        ("wd10ezex",   "默认", 680, 5),
        ("st1000vx009","默认", 650, 8),
    ]

    quotes = []
    for dk in date_keys:
        for pk, ptype, base, amp in price_series:
            p = round(base + random.uniform(-amp, amp))
            quotes.append((pk, dk, p, ptype, f"{p}(模拟)", "mock_01.png"))

    cur.executemany(
        "INSERT INTO quotes (product_key, date_key, price, price_type, price_raw, source_image) VALUES (?,?,?,?,?,?)",
        quotes,
    )

    conn.commit()
    print(f"数据库已创建: {DB_PATH}")
    print(f"products: {cur.execute('SELECT COUNT(*) FROM products').fetchone()[0]} 行")
    print(f"dates:    {cur.execute('SELECT COUNT(*) FROM dates').fetchone()[0]} 行")
    print(f"quotes:   {cur.execute('SELECT COUNT(*) FROM quotes').fetchone()[0]} 行")
    conn.close()

if __name__ == "__main__":
    main()
