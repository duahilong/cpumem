# -*- coding: utf-8 -*-
"""
MEM 专用提取管线 —— 与其他类目完全独立（结构对齐 extract_cpu.py）。

流程：
    1. 遍历 价格图片/mem/ 下的全部图片（断点续跑：已有结果 JSON 则跳过）
    2. OCR 通道 ocr_markdown()：llama.cpp GLM-OCR 转写整图为 Markdown 表格
       （temperature=0 确定性输出；缓存 ocr_cache/）
    3. 组装提示词：prompts/OCR主_指令_MEM.txt + OCR Markdown
       （OCR 为主、原图为辅；指令自带完整 JSON 契约，无 CPU 白名单）
    4. 调用多模态 LLM（配置 llm_config.json），输出 JSON 存档 extracted_mem/
       OCR 失败/返回空 → 抛 RuntimeError，该图计失败（不降级，重跑自动重试）

与 CPU 管线的三处关键差异：
    - 无裁剪预处理：整张原图直送 OCR 和 LLM（mem 图为近方形多区块版式，
      全类目提取，裁剪会切掉目标数据）
    - 无 CPU 白名单
    - 全类目保留：不按 category 过滤，为每条记录补齐 hardware_type

用法：
    python extract_mem.py <图片文件或目录> [--workers N] [--out-dir 目录] [--status]
    （必须显式传入目标；无参数时只打印用法，不执行管线）

示例：
    python extract_mem.py ../价格图片/mem                     # 提取整个目录
    python extract_mem.py ../价格图片/mem/0cd7c….png          # 提取单张
    python extract_mem.py ../价格图片/mem --workers 4         # 指定并发
    python extract_mem.py ../价格图片/mem --out-dir ./my_out  # 指定输出目录
    python extract_mem.py --status                            # 查看默认目录进度

输出（默认 database/output_mem/，与脚本同级；全部管线产物都在其中）：
    output_mem/extracted_mem/<图片名>.json   # 提取结果（含 source_image、hardware_type，
                                             #  断点续跑检查点）
    output_mem/ocr_cache/<图片名>.md         # OCR Markdown 转写缓存（同图同输出）
    output_mem/extract_mem_progress.log      # 提取进度日志

环境变量：
    OCR_BASE_URL=... # 覆盖 OCR 服务地址（优先级低于 llm_config.json 的 ocr_base_url）

超时（llm_config.json 配置，防止脚本卡住）：
    timeout_seconds: 300     # LLM 调用超时
    ocr_timeout_seconds: 600 # OCR 调用超时

独立性：
    - 只处理 价格图片/mem/ 目录
    - 全部产物写入统一的输出目录（默认 database/output_mem/），与其他类目互不影响
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

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
IMG_DIR = os.path.join(os.path.dirname(BASE_DIR), "价格图片", "mem")   # --status 默认查看目录
CONFIG_PATH = os.path.join(BASE_DIR, "llm_config.json")
PROMPTS_DIR = os.path.join(BASE_DIR, "prompts")
JOINT_PROMPT_PATH = os.path.join(PROMPTS_DIR, "OCR主_指令_MEM.txt")   # MEM 联合提示词（OCR 为主、图为辅）

# 默认输出目录：与脚本同级（database/output_mem/），可用 --out-dir 调整。
# 输出目录包含管线产生的全部文件：结果 JSON、OCR Markdown 缓存。
OUTPUT_DIR = os.path.join(BASE_DIR, "output_mem")
OUT_DIR = os.path.join(OUTPUT_DIR, "extracted_mem")
OCR_CACHE_DIR = os.path.join(OUTPUT_DIR, "ocr_cache")
PROGRESS_LOG = os.path.join(OUTPUT_DIR, "extract_mem_progress.log")

MAX_WORKERS = 2    # 默认并发数（可用 --workers N 调整）
MAX_WORKERS_LIMIT = 6   # 并发上限（OCR/LLM 服务承压限制）


# ============ 提示词（MEM 专用） ============

def load_prompt(fname: str, default: str = "") -> str:
    path = os.path.join(PROMPTS_DIR, fname)
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            return f.read()
    return default


def build_joint_prompt(ocr_md: str) -> str:
    """组装 MEM 联合提示词（OCR 为主、图为辅）：
    prompts/OCR主_指令_MEM.txt 的指令 + OCR Markdown 数据区。
    指令自带完整 JSON 契约（含 hardware_type），无 CPU 白名单，
    不再拼接 base.txt/mem.txt。"""
    instr = load_prompt("OCR主_指令_MEM.txt")
    if not instr:
        raise FileNotFoundError(f"未找到联合提示词 {JOINT_PROMPT_PATH}")
    return instr + ocr_md


# ============ OCR 辅助通道（llama.cpp GLM-OCR） ============

OCR_PROMPT = "识别图片中的所有文字，输出为Markdown格式"


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
    # 无裁剪：整张原图直送 OCR
    with open(img_path, "rb") as f:
        b64 = base64.b64encode(f.read()).decode()
    ext = os.path.splitext(img_path)[1].lower().lstrip(".")
    mime = "image/jpeg" if ext in ("jpg", "jpeg") else "image/png"
    try:
        import urllib.request
        body = {
            "model": "glm-ocr",
            "messages": [{"role": "user", "content": [
                {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}},
                {"type": "text", "text": OCR_PROMPT},
            ]}],
            # 输出上限：llm_config.json 的 ocr_max_tokens（默认 16384）。
            # 实测教训：GLM-OCR 在合理位置完成输出后若仍有余量，会陷入重复退化
            # （同一行重复直到耗尽 token），所以上限不是越大越好，16384 是平衡点。
            "max_tokens": load_config().get("ocr_max_tokens", 16384),
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


def call_llm(img_path: str, prompt: str) -> dict:
    """调用多模态 LLM，返回解析后的 JSON。随消息传原整图（无裁剪）。"""
    from openai import OpenAI

    cfg = load_config()
    client = OpenAI(base_url=cfg["base_url"], api_key=cfg["api_key"])

    ext = os.path.splitext(img_path)[1].lower().lstrip(".")
    mime = "image/jpeg" if ext in ("jpg", "jpeg") else "image/png"
    with open(img_path, "rb") as f:
        b64 = base64.b64encode(f.read()).decode()
    content = [
        {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}},
        {"type": "text", "text": prompt},
    ]

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


# ============ 后处理：hardware_type 补齐 ============

# 硬件类别缩写映射：category -> hardware_type 兜底值
HARDWARE_ABBR = {
    "内存": "UNKNOWN_DDR",   # 内存需按 DDR 代际细分，由 infer_ddr_type 处理
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


# ============ 提取单张 ============

def extract_one(img_path: str) -> tuple[str, bool, str]:
    """顺序处理：OCR → 组装提示词 → LLM 提取。
    提示词以 OCR 输出为主（prompts/OCR主_指令_MEM.txt + OCR Markdown + 原整图），
    OCR 失败/返回空时抛出 RuntimeError，该图计为失败（不降级纯视觉）。"""
    name = os.path.splitext(os.path.basename(img_path))[0]
    out_path = os.path.join(OUT_DIR, name + ".json")
    if os.path.exists(out_path):
        return (name, True, "跳过（已存在）")
    try:
        t0 = time.time()
        ocr_md = ocr_markdown(img_path)
        full_prompt = build_joint_prompt(ocr_md)
        data = call_llm(img_path, full_prompt)
        data["source_image"] = os.path.basename(img_path)
        # 保留全类目（mem 报价单常同时含内存/固态/主板/显卡等，不剔除非内存产品），
        # 并补齐 hardware_type 字段（LLM 漏填或非法值时按 category + 型号名兜底推断）
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


# ============ 入口辅助 ============

IMG_EXTS = (".png", ".jpg", ".jpeg")


def collect_images(target: str) -> list[str] | None:
    """根据传入的目标（图片文件或目录）收集待提取图片。
    - target 是图片文件：返回 [该文件]（绝对路径）
    - target 是目录：递归扫描其中的图片文件
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
    # --status：查看默认目录（价格图片/mem/）进度
    if "--status" in sys.argv:
        status()
        return

    # --out-dir <目录>：输出目录（默认 database/output_mem/，与脚本同级）
    global OUTPUT_DIR, OUT_DIR, OCR_CACHE_DIR, PROGRESS_LOG
    if "--out-dir" in sys.argv:
        i = sys.argv.index("--out-dir")
        if i + 1 >= len(sys.argv):
            print("错误: --out-dir 需要一个目录参数")
            return
        OUTPUT_DIR = os.path.abspath(sys.argv[i + 1])
        OUT_DIR = os.path.join(OUTPUT_DIR, "extracted_mem")
        OCR_CACHE_DIR = os.path.join(OUTPUT_DIR, "ocr_cache")
        PROGRESS_LOG = os.path.join(OUTPUT_DIR, "extract_mem_progress.log")
    os.makedirs(OUT_DIR, exist_ok=True)
    os.makedirs(OCR_CACHE_DIR, exist_ok=True)

    # 目标收集：除 --out-dir 参数值外的第一个位置参数
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    if "--out-dir" in sys.argv:
        i = sys.argv.index("--out-dir")
        if i + 1 < len(sys.argv):
            args = [a for a in args if a != sys.argv[i + 1]]
    if not args:
        print(__doc__)
        return
    images = collect_images(args[0])
    if not images:
        return

    # 并发数（上限钳制）
    workers = MAX_WORKERS
    if "--workers" in sys.argv:
        i = sys.argv.index("--workers")
        if i + 1 < len(sys.argv):
            try:
                workers = int(sys.argv[i + 1])
            except ValueError:
                print(f"错误: --workers 需要一个整数参数")
                return
            if workers > MAX_WORKERS_LIMIT:
                print(f"提示: 并发数不能超过上限 {MAX_WORKERS_LIMIT}，已自动调整为 {MAX_WORKERS_LIMIT}")
                workers = MAX_WORKERS_LIMIT
            if workers < 1:
                workers = 1

    print(f"[MEM 管线] 共 {len(images)} 张图片，并发数: {workers}")
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

    print(f"\n[MEM 管线] 提取完成: 新提取 {ok}, 跳过 {skipped}, 失败 {fail}，结果存于 {OUT_DIR}")
    if failed_list:
        print("失败清单（重跑 extract_mem.py 会自动重试）:")
        for n in failed_list[:10]:
            print(f"  - {n}")
        if len(failed_list) > 10:
            print(f"  ... 等共 {len(failed_list)} 张")
    total_done = sum(1 for img in images
                     if os.path.exists(os.path.join(OUT_DIR, os.path.splitext(os.path.basename(img))[0] + ".json")))
    with open(PROGRESS_LOG, "a", encoding="utf-8") as f:
        f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} 本次新提取={ok} 失败={fail} 总进度={total_done}/{len(images)}\n")


def status():
    """查看提取进度（断点状态）。--status 是唯一无副作用模式。"""
    exts = ("*.png", "*.jpg", "*.jpeg")
    images = []
    for e in exts:
        images.extend(glob.glob(os.path.join(IMG_DIR, e)))
    images.sort()
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
    main()
