# -*- coding: utf-8 -*-
"""
CPU 专用提取管线 —— 与其他类目完全独立。

流程：
    1. 遍历 价格图片/CPU/ 下的全部图片
    2. 左半裁剪预处理（顶条全宽保日期 + 左侧 42% CPU 列，右侧白色覆盖消除幻觉源）
    3. 组装 CPU 专用提示词（prompts/base.txt + prompts/CPU.txt + cpu_watchlist.json 白名单）
    4. 调用多模态 LLM（配置 llm_config.json），输出 JSON 存档 extracted_cpu/

用法：
    python extract_cpu.py                # 批量提取（10并发，断点续跑）
    python extract_cpu.py --file 0a04d486d137d2d382483b63a0f84e78.png   # 单张测试
    python extract_cpu.py --status       # 查看提取进度

输出：
    extracted_cpu/<图片名>.json      # 每张图一份提取结果（含 source_image 溯源）
    crop_cache/<图片名>.png          # 裁剪图缓存（复用，避免重复裁剪）

独立性：
    - 只处理 价格图片/CPU/ 目录
    - 只用 prompts/base.txt + prompts/CPU.txt（不加载 mem/TF/其他 的规则）
    - 只施加 CPU 白名单
    - 输出到独立目录 extracted_cpu/，与 extracted/（其他类目）互不影响
"""
import os
import sys
import json
import glob
import time
import base64
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed

from PIL import Image

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
IMG_DIR = os.path.join(os.path.dirname(BASE_DIR), "价格图片", "CPU")   # 只处理 CPU 子目录
OUT_DIR = os.path.join(BASE_DIR, "extracted_cpu")
CROP_DIR = os.path.join(BASE_DIR, "crop_cache")
CONFIG_PATH = os.path.join(BASE_DIR, "llm_config.json")
PROMPTS_DIR = os.path.join(BASE_DIR, "prompts")
WATCHLIST_PATH = os.path.join(BASE_DIR, "cpu_watchlist.json")
os.makedirs(OUT_DIR, exist_ok=True)
os.makedirs(CROP_DIR, exist_ok=True)

MAX_WORKERS = 10   # 并发数


# ============ 提示词（CPU 专用） ============

def load_prompt(fname: str, default: str = "") -> str:
    path = os.path.join(PROMPTS_DIR, fname)
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            return f.read()
    return default


def build_cpu_prompt() -> str:
    """组装 CPU 专用提示词：base + CPU 分类规则 + 白名单。"""
    prompt = load_prompt("base.txt") + "\n" + load_prompt("CPU.txt")

    if os.path.exists(WATCHLIST_PATH):
        try:
            wl = json.load(open(WATCHLIST_PATH, encoding="utf-8"))
            if wl.get("enabled"):
                intel = wl.get("Intel", [])
                amd = wl.get("AMD", "")
                amd_text = "、".join(amd) if isinstance(amd, list) else str(amd)
                wl_text = "\n【CPU 提取白名单】只提取以下 CPU 型号的价格，其他 CPU 型号全部忽略：\n"
                if intel:
                    wl_text += "Intel 及其他型号：" + "、".join(intel) + "\n"
                if amd_text:
                    wl_text += f"AMD 型号：{amd_text}\n"
                wl_text += """【白名单匹配规则】（用于处理图片中不规范的型号书写方式）：
1. 一行含多个型号（用 / 或 - 分隔，如 "i7 10700F/10700"、"i3 4160/4170"）：逐个拆开判断，只要其中任一型号在白名单内，就提取该行对应型号的价格（只输出白名单内的型号）
2. 前缀变体等价："US" = "U5"、"I5" = "i5"、大小写不敏感，视为同一型号
3. 后缀变体等价："U5 225集成"/"U5 225带显"/"U5 225焦显" 是核显版；"xxxF""xxxK""xxxKF" 后缀是独立型号，各自判断是否在白名单内
4. 白名单外的 CPU 型号不要输出；非 CPU 类产品不受白名单限制，照常提取
"""
                prompt += wl_text
        except Exception:
            pass
    return prompt


# ============ 裁剪预处理 ============

def crop_cpu_image(img_path: str) -> str:
    """左半裁剪：顶条全宽（6.2% 高度，保日期）+ 左侧 42%（CPU 列），拼接到画布。
    结果缓存到 crop_cache/，已存在直接复用。返回裁剪图路径。"""
    name = os.path.splitext(os.path.basename(img_path))[0]
    out_path = os.path.join(CROP_DIR, name + ".png")
    if os.path.exists(out_path):
        return out_path
    img = Image.open(img_path).convert("RGB")
    w, h = img.size
    top = img.crop((0, 0, w, int(h * 0.062)))
    left = img.crop((0, int(h * 0.062), int(w * 0.42), h))
    canvas = Image.new("RGB", (w, top.size[1] + left.size[1]), "white")
    canvas.paste(top, (0, 0))
    canvas.paste(left, (0, top.size[1]))
    canvas.save(out_path)
    return out_path


# ============ LLM 调用 ============

def load_config() -> dict:
    if not os.path.exists(CONFIG_PATH):
        raise FileNotFoundError(f"未找到配置文件 {CONFIG_PATH}")
    with open(CONFIG_PATH, encoding="utf-8") as f:
        cfg = json.load(f)
    if not cfg.get("api_key") or "你的" in str(cfg.get("api_key", "")):
        raise ValueError("llm_config.json 中的 api_key 尚未填写")
    if not cfg.get("base_url") or "你的" in str(cfg.get("base_url", "")):
        raise ValueError("llm_config.json 中的 base_url 尚未填写")
    return cfg


def call_llm(crop_path: str, prompt: str) -> dict:
    """调用多模态 LLM，返回解析后的 JSON。"""
    from openai import OpenAI

    cfg = load_config()
    client = OpenAI(base_url=cfg["base_url"], api_key=cfg["api_key"])

    with open(crop_path, "rb") as f:
        b64 = base64.b64encode(f.read()).decode()

    messages = [{
        "role": "user",
        "content": [
            {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}},
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
    if text.startswith("```"):
        text = text.split("```")[1]
        if text.startswith("json"):
            text = text[4:]
    return json.loads(text)


# ============ 提取单张 ============

def extract_one(img_path: str, prompt: str) -> tuple[str, bool, str]:
    name = os.path.splitext(os.path.basename(img_path))[0]
    out_path = os.path.join(OUT_DIR, name + ".json")
    if os.path.exists(out_path):
        return (name, True, "跳过（已存在）")
    try:
        t0 = time.time()
        crop_path = crop_cpu_image(img_path)
        data = call_llm(crop_path, prompt)
        data["source_image"] = os.path.basename(img_path)
        # 只保留 CPU 类目的记录（双保险：白名单外或 LLM 串区的记录剔除）
        data["products"] = [p for p in data.get("products", [])
                            if str(p.get("category", "")) == "CPU"]
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

    # --file 单张测试
    if "--file" in sys.argv:
        target = sys.argv[sys.argv.index("--file") + 1]
        hits = [p for p in images if os.path.basename(p) == target
                or p.endswith(target)]
        if not hits:
            print(f"错误: 价格图片/CPU/ 下找不到 {target}")
            return
        images = hits[:1]

    print(f"[CPU 管线] 共 {len(images)} 张图片，并发数: {MAX_WORKERS}")
    done = sum(1 for img in images
               if os.path.exists(os.path.join(OUT_DIR, os.path.splitext(os.path.basename(img))[0] + ".json")))
    print(f"断点续跑: 已完成 {done} 张，待提取 {len(images) - done} 张")

    prompt = build_cpu_prompt()

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

    print(f"\n[CPU 管线] 提取完成: 新提取 {ok}, 跳过 {skipped}, 失败 {fail}，结果存于 {OUT_DIR}")
    if failed_list:
        print("失败清单（重跑 extract_cpu.py 会自动重试）:")
        for n in failed_list[:10]:
            print(f"  - {n}")
        if len(failed_list) > 10:
            print(f"  ... 等共 {len(failed_list)} 张")
    total_done = sum(1 for img in images
                     if os.path.exists(os.path.join(OUT_DIR, os.path.splitext(os.path.basename(img))[0] + ".json")))
    with open(os.path.join(BASE_DIR, "extract_cpu_progress.log"), "a", encoding="utf-8") as f:
        f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} 本次新提取={ok} 失败={fail} 总进度={total_done}/{len(images)}\n")


def status():
    images = get_images()
    done = {os.path.splitext(os.path.basename(p))[0]
            for p in glob.glob(os.path.join(OUT_DIR, "*.json"))}
    pending = [os.path.basename(i) for i in images
               if os.path.splitext(os.path.basename(i))[0] not in done]
    print(f"[CPU 管线] 图片总数: {len(images)}")
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
