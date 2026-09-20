# -*- coding: utf-8 -*-
"""验证脚本：将提取结果与人工基准（pricebenchmark/0a04-pricebenchmark.xlsx）精确比对，输出准确率报告。

比对口径：
- 人工"散片=x, 原盒=y" → 提取必须有两条记录（x散片 + y原盒）才算对
- 人工某类型为 None → 提取多出该类型算"多提"
- 型号匹配严格：12400F ≠ 12400（F/K/KF 后缀是不同型号）
- U 系特殊：人工写 '15 12490F' 表示 i5 12490F

用法：
    python verify.py <提取JSON路径>          # 如 extracted/test_0a04.json
"""
import sys
import json
import re
import os

sys.stdout.reconfigure(encoding="utf-8")
import openpyxl

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
XLSX_PATH = os.path.join(BASE_DIR, "pricebenchmark", "0a04-pricebenchmark.xlsx")


def norm(name: str) -> str:
    """型号名严格归一：去空格/连字符，统一前缀，小写，去掉核数/频率后缀。
    注意：F/K/KF 后缀保留（是不同型号）。
    集显/带显变体归一到无后缀型号；双型号行（i3 10100F / 10105F）归到第一个型号"""
    s = str(name).strip().lower().replace(" ", "").replace("-", "").replace("_", "")
    # 人工表特例：'15 12490F' 是 i5 12490F（截图打字漏了 i）；'1I9 14900K' 是 i9
    s = re.sub(r"^15(?=\d{4,5})", "i5", s)
    s = re.sub(r"^1i9", "i9", s)
    # 去掉核数/频率后缀：'i312100f4核8线程' -> 'i312100f'；'i513400f10核16线程2.5/4.6' -> 'i513400f'
    # 截断型号主体之后的内容：型号 = 前缀(i3/i5/i7/i9/u5/u7/u9/ryzen/g/inte300...) + 4-5位数字 + 字母后缀
    m = re.match(r"((?:intel|i\d|u\d|ryzen[\d]*|g\d+|inte300)\d{3,5}[a-z]*)", s)
    if m:
        s = m.group(1)
    else:
        s = re.split(r"\d{1,2}核\d{0,3}线程|\d{1,2}核", s)[0]
    # 集显/带显/傲雪等后缀变体归一到无后缀型号
    s = s.replace("集显", "").replace("带显", "").replace("焦显", "")
    s = re.sub(r"(傲雪|凤凰|天蝎座|神藏|猛禽)$", "", s)  # 常见营销后缀
    # 人工表笔误归一：'Inte 300'（少个l）与 'Intel 300' 是同一型号
    s = s.replace("inte300", "intel300")
    # 双型号行：取第一个型号（i310100f/10105f -> i310100f）
    if "/" in s:
        first = s.split("/")[0]
        if re.search(r"\d", first):
            s = first
        else:
            s = s.replace("/", "")
    return s


def load_ground_truth() -> dict:
    """读人工基准 → {norm_key: {'raw': 原名, '散片': x or None, '原盒': y or None}}"""
    wb = openpyxl.load_workbook(XLSX_PATH)
    ws = wb.worksheets[0]
    gt = {}
    for row in ws.iter_rows(min_row=2, values_only=True):
        name, sp, yh = row
        if not name:
            continue
        key = norm(name)
        gt[key] = {"raw": str(name).strip(), "散片": sp, "原盒": yh}
    return gt


def load_extracted(path: str) -> dict:
    """读提取 JSON → {norm_key: {'散片': price or None, '原盒': price or None, 'raws': [...]}}
    双型号行（如 'i3 10100F / 10105  375/490'）：提取若输出为单条，则按第一个型号归档（提取端的问题由 verify 报告）"""
    d = json.load(open(path, encoding="utf-8"))
    ext = {}
    for p in d.get("products", []):
        raw_name = str(p.get("product_name", "")).strip()
        key = norm(raw_name)
        pt = p.get("price_type")
        pr = p.get("price")
        item = ext.setdefault(key, {"散片": None, "原盒": None, "raws": []})
        item["raws"].append(raw_name)
        if pt in ("散片", "原盒") and item[pt] is None:
            try:
                item[pt] = float(pr)
            except (TypeError, ValueError):
                pass
    return ext


def main():
    if len(sys.argv) < 2:
        print("用法: python verify.py <提取JSON路径>")
        return
    ext_path = sys.argv[1]
    gt = load_ground_truth()
    ext = load_extracted(ext_path)

    # 核对点统计
    ok_points, errors = 0, []
    checked_models = set()

    for key, m in gt.items():
        has_price = m["散片"] is not None or m["原盒"] is not None
        e = ext.get(key)
        checked_models.add(key)

        if not has_price:
            # 人工无价：提取也不应有
            if e and (e["散片"] is not None or e["原盒"] is not None):
                prices = {k: v for k, v in e.items() if k in ("散片", "原盒") and v is not None}
                errors.append(("多提", m["raw"], f"人工无价, 提取输出了 {prices}"))
            else:
                ok_points += 1
            continue

        if e is None:
            if m["散片"] is not None:
                errors.append(("漏提", m["raw"], f"人工散片={m['散片']}, 提取无此型号"))
            if m["原盒"] is not None:
                errors.append(("漏提", m["raw"], f"人工原盒={m['原盒']}, 提取无此型号"))
            continue

        for ptype, mval in (("散片", m["散片"]), ("原盒", m["原盒"])):
            if mval is None:
                if e[ptype] is not None:
                    errors.append(("多提", m["raw"], f"人工{ptype}=无价, 提取输出了 {ptype}={e[ptype]}"))
                else:
                    ok_points += 1
                continue
            eval_ = e[ptype]
            if eval_ is None:
                # 数值可能对但类型错了：查另一个类型
                other = e["原盒" if ptype == "散片" else "散片"]
                if other is not None and abs(other - float(mval)) < 0.01:
                    errors.append(("类型错", m["raw"], f"{ptype}: 人工={mval}, 提取数值对但标成了{'原盒' if ptype=='散片' else '散片'}"))
                else:
                    errors.append(("漏提", m["raw"], f"{ptype}: 人工={mval}, 提取无此价"))
            elif abs(eval_ - float(mval)) < 0.01:
                ok_points += 1
            else:
                errors.append(("数值错", m["raw"], f"{ptype}: 人工={mval}, 提取={eval_}"))

    # 提取了但人工清单里完全没有的型号
    for key, e in ext.items():
        if key not in gt:
            prices = {k: v for k, v in e.items() if k in ("散片", "原盒") and v is not None}
            if prices:
                errors.append(("清单外", e["raws"][0], f"人工清单无此型号, 提取输出了 {prices}"))

    total = ok_points + len(errors)
    acc = ok_points / total * 100 if total else 0

    print("=" * 70)
    print(f"验证报告: {os.path.basename(ext_path)}")
    print(f"基准: {os.path.basename(XLSX_PATH)} ({len(gt)} 个型号) | 提取: {len(ext)} 个型号")
    print("-" * 70)
    print(f"✅ 正确: {ok_points}/{total} 核对点 | 准确率: {acc:.1f}%")
    print()
    if errors:
        by_type = {}
        for t, name, msg in errors:
            by_type.setdefault(t, []).append((name, msg))
        for t in ("类型错", "数值错", "漏提", "多提", "清单外"):
            if t in by_type:
                items = by_type[t]
                print(f"❌ [{t}] {len(items)} 项:")
                for name, msg in items:
                    print(f"    {name}: {msg}")
                print()
    else:
        print("完美！全部核对点一致。")


if __name__ == "__main__":
    main()
