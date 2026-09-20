# -*- coding: utf-8 -*-
"""
MEM（内存）专用提取管线 —— 与其他类目完全独立。

流程：
    1. 遍历 价格图片/mem/ 下的全部图片
    2. 组装 MEM 专用提示词（prompts/base.txt + prompts/mem.txt）
    3. 调用多模态 LLM（配置 llm_config.json），输出 JSON 存档 extracted_mem/

用法：
    python extract_mem.py                # 批量提取（10并发，断点续跑）
    python extract_mem.py --file 0cd7c32940974ecba18fcb159d4c5568.png   # 单张测试
    python extract_mem.py --status       # 查看提取进度

输出：
    extracted_mem/<图片名>.json      # 每张图一份提取结果（含 source_image 溯源）

独立性：
    - 只处理 价格图片/mem/ 目录
    - 只用 prompts/base.txt + prompts/mem.txt（不加载 CPU/TF/其他 的规则）
    - 不施加 CPU 白名单（mem 图片与白名单无关）
    - 输出到独立目录 extracted_mem/，与 extracted/、extracted_cpu/ 互不影响
    - 保留图片中的全部类目（mem 报价单常同时含内存/固态/主板/显卡等），
      并为每条记录补齐 hardware_type 类别缩写字段（DDR3/DDR4/DDR5/SSD/HDD/GPU/MB/PSU/...）
"""
import os
import sys
import json
import glob
import time
import base64
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed

import re

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
IMG_DIR = os.path.join(os.path.dirname(BASE_DIR), "价格图片", "mem")   # 只处理 mem 子目录
OUT_DIR = os.path.join(BASE_DIR, "extracted_mem")
CONFIG_PATH = os.path.join(BASE_DIR, "llm_config.json")
PROMPTS_DIR = os.path.join(BASE_DIR, "prompts")
os.makedirs(OUT_DIR, exist_ok=True)

MAX_WORKERS = 10   # 并发数

# 硬件类别缩写映射：category -> hardware_type 兜底值
HARDWARE_ABBR = {
    "内存": "UNKNOWN_DDR",   # 内存需按 DDR 代际细分，由 infer_hardware_type 处理
    "固态硬盘": "SSD", "机械硬盘": "HDD", "显卡": "GPU", "主板": "MB",
    "电源": "PSU", "显示器": "MON", "CPU": "CPU", "外设": "PERIPH",
    "TF卡": "CARD", "SD卡": "CARD", "U盘": "CARD",
}

# DDR 代际识别：优先从型号名中匹配显式 DDR 标记，匹配不到时按频率启发式判断
DDR_RE = re.compile(r"ddr\s*([345])", re.I)


def infer_ddr_type(name: str) -> str:
    """从型号名推断 DDR 代际：显式 DDR 标记优先，其次按频率启发式（<=2133 DDR3/DDR4，>=4000 DDR5）。"""
    m = DDR_RE.search(str(name))
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


# ============ 提示词（MEM 专用） ============

def load_prompt(fname: str, default: str = "") -> str:
    path = os.path.join(PROMPTS_DIR, fname)
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            return f.read()
    return default


def build_mem_prompt() -> str:
    """组装 MEM 专用提示词：base 通用契约 + mem 分类规则。不加载任何其他类目的规则，不施加 CPU 白名单。"""
    return load_prompt("base.txt") + "\n" + load_prompt("mem.txt")


# ============ LLM 调用 ============

def load_config() -> dict:
    if not os.path.exists(CONFIG_PATH):
        raise FileNotFoundError(f"未找到配置文件 {CONFIG_PATH}，请先填入 base_url / api_key / model")
    with open(CONFIG_PATH, encoding="utf-8") as f:
        cfg = json.load(f)
    if not cfg.get("api_key") or "你的" in str(cfg.get("api_key", "")):
        raise ValueError("llm_config.json 中的 api_key 尚未填写")
    if not cfg.get("base_url") or "你的" in str(cfg.get("base_url", "")):
        raise ValueError("llm_config.json 中的 base_url 尚未填写")
    return cfg


def call_llm(img_path: str, prompt: str) -> dict:
    """调用多模态 LLM（图片整张发送，不做裁剪），返回解析后的 JSON。"""
    from openai import OpenAI

    cfg = load_config()
    client = OpenAI(base_url=cfg["base_url"], api_key=cfg["api_key"])

    ext = os.path.splitext(img_path)[1].lower().lstrip(".")
    mime = "image/jpeg" if ext in ("jpg", "jpeg") else "image/png"
    with open(img_path, "rb") as f:
        b64 = base64.b64encode(f.read()).decode()

    messages = [{
        "role": "user",
        "content": [
            {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}},
            {"type": "text", "text": prompt},
        ],
    }]
    kwargs = dict(
        model=cfg["model"],
        messages=messages,
        timeout=cfg.get("timeout_seconds", 300),
        extra_body=cfg.get("extra_body"),
    )
    try:
        resp = client.chat.completions.create(temperature=cfg.get("temperature", 0), **kwargs)
    except Exception as e:
        if "temperature" in str(e).lower():
            resp = client.chat.completions.create(**kwargs)  # temperature 不被支持时用默认值
        else:
            raise

    text = (resp.choices[0].message.content or "").strip()
    # 剥掉可能的 markdown 代码块包裹
    if text.startswith("```"):
        text = text.split("```")[1]
        if text.startswith("json"):
            text = text[4:]
    return json.loads(text)


# ============ 提取单张 ============

def extract_one(img_path: str, prompt: str) -> tuple[str, bool, str]:
    """提取单张图片，返回 (图片名, 成功, 消息)。线程安全：每张图独立落盘。"""
    name = os.path.splitext(os.path.basename(img_path))[0]
    out_path = os.path.join(OUT_DIR, name + ".json")
    if os.path.exists(out_path):
        return (name, True, "跳过（已存在）")
    try:
        t0 = time.time()
        data = call_llm(img_path, prompt)
        data["source_image"] = os.path.basename(img_path)
        # 补齐 hardware_type 字段（LLM 漏填或非法值时按 category + 型号名兜底推断）
        # 保留图片中的全部类目（内存/固态/主板/显卡等），不剔除非内存产品
        valid_hw = set(HARDWARE_ABBR.values()) | {"DDR3", "DDR4", "DDR5"}
        for p in data.get("products", []):
            if not p.get("hardware_type") or p.get("hardware_type") not in valid_hw:
                p["hardware_type"] = infer_hardware_type(p)
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        n = len(data.get("products", []))
        return (name, True, f"完成 ({n} 条, {time.time()-t0:.0f}s)")
    except Exception as e:
        return (name, False, str(e))


# ============ 批量入口 ============

def get_images():
    exts = ("*.png", "*.jpg", "*.jpeg")
    images = []
    for e in exts:
        images.extend(glob.glob(os.path.join(IMG_DIR, e)))
    images.sort()
    return images


def main():
    images = get_images()

    # --file 单张测试（文件名或相对 价格图片/mem/ 的路径）
    if "--file" in sys.argv:
        target = sys.argv[sys.argv.index("--file") + 1]
        hits = [p for p in images
                if os.path.basename(p) == target or p.endswith(target)]
        if not hits:
            print(f"错误: 价格图片/mem/ 下找不到 {target}")
            return
        images = hits[:1]

    print(f"[MEM 管线] 共 {len(images)} 张图片，并发数: {MAX_WORKERS}")
    done = sum(1 for img in images
               if os.path.exists(os.path.join(OUT_DIR, os.path.splitext(os.path.basename(img))[0] + ".json")))
    print(f"断点续跑: 已完成 {done} 张，待提取 {len(images) - done} 张")

    prompt = build_mem_prompt()

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
            print(f"[{idx}/{len(images)}] {name[:16]}...: {msg}", flush=True)

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        futures = {pool.submit(extract_one, img, prompt): img for img in images}
        for fut in as_completed(futures):
            name, success, msg = fut.result()
            report(name, success, msg)

    print(f"\n[MEM 管线] 提取完成: 新提取 {ok}, 跳过 {skipped}, 失败 {fail}，结果存于 {OUT_DIR}")
    if failed_list:
        print("失败清单（重跑 extract_mem.py 会自动重试）:")
        for n in failed_list[:10]:
            print(f"  - {n}")
        if len(failed_list) > 10:
            print(f"  ... 等共 {len(failed_list)} 张")
    total_done = sum(1 for img in images
                     if os.path.exists(os.path.join(OUT_DIR, os.path.splitext(os.path.basename(img))[0] + ".json")))
    with open(os.path.join(BASE_DIR, "extract_mem_progress.log"), "a", encoding="utf-8") as f:
        f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} 本次新提取={ok} 失败={fail} 总进度={total_done}/{len(images)}\n")


def status():
    """查看提取进度（断点状态）"""
    images = get_images()
    done = {os.path.splitext(os.path.basename(p))[0]
            for p in glob.glob(os.path.join(OUT_DIR, "*.json"))}
    pending = [os.path.basename(i) for i in images
               if os.path.splitext(os.path.basename(i))[0] not in done]
    print(f"[MEM 管线] 图片总数: {len(images)}")
    print(f"已提取: {len(done)}")
    print(f"待提取: {len(pending)}")
    if pending:
        print("\n待提取清单（前10个）:")
        for n in pending[:10]:
            print(f"  - {n}")
        if len(pending) > 10:
            print(f"  ... 等共 {len(pending)} 张")


if __name__ == "__main__":
    if "--status" in sys.argv:
        status()
    else:
        main()
