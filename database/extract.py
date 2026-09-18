# -*- coding: utf-8 -*-
"""
步骤1：提取 —— 逐张调用多模态 LLM 读取报价图片，输出原始 JSON 存档。

配置：编辑 llm_config.json，填入 base_url / api_key / model（OpenAI 兼容格式）。

用法：
    python extract.py                # 批量提取（调用 llm_config.json 配置的 LLM，10并发，断点续跑）
    python extract.py --file 图片文件名   # 只提取指定图片（单张测试）
    python extract.py --status       # 查看提取进度

输出：
    extracted/<图片名>.json          # 每张图一份原始提取结果
"""
import os
import sys
import json
import glob
import time
import base64
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
IMG_DIR = os.path.join(os.path.dirname(BASE_DIR), "价格图片")
OUT_DIR = os.path.join(BASE_DIR, "extracted")
CONFIG_PATH = os.path.join(BASE_DIR, "llm_config.json")
os.makedirs(OUT_DIR, exist_ok=True)

# ============ 提取提示词（从本地 prompts/ 目录加载，可运行时修改） ============
PROMPTS_DIR = os.path.join(BASE_DIR, "prompts")


def load_prompt(fname: str, default: str = "") -> str:
    """从 prompts/ 目录加载提示词文件；文件不存在时返回 default"""
    path = os.path.join(PROMPTS_DIR, fname)
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            return f.read()
    return default


def build_prompt(img_path: str) -> str:
    """组装提示词：基础规则(prompts/base.txt) + 分类专用规则(prompts/<分类>.txt) + CPU白名单(cpu_watchlist.json)。
    分类专用文件名 = 图片所在文件夹名；找不到时回退到 base。"""
    parent = os.path.basename(os.path.dirname(img_path))
    base = load_prompt("base.txt")
    category_rule = load_prompt(f"{parent}.txt")
    prompt = base
    if category_rule and category_rule != base:
        prompt += "\n" + category_rule

    # CPU 白名单：只在提取 CPU 类目时限制型号范围
    wl_path = os.path.join(BASE_DIR, "cpu_watchlist.json")
    if os.path.exists(wl_path):
        try:
            wl = json.load(open(wl_path, encoding="utf-8"))
            if wl.get("enabled"):
                intel = wl.get("Intel", [])
                amd = wl.get("AMD", "")
                wl_text = "\n【CPU 提取白名单】只提取以下 CPU 型号的价格，其他 CPU 型号全部忽略：\n"
                if intel:
                    wl_text += "Intel 型号（第10代及以后）：" + "、".join(intel) + "\n"
                if amd:
                    wl_text += f"AMD 型号：{amd}\n"
                wl_text += """【白名单匹配规则】（用于处理图片中不规范的型号书写方式）：
1. 一行含多个型号（用 / 或 - 分隔，如 "i7 10700F/10700"、"i3 4160/4170"）：逐个拆开判断，只要其中任一型号在白名单内，就提取该行对应型号的价格（只输出白名单内的型号）
2. 前缀变体等价："US" = "U5"、"I5" = "i5"、大小写不敏感，视为同一型号
3. 后缀变体等价："U5 225集成"/"U5 225带显"/"U5 225焦显" 是核显版；"xxxF""xxxK""xxxKF" 后缀是独立型号，各自判断是否在白名单内
4. 白名单外的 CPU 型号不要输出；非 CPU 类产品不受白名单限制，照常提取
"""
                prompt += wl_text
        except Exception:
            pass  # 白名单文件损坏时忽略，全量提取
    return prompt


def load_config() -> dict:
    if not os.path.exists(CONFIG_PATH):
        raise FileNotFoundError(f"未找到配置文件 {CONFIG_PATH}，请先填入 base_url / api_key / model")
    with open(CONFIG_PATH, encoding="utf-8") as f:
        cfg = json.load(f)
    if "你的" in str(cfg.get("api_key", "")) or not cfg.get("api_key"):
        raise ValueError("llm_config.json 中的 api_key 尚未填写")
    if "你的" in str(cfg.get("base_url", "")) or not cfg.get("base_url"):
        raise ValueError("llm_config.json 中的 base_url 尚未填写")
    return cfg


def real_extract(img_path: str) -> dict:
    """真实模式：调用 OpenAI 兼容接口的多模态 LLM（配置见 llm_config.json）。"""
    from openai import OpenAI

    cfg = load_config()
    client = OpenAI(base_url=cfg["base_url"], api_key=cfg["api_key"])

    ext = os.path.splitext(img_path)[1].lower().lstrip(".")
    mime = "image/jpeg" if ext in ("jpg", "jpeg") else "image/png"
    with open(img_path, "rb") as f:
        b64 = base64.b64encode(f.read()).decode()

    prompt = build_prompt(img_path)

    # temperature 兼容性：部分模型（如 kimi）只允许 temperature=1，读取配置或自动降级
    try:
        resp = client.chat.completions.create(
            model=cfg["model"],
            messages=[{
                "role": "user",
                "content": [
                    {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}},
                    {"type": "text", "text": prompt},
                ],
            }],
            temperature=cfg.get("temperature", 0),
            timeout=cfg.get("timeout_seconds", 300),
            extra_body=cfg.get("extra_body"),
        )
    except Exception as e:
        if "Temperature" in str(e) or "temperature" in str(e):
            # temperature 不被支持，用默认值重试
            resp = client.chat.completions.create(
                model=cfg["model"],
                messages=[{
                    "role": "user",
                    "content": [
                        {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}},
                        {"type": "text", "text": prompt},
                    ],
                }],
                timeout=cfg.get("timeout_seconds", 300),
                extra_body=cfg.get("extra_body"),
            )
        else:
            raise
    text = resp.choices[0].message.content.strip()
    # 剥掉可能的 markdown 代码块包裹
    if text.startswith("```"):
        text = text.split("```")[1]
        if text.startswith("json"):
            text = text[4:]
    data = json.loads(text)
    data["_source"] = os.path.basename(img_path)
    return data


MAX_WORKERS = 10   # 并发数：同时向 LLM 发起的请求数（限流失败多则降回 5）


def extract_one(img_path: str) -> tuple[str, bool, str]:
    """提取单张图片，返回 (图片名, 成功, 消息)。线程安全：每张图独立落盘。"""
    name = os.path.splitext(os.path.basename(img_path))[0]
    out_path = os.path.join(OUT_DIR, name + ".json")
    if os.path.exists(out_path):
        return (name, True, "跳过（已存在）")
    try:
        t0 = time.time()
        data = real_extract(img_path)
        data["_source_image"] = os.path.basename(img_path)
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        n = len(data.get("products", []))
        return (name, True, f"完成 ({n} 条, {time.time()-t0:.0f}s)")
    except Exception as e:
        return (name, False, str(e))


def main():
    # --file 指定单张图片（测试用，可以是文件名或相对 价格图片/ 的路径）
    if "--file" in sys.argv:
        target = sys.argv[sys.argv.index("--file") + 1]
        images = [os.path.join(IMG_DIR, target)]
        if not os.path.exists(images[0]):
            # 尝试按文件名在子文件夹中查找
            hits = [p for p in glob.glob(os.path.join(IMG_DIR, "**", "*"), recursive=True)
                    if os.path.basename(p) == target]
            if hits:
                images = hits[:1]
            else:
                print(f"错误: 找不到图片 {target}")
                return
    else:
        exts = ("*.png", "*.jpg", "*.jpeg")
        images = []
        for e in exts:
            images.extend(glob.glob(os.path.join(IMG_DIR, "**", e), recursive=True))
        images.sort()

    print(f"共 {len(images)} 张图片，并发数: {MAX_WORKERS}")
    done = sum(1 for img in images if os.path.exists(os.path.join(OUT_DIR, os.path.splitext(os.path.basename(img))[0] + ".json")))
    print(f"断点续跑: 已完成 {done} 张，待提取 {len(images) - done} 张")

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
        futures = {pool.submit(extract_one, img): img for img in images}
        for fut in as_completed(futures):
            name, success, msg = fut.result()
            report(name, success, msg)

    print(f"\n提取完成: 新提取 {ok}, 跳过 {skipped}, 失败 {fail}，结果存于 {OUT_DIR}")
    if failed_list:
        print("失败清单（重跑 extract.py 会自动重试）:")
        for n in failed_list[:10]:
            print(f"  - {n}")
        if len(failed_list) > 10:
            print(f"  ... 等共 {len(failed_list)} 张")
    # 写入进度日志（断点续跑状态）
    total_done = sum(1 for img in images if os.path.exists(os.path.join(OUT_DIR, os.path.splitext(os.path.basename(img))[0] + ".json")))
    with open(os.path.join(BASE_DIR, "extract_progress.log"), "a", encoding="utf-8") as f:
        f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} 本次新提取={ok} 失败={fail} 总进度={total_done}/{len(images)}\n")


def status():
    """查看提取进度（断点状态）"""
    exts = ("*.png", "*.jpg", "*.jpeg")
    images = []
    for e in exts:
        images.extend(glob.glob(os.path.join(IMG_DIR, "**", e), recursive=True))
    done = [os.path.basename(p) for p in glob.glob(os.path.join(OUT_DIR, "*.json"))]
    done_set = {os.path.splitext(n)[0] for n in done}
    pending = [os.path.basename(i) for i in images if os.path.splitext(os.path.basename(i))[0] not in done_set]
    print(f"图片总数: {len(images)}")
    print(f"已提取: {len(done_set)}")
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
