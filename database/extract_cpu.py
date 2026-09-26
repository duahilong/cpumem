# -*- coding: utf-8 -*-
"""
CPU 专用提取管线 —— 与其他类目完全独立。

流程：
    1. 遍历 价格图片/CPU/ 下的全部图片（断点续跑：已有结果 JSON 则跳过）
    2. 三段式锚点裁剪 crop_cpu_image()（像素检测表格线，逐图自适应，缓存 crop_cache/）
    3. OCR 通道 ocr_markdown()：llama.cpp GLM-OCR 转写裁剪图为 Markdown 表格
       （prompt: prompts 外置；temperature=0 确定性输出；缓存 ocr_cache/）
    4. 组装提示词：prompts/OCR主_指令.txt + OCR Markdown
       （OCR 为主、图为辅，两步式提取；指令自带完整 JSON 契约与白名单）
    5. 调用多模态 LLM（配置 llm_config.json），输出 JSON 存档 extracted_cpu/
       OCR 失败/返回空 → 抛 RuntimeError，该图计失败（不降级，重跑自动重试）

用法：
    python extract_cpu.py <图片文件或目录> [--workers N] [--out-dir 目录] [--status]
    （必须显式传入目标；无参数时只打印用法，不执行管线）

示例：
    python extract_cpu.py ../价格图片/CPU                     # 提取整个目录
    python extract_cpu.py ../价格图片/CPU/0a04….png           # 提取单张
    python extract_cpu.py ../价格图片/CPU --workers 4         # 指定并发
    python extract_cpu.py ../价格图片/CPU --out-dir ./my_out  # 指定输出目录
    python extract_cpu.py --status                            # 查看默认目录进度

输出（默认 database/output_cpu/，与脚本同级；全部管线产物都在其中）：
    output_cpu/extracted_cpu/<图片名>.json   # 提取结果（含 source_image 溯源，断点续跑检查点）
    output_cpu/crop_cache/<图片名>.png       # 裁剪图缓存（复用，避免重复裁剪）
    output_cpu/ocr_cache/<图片名>.md         # OCR Markdown 转写缓存（同图同输出）
    output_cpu/extract_cpu_progress.log      # 提取进度日志

环境变量：
    OCR_BASE_URL=... # 覆盖 OCR 服务地址（优先级低于 llm_config.json 的 ocr_base_url）

超时（llm_config.json 配置，防止脚本卡住）：
    timeout_seconds: 300     # LLM 调用超时
    ocr_timeout_seconds: 600 # OCR 调用超时

独立性：
    - 只处理 价格图片/CPU/ 目录
    - 全部产物写入统一的输出目录（默认 database/output_cpu/），与其他类目互不影响
"""
import os
import sys
import json
import glob
import re
import time
import base64
import hashlib
import signal
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed

import numpy as np
from PIL import Image

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
IMG_DIR = os.path.join(os.path.dirname(BASE_DIR), "价格图片", "CPU")   # --status 默认查看目录
CONFIG_PATH = os.path.join(BASE_DIR, "llm_config.json")
PROMPTS_DIR = os.path.join(BASE_DIR, "prompts")
JOINT_PROMPT_PATH = os.path.join(PROMPTS_DIR, "OCR主_指令.txt")   # 新结构联合提示词（OCR 为主、图为辅）

# 默认输出目录：与脚本同级（database/output_cpu/），可用 --out-dir 调整。
# 输出目录包含管线产生的全部文件：结果 JSON、裁剪缓存、OCR Markdown 缓存。
OUTPUT_DIR = os.path.join(BASE_DIR, "output_cpu")
OUT_DIR = os.path.join(OUTPUT_DIR, "extracted_cpu")
CROP_DIR = os.path.join(OUTPUT_DIR, "crop_cache")
OCR_CACHE_DIR = os.path.join(OUTPUT_DIR, "ocr_cache")
PROGRESS_LOG = os.path.join(OUTPUT_DIR, "extract_cpu_progress.log")
MANIFEST_PATH = os.path.join(OUTPUT_DIR, "manifest.json")   # 哈希 → 原名映射（随 DB 备份）

MAX_WORKERS = 2    # 默认并发数（可用 --workers N 调整）
_MANIFEST_LOCK = threading.Lock()   # manifest 读改写互斥（多 worker 并发保护）


def _write_manifest(manifest: dict) -> None:
    """原子写 manifest（临时文件 + os.replace），中断不会留下截断 JSON。"""
    tmp = MANIFEST_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2)
    os.replace(tmp, MANIFEST_PATH)


# ============ 内容寻址键（方案 B：哈希贯穿全链路） ============

def content_key(img_path: str) -> str:
    """计算图片内容的 MD5 作为管线内部键（分块读取，大文件不占内存）。
    内容寻址：字节不变 → 键不变（改名/移动/复制无影响）；字节变 → 新键。"""
    h = hashlib.md5()
    with open(img_path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def load_manifest() -> dict:
    """读取 哈希 → 原名 映射表；不存在返回空 dict；损坏时告警并按空映射继续
    （避免 status/提取全量崩溃，下次登记会重建）。"""
    if os.path.exists(MANIFEST_PATH):
        try:
            with open(MANIFEST_PATH, encoding="utf-8") as f:
                return json.load(f)
        except json.JSONDecodeError as e:
            print(f"警告: manifest.json 损坏（{e}），按空映射继续，下次登记会重建", file=sys.stderr)
            return {}
    return {}


def register_key(img_path: str) -> str:
    """返回源图的内容哈希键，并在 manifest 中登记 键 → 原名。
    同键已登记同名 → 不重复写；已登记异名 → 记入 aliases 别名列表。
    读改写由 _MANIFEST_LOCK 互斥，写入经 _write_manifest 原子替换。"""
    key = content_key(img_path)
    orig = os.path.basename(img_path)
    with _MANIFEST_LOCK:
        manifest = load_manifest()
        entry = manifest.get(key)
        if entry is None:
            manifest[key] = orig
            _write_manifest(manifest)
        elif isinstance(entry, str) and entry != orig:
            # 同内容异名：同一张图，升级为 {name, aliases} 结构保留完整线索
            manifest[key] = {"name": entry, "aliases": [orig]}
            _write_manifest(manifest)
        elif isinstance(entry, dict) and orig != entry.get("name") \
                and orig not in entry.get("aliases", []):
            entry["aliases"].append(orig)
            _write_manifest(manifest)
    return key


# ============ 提示词（CPU 专用） ============

def load_prompt(fname: str, default: str = "") -> str:
    path = os.path.join(PROMPTS_DIR, fname)
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            return f.read()
    return default


# ============ 裁剪预处理 ============

def crop_cpu_image(img_path: str, key: str) -> str:
    """三段式锚点裁剪（内容寻址：缓存键 = 内容哈希 key）：像素检测表格线位置，逐图自适应裁剪。

    替代旧版固定 6.2%/42% 裁剪（会切进右侧表格窄边、露出其他产品列头）。
    检测到的锚点在全部 316 张 CPU 图片上验证：
      - 表格左边界 x/w = 0.110 ± 0.0008
      - CPU 区右边界 x/w = 0.399 ± 0.0031（316/316 检测成功）
      - 横幅底部 y/h ≈ 0.090（两种顶条版式统一覆盖）

    三段裁剪：
      段1 横幅区：0 ~ 横幅底部，x 裁到 55%（保住完整日期）
      段2 表头行：横幅底 ~ +3.5%，x 裁到 CPU 右边界锚点 +2px（只留边界竖线本身，
          消除相邻子表残字）
      段3 表格主体：表头下 ~ 底部，x 到 CPU 右边界锚点 +2px
    注意：画布保留全宽 w——SPLIT1/SPLIT2 分块先验（0.397/0.536）按全宽画布标定，
    收紧画布会使分界比例失配、把 CPU 表切断；若要收紧画布须同步重标分块先验。

    结果缓存到 crop_cache/，已存在直接复用。返回裁剪图路径。
    """
    name = key
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
    # 切点 right+2：只保留边界竖线本身，不再切进相邻子表的残字
    s2 = img.crop((0, banner_end, right + 2, banner_end + head_h))
    s3 = img.crop((0, banner_end + head_h, right + 2, h))
    # 画布保留全宽 w：SPLIT1/SPLIT2 分块先验按全宽画布标定，收紧会失配
    canvas = Image.new("RGB", (w, s1.size[1] + s2.size[1] + s3.size[1]), "white")
    canvas.paste(s1, (0, 0))
    canvas.paste(s2, (0, s1.size[1]))
    canvas.paste(s3, (0, s1.size[1] + s2.size[1]))
    canvas.save(out_path)
    return out_path


# ============ 表级竖向分块（切块方案已定稿） ============

# 分块产物与前缀命名平铺在 crop_cache/（<图片名>_LL.png / <图片名>_LR.png）；
# 第二层切分为纯内存中间操作，不落盘（从裁剪图重新生成是确定性操作）

# 分界竖线先验（实测 316 张裁剪图：CPU 区右边界 0.392~0.405 极差 1.3%，
# 处理器表/15/14 表分界在 left 块内 x/w≈0.536）；先验校验范围设 ±2%
SPLIT1_RANGE = (0.38, 0.42)   # 第二层分界：CPU 区（两个 CPU 子表）与硬盘/内存区的分界竖线 x≈0.397
SPLIT1_FALLBACK = 0.399       # 第二层检测失败回退（实测均值）
SPLIT2_RANGE = (0.50, 0.58)   # 第三层分界：left 块内处理器表原盒列右边界 x≈0.536
SPLIT2_FALLBACK = 0.538       # 第三层检测失败回退（实测均值）


def detect_vlines(arr, y0_ratio=0.10, y1_ratio=0.98, threshold=0.6,
                  cluster_gap=3, dark=180):
    """在图像主体区域检测竖线，返回竖线 x 坐标列表（聚类后的线中心）。
    与 3.2 锚点检测同一套像素技术：主体带统计每列深色占比，
    占比超阈值的列聚类成线。"""
    h, w = arr.shape
    band = arr[int(h * y0_ratio):int(h * y1_ratio)]
    dark_ratio = (band < dark).mean(axis=0)
    cols = [x for x in range(w) if dark_ratio[x] > threshold]
    groups = []
    for x in cols:
        if groups and x - groups[-1][-1] <= cluster_gap:
            groups[-1].append(x)
        else:
            groups.append([x])
    return [int(sum(g) / len(g)) for g in groups]


def _find_split(vlines, width, ratio_range, fallback, offset=0):
    """在预期比例范围内选分界竖线；无候选时回退固定比例。
    多根候选时取最靠左的一根（cand[0]）：SPLIT2 的目标是
    处理器表右边界（x≈0.536），15/14 表左边界（x≈0.560）比它靠右，
    取最左自然跳过。offset: 切点相对竖线的偏移像素（如 +2
    避开竖线本身，落在表间隙内）。"""
    lo, hi = ratio_range
    cand = [x for x in vlines if lo < x / width < hi]
    if cand:
        return cand[0] + offset
    return int(width * fallback)


def split_table_blocks(crop_path: str, key: str) -> dict:
    """表级竖向分块：两级左右分块，把裁剪图切成两个独立子表块（方案定稿）。

    第二层 @ x/w≈0.397（"intel 15/14 代处理器"表原盒列右边界，
    即与台式硬盘表之间的分界竖线，纯内存中间操作，不落盘）：
      left = 全部 CPU 子表（处理器表 + 15/14 表 + AMD 表，全部 CPU 价格列都在）
      right = 台式硬盘/内存等非 CPU 区域（干扰源，不参与 CPU 提取）

    第三层 @ left 块内 x/w≈0.536（处理器表原盒列右边界，与 15/14 表分界）：
      LL = 完整 "intel 处理器" 表（型号+散片+原盒 三列全在）
      LR = 完整 "intel 15/14 代处理器" 表（三列全在）+ AMD 表 + 硬盘表边缘

    切点均相对分界竖线偏移 +2px，落在表间隙内（表内信息不拆散，无信息丢失）；
    竖线位置是模板属性，与图片缩放/宽高比无关；检测失败回退固定比例。

    结果前缀命名平铺缓存到 crop_cache/（{哈希键}_LL.png / _LR.png），
    已存在直接复用。返回 {'LL': 路径, 'LR': 路径}。"""
    name = key
    ll_path = os.path.join(CROP_DIR, name + "_LL.png")
    lr_path = os.path.join(CROP_DIR, name + "_LR.png")
    if os.path.exists(ll_path) and os.path.exists(lr_path):
        return {"LL": ll_path, "LR": lr_path}

    img = Image.open(crop_path).convert("RGB")
    arr = np.array(img.convert("L"))
    w, h = img.size

    # 第二层：CPU 区与硬盘/内存区之间的分界竖线（纯内存，不落盘）
    vlines = detect_vlines(arr)
    split1 = _find_split(vlines, w, SPLIT1_RANGE, SPLIT1_FALLBACK, offset=2)
    left = img.crop((0, 0, split1, h))

    # 第三层：left 块内处理器表与 15/14 表之间的分界竖线
    arr_l = np.array(left.convert("L"))
    w_l = left.size[0]
    vlines_l = detect_vlines(arr_l)
    split2 = _find_split(vlines_l, w_l, SPLIT2_RANGE, SPLIT2_FALLBACK, offset=2)
    ll = left.crop((0, 0, split2, h))
    lr = left.crop((split2, 0, w_l, h))

    ll.save(ll_path)
    lr.save(lr_path)
    return {"LL": ll_path, "LR": lr_path}


# ============ OCR 辅助通道（llama.cpp GLM-OCR） ============

MAX_WORKERS_LIMIT = 6   # 并发上限（OCR/LLM 服务承压限制）
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


def _ocr_image_to_md(image_path: str, cache_name: str) -> str:
    """对单张图片调用 llama.cpp GLM-OCR，返回 Markdown 格式的表格转写文本。
    OCR 输出确定性与图片绑定（同图同输出），结果按 cache_name 缓存到 ocr_cache/。
    服务不可用/返回空时抛出 RuntimeError（提示词以 OCR 为主，无 OCR 无法提取）。"""
    cache_path = os.path.join(OCR_CACHE_DIR, cache_name + ".md")
    if os.path.exists(cache_path):
        with open(cache_path, encoding="utf-8") as f:
            return f.read()
    with open(image_path, "rb") as f:
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
        raise RuntimeError(f"OCR 服务调用失败（{cache_name[:24]}）: {e}")
    if not text:
        raise RuntimeError(f"OCR 返回空内容（{cache_name[:24]}）")
    md = html_table_to_markdown(text)
    with open(cache_path, "w", encoding="utf-8") as f:
        f.write(md)
    return md


def ocr_markdown(img_path: str, key: str) -> str:
    """对 LL/LR 两个分块图分别调用 GLM-OCR，合并为带左右子表标注的 Markdown。

    分块图来自 split_table_blocks()（裁剪图的表级竖向分块）：
      LL = "intel 处理器" 表（老款 + 11~14 代），LR = "intel 15/14 代" 表 + AMD。
    分块后每块更窄、文字有效分辨率更高，OCR 误识更少；两块的缓存键为
    <哈希键>_LL.md / <哈希键>_LR.md。
    报价日期不在分块图内（横幅随分块被排除），sheet_date 按提示词契约
    从原图标题提取（call_llm 随消息附带完整裁剪图，横幅在其中）。
    合并格式与提示词【OCR 表格结构说明】的左/右子表约定一致。"""
    crop_path = crop_cpu_image(img_path, key)
    blocks = split_table_blocks(crop_path, key)
    md_ll = _ocr_image_to_md(blocks["LL"], key + "_LL")
    md_lr = _ocr_image_to_md(blocks["LR"], key + "_LR")
    return (
        "【左子表（intel 老款 + 11~14 代处理器）】\n"
        + md_ll
        + "\n\n【右子表（intel 15/14 代（U 系）处理器 + AMD）】\n"
        + md_lr
    )


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


def _is_transient(e: Exception) -> bool:
    """超时/连接类瞬时错误（值得重试一次）。"""
    s = str(e).lower()
    return any(k in s for k in ("timeout", "timed out", "connection", "temporarily", "rate limit"))


def call_llm(crop_path: str, prompt: str, use_image: bool = True) -> dict:
    """调用多模态 LLM，返回解析后的 JSON。默认随消息传裁剪图。
    超时/连接类瞬时错误重试一次；temperature 不被支持时去参数重试。"""
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
        emsg = str(e).lower()
        if "temperature" in emsg:
            # temperature 不被支持：去参数重试；若为瞬时错误再补一次重试
            try:
                resp = client.chat.completions.create(**kwargs)
            except Exception as e2:
                if not _is_transient(e2):
                    raise
                resp = client.chat.completions.create(**kwargs)
        elif _is_transient(e):
            # 超时/连接抖动：原参数重试一次
            resp = client.chat.completions.create(temperature=cfg.get("temperature", 0), **kwargs)
        else:
            raise

    text = (resp.choices[0].message.content or "").strip()
    if text.startswith("```"):
        text = text.split("```")[1]
        text = text.lstrip()
        if text[:4].lower() == "json":   # ```json 与 ```JSON 均兼容
            text = text[4:]
    return json.loads(text)


# ============ 提取单张 ============

def extract_one(img_path: str) -> tuple[str, bool, str]:
    """内容寻址处理：PNG 验证 → 算哈希键 → 断点续跑 → 裁剪 → OCR → LLM 提取。

    全链路以源图内容的 MD5 哈希为唯一键（方案 B）：
      - 非 PNG 格式：该图失败，不进管线（管线内格式固定 PNG）
      - key 全链路贯穿：裁剪/分块/OCR 缓存/最终 JSON/DB source_image
      - 唯一检查点 = extracted_cpu/{key}.json（只在全链路成功后原子落盘）
      - 同内容异名重发 → 相同哈希 → 自动跳过（免费去重）
      - 图片字节不变 → 哈希不变 → 缓存/结果永远命中；字节变 → 新键新条目
    提示词以 OCR 输出为主（prompts/OCR主_指令.txt + OCR Markdown + 裁剪图）,
    OCR 失败/返回空时抛出 RuntimeError，该图计为失败（不降级纯视觉）。"""
    # 入口验证：管线内格式固定 PNG
    if os.path.splitext(img_path)[1].lower() != ".png":
        return (os.path.basename(img_path), False,
                f"非 PNG 格式（{os.path.splitext(img_path)[1]}），请转换为 PNG 后放入目录")
    try:
        key = register_key(img_path)
    except Exception as e:
        return (os.path.basename(img_path), False, f"哈希计算/manifest 登记失败: {e}")
    name = key   # 返回完整哈希键（展示时截断；非 PNG 返回 basename），供失败清单反查 manifest
    out_path = os.path.join(OUT_DIR, key + ".json")
    if os.path.exists(out_path):
        return (name, True, "跳过（已存在）")
    try:
        t0 = time.time()
        crop_path = crop_cpu_image(img_path, key)
        ocr_md = ocr_markdown(img_path, key)
        full_prompt = build_joint_prompt(ocr_md)
        data = call_llm(crop_path, full_prompt)
        # source_image 固定为 哈希 + .png（内容寻址溯源，反查原名走 manifest）
        data["source_image"] = key + ".png"
        # 只保留 CPU 类目的记录（双保险：白名单外或 LLM 串区的记录剔除）
        data["products"] = [p for p in data.get("products", [])
                            if str(p.get("category", "")) == "CPU"]
        # 原子落盘（临时文件 + os.replace）：中断不会留下被断点续跑误认的截断 JSON
        tmp_path = out_path + ".tmp"
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp_path, out_path)
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
        ext = os.path.splitext(target)[1].lower()
        if ext != ".png":
            # 单张非 PNG 同样在收集阶段拦截（extract_one 也会拒绝），提前提示转换
            print(f"错误: {target} 不是 PNG 文件（管线仅支持 PNG，其他格式请先转换）")
            return None
        return [os.path.abspath(target)]
    if os.path.isdir(target):
        images = []
        skipped_non_png = []
        for root, _dirs, files in os.walk(target):
            for f in sorted(files):
                ext = os.path.splitext(f)[1].lower()
                if ext == ".png":
                    images.append(os.path.join(root, f))
                elif ext in IMG_EXTS:
                    skipped_non_png.append(os.path.join(root, f))
        images.sort()
        # 收集阶段即过滤非 PNG（extract_one 会拒绝），提前提示转换
        if skipped_non_png:
            print(f"提示: 跳过 {len(skipped_non_png)} 个非 PNG 文件（管线仅支持 PNG，请先转换）:")
            for p in skipped_non_png[:5]:
                print(f"  - {p}")
            if len(skipped_non_png) > 5:
                print(f"  ... 等共 {len(skipped_non_png)} 个")
        if not images:
            print(f"错误: 目录 {target} 下没有找到 PNG 图片（支持 .png；其他格式请先转换）")
            return None
        return images
    print(f"错误: 路径不存在: {target}")
    return None



def main():
    # --status：查看默认目录（价格图片/CPU/）进度
    if "--status" in sys.argv:
        status()
        return

    # --out-dir <目录>：输出目录（默认 database/output_cpu/，与脚本同级）
    global OUTPUT_DIR, OUT_DIR, CROP_DIR, OCR_CACHE_DIR, PROGRESS_LOG, MANIFEST_PATH
    if "--out-dir" in sys.argv:
        i = sys.argv.index("--out-dir")
        if i + 1 >= len(sys.argv):
            print("错误: --out-dir 需要一个目录参数")
            return
        OUTPUT_DIR = os.path.abspath(sys.argv[i + 1])
        OUT_DIR = os.path.join(OUTPUT_DIR, "extracted_cpu")
        CROP_DIR = os.path.join(OUTPUT_DIR, "crop_cache")
        OCR_CACHE_DIR = os.path.join(OUTPUT_DIR, "ocr_cache")
        PROGRESS_LOG = os.path.join(OUTPUT_DIR, "extract_cpu_progress.log")
        MANIFEST_PATH = os.path.join(OUTPUT_DIR, "manifest.json")   # manifest 与产物同步切目录

    # 创建输出目录结构（全部管线产物都写入其中）
    for d in (OUT_DIR, CROP_DIR, OCR_CACHE_DIR):
        os.makedirs(d, exist_ok=True)

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
    # 排除 --out-dir 和 --workers 的参数值
    for opt in ("--out-dir", "--workers"):
        if opt in sys.argv:
            i = sys.argv.index(opt)
            if i + 1 < len(sys.argv):
                args = [a for a in args if a != sys.argv[i + 1]]
    if not args:
        print("用法: python extract_cpu.py <图片文件或目录> [--workers N] [--out-dir 目录] [--status]")
        print("示例:")
        print(f"  python {os.path.basename(sys.argv[0])} ../价格图片/CPU")
        print(f"  python {os.path.basename(sys.argv[0])} ../价格图片/CPU/0a04d486d137d2d382483b63a0f84e78.png")
        print(f"  python {os.path.basename(sys.argv[0])} ../价格图片/CPU --workers 4")
        print(f"  python {os.path.basename(sys.argv[0])} ../价格图片/CPU --out-dir ./my_output")
        return

    target = args[0]
    images = collect_images(target)
    if not images:
        return

    print(f"[CPU 管线] 共 {len(images)} 张图片，并发数: {workers}")
    # 断点统计与 --status 同口径：按内容哈希键检查（basename 时代结果不计入）
    done = 0
    for img in images:
        try:
            if os.path.exists(os.path.join(OUT_DIR, content_key(img) + ".json")):
                done += 1
        except Exception:
            pass
    print(f"断点续跑: 已完成 {done} 张，待提取 {len(images) - done} 张")

    ok = fail = skipped = 0
    failed_list = []
    zero_list = []
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
                if msg.startswith("完成 (0 条"):
                    zero_list.append(name)   # LLM 返回空 products，提醒人工核图
            else:
                fail += 1
                failed_list.append(name)
            print(f"[{idx}/{len(images)}] {name[:16]}...: {msg}", flush=True)

    stop = threading.Event()
    
    def _sigint(sig, frame):
        # 第一次 Ctrl+C：请求停止（不再提交新任务，等待在跑的图完成）
        if not stop.is_set():
            stop.set()
            print("\n[CPU 管线] 收到 Ctrl+C，正在优雅停止：不再开始新图，等待在跑的图完成（再按一次 Ctrl+C 强制退出）...", flush=True)
        else:
            # 第二次 Ctrl+C：真强制退出（executor __exit__ 会等在跑任务，必须直接终止进程）
            print("\n[CPU 管线] 强制退出（进度日志未写入；未完成的图下次重跑会自动重试）。", flush=True)
            os._exit(1)
    
    signal.signal(signal.SIGINT, _sigint)
    
    with ThreadPoolExecutor(max_workers=workers) as pool:
        pending_imgs = list(images)
        futures = set()
        try:
            # 主循环：按需提交新任务；stop 置位后不再提交
            idx_submit = 0
            while idx_submit < len(pending_imgs) and not stop.is_set():
                while len(futures) < workers and idx_submit < len(pending_imgs) and not stop.is_set():
                    fut = pool.submit(extract_one, pending_imgs[idx_submit])
                    futures.add(fut)
                    idx_submit += 1
                done_futs = {f for f in futures if f.done()}
                for fut in done_futs:
                    futures.discard(fut)
                    name, success, msg = fut.result()
                    report(name, success, msg)
                if not stop.is_set() and futures:
                    time.sleep(0.2)
            # stop 置位或提交完毕：等待剩余在跑任务完成
            for fut in as_completed(futures):
                name, success, msg = fut.result()
                report(name, success, msg)
        except KeyboardInterrupt:
            print("\n[CPU 管线] 强制退出（进度日志未写入）。", flush=True)
            os._exit(1)
    
    if stop.is_set():
        print(f"\n[CPU 管线] 已优雅停止: 新提取 {ok}, 跳过 {skipped}, 失败 {fail}，剩余待提取 {len(images) - ok - skipped - fail} 张，结果存于 {OUT_DIR}")
    else:
        print(f"\n[CPU 管线] 提取完成: 新提取 {ok}, 跳过 {skipped}, 失败 {fail}，结果存于 {OUT_DIR}")
    if failed_list:
        print("失败清单（重跑 extract_cpu.py 会自动重试）:")
        failed_manifest = load_manifest()
        for n in failed_list[:10]:
            entry = failed_manifest.get(n)
            disp = entry.get("name") if isinstance(entry, dict) else (entry if entry else n)
            print(f"  - {disp}")
        if len(failed_list) > 10:
            print(f"  ... 等共 {len(failed_list)} 张")
    if zero_list:
        print("0 条结果清单（LLM 返回空 products，建议人工核图）:")
        zero_manifest = load_manifest()
        for n in zero_list[:10]:
            entry = zero_manifest.get(n)
            disp = entry.get("name") if isinstance(entry, dict) else (entry if entry else n)
            print(f"  - {disp}")
        if len(zero_list) > 10:
            print(f"  ... 等共 {len(zero_list)} 张")
    # 总进度按内容哈希键统计（与检查点 {key}.json 一致；无需查 manifest）
    total_done = 0
    for img in images:
        try:
            if os.path.exists(os.path.join(OUT_DIR, content_key(img) + ".json")):
                total_done += 1
        except Exception:
            pass
    with open(PROGRESS_LOG, "a", encoding="utf-8") as f:
        f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} 本次新提取={ok} 失败={fail} 总进度={total_done}/{len(images)}\n")

    # --db：统一入库（管线结束时执行 load_cpu 的完整主流程，做法 B）
    if "--db" in sys.argv:
        print("\n[CPU 管线] 开始数据库导入（全量 extracted_cpu/，含全库去重）...")
        try:
            sys.path.insert(0, BASE_DIR)
            from load_cpu import run_import   # 复用 load_cpu 现有逻辑，不重写
            # 传入当前输出目录（--out-dir 自定义时对齐，默认时与 load_cpu 默认一致），
            # 避免自定义输出时误导 database/output_cpu/ 的默认目录
            result = run_import(OUT_DIR)      # 全量，幂等无害
            print(f"[CPU 管线] 入库完成: 导入 {result['passed']} 条, "
                  f"跳过 {result['skipped']} 条, "
                  f"去重删除 {result['dup_same']} 条, "
                  f"冲突 {result['conflicts']} 条")
            if result['conflicts']:
                print("  冲突明细见 load_cpu_report.md / load_cpu_conflicts.json，请人工核对原图")
        except Exception as e:
            print(f"[CPU 管线] 入库失败: {e}")
            print("  （提取结果不受影响，JSON 已在盘上；可手动执行 python database/load_cpu.py 重试）")
            sys.exit(1)   # 非零退出码提示有错，但数据安全


def status():
    """查看默认目录（价格图片/CPU/）的提取进度。"""
    images = []
    for root, _dirs, files in os.walk(IMG_DIR):
        for f in sorted(files):
            if os.path.splitext(f)[1].lower() in IMG_EXTS:
                images.append(os.path.join(root, f))
    images.sort()
    manifest = load_manifest()
    all_done = {os.path.splitext(os.path.basename(p))[0]
                for p in glob.glob(os.path.join(OUT_DIR, "*.json"))}
    # 已提取只统计与当前图片哈希匹配的结果（陈旧结果单独计数，避免偏大）
    png_keys = set()
    for i in images:
        if os.path.splitext(i)[1].lower() == ".png":
            try:
                png_keys.add(content_key(i))
            except Exception:
                pass
    done = all_done & png_keys
    stale = len(all_done) - len(done)
    pending = []
    for i in images:
        if os.path.splitext(i)[1].lower() != ".png":
            continue
        try:
            if content_key(i) not in all_done:
                pending.append(i)
        except Exception:
            pass   # 文件不可读：跳过，不让 --status 崩溃
    non_png = [os.path.basename(i) for i in images
               if os.path.splitext(i)[1].lower() != ".png"]
    print(f"[CPU 管线] 图片总数: {len(images)}")
    print(f"已提取: {len(done)}")
    if stale:
        print(f"陈旧结果（哈希不在当前图片中，不占用断点）: {stale}")
    print(f"待提取: {len(pending)}")
    if non_png:
        print(f"非 PNG（提取时会报错）: {len(non_png)}")
        for n in non_png[:5]:
            print(f"  - {n}")
    if pending:
        print("\n待提取清单（前10个，显示 manifest 原名）:")
        for i in pending[:10]:
            k = content_key(i)
            entry = manifest.get(k, k[:16])
            disp = entry.get("name") if isinstance(entry, dict) else entry
            print(f"  - {disp}")
        if len(pending) > 10:
            print(f"  ... 等共 {len(pending)} 张")


if __name__ == "__main__":
    main()
