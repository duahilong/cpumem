# -*- coding: utf-8 -*-
"""
CPU 专用提取管线 —— 与其他类目完全独立。

流程：
    1. 遍历 价格图片/CPU/ 下的全部图片（断点续跑：已有结果 JSON 则跳过）
    2. 三段式锚点裁剪 crop_cpu_image()（像素检测表格线，逐图自适应，缓存 crop_cache/）
    3. OCR 通道 ocr_markdown()：llama.cpp GLM-OCR 转写裁剪图为 Markdown 表格
       （prompt: prompts 外置；temperature=0 确定性输出；缓存 ocr_cache/）
    4. 组装提示词：
       - OCR 可用 → 新结构联合提示词（prompts/OCR主_指令.txt + OCR Markdown），
         OCR 为主、图为辅，两步式提取
       - OCR 失败但有缓存 → 纯 OCR 模式（缓存 Markdown + 指令，无图）
       - OCR 不可用且无缓存 → 纯视觉模式（prompts/base.txt + prompts/CPU.txt + 白名单）
    5. 调用多模态 LLM（配置 llm_config.json），输出 JSON 存档 extracted_cpu/

用法：
    python extract_cpu.py <图片文件或目录> [--workers N] [--status]
    （必须显式传入目标；无参数时只打印用法，不执行管线）

示例：
    python extract_cpu.py ../价格图片/CPU                     # 提取整个目录
    python extract_cpu.py ../价格图片/CPU/0a04….png           # 提取单张
    python extract_cpu.py ../价格图片/CPU --workers 4         # 指定并发
    python extract_cpu.py --status                            # 查看默认目录进度

输出：
    extracted_cpu/<图片名>.json      # 每张图一份提取结果（含 source_image 溯源）
    crop_cache/<图片名>.png          # 裁剪图缓存（复用，避免重复裁剪）
    ocr_cache/<图片名>.md            # OCR Markdown 转写缓存（同图同输出）

环境变量：
    OCR_BASE_URL=... # 覆盖 OCR 服务地址（优先级低于 llm_config.json 的 ocr_base_url）

超时（llm_config.json 配置，防止脚本卡住）：
    timeout_seconds: 300     # LLM 调用超时
    ocr_timeout_seconds: 600 # OCR 调用超时

独立性：
    - 只处理 价格图片/CPU/ 目录
    - 输出到独立目录 extracted_cpu/，与 extracted/（其他类目）互不影响
"""
import os
import sys
import json
import glob
import re
import time
import base64
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed

import numpy as np
from PIL import Image

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
IMG_DIR = os.path.join(os.path.dirname(BASE_DIR), "价格图片", "CPU")   # 只处理 CPU 子目录
OUT_DIR = os.path.join(BASE_DIR, "extracted_cpu")
CROP_DIR = os.path.join(BASE_DIR, "crop_cache")
CONFIG_PATH = os.path.join(BASE_DIR, "llm_config.json")
PROMPTS_DIR = os.path.join(BASE_DIR, "prompts")
WATCHLIST_PATH = os.path.join(BASE_DIR, "cpu_watchlist.json")
JOINT_PROMPT_PATH = os.path.join(PROMPTS_DIR, "OCR主_指令.txt")   # 新结构联合提示词（OCR 为主、图为辅）
os.makedirs(OUT_DIR, exist_ok=True)
os.makedirs(CROP_DIR, exist_ok=True)

MAX_WORKERS = 2    # 默认并发数（可用 --workers N 调整）


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
    """三段式锚点裁剪：像素检测表格线位置，逐图自适应裁剪。

    替代旧版固定 6.2%/42% 裁剪（会切进右侧表格窄边、露出其他产品列头）。
    检测到的锚点在全部 316 张 CPU 图片上验证：
      - 表格左边界 x/w = 0.110 ± 0.0008
      - CPU 区右边界 x/w = 0.399 ± 0.0031（316/316 检测成功）
      - 横幅底部 y/h ≈ 0.090（两种顶条版式统一覆盖）

    三段裁剪：
      段1 横幅区：0 ~ 横幅底部，x 裁到 55%（保住完整日期）
      段2 表头行：横幅底 ~ +3.5%，x 裁到 CPU 右边界锚点（消除其他产品列头）
      段3 表格主体：表头下 ~ 底部，x 到 CPU 右边界锚点 +8px（沿表格线切割）

    结果缓存到 crop_cache/，已存在直接复用。返回裁剪图路径。
    """
    name = os.path.splitext(os.path.basename(img_path))[0]
    out_path = os.path.join(CROP_DIR, name + ".png")
    if os.path.exists(out_path):
        return out_path
    img = Image.open(img_path).convert("RGB")
    w, h = img.size
    arr = np.array(img.convert("L"))

    # 锚点 1：横幅底部（多列采样找持续亮区，避免横幅内白色分隔线误判）
    cols_px = [arr[:, int(w * r)] for r in (0.10, 0.25)]
    banner_end = None
    min_run = max(int(h * 0.01), 3)
    for y in range(int(h * 0.02), int(h * 0.2)):
        if all(c[y] >= 200 for c in cols_px):
            if all(all(c[yy] >= 200 for c in cols_px) for yy in range(y, min(y + min_run, h))):
                banner_end = y
                break
    if not banner_end:
        banner_end = int(h * 0.09)  # 兼容检测失败的历史均值

    # 锚点 2：CPU 区右边界（30%~50% 范围内的最后一根长竖线）
    band = arr[int(h * 0.15):int(h * 0.70)]
    dark_ratio = (band < 150).mean(axis=0)
    cols = [x for x in range(int(w * 0.30), int(w * 0.50)) if dark_ratio[x] > 0.8]
    if not cols:
        # 锚点检测失败：回退到固定比例（历史均值 0.399）
        right = int(w * 0.40)
    else:
        right = max(cols)

    head_h = int(h * 0.035)  # 表头行高度
    s1 = img.crop((0, 0, int(w * 0.55), banner_end))
    s2 = img.crop((0, banner_end, right + 8, banner_end + head_h))
    s3 = img.crop((0, banner_end + head_h, right + 8, h))
    canvas = Image.new("RGB", (w, s1.size[1] + s2.size[1] + s3.size[1]), "white")
    canvas.paste(s1, (0, 0))
    canvas.paste(s2, (0, s1.size[1]))
    canvas.paste(s3, (0, s1.size[1] + s2.size[1]))
    canvas.save(out_path)
    return out_path


# ============ OCR 辅助通道（llama.cpp GLM-OCR） ============

MAX_WORKERS_LIMIT = 6   # 并发上限（OCR/LLM 服务承压限制）
OCR_PROMPT = "识别图片中的所有文字，输出为Markdown格式"
OCR_CACHE_DIR = os.path.join(BASE_DIR, "ocr_cache")
os.makedirs(OCR_CACHE_DIR, exist_ok=True)


def get_ocr_base_url() -> str:
    """OCR 服务地址：优先 llm_config.json 的 ocr_base_url，兼容环境变量覆盖。"""
    try:
        cfg = load_config()
        if cfg.get("ocr_base_url"):
            return cfg["ocr_base_url"]
    except Exception:
        pass
    return os.environ.get("OCR_BASE_URL", "http://127.0.0.1:8080")


def html_table_to_markdown(html: str) -> str:
    """将 GLM-OCR 输出的 HTML 表格统一转成 Markdown 管道表格。
    处理 rowspan/colspan 合并单元格（展开到每个占位格）。"""
    rows_html = re.findall(r'<tr[^>]*>(.*?)</tr>', html, re.S)
    if not rows_html:
        return html  # 无表格结构，原样返回
    grid = {}
    for r, rh in enumerate(rows_html):
        cells_raw = re.findall(r'<(t[dh])((?:\s+[^>]*)?)>(.*?)</\1>', rh, re.S)
        c = 0
        for _tag, attrs, content in cells_raw:
            while (r, c) in grid:
                c += 1
            text = ' '.join(re.sub(r'<[^>]+>', '', content).split())
            rs = re.search(r'rowspan="(\d+)"', attrs)
            cs = re.search(r'colspan="(\d+)"', attrs)
            rowspan = int(rs.group(1)) if rs else 1
            colspan = int(cs.group(1)) if cs else 1
            for dr in range(rowspan):
                for dc in range(colspan):
                    grid[(r + dr, c + dc)] = text
            c += colspan
    if not grid:
        return html
    n_rows = max(r for r, _ in grid) + 1
    n_cols = max(c for _, c in grid) + 1
    lines = []
    for r in range(n_rows):
        cells = [grid.get((r, c), '') for c in range(n_cols)]
        # 竖线转义，避免破坏 Markdown 表格
        cells = [x.replace('|', '\\|') for x in cells]
        lines.append('| ' + ' | '.join(cells) + ' |')
        if r == 0:
            lines.append('|' + '---|' * n_cols)
    return '\n'.join(lines)


def ocr_markdown(img_path: str) -> str:
    """调用 llama.cpp GLM-OCR，返回 Markdown 格式的表格转写文本。
    OCR 输出确定性与图片绑定（同图同输出），结果缓存到 ocr_cache/。
    服务不可用/返回空时抛出 RuntimeError（提示词以 OCR 为主，无 OCR 无法提取）。"""
    name = os.path.splitext(os.path.basename(img_path))[0]
    cache_path = os.path.join(OCR_CACHE_DIR, name + ".md")
    if os.path.exists(cache_path):
        with open(cache_path, encoding="utf-8") as f:
            return f.read()
    crop_path = crop_cpu_image(img_path)
    with open(crop_path, "rb") as f:
        b64 = base64.b64encode(f.read()).decode()
    try:
        import urllib.request
        body = {
            "model": "glm-ocr",
            "messages": [{"role": "user", "content": [
                {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}},
                {"type": "text", "text": OCR_PROMPT},
            ]}],
            "max_tokens": 16384,
            "temperature": 0,
            "stream": False,
        }
        req = urllib.request.Request(
            get_ocr_base_url() + "/v1/chat/completions",
            data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json"},
        )
        # OCR 超时：llm_config.json 的 ocr_timeout_seconds，默认 600 秒，防止脚本卡住
        ocr_timeout = load_config().get("ocr_timeout_seconds", 600)
        r = json.loads(urllib.request.urlopen(req, timeout=ocr_timeout).read())
        text = (r["choices"][0]["message"]["content"] or "").strip()
    except Exception as e:
        raise RuntimeError(f"OCR 服务调用失败（{name[:16]}）: {e}")
    if not text:
        raise RuntimeError(f"OCR 返回空内容（{name[:16]}）")
    md = html_table_to_markdown(text)
    with open(cache_path, "w", encoding="utf-8") as f:
        f.write(md)
    return md


def build_joint_prompt(ocr_md: str) -> str:
    """组装新结构联合提示词（OCR 为主、图为辅）：
    prompts/OCR主_指令.txt 的指令 + OCR Markdown 数据区。
    指令自带完整 JSON 契约和白名单，不再拼接 base.txt/CPU.txt。"""
    instr = load_prompt("OCR主_指令.txt")
    if not instr:
        raise FileNotFoundError(f"未找到联合提示词 {JOINT_PROMPT_PATH}")
    return instr + ocr_md


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


def call_llm(crop_path: str, prompt: str, use_image: bool = True) -> dict:
    """调用多模态 LLM，返回解析后的 JSON。默认随消息传裁剪图。"""
    from openai import OpenAI

    cfg = load_config()
    client = OpenAI(base_url=cfg["base_url"], api_key=cfg["api_key"])

    if use_image:
        with open(crop_path, "rb") as f:
            b64 = base64.b64encode(f.read()).decode()
        content = [
            {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}},
            {"type": "text", "text": prompt},
        ]
    else:
        content = [{"type": "text", "text": prompt}]

    messages = [{"role": "user", "content": content}]
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

def extract_one(img_path: str) -> tuple[str, bool, str]:
    """顺序处理：裁剪 → OCR → 组装提示词 → LLM 提取。
    提示词以 OCR 输出为主（prompts/OCR主_指令.txt + OCR Markdown + 裁剪图），
    OCR 失败/返回空时抛出 RuntimeError，该图计为失败（不降级纯视觉）。"""
    name = os.path.splitext(os.path.basename(img_path))[0]
    out_path = os.path.join(OUT_DIR, name + ".json")
    if os.path.exists(out_path):
        return (name, True, "跳过（已存在）")
    try:
        t0 = time.time()
        crop_path = crop_cpu_image(img_path)
        ocr_md = ocr_markdown(img_path)
        full_prompt = build_joint_prompt(ocr_md)
        data = call_llm(crop_path, full_prompt)
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


# ============ 入口辅助 ============

IMG_EXTS = (".png", ".jpg", ".jpeg")


def collect_images(target: str) -> list[str] | None:
    """根据传入的目标（图片文件或目录）收集待提取图片。
    - target 是图片文件：返回 [该文件]（绝对路径）
    - target 是目录：递归/非递归扫描其中的图片文件
    - 无图片或路径无效：打印错误并返回 None
    """
    if os.path.isfile(target):
        if os.path.splitext(target)[1].lower() not in IMG_EXTS:
            print(f"错误: {target} 不是图片文件（支持 {', '.join(IMG_EXTS)}）")
            return None
        return [os.path.abspath(target)]
    if os.path.isdir(target):
        images = []
        for root, _dirs, files in os.walk(target):
            for f in sorted(files):
                if os.path.splitext(f)[1].lower() in IMG_EXTS:
                    images.append(os.path.join(root, f))
        images.sort()
        if not images:
            print(f"错误: 目录 {target} 下没有找到图片文件（支持 {', '.join(IMG_EXTS)}）")
            return None
        return images
    print(f"错误: 路径不存在: {target}")
    return None


def main():
    # --status：查看默认目录（价格图片/CPU/）进度
    if "--status" in sys.argv:
        status()
        return

    # --workers N：调整并发数（默认 2，上限 6）
    workers = MAX_WORKERS
    if "--workers" in sys.argv:
        i = sys.argv.index("--workers")
        if i + 1 < len(sys.argv):
            try:
                workers = int(sys.argv[i + 1])
                if workers < 1:
                    print("错误: --workers 至少为 1")
                    return
                if workers > MAX_WORKERS_LIMIT:
                    print(f"提示: 并发数不能超过上限 {MAX_WORKERS_LIMIT}，已自动调整为 {MAX_WORKERS_LIMIT}")
                    workers = MAX_WORKERS_LIMIT
            except ValueError:
                print("错误: --workers 需要一个正整数参数，如 --workers 4")
                return

    # 必须显式传入目标（图片文件或目录），否则打印用法并退出
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    if "--workers" in sys.argv:
        i = sys.argv.index("--workers")
        if i + 1 < len(sys.argv):
            args = [a for a in args if a != sys.argv[i + 1]]
    if not args:
        print("用法: python extract_cpu.py <图片文件或目录> [--workers N] [--status]")
        print("示例:")
        print(f"  python {os.path.basename(sys.argv[0])} ../价格图片/CPU")
        print(f"  python {os.path.basename(sys.argv[0])} ../价格图片/CPU/0a04d486d137d2d382483b63a0f84e78.png")
        print(f"  python {os.path.basename(sys.argv[0])} ../价格图片/CPU --workers 4")
        return

    target = args[0]
    images = collect_images(target)
    if not images:
        return

    print(f"[CPU 管线] 共 {len(images)} 张图片，并发数: {workers}")
    done = sum(1 for img in images
               if os.path.exists(os.path.join(OUT_DIR, os.path.splitext(os.path.basename(img))[0] + ".json")))
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

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(extract_one, img): img for img in images}
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
    """查看默认目录（价格图片/CPU/）的提取进度。"""
    images = []
    for root, _dirs, files in os.walk(IMG_DIR):
        for f in sorted(files):
            if os.path.splitext(f)[1].lower() in IMG_EXTS:
                images.append(os.path.join(root, f))
    images.sort()
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
    main()
