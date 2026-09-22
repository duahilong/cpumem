# -*- coding: utf-8 -*-
"""对比实验：纯视觉（无 OCR）提取测试。

与 extract_cpu.py 的区别：
    - 直接把 crop_cache/ 中的裁剪图发给 LLM
    - Prompt 只用 prompts/OCR主_指令.txt 本身，不拼接 OCR Markdown
    - 输出写入独立目录 output_cpu/extracted_cpu_noocr/，不影响现有管线产物

用途：检验同一提示词下，去掉 OCR 文本后 LLM 的提取质量（型号后缀、价格准确性）。

用法：
    python test_noocr_extract.py <图片文件或目录> [--workers N] [--limit N] [--status]
    无参数时默认处理 crop_cache/ 中全部已有裁剪图。
"""
import os
import sys
import json
import glob
import time
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed

# 复用主管线的函数与配置（load_prompt / call_llm / CROP_DIR 等）
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import extract_cpu as base

BASE_DIR = base.BASE_DIR
SRC_DIR = base.CROP_DIR                              # 裁剪图来源（已有缓存）
OUT_DIR = os.path.join(base.OUTPUT_DIR, "extracted_cpu_noocr")
PROGRESS_LOG = os.path.join(base.OUTPUT_DIR, "test_noocr_progress.log")

IMG_EXTS = (".png", ".jpg", ".jpeg")
MAX_WORKERS = 2
MAX_WORKERS_LIMIT = 6


def build_prompt() -> str:
    """与 OCR 通道相同的 Prompt：只用 OCR主_指令.txt 本身（含 JSON 契约与白名单）。
    不拼接任何 OCR Markdown —— 纯视觉测试。"""
    instr = base.load_prompt("OCR主_指令.txt")
    if not instr:
        raise FileNotFoundError("未找到联合提示词 OCR主_指令.txt")
    return instr


def extract_one(crop_path: str, prompt: str) -> tuple[str, bool, str]:
    """直接把裁剪图 + 指令发给 LLM，结果写入 extracted_cpu_noocr/。"""
    name = os.path.splitext(os.path.basename(crop_path))[0]
    out_path = os.path.join(OUT_DIR, name + ".json")
    if os.path.exists(out_path):
        return (name, True, "跳过（已存在）")
    try:
        t0 = time.time()
        data = base.call_llm(crop_path, prompt)
        data["source_image"] = name + ".png"
        data["_test_noocr"] = True   # 标记为实验产物，防止误入库
        data["products"] = [p for p in data.get("products", [])
                            if str(p.get("category", "")) == "CPU"]
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        n = len(data.get("products", []))
        return (name, True, f"完成 ({n} 条, {time.time()-t0:.0f}s)")
    except Exception as e:
        return (name, False, str(e))


def main():
    os.makedirs(OUT_DIR, exist_ok=True)

    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    # 目标：显式传入图片/目录，否则默认 crop_cache 全部
    if args:
        target = args[0]
        if os.path.isfile(target):
            crops = [os.path.abspath(target)]
        elif os.path.isdir(target):
            crops = []
            for root, _d, files in os.walk(target):
                for f in sorted(files):
                    if os.path.splitext(f)[1].lower() in IMG_EXTS:
                        crops.append(os.path.join(root, f))
        else:
            print(f"错误: 路径不存在: {target}")
            return
    else:
        crops = sorted(glob.glob(os.path.join(SRC_DIR, "*.png")))

    # --limit N：只测前 N 张（快速抽样验证用）
    limit = None
    if "--limit" in sys.argv:
        i = sys.argv.index("--limit")
        limit = int(sys.argv[i + 1])
        crops = crops[:limit]

    workers = MAX_WORKERS
    if "--workers" in sys.argv:
        i = sys.argv.index("--workers")
        try:
            workers = min(max(int(sys.argv[i + 1]), 1), MAX_WORKERS_LIMIT)
        except ValueError:
            print("错误: --workers 需要正整数")
            return

    if not crops:
        print(f"错误: 没有找到测试图片（crop_cache: {SRC_DIR}）")
        return

    prompt = build_prompt()
    print(f"[纯视觉测试] 图片数: {len(crops)}, 并发: {workers}")
    print(f"[纯视觉测试] Prompt: OCR主_指令.txt 本身（不拼接 OCR Markdown）")
    print(f"[纯视觉测试] 输出目录: {OUT_DIR}")
    print(f"[纯视觉测试] 注意: 会真实调用 LLM（消耗配额），裁剪图直接发送\n")

    ok = fail = skipped = 0
    failed_list = []
    lock = threading.Lock()
    progress = {"n": 0}

    def report(name, success, msg):
        nonlocal ok, fail, skipped
        with lock:
            progress["n"] += 1
            idx = progress["n"]
            if msg.startswith("跳过"):
                skipped += 1
            elif success:
                ok += 1
            else:
                fail += 1
                failed_list.append(name)
            print(f"[{idx}/{len(crops)}] {name[:16]}...: {msg}", flush=True)

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(extract_one, c, prompt): c for c in crops}
        for fut in as_completed(futures):
            name, success, msg = fut.result()
            report(name, success, msg)

    print(f"\n[纯视觉测试] 完成: 新提取 {ok}, 跳过 {skipped}, 失败 {fail}")
    print(f"结果存于 {OUT_DIR}")
    if failed_list:
        print("失败清单（重跑自动重试）:")
        for n in failed_list[:10]:
            print(f"  - {n}")
    with open(PROGRESS_LOG, "a", encoding="utf-8") as f:
        f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} 本次新提取={ok} 失败={fail} "
                f"目标={len(crops)} 输出={OUT_DIR}\n")


if __name__ == "__main__":
    main()
