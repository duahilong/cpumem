# -*- coding: utf-8 -*-
"""
MEM 专用数据导入模块 —— 管线最后一步：提取落盘后的数据验证 + 入库。

流程（MEM 管线 S7 落盘之后运行）：
    1. 读取提取结果 JSON（默认 extract_mem.py 的输出目录 output_mem/extracted_mem/，
       支持指定单份文件、目录或 --out-dir 对齐自定义输出目录）
    2. 预验证（入库前的规则校验，不通过则跳过该条并记录原因）：
       - source_image 非空（数据库溯源/幂等键，随记录写入 quotes）
       - sheet_date 可解析（norm_date）
       - product_name 非空
       - price 可转数字、> 0、在 1..200000、不含 * / X
       - price_type 在 单条/套装/默认 枚举内
       - hardware_type 在枚举表内（非法时按 category + 型号名兜底推断后再入库）
    3. 幂等入库：先删除同 source_image 的旧 quotes 记录，再写入
       products（category 按记录实际值）/ dates / quotes（含 hardware_type）三表
    4. 库内去重：同 (date_key, product_key, price_type) 多条时，
       同价只留一条；异价记入 load_mem_conflicts.json 等人工确认

用法：
    python load_mem.py                              # 默认导入 output_mem/extracted_mem/
    python load_mem.py extracted_mem                # 导入指定目录
    python load_mem.py extracted_mem/<key>.json     # 导入指定单份
    python load_mem.py --out-dir ./my_out           # 对齐 extract_mem.py 的自定义输出目录
    python load_mem.py --status                     # 查看库内 MEM 数据统计

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
# 导入报告/冲突清单等错误输出，默认写入 extract_mem.py 的输出目录（output_mem/）
OUTPUT_DIR = os.path.join(BASE_DIR, "output_mem")
CONFLICT_PATH = os.path.join(OUTPUT_DIR, "load_mem_conflicts.json")
REPORT_PATH = os.path.join(OUTPUT_DIR, "load_mem_report.md")

sys.path.insert(0, BASE_DIR)
from clean_load import (  # 复用清洗映射与规范化函数
    norm_date, norm_vendor, norm_category, norm_price_type, norm_product_key,
    _create_tables,
)

VALID_PRICE_TYPES = {"单条", "套装", "默认"}
VALID_HARDWARE_TYPES = {
    "DDR3", "DDR4", "DDR5", "SSD", "HDD", "GPU", "MB",
    "PSU", "MON", "CPU", "PERIPH", "CARD", "OTHER",
}

# hardware_type 兜底推断（与 extract_mem.py 的后处理逻辑一致）
HARDWARE_ABBR = {
    "内存": "OTHER",   # 内存需按 DDR 代际细分，由 infer_ddr_type 处理
    "固态硬盘": "SSD", "机械硬盘": "HDD", "显卡": "GPU", "主板": "MB",
    "电源": "PSU", "显示器": "MON", "CPU": "CPU", "外设": "PERIPH",
    "TF卡": "CARD", "SD卡": "CARD", "U盘": "CARD",
}
_DDR_RE = re.compile(r"ddr\s*([345])", re.I)


def infer_ddr_type(name: str) -> str:
    """从型号名推断 DDR 代际：显式标记优先，其次频率启发式。"""
    m = _DDR_RE.search(str(name))
    if m:
        return f"DDR{m.group(1)}"
    m = re.search(r"(\d{3,4})", str(name))
    if m:
        freq = int(m.group(1))
        if 4000 <= freq <= 12000:
            return "DDR5"
        if 800 <= freq <= 2133:
            return "DDR4"
    return "OTHER"


def infer_hardware_type(item: dict) -> str:
    """按 category 兜底推断 hardware_type：内存按 DDR 代际细分，其余用类别缩写映射。"""
    category = str(item.get("category", "")).strip()
    if category == "内存":
        return infer_ddr_type(item.get("product_name", ""))
    return HARDWARE_ABBR.get(category, "OTHER")


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

    # price_type：内存枚举为 单条/套装/默认；未在 PRICE_TYPE_MAP 中的标签（如保固/国行/盒装）拒绝
    raw_ptype = str(item.get("price_type") or "").strip()
    ptype = norm_price_type(raw_ptype)
    if raw_ptype and raw_ptype not in ("单条", "套装", "默认", "套价", ""):
        return False, f"price_type 非法: {item.get('price_type')!r}"
    if ptype not in VALID_PRICE_TYPES:
        return False, f"price_type 非法: {item.get('price_type')!r}"

    # hardware_type：非法时兜底推断（不拒绝，只补齐）
    hw = str(item.get("hardware_type") or "").strip()
    if hw not in VALID_HARDWARE_TYPES:
        hw = infer_hardware_type(item)
        if hw not in VALID_HARDWARE_TYPES:
            hw = "OTHER"
        item["hardware_type"] = hw   # 回填，入库时使用

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
    """库内去重（load_mem 版）：同 (date_key, product_key, price_type) 多条时，
    价格相同 -> 只留一条；价格不同 -> 记录冲突到本模块 _conflicts 字典
    （冲突明细归本模块，写入 load_mem_conflicts.json）。"""
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

    sheet_date = norm_date(sheet_date_raw)

    # 幂等：删同 source_image 旧记录
    cur.execute("DELETE FROM quotes WHERE source_image = ?", (source_image,))

    for item in valid_rows:
        name = str(item.get("product_name")).strip()
        price = float(item.get("price"))
        vendor = norm_vendor(item.get("vendor"))
        ptype = norm_price_type(item.get("price_type"))
        hw = str(item.get("hardware_type") or "OTHER")

        pkey = norm_product_key(name, vendor)
        row = cur.execute("SELECT 1 FROM products WHERE product_key=?", (pkey,)).fetchone()
        if not row:
            # 展示名：MEM 保留规格（容量/代际/频率/时序是区分型号的必要信息），
            # 仅剥离 CPU 类目的规格后缀和描述文字
            clean_name = str(name).strip()
            if str(item.get("category", "")).strip() == "CPU":
                clean_name = re.split(r"\d{1,2}核\d{0,3}线程", clean_name)[0]
                clean_name = re.split(r"\d+\.\d+", clean_name)[0].strip()
                clean_name = re.sub(r"（[^）]*奔腾[^）]*）.*$|\([^)]*奔腾[^）]*\).*$", "", clean_name).strip()
            clean_name = clean_name or name
            display = f"{vendor} {clean_name}".strip()
            cur.execute("INSERT INTO products VALUES (?,?,?,?)",
                        (pkey, display, norm_category(item.get("category")), vendor))
            n_prod += 1

        cur.execute(
            "INSERT OR IGNORE INTO dates(date_key, year, month, day, weekday) VALUES (?,?,?,?,?)",
            (sheet_date, int(sheet_date[:4]), int(sheet_date[5:7]),
             int(sheet_date[8:10]), datetime.date.fromisoformat(sheet_date).weekday()))

        cur.execute(
            "INSERT INTO quotes (product_key, date_key, price, price_type, source_image, hardware_type) VALUES (?,?,?,?,?,?)",
            (pkey, sheet_date, price, ptype, source_image, hw))
        n_quote += 1

    conn.commit()
    return (passed, skipped, n_prod, problems)


# ============ 主流程 ============

def run_import(out_dir: str = None) -> dict:
    """完整导入主流程（供 extract_mem.py --db 复用，也可由 CLI 直接触发）。
    返回统计 dict：passed / skipped / new_products / dup_same / conflicts / files。"""
    global CONFLICT_PATH, REPORT_PATH
    if out_dir:
        out_dir = os.path.abspath(out_dir)
    else:
        out_dir = os.path.join(OUTPUT_DIR, "extracted_mem")
    conflict_path = os.path.join(os.path.dirname(out_dir), "load_mem_conflicts.json")
    report_path = os.path.join(os.path.dirname(out_dir), "load_mem_report.md")

    files = load_extracted(out_dir)
    if not files:
        return {"passed": 0, "skipped": 0, "new_products": 0,
                "dup_same": 0, "conflicts": 0, "files": 0}

    conn = get_conn()
    tp = tpass = tskip = 0
    all_problems = []
    for f in files:
        passed, skipped, n_prod, problems = import_file(conn, f)
        tp += n_prod
        tpass += passed
        tskip += skipped
        all_problems.extend([(os.path.basename(f),) + p for p in problems])

    dup_same, n_conf = dedupe_quotes(conn)
    if _conflicts:
        with open(conflict_path, "w", encoding="utf-8") as f:
            json.dump(_conflicts, f, ensure_ascii=False, indent=2)
    conn.close()

    return {"passed": tpass, "skipped": tskip, "new_products": tp,
            "dup_same": dup_same, "conflicts": n_conf, "files": len(files),
            "problems": all_problems}


def main():
    if "--status" in sys.argv:
        status()
        return

    # 默认导入目标：extract_mem.py 的输出目录（与脚本同级的 output_mem/extracted_mem）
    # 可用 --out-dir 对齐 extract_mem.py 的自定义输出目录，或显式传入 JSON 文件/目录
    default_dir = os.path.join(BASE_DIR, "output_mem", "extracted_mem")
    out_dir = default_dir
    if "--out-dir" in sys.argv:
        i = sys.argv.index("--out-dir")
        if i + 1 < len(sys.argv):
            out_dir = os.path.abspath(sys.argv[i + 1])

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

    # 错误输出跟随导入目标所在的输出目录（与 run_import 口径一致）
    global CONFLICT_PATH, REPORT_PATH
    target_dir = target if os.path.isdir(target) else os.path.dirname(os.path.abspath(target))
    conflict_path = os.path.join(target_dir, "load_mem_conflicts.json")
    report_path = os.path.join(target_dir, "load_mem_report.md")

    files = load_extracted(target)
    if not files:
        return

    print(f"共 {len(files)} 个提取结果文件，目标库: {DB_PATH}")
    conn = get_conn()

    tp = tq = tpass = tskip = 0
    all_problems = []
    file_stats = []
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
        "# MEM 数据导入报告",
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
    # MEM 数据以 hardware_type 非空为特征（CPU 管线写入 NULL）
    n_quote = cur.execute(
        "SELECT COUNT(*) FROM quotes WHERE hardware_type IS NOT NULL").fetchone()[0]
    n_dates = cur.execute("SELECT COUNT(*) FROM dates").fetchone()[0]
    print(f"[MEM 导入状态] 库: {os.path.basename(DB_PATH)}")
    print(f"  MEM 价格记录（hardware_type 非空）: {n_quote}")
    print(f"  报价日期: {n_dates}")
    # 按 hardware_type 分布
    rows = cur.execute("""
        SELECT hardware_type, COUNT(*) FROM quotes
        WHERE hardware_type IS NOT NULL
        GROUP BY hardware_type ORDER BY COUNT(*) DESC
    """).fetchall()
    if rows:
        print("  hardware_type 分布:")
        for hw, n in rows:
            print(f"    {hw}: {n} 条")
    conn.close()


if __name__ == "__main__":
    main()
