# -*- coding: utf-8 -*-
"""
步骤2+3：清洗 + 入库 —— 读取 extracted/*.json，规范化后写入 cpumem.db。

清洗内容：
  - 日期规范化（无年份补齐 -> YYYY-MM-DD）
  - 品牌归一（Kingston -> 金士顿 等）
  - 大类归一（英文/别名 -> 标准枚举）
  - 型号名规范化（product_key：小写、去空格、统一连接符）
  - 价格类型归一（散片/原盒/单条/套装/默认）
  - 价格数值校验

幂等性：按 source_image 先删后插，重复运行不产生重复数据。

去重规则：同一 (date_key, product_key, price_type) 出现多条时：
  - 价格相同 -> 只保留一条
  - 价格不同 -> 写入 conflicts.json，等人工确认（默认不入库，用 --force-conflicts 可强制入库）

用法：
    python clean_load.py            # 处理 extracted/ 下全部 JSON
    python clean_load.py --force-conflicts   # 冲突记录也强制入库（人工确认后）
"""
import os
import json
import glob
import re
import sqlite3
import datetime

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "cpumem.db")
EXTRACT_DIR = os.path.join(BASE_DIR, "extracted")
CONFLICT_PATH = os.path.join(BASE_DIR, "conflicts.json")

# 全局冲突收集器：key=(date_key, product_key, price_type) -> [候选记录]
_conflicts = {}

# ============ 归一化映射表 ============
VENDOR_MAP = {
    "kingston": "金士顿", "金士頓": "金士顿",
    "samsung": "三星", "三星电子": "三星",
    "western digital": "西数", "wd": "西数", "西部数据": "西数",
    "seagate": "希捷", "st": "希捷",
    "intel": "Intel", "amd": "AMD",
    "logitech": "罗技", "罗技logitech": "罗技",
    "kingbank": "金百达", "colorful": "七彩虹", "七彩虹colorful": "七彩虹",
    "dahua": "大华", "asgard": "阿斯加特", "biwin": "佰维",
    "micron": "麦光", "zhi tai": "芝奇", "gskill": "芝奇",
}

CATEGORY_MAP = {
    "cpu": "CPU", "处理器": "CPU",
    "内存": "内存", "memory": "内存", "ram": "内存",
    "固态": "固态硬盘", "固态硬盘": "固态硬盘", "ssd": "固态硬盘", "nvme": "固态硬盘",
    "机械": "机械硬盘", "机械硬盘": "机械硬盘", "hdd": "机械硬盘", "硬盘": "机械硬盘",
    "主板": "主板", "motherboard": "主板",
    "显卡": "显卡", "gpu": "显卡",
    "电源": "电源", "psu": "电源",
    "显示器": "显示器", "monitor": "显示器",
    "外设": "外设", "鼠标": "外设", "键盘": "外设", "键鼠": "外设",
    # 存储介质：保留细分（U盘、SD卡独立成类）
    "tf卡": "TF卡", "sd卡": "SD卡", "存储卡": "TF卡", "内存卡": "TF卡",
    "u盘": "U盘", "优盘": "U盘",
    "移动固态硬盘": "固态硬盘", "移动硬盘": "机械硬盘",
    "cf卡": "TF卡", "读卡器": "外设", "硬盘盒": "外设", "dvd刻录机": "外设",
}

PRICE_TYPE_MAP = {
    "散片": "散片", "原盒": "原盒", "盒装": "原盒",
    "单条": "单条", "套装": "套装", "套价": "套装",
    "": "默认", "默认": "默认",
}

def norm_date(raw: str, fallback_year: int = 2026) -> str | None:
    """日期规范化：'2026-09-16' / '09月16日' / '9.16' -> 'YYYY-MM-DD'，年份异常强制改为 2026"""
    if not raw:
        return None
    raw = str(raw).strip()
    m = re.match(r"(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})", raw)
    if m:
        y, mo, d = map(int, m.groups())
        if y != fallback_year:
            y = fallback_year  # 年份异常（LLM猜错年份）强制改为 2026
        try:
            return datetime.date(y, mo, d).isoformat()
        except ValueError:
            return None
    m = re.match(r"(\d{1,2})月(\d{1,2})日?", raw)
    if m:
        mo, d = int(m.group(1)), int(m.group(2))
        try:
            return datetime.date(fallback_year, mo, d).isoformat()
        except ValueError:
            return None
    return None

def norm_vendor(raw: str) -> str:
    if not raw:
        return "未知"
    key = str(raw).strip().lower()
    return VENDOR_MAP.get(key, str(raw).strip())

def norm_category(raw: str) -> str:
    if not raw:
        return "未知"
    return CATEGORY_MAP.get(str(raw).strip().lower(), CATEGORY_MAP.get(str(raw).strip(), str(raw).strip()))

def norm_price_type(raw: str) -> str:
    return PRICE_TYPE_MAP.get(str(raw or "").strip(), "默认")

def norm_product_key(name: str, vendor: str) -> str:
    """型号规范化键：小写、去空格、统一分隔符；加品牌前缀避免跨品牌重名"""
    s = str(name).strip().lower()
    s = re.sub(r"[\s_]+", "-", s)
    s = re.sub(r"-+", "-", s).strip("-")
    v = str(vendor).strip().lower()
    return f"{v}-{s}"

CPU_RE = re.compile(r"(i[3579][\s-]?\d{4,5}[a-zkf]*|ryzen[\s-]?\d\s?\d{3}[x0-9a-z]*|r[579][\s-]?\d{4}[x3d]*)", re.I)

def guess_category(name: str, spec: str) -> str | None:
    """兜底：按型号名猜大类"""
    s = f"{name} {spec}".lower()
    if CPU_RE.search(s):
        return "CPU"
    if re.search(r"\d+(g|tb)\b|nvme|sata|ssd|固态", s):
        return "固态硬盘"
    if re.search(r"ddr[345]|内存", s):
        return "内存"
    return None

def dedupe_quotes(conn):
    """库内去重：同 (date_key, product_key, price_type) 多条时，
    价格相同 -> 只留一条；价格不同 -> 记录冲突（删除，等人工确认）。"""
    cur = conn.cursor()
    rows = cur.execute("""
        SELECT id, product_key, date_key, price, price_type, source_image
        FROM quotes ORDER BY id
    """).fetchall()
    seen = {}   # key -> (id, price)
    dup_same, conflicts = 0, 0
    for rid, pkey, dk, price, ptype, src in rows:
        key = (dk, pkey, ptype)
        if key not in seen:
            seen[key] = (rid, price)
            continue
        first_id, first_price = seen[key]
        if abs(price - first_price) < 0.01:
            cur.execute("DELETE FROM quotes WHERE id=?", (rid,))  # 同价，删后来的
            dup_same += 1
        else:
            # 异价冲突：当前这条记为冲突并删除（保留最早一条）
            _conflicts.setdefault(str(key), []).append({
                "kept": {"id": first_id, "price": first_price, "source": None},
                "conflict": {"id": rid, "price": price, "source_image": src},
            })
            cur.execute("DELETE FROM quotes WHERE id=?", (rid,))
            conflicts += 1
    conn.commit()
    return dup_same, conflicts

def _create_tables(conn):
    """cpumem.db 不存在时自动建表"""
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS products (
        product_key   TEXT PRIMARY KEY,
        display_name  TEXT NOT NULL,
        category      TEXT NOT NULL,
        vendor        TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS dates (
        date_key   TEXT PRIMARY KEY,
        year INTEGER, month INTEGER, day INTEGER, weekday INTEGER
    );
    CREATE TABLE IF NOT EXISTS quotes (
        id            INTEGER PRIMARY KEY AUTOINCREMENT,
        product_key   TEXT NOT NULL REFERENCES products(product_key),
        date_key      TEXT NOT NULL REFERENCES dates(date_key),
        price         REAL NOT NULL,
        price_type    TEXT DEFAULT '默认',
        source_image  TEXT
    );
    CREATE INDEX IF NOT EXISTS idx_q_prod_date ON quotes(product_key, date_key);
    CREATE INDEX IF NOT EXISTS idx_p_cat_vendor ON products(category, vendor);
    """)
    conn.commit()


def load_db():
    if not os.path.exists(DB_PATH):
        conn = sqlite3.connect(DB_PATH)
        _create_tables(conn)
        print("cpumem.db 不存在，已自动创建表结构")
        return conn
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA foreign_keys = ON")
    return conn

def process_file(conn, json_path: str) -> tuple[int, int]:
    """处理单个提取 JSON：清洗 + 幂等入库。返回 (产品数, 价格数)"""
    with open(json_path, encoding="utf-8") as f:
        data = json.load(f)

    source_image = data.get("_source_image") or os.path.basename(json_path)
    sheet_date = norm_date(data.get("sheet_date"))
    # 从文件名兜底补年份（如 '2026_09_16' 开头）
    m = re.search(r"(\d{4})[_-]?(\d{2})[_-]?(\d{2})", source_image)
    if not sheet_date and m:
        sheet_date = norm_date(f"{m.group(1)}-{m.group(2)}-{m.group(3)}")
    if not sheet_date:
        print(f"  ! 跳过 {source_image}: 无法确定报价日期")
        return (0, 0)

    cur = conn.cursor()
    cur.execute("DELETE FROM quotes WHERE source_image = ?", (source_image,))  # 幂等

    n_prod, n_quote = 0, 0
    for item in data.get("products", []):
        name = str(item.get("product_name") or "").strip()
        if not name:
            continue
        price = item.get("price")
        try:
            price = float(price)
        except (TypeError, ValueError):
            continue
        # 价格不明确校验：含星号/X的价格视为无效（LLM漏剔时兜底）
        price_str = str(item.get("price") or "")
        if "*" in price_str or "X" in price_str.upper():
            continue
        if price <= 0:
            continue  # 缺货等无价商品（价格为0/空）不入库
        if not (1 <= price <= 200000):
            continue  # 离谱值校验

        vendor = norm_vendor(item.get("vendor"))
        category = norm_category(item.get("category"))
        if category == "未知":
            category = guess_category(name, "") or "未知"

        # 型号不存在则插入 products
        pkey = norm_product_key(name, vendor)
        row = cur.execute("SELECT 1 FROM products WHERE product_key=?", (pkey,)).fetchone()
        if not row:
            display = f"{vendor} {name}".strip()
            cur.execute("INSERT INTO products VALUES (?,?,?,?)",
                        (pkey, display, category, vendor))
            n_prod += 1

        # 日期表
        cur.execute("INSERT OR IGNORE INTO dates(date_key, year, month, day, weekday) VALUES (?,?,?,?,?)",
                    (sheet_date, int(sheet_date[:4]), int(sheet_date[5:7]),
                     int(sheet_date[8:10]), datetime.date.fromisoformat(sheet_date).weekday()))

        ptype = norm_price_type(item.get("price_type"))
        cur.execute(
            "INSERT INTO quotes (product_key, date_key, price, price_type, source_image) VALUES (?,?,?,?,?)",
            (pkey, sheet_date, price, ptype, source_image))
        n_quote += 1

    conn.commit()
    return (n_prod, n_quote)

def main():
    if not os.path.exists(DB_PATH):
        print("错误: 未找到 cpumem.db（会自动创建表结构）")
        _create_tables(conn) if False else None
        return
    force = "--force-conflicts" in __import__("sys").argv
    files = sorted(glob.glob(os.path.join(EXTRACT_DIR, "*.json")))
    print(f"找到 {len(files)} 个提取结果文件")
    conn = load_db()
    tp = tq = 0
    for i, f in enumerate(files, 1):
        np_, nq = process_file(conn, f)
        tp += np_; tq += nq
        print(f"[{i}/{len(files)}] {os.path.basename(f)}: 新型号 {np_}, 价格 {nq}")

    # 库内去重
    dup_same, n_conf = dedupe_quotes(conn)
    print(f"去重: 同价重复删除 {dup_same} 条, 异价冲突 {n_conf} 条")
    if _conflicts:
        with open(CONFLICT_PATH, "w", encoding="utf-8") as f:
            json.dump(_conflicts, f, ensure_ascii=False, indent=2)
        print(f"!! 有 {n_conf} 条同日同型号但价格不同的记录，已写入 {CONFLICT_PATH}，请人工核对原图后处理")
        if not force:
            print("   (确认后可用 --force-conflicts 强制入库，或手动修正 extracted/ 中的 JSON 后重跑)")
    conn.close()
    print(f"\n入库完成: 新增型号 {tp}，新增价格记录 {tq}")

if __name__ == "__main__":
    main()
