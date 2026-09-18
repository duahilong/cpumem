# -*- coding: utf-8 -*-
"""建库脚本：创建空的 SQLite 数据库（products / dates / quotes 三表）。
cpumem.db 不存在时 clean_load.py 也会自动建表；本脚本用于手动重置。"""
import sqlite3
import os

DB_PATH = os.path.join(os.path.dirname(__file__), "cpumem.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS products (
    product_key   TEXT PRIMARY KEY,   -- 规范化键，如 'i5-12400f'
    display_name  TEXT NOT NULL,     -- 展示名，如 'Intel i5-12400F'
    category      TEXT NOT NULL,     -- 大类：CPU/内存/固态硬盘/机械硬盘/主板/显卡/电源/显示器/TF卡/SD卡/U盘/外设
    vendor        TEXT NOT NULL      -- 品牌
);

CREATE TABLE IF NOT EXISTS dates (
    date_key   TEXT PRIMARY KEY,     -- 'YYYY-MM-DD'
    year       INTEGER,
    month      INTEGER,
    day        INTEGER,
    weekday    INTEGER               -- 0=周一
);

CREATE TABLE IF NOT EXISTS quotes (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    product_key   TEXT NOT NULL REFERENCES products(product_key),
    date_key      TEXT NOT NULL REFERENCES dates(date_key),
    price         REAL NOT NULL,       -- 数值价格
    price_type    TEXT DEFAULT '默认',  -- 散片/原盒/单条/套装/默认
    source_image  TEXT                 -- 来源图片（溯源）
);

CREATE INDEX IF NOT EXISTS idx_q_prod_date  ON quotes(product_key, date_key);
CREATE INDEX IF NOT EXISTS idx_p_cat_vendor ON products(category, vendor);
"""


def main():
    if os.path.exists(DB_PATH):
        os.remove(DB_PATH)
        print(f"已删除旧库: {DB_PATH}")
    conn = sqlite3.connect(DB_PATH)
    conn.executescript(SCHEMA)
    conn.commit()
    cur = conn.cursor()
    print(f"数据库已创建: {DB_PATH}")
    for t in ("products", "dates", "quotes"):
        print(f"  {t}: {cur.execute(f'SELECT COUNT(*) FROM {t}').fetchone()[0]} 行")
    conn.close()


if __name__ == "__main__":
    main()
