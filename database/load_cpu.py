# -*- coding: utf-8 -*-
"""
CPU 专用数据导入模块 —— 管线最后一步：提取落盘后的数据验证 + 入库。

流程（CPU 管线 S6 落盘之后运行）：
    1. 读取提取结果 JSON（默认 extract_cpu.py 的输出目录 output_cpu/extracted_cpu/，
       支持指定单份文件、目录或 --out-dir 对齐自定义输出目录）
    2. 预验证（入库前的规则校验，不通过则跳过该条并记录原因）：
       - source_image 非空（数据库溯源/幂等键，随记录写入 quotes）
       - sheet_date 可解析（norm_date）
       - product_name 非空
       - price 可转数字、> 0、在 1..200000、不含 * / X
       - price_type 在散片/原盒枚举内
       - category 非空
    3. 幂等入库：先删除同 source_image 的旧 quotes 记录，再写入
       products（型号不存在时插入）/ dates / quotes 三表
    4. 库内去重：同 (date_key, product_key, price_type) 多条时，
       同价只留一条；异价记入 conflicts_cpu.json 等人工确认

用法：
    python load_cpu.py                              # 默认导入 output_cpu/extracted_cpu/
    python load_cpu.py extracted_cpu                # 导入指定目录
    python load_cpu.py extracted_cpu/0a04….json     # 导入指定单份
    python load_cpu.py --out-dir ./my_out           # 对齐 extract_cpu.py 的自定义输出目录
    python load_cpu.py --status                     # 查看库内 CPU 数据统计

配置：数据库路径 database/cpumem.db；清洗规则复用 clean_load.py 的映射表。
"""
import os
import sys
import json
import glob
import re
import sqlite3
import datetime

sys.stdout.reconfigure(encoding="utf-8")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "cpumem.db")
# 导入报告/冲突清单等错误输出，默认写入 extract_cpu.py 的输出目录（output_cpu/）
OUTPUT_DIR = os.path.join(BASE_DIR, "output_cpu")
CONFLICT_PATH = os.path.join(OUTPUT_DIR, "load_cpu_conflicts.json")
REPORT_PATH = os.path.join(OUTPUT_DIR, "load_cpu_report.md")

sys.path.insert(0, BASE_DIR)
from clean_load import (  # 复用清洗映射与规范化函数
    norm_date, norm_vendor, norm_category, norm_price_type, norm_product_key,
    _create_tables, VENDOR_MAP, CATEGORY_MAP, PRICE_TYPE_MAP,
)

VALID_PRICE_TYPES = {"散片", "原盒"}


# ============ 预验证 ============

def validate_record(item: dict, sheet_date_raw) -> tuple[bool, str]:
    """对单条提取记录做入库前规则校验。返回 (通过, 不通过原因)。"""
    name = str(item.get("product_name") or "").strip()
    if not name:
        return False, "product_name 为空"

    # 日期
    if not norm_date(sheet_date_raw):
        return False, f"sheet_date 无法解析: {sheet_date_raw!r}"

    # 价格
    price = item.get("price")
    try:
        price = float(price)
    except (TypeError, ValueError):
        return False, f"price 无法转数字: {price!r}"
    price_str = str(item.get("price") or "")
    if "*" in price_str or "X" in price_str.upper():
        return False, f"price 含 */X 标记: {price_str}"
    if price <= 0:
        return False, f"price<=0（无价/缺货）: {price}"
    if not (1 <= price <= 200000):
        return False, f"price 超出 1..200000: {price}"

    # price_type
    ptype = norm_price_type(item.get("price_type"))
    if item.get("price_type") in ("散片", "原盒"):
        ptype = item["price_type"]
    if ptype not in VALID_PRICE_TYPES:
        return False, f"price_type 非法: {item.get('price_type')!r}"

    # category
    cat = item.get("category")
    if not str(cat or "").strip():
        return False, "category 为空"

    return True, ""


def load_extracted(path: str) -> list[str]:
    """收集待导入的提取 JSON 文件列表。"""
    if os.path.isfile(path):
        if os.path.splitext(path)[1].lower() != ".json":
            print(f"错误: {path} 不是 JSON 文件")
            return []
        return [os.path.abspath(path)]
    if os.path.isdir(path):
        files = sorted(glob.glob(os.path.join(path, "*.json")))
        if not files:
            print(f"错误: 目录 {path} 下没有 JSON 文件")
            return []
        return files
    print(f"错误: 路径不存在: {path}")
    return []


# ============ 入库 ============

_conflicts = {}


def dedupe_quotes(conn):
    """库内去重（load_cpu 版）：同 (date_key, product_key, price_type) 多条时，
    价格相同 -> 只留一条；价格不同 -> 记录冲突到本模块 _conflicts 字典
    （逻辑复用 clean_load.dedupe_quotes，但冲突明细归本模块，写入 conflicts_cpu.json）。"""
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


def get_conn() -> sqlite3.Connection:
    if not os.path.exists(DB_PATH):
        conn = sqlite3.connect(DB_PATH)
        _create_tables(conn)
        print("cpumem.db 不存在，已自动创建表结构")
        return conn
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


_conflicts = {}


def import_file(conn: sqlite3.Connection, json_path: str) -> tuple[int, int, int, list]:
    """导入单个提取 JSON：预验证 + 幂等入库。返回 (通过, 跳过, 新型号, 问题明细)。"""
    with open(json_path, encoding="utf-8") as f:
        data = json.load(f)

    # source_image 预验证：必须非空（它是数据库溯源/幂等键）
    source_image = str(data.get("source_image") or "").strip()
    if not source_image:
        return (0, len(data.get("products", [])), 0,
                [(None, None, "source_image 为空，无法溯源")])
    sheet_date_raw = data.get("sheet_date")

    cur = conn.cursor()

    passed = skipped = n_prod = n_quote = 0
    problems = []
    valid_rows = []  # 预验证通过的记录，先攒着统一入库

    for item in data.get("products", []):
        ok, reason = validate_record(item, sheet_date_raw)
        if not ok:
            skipped += 1
            problems.append((item.get("product_name"), item.get("price"), reason))
            continue
        passed += 1
        valid_rows.append(item)

    if not valid_rows:
        return (passed, skipped, n_prod, problems)

    # 预验证全部通过后才确定日期（保证该文件要么全入要么不入）
    sheet_date = norm_date(sheet_date_raw)

    # 幂等：删同 source_image 旧记录
    cur.execute("DELETE FROM quotes WHERE source_image = ?", (source_image,))

    for item in valid_rows:
        name = str(item.get("product_name")).strip()
        price = float(item.get("price"))
        vendor = norm_vendor(item.get("vendor"))
        ptype = norm_price_type(item.get("price_type"))

        pkey = norm_product_key(name, vendor)
        row = cur.execute("SELECT 1 FROM products WHERE product_key=?", (pkey,)).fetchone()
        if not row:
            # 剥离 product_name 中的规格后缀和描述文字（LLM 偶发拼入）
            clean_name = re.split(r"\d{1,2}核\d{0,3}线程", name)[0]
            clean_name = re.split(r"\d+\.\d+", clean_name)[0].strip()
            clean_name = re.sub(r"（[^）]*奔腾[^）]*）.*$|\([^)]*奔腾[^)]*\).*$", "", clean_name).strip()
            clean_name = clean_name or name
            display = f"{vendor} {clean_name}".strip()
            cur.execute("INSERT INTO products VALUES (?,?,?,?)",
                        (pkey, display, "CPU", vendor))
            n_prod += 1

        cur.execute(
            "INSERT OR IGNORE INTO dates(date_key, year, month, day, weekday) VALUES (?,?,?,?,?)",
            (sheet_date, int(sheet_date[:4]), int(sheet_date[5:7]),
             int(sheet_date[8:10]), datetime.date.fromisoformat(sheet_date).weekday()))

        cur.execute(
            "INSERT INTO quotes (product_key, date_key, price, price_type, source_image) VALUES (?,?,?,?,?)",
            (pkey, sheet_date, price, ptype, source_image))
        n_quote += 1

    conn.commit()
    return (passed, skipped, n_prod, problems)


# ============ 主流程 ============

def main():
    if "--status" in sys.argv:
        status()
        return

    # 默认导入目标：extract_cpu.py 的输出目录（与脚本同级的 output_cpu/extracted_cpu）
    # 可用 --out-dir 对齐 extract_cpu.py 的自定义输出目录，或显式传入 JSON 文件/目录
    default_dir = os.path.join(BASE_DIR, "output_cpu", "extracted_cpu")
    out_dir = default_dir
    if "--out-dir" in sys.argv:
        i = sys.argv.index("--out-dir")
        if i + 1 < len(sys.argv):
            out_dir = os.path.abspath(sys.argv[i + 1])
    # 错误输出跟随导入目标所在的输出目录
    global CONFLICT_PATH, REPORT_PATH
    conflict_path = os.path.join(os.path.dirname(out_dir), "load_cpu_conflicts.json")
    report_path = os.path.join(os.path.dirname(out_dir), "load_cpu_report.md")

    # 显式传入的 JSON 文件/目录优先；否则使用默认输出目录
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    if "--out-dir" in sys.argv:
        i = sys.argv.index("--out-dir")
        if i + 1 < len(sys.argv):
            args = [a for a in args if a != sys.argv[i + 1]]
    if args:
        target = args[0]
    else:
        target = out_dir
        print(f"未指定目标，默认导入 {target}")

    files = load_extracted(target)
    if not files:
        return

    print(f"共 {len(files)} 个提取结果文件，目标库: {DB_PATH}")
    conn = get_conn()

    tp = tq = tpass = tskip = 0
    all_problems = []
    file_stats = []   # (文件名, 总条数, 通过, 跳过, 新型号, 问题列表)
    for i, f in enumerate(files, 1):
        passed, skipped, n_prod, problems = import_file(conn, f)
        data = json.load(open(f, encoding='utf-8'))
        total_n = len(data.get('products', []))
        tp += n_prod
        tq += sum(1 for p in data.get('products', [])
                  if validate_record(p, data.get('sheet_date'))[0])
        tpass += passed
        tskip += skipped
        fname = os.path.basename(f)
        plist = [(name, price, reason) for name, price, reason in problems]
        file_stats.append((fname, total_n, passed, skipped, n_prod, plist))
        all_problems.extend([(fname,) + p for p in plist])
        print(f"[{i}/{len(files)}] {fname}: 通过 {passed}, 跳过 {skipped}, 新型号 {n_prod}")
        for name, price, reason in plist:
            print(f"    ! {name} ({price}): {reason}")

    # 库内去重
    dup_same, n_conf = dedupe_quotes(conn)
    print(f"\n去重: 同价重复删除 {dup_same} 条, 异价冲突 {n_conf} 条")
    if _conflicts:
        with open(conflict_path, "w", encoding="utf-8") as f:
            json.dump(_conflicts, f, ensure_ascii=False, indent=2)
        print(f"!! 有 {n_conf} 条同日同型号但价格不同的记录，已写入 {conflict_path}，请人工核对原图后处理")

    conn.close()

    # 生成人可读的导入报告（Markdown，写入输出目录）
    os.makedirs(os.path.dirname(report_path), exist_ok=True)
    now = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    lines = [
        f"# CPU 数据导入报告",
        "",
        f"- **导入时间**：{now}",
        f"- **导入目标**：`{target}`",
        f"- **目标数据库**：`{DB_PATH}`",
        "",
        "## 总览",
        "",
        "| 指标 | 数量 |",
        "| --- | --- |",
        f"| 提取结果文件 | {len(files)} |",
        f"| 提取记录总数 | {tpass + tskip} |",
        f"| ✅ 预验证通过（已入库） | {tpass} |",
        f"| ⛔ 预验证跳过（未入库） | {tskip} |",
        f"| 新增型号 | {tp} |",
        f"| 入库价格记录 | {tq} |",
        f"| 同价重复删除 | {dup_same} |",
        f"| 异价冲突（待人工确认） | {n_conf} |",
        "",
        "## 各文件导入明细",
        "",
        "| 文件 | 总条数 | 通过 | 跳过 | 新增型号 |",
        "| --- | --- | --- | --- | --- |",
    ]
    for fname, total_n, passed, skipped, n_prod, _ in file_stats:
        lines.append(f"| {fname} | {total_n} | {passed} | {skipped} | {n_prod} |")

    # 异价冲突明细（人可读表格）
    if _conflicts:
        lines += ["", "## ⚠️ 异价冲突（同日同型号但价格不同，已删除待人工确认）", "",
                  "以下记录因与已有记录价格不同被删除，请核对原图后决定处理方式。", "",
                  "| 日期 | 型号 | 价格类型 | 保留价格 | 冲突价格 | 冲突来源图 |", "| --- | --- | --- | --- | --- | --- |"]
        for key_str, items in _conflicts.items():
            for it in items:
                dk, pk, pt = eval(key_str)
                kept_price = it["kept"]["price"]
                lines.append(f"| {dk} | {pk} | {pt} | {kept_price} | {it['conflict']['price']} | {it['conflict'].get('source_image', '-')} |")

    # 预验证未通过明细（按原因分组，人可读）
    if all_problems:
        lines += ["", "## ⛔ 预验证未通过明细（未入库）", "",
                  "| 来源文件 | 型号 | 价格 | 未通过原因 |", "| --- | --- | --- | --- |"]
        for src, name, price, reason in all_problems:
            lines.append(f"| {src} | {name or '-'} | {price if price is not None else '-'} | {reason} |")

    with open(report_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    print(f"\n导入报告已写入: {report_path}")

    print(f"\n导入完成: 预验证通过 {tpass}, 跳过 {tskip}, 新增型号 {tp}")


def status():
    if not os.path.exists(DB_PATH):
        print(f"数据库不存在: {DB_PATH}")
        return
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    n_prod = cur.execute("SELECT COUNT(*) FROM products WHERE category='CPU'").fetchone()[0]
    n_quote = cur.execute("SELECT COUNT(*) FROM quotes WHERE source_image IS NOT NULL").fetchone()[0]
    n_dates = cur.execute("SELECT COUNT(*) FROM dates").fetchone()[0]
    print(f"[CPU 导入状态] 库: {os.path.basename(DB_PATH)}")
    print(f"  CPU 型号: {n_prod}")
    print(f"  价格记录: {n_quote}")
    print(f"  报价日期: {n_dates}")
    # 按来源统计
    rows = cur.execute("""
        SELECT source_image, COUNT(*) FROM quotes
        WHERE source_image IS NOT NULL
        GROUP BY source_image ORDER BY source_image
    """).fetchall()
    if rows:
        print("  按来源:")
        for src, n in rows:
            print(f"    {src}: {n} 条")
    conn.close()


if __name__ == "__main__":
    main()
