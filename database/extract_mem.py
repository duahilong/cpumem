# -*- coding: utf-8 -*-
"""
MEM 专用提取管线 —— 与其他类目完全独立（结构对齐 extract_cpu.py）。

流程：
    1. 显式传入 PNG 图片或目录（断点续跑：已有结果 JSON 则跳过）
    2. 内容寻址：key = MD5(源图字节)，register_key() 登记 manifest（原子写+锁）
    3. 裁剪预处理 crop_mem_image()：裁剪图缓存 crop_cache/{key}.png
       【首版为直通拷贝占位；细节锚点/参数待测试校准，见函数 docstring】
    4. OCR 通道 ocr_markdown()：读裁剪图 → llama.cpp GLM-OCR → Markdown 表格
       （temperature=0 确定性输出；max_tokens 可配置 ocr_max_tokens；缓存 ocr_cache/{key}.md）
    5. 组装提示词：prompts/OCR主_指令_MEM.txt + OCR Markdown
       （OCR 为主、原图为辅；指令自带完整 JSON 契约，无 CPU 白名单）
    6. 调用多模态 LLM（配置 llm_config.json），输出 JSON 存档 extracted_mem/
       OCR 失败/返回空 → 抛 RuntimeError，该图计失败（不降级，重跑自动重试）

与 CPU 管线的三处关键差异：
    - 裁剪预处理首版为直通拷贝占位（细节待测试校准）
    - 无 CPU 白名单
    - 全类目保留：不按 category 过滤，为每条记录补齐 hardware_type

内容寻址（方案 B，与 CPU 管线一致）：key = MD5(源图字节) 贯穿全链路：
    - 裁剪/OCR 缓存/最终 JSON/DB source_image 都用该 key
    - 同内容异名重发 → 相同哈希 → 自动跳过（免费去重）
    - 字节不变 → 缓存/结果永远命中；字节变 → 新键新条目
    - manifest.json 记录 键 → 原名（同内容异名升级为 {name, aliases}）

用法：
    python extract_mem.py <PNG图片或目录> [--workers N] [--out-dir 目录] [--db] [--status]
    （必须显式传入目标；无参数时只打印用法，不执行管线）

示例：
    python extract_mem.py ../价格图片/mem                     # 提取整个目录
    python extract_mem.py ../价格图片/mem/003110….png         # 提取单张
    python extract_mem.py ../价格图片/mem --workers 4         # 指定并发
    python extract_mem.py ../价格图片/mem --out-dir ./my_out  # 指定输出目录
    python extract_mem.py ../价格图片/mem --db                # 提取后统一入库
    python extract_mem.py --status                            # 查看默认目录进度

输出（默认 database/output_mem/，与脚本同级；全部管线产物都在其中）：
    output_mem/extracted_mem/<key>.json      # 提取结果（含 source_image、hardware_type，
                                             #  断点续跑检查点，原子落盘）
    output_mem/crop_cache/<key>.png          # 裁剪图缓存（复用，避免重复裁剪）
    output_mem/ocr_cache/<key>.md            # OCR Markdown 转写缓存（同图同输出）
    output_mem/manifest.json                 # 内容哈希 → 原名映射（随 DB 备份）
    output_mem/extract_mem_progress.log      # 提取进度日志

环境变量：
    OCR_BASE_URL=... # 覆盖 OCR 服务地址（优先级低于 llm_config.json 的 ocr_base_url）

超时（llm_config.json 配置，防止脚本卡住）：
    timeout_seconds: 300     # LLM 调用超时
    ocr_timeout_seconds: 600 # OCR 调用超时
    ocr_max_tokens: 16384    # OCR 输出上限（实测教训：过大诱发重复退化，见 ocr_markdown）

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
import hashlib
import signal
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
IMG_DIR = os.path.join(os.path.dirname(BASE_DIR), "价格图片", "mem")   # --status 默认查看目录
CONFIG_PATH = os.path.join(BASE_DIR, "llm_config.json")
PROMPTS_DIR = os.path.join(BASE_DIR, "prompts")
JOINT_PROMPT_PATH = os.path.join(PROMPTS_DIR, "OCR主_指令_MEM.txt")   # MEM 联合提示词（OCR 为主、图为辅）

# 默认输出目录：与脚本同级（database/output_mem/），可用 --out-dir 调整。
# 输出目录包含管线产生的全部文件：结果 JSON、裁剪缓存、OCR Markdown 缓存、manifest。
OUTPUT_DIR = os.path.join(BASE_DIR, "output_mem")
OUT_DIR = os.path.join(OUTPUT_DIR, "extracted_mem")
CROP_DIR = os.path.join(OUTPUT_DIR, "crop_cache")
OCR_CACHE_DIR = os.path.join(OUTPUT_DIR, "ocr_cache")
PROGRESS_LOG = os.path.join(OUTPUT_DIR, "extract_mem_progress.log")
MANIFEST_PATH = os.path.join(OUTPUT_DIR, "manifest.json")   # 哈希 → 原名映射（随 DB 备份）

MAX_WORKERS = 2    # 默认并发数（可用 --workers N 调整）
MAX_WORKERS_LIMIT = 6   # 并发上限（OCR/LLM 服务承压限制）
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


# ============ 裁剪预处理 + 表级竖向分块（对齐 CPU 管线模式） ============

def crop_mem_image(img_path: str, key: str) -> str:
    """裁剪预处理：为 OCR 和 LLM 提供预处理后的图。

    MEM 版实现（三段裁剪，对齐 CPU 管线 crop_cpu_image 模式）：
      段1 标题行（报价单 XX月XX日，全宽保留，日期来源）
      段3 表格主体（从区块表头行开始，含全部产品区块）
      —— 中间的"总代标头行"（两行：微星主板显示器机电总代 / 影驰全系列总代 /
      金邦存储总代 / 金士顿存储总代 等重复标头）裁剪掉（OCR 干扰源：分块后这些跨列文字
      会产生残字与乱码行）

    锚点检测（实测 4 种版式一致）：
      - 标题行底线：横幅区后的第一根全宽深色表格线（x/w≈0.027）
      - 总代区底线：其后的第三根全宽深色表格线（含两行总代，区块表头上边界，x/w≈0.068）
      检测失败回退固定比例（标题底 0.027 / 总代底 0.068）。

    结果缓存到 crop_cache/{key}.png，已存在直接复用。返回裁剪图路径。
    """
    out_path = os.path.join(CROP_DIR, key + ".png")
    if os.path.exists(out_path):
        return out_path
    import numpy as np
    from PIL import Image
    img = Image.open(img_path).convert("RGB")
    w, h = img.size
    arr = np.array(img.convert("L"))

    # 找全宽深色表格线（行深色占比 > 0.9）
    def full_width_lines(y_from, y_to):
        lines = []
        for y in range(int(h * y_from), int(h * y_to)):
            if (arr[y] < 150).mean() > 0.9:
                lines.append(y)
        # 聚类相邻行
        groups = []
        for y in lines:
            if groups and y - groups[-1][-1] <= 3:
                groups[-1].append(y)
            else:
                groups.append([y])
        return [g[0] for g in groups]

    # 标题行底线：y 2%~15% 内的前三根全宽表格线
    # （实测结构：标题底 → 总代行1底 → 总代行2底=区块表头上边界）
    title_lines = full_width_lines(0.02, 0.15)
    if len(title_lines) >= 3:
        title_end = title_lines[0]        # 标题行底线（横幅底）
        banner_end = title_lines[2]       # 总代区底线（含两行总代，区块表头上边界）
    elif len(title_lines) == 2:
        # 只有两根线：可能只有一行总代
        title_end = title_lines[0]
        banner_end = title_lines[1]
    else:
        # 检测失败回退固定比例（实测均值：标题底 0.027 / 总代区底 0.068）
        title_end = int(h * 0.027)
        banner_end = int(h * 0.068)

    # 三段拼接：标题行 + 表格主体（跳过总代标头区）
    s1 = img.crop((0, 0, w, title_end))
    s3 = img.crop((0, banner_end, w, h))
    canvas = Image.new("RGB", (w, s1.height + s3.height), "white")
    canvas.paste(s1, (0, 0))
    canvas.paste(s3, (0, s1.height))
    canvas.save(out_path)
    return out_path


# 表级竖向分块（切块方案已定稿：4 等分内容区，分界竖线逐图自适应检测）

# 分界竖线先验（实测 171 张 mem 图全部命中，零回退）：
#   mem 报价单为 4 个内容区+各自价格列结构，分界竖线（价格列右边界）
#   在 x/w ≈ 0.25 / 0.50 / 0.75 附近；部分版式价格列更宽（如 0.7348），
#   先验范围 ±3% 逐图自适应命中真实竖线。检测失败回退均分比例。
MEM_SPLIT_PRIORS = [(0.22, 0.28), (0.47, 0.53), (0.72, 0.78)]   # 3 条分界先验范围
MEM_SPLIT_FALLBACKS = [0.25, 0.50, 0.75]                        # 检测失败回退（均分）


def detect_vlines(arr, y0_ratio=0.10, y1_ratio=0.98, threshold=0.6,
                  cluster_gap=3, dark=180):
    """在图像主体区域检测竖线，返回竖线 x 坐标列表（聚类后的线中心）。
    与 CPU 管线 detect_vlines 同一套像素技术：主体带统计每列深色占比，
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


def _find_4splits(vlines, width):
    """在 3 条分界竖线的先验比例范围内各选一根（取最左候选），
    无候选时回退均分比例。返回 3 个分界 x 坐标（升序）。"""
    splits = []
    for (lo, hi), fb in zip(MEM_SPLIT_PRIORS, MEM_SPLIT_FALLBACKS):
        cand = [x for x in vlines if lo < x / width < hi]
        splits.append(cand[0] if cand else int(width * fb))
    return splits


def split_mem_blocks(crop_path: str, key: str) -> dict:
    """表级竖向 4 块切分（对齐 CPU 管线 split_table_blocks 模式，方案定稿）。

    分界竖线逐图自适应检测（detect_vlines + 先验范围选择）：
      mem 报价单为 4 个产品区块并排（每个区块 = 型号列 + 价格列），
      3 条分界竖线把主体切成 4 块，每块含完整的"型号+价格"对。
    切点落在表格间隙内（表内信息不拆散，无信息丢失）；
    竖线位置是模板属性（x/w 相对比例），与图片分辨率/宽高比无关；
    检测失败回退均分比例。

    每块顶部拼回标题行（报价日期，来自已裁剪的 crop 图），供 sheet_date 提取。
    结果平铺缓存到 crop_cache/（{key}_B0.png ~ {key}_B3.png），
    已存在直接复用。返回 {'B0': 路径, 'B1': 路径, 'B2': 路径, 'B3': 路径}。"""
    paths = {f"B{i}": os.path.join(CROP_DIR, f"{key}_B{i}.png") for i in range(4)}
    if all(os.path.exists(p) for p in paths.values()):
        return paths

    import numpy as np
    from PIL import Image
    img = Image.open(crop_path).convert("RGB")
    w, h = img.size
    arr = np.array(img.convert("L"))

    # 标题行底线（crop 图内第一根全宽表格线，即标题区结束）
    def full_width_lines(y_from, y_to):
        lines = []
        for y in range(int(h * y_from), int(h * y_to)):
            if (arr[y] < 150).mean() > 0.9:
                lines.append(y)
        groups = []
        for y in lines:
            if groups and y - groups[-1][-1] <= 3:
                groups[-1].append(y)
            else:
                groups.append([y])
        return [g[0] for g in groups]

    tl = full_width_lines(0.0, 0.10)
    title_end = tl[0] if tl else int(h * 0.027)

    # 主体区域检测 3 条分界竖线（在表格主体带内）
    body_arr = arr[title_end:, :]
    vlines = detect_vlines(body_arr)
    s1, s2, s3 = _find_4splits(vlines, w)

    # 4 块裁剪：每块顶部拼回标题行（保日期），主体按分界竖线切割
    title = img.crop((0, 0, w, title_end))
    bounds = [0, s1, s2, s3, w]
    for i in range(4):
        body = img.crop((bounds[i], title_end, bounds[i + 1], h))
        canvas = Image.new("RGB", (body.width, title.height + body.height), "white")
        canvas.paste(title, (0, 0))
        canvas.paste(body, (0, title.height))
        canvas.save(paths[f"B{i}"])
    return paths


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


def ocr_markdown(crop_path: str, key: str) -> str:
    """对 4 个分块图分别调用 GLM-OCR，合并为带块标注的联合 Markdown。

    分块图来自 split_mem_blocks()（裁剪图的表级竖向 4 等分，逐图自适应
    检测分界竖线）：B0~B3 每块含完整的"型号+价格"对，块更窄、文字有效
    分辨率更高，OCR 误识/粘连更少；缓存键为 {key}_B0.md ~ {key}_B3.md。
    报价日期在分块图顶部横幅内（每块拼回横幅），sheet_date 按提示词契约
    从横幅提取（call_llm 随消息附带完整裁剪图）。
    合并格式与提示词【OCR 表格结构说明】的 4 块布局约定一致。
    OCR 失败/返回空时抛出 RuntimeError（提示词以 OCR 为主，无 OCR 无法提取）。"""
    blocks = split_mem_blocks(crop_path, key)
    parts = []
    for i in range(4):
        bi = f"B{i}"
        cache_path = os.path.join(OCR_CACHE_DIR, f"{key}_{bi}.md")
        if os.path.exists(cache_path):
            with open(cache_path, encoding="utf-8") as f:
                md = f.read()
        else:
            md = _ocr_image_to_md(blocks[bi], f"{key}_{bi}")
        parts.append(f"【第{i+1}块（报价单从左数第{i+1}个产品区块）】\n" + md)
    return "\n\n".join(parts)


def _ocr_image_to_md(block_path: str, cache_key: str) -> str:
    """对单个分块图调用 GLM-OCR，返回 Markdown（缓存 ocr_cache/{cache_key}.md）。
    服务不可用/返回空时抛出 RuntimeError。"""
    cache_path = os.path.join(OCR_CACHE_DIR, cache_key + ".md")
    ext = os.path.splitext(block_path)[1].lower().lstrip(".")
    mime = "image/jpeg" if ext in ("jpg", "jpeg") else "image/png"
    with open(block_path, "rb") as f:
        b64 = base64.b64encode(f.read()).decode()
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
        raise RuntimeError(f"OCR 服务调用失败（{cache_key[:24]}）: {e}")
    if not text:
        raise RuntimeError(f"OCR 返回空内容（{cache_key[:24]}）")
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


def _is_transient(e: Exception) -> bool:
    """超时/连接类瞬时错误（值得重试一次）。"""
    s = str(e).lower()
    return any(k in s for k in ("timeout", "timed out", "connection", "temporarily", "rate limit"))


def call_llm(crop_path: str, prompt: str) -> dict:
    """调用多模态 LLM，返回解析后的 JSON。随消息传裁剪图。
    超时/连接类瞬时错误重试一次；temperature 不被支持时去参数重试。"""
    from openai import OpenAI

    cfg = load_config()
    client = OpenAI(base_url=cfg["base_url"], api_key=cfg["api_key"])

    ext = os.path.splitext(crop_path)[1].lower().lstrip(".")
    mime = "image/jpeg" if ext in ("jpg", "jpeg") else "image/png"
    with open(crop_path, "rb") as f:
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


# ============ 后处理：hardware_type 补齐 ============

# 硬件类别缩写映射：category -> hardware_type 兜底值
HARDWARE_ABBR = {
    "内存": "OTHER",   # 内存需按 DDR 代际细分，由 infer_ddr_type 处理；与 load_mem.py 保持一致
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
    """内容寻址处理：PNG 验证 → 算哈希键 → 断点续跑 → 裁剪 → OCR → LLM 提取。

    全链路以源图内容的 MD5 哈希为唯一键（方案 B）：
      - 非 PNG 格式：该图失败，不进管线（管线内格式固定 PNG）
      - key 全链路贯穿：裁剪/OCR 缓存/最终 JSON/DB source_image
      - 唯一检查点 = extracted_mem/{key}.json（只在全链路成功后原子落盘）
      - 同内容异名重发 → 相同哈希 → 自动跳过（免费去重）
    提示词以 OCR 输出为主（prompts/OCR主_指令_MEM.txt + OCR Markdown + 裁剪图）,
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
        crop_path = crop_mem_image(img_path, key)
        ocr_md = ocr_markdown(crop_path, key)
        full_prompt = build_joint_prompt(ocr_md)
        data = call_llm(crop_path, full_prompt)
        # source_image 固定为 哈希 + .png（内容寻址溯源，反查原名走 manifest）
        data["source_image"] = key + ".png"
        # 保留全类目（mem 报价单常同时含内存/固态/主板/显卡等，不剔除非内存产品），
        # 并补齐 hardware_type 字段（LLM 漏填或非法值时按 category + 型号名兜底推断）
        # 合法枚举 = 全部兜底缩写 + 显式 DDR 代际 + OTHER（提示词契约允许 LLM 输出 OTHER，
        # 不能当非法值重推，否则内存条目可能被频率启发式误改写）
        valid_hw = set(HARDWARE_ABBR.values()) | {"DDR3", "DDR4", "DDR5", "OTHER"}
        for p in data.get("products", []):
            if not p.get("hardware_type") or p.get("hardware_type") not in valid_hw:
                p["hardware_type"] = infer_hardware_type(p)
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
    """根据传入的目标（PNG 图片文件或目录）收集待提取图片。
    - target 是 PNG 文件：返回 [该文件]（绝对路径）；非 PNG 报错拦截
    - target 是目录：递归扫描其中 PNG；非 PNG 图片跳过并提示清单（不计入待提取）
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
    # --status：查看默认目录（价格图片/mem/）进度
    if "--status" in sys.argv:
        status()
        return

    # --out-dir <目录>：输出目录（默认 database/output_mem/，与脚本同级）
    global OUTPUT_DIR, OUT_DIR, CROP_DIR, OCR_CACHE_DIR, PROGRESS_LOG, MANIFEST_PATH
    if "--out-dir" in sys.argv:
        i = sys.argv.index("--out-dir")
        if i + 1 >= len(sys.argv):
            print("错误: --out-dir 需要一个目录参数")
            return
        OUTPUT_DIR = os.path.abspath(sys.argv[i + 1])
        OUT_DIR = os.path.join(OUTPUT_DIR, "extracted_mem")
        CROP_DIR = os.path.join(OUTPUT_DIR, "crop_cache")
        OCR_CACHE_DIR = os.path.join(OUTPUT_DIR, "ocr_cache")
        PROGRESS_LOG = os.path.join(OUTPUT_DIR, "extract_mem_progress.log")
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
        print("用法: python extract_mem.py <PNG图片或目录> [--workers N] [--out-dir 目录] [--db] [--status]")
        print("示例:")
        print(f"  python {os.path.basename(sys.argv[0])} ../价格图片/mem")
        print(f"  python {os.path.basename(sys.argv[0])} ../价格图片/mem/0031103783ad9ee85706f7021e2af2b4.png")
        print(f"  python {os.path.basename(sys.argv[0])} ../价格图片/mem --workers 4")
        print(f"  python {os.path.basename(sys.argv[0])} ../价格图片/mem --out-dir ./my_output")
        print(f"  python {os.path.basename(sys.argv[0])} ../价格图片/mem --db   # 提取后统一入库")
        return

    target = args[0]
    images = collect_images(target)
    if not images:
        return

    print(f"[MEM 管线] 共 {len(images)} 张图片，并发数: {workers}")
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
            print("\n[MEM 管线] 收到 Ctrl+C，正在优雅停止：不再开始新图，等待在跑的图完成（再按一次 Ctrl+C 强制退出）...", flush=True)
        else:
            # 第二次 Ctrl+C：真强制退出（executor __exit__ 会等在跑任务，必须直接终止进程）
            print("\n[MEM 管线] 强制退出（进度日志未写入；未完成的图下次重跑会自动重试）。", flush=True)
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
            print("\n[MEM 管线] 强制退出（进度日志未写入）。", flush=True)
            os._exit(1)

    if stop.is_set():
        print(f"\n[MEM 管线] 已优雅停止: 新提取 {ok}, 跳过 {skipped}, 失败 {fail}，剩余待提取 {len(images) - ok - skipped - fail} 张，结果存于 {OUT_DIR}")
    else:
        print(f"\n[MEM 管线] 提取完成: 新提取 {ok}, 跳过 {skipped}, 失败 {fail}，结果存于 {OUT_DIR}")
    if failed_list:
        print("失败清单（重跑 extract_mem.py 会自动重试）:")
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

    # --db：统一入库（管线结束时执行 load_mem 的完整主流程，做法 B）
    if "--db" in sys.argv:
        print("\n[MEM 管线] 开始数据库导入（全量 extracted_mem/，含全库去重）...")
        try:
            sys.path.insert(0, BASE_DIR)
            from load_mem import run_import   # 复用 load_mem 现有逻辑，不重写
            # 传入当前输出目录（--out-dir 自定义时对齐，默认时与 load_mem 默认一致）
            result = run_import(OUT_DIR)      # 全量，幂等无害
            print(f"[MEM 管线] 入库完成: 导入 {result['passed']} 条, "
                  f"跳过 {result['skipped']} 条, "
                  f"去重删除 {result['dup_same']} 条, "
                  f"冲突 {result['conflicts']} 条")
            if result['conflicts']:
                print("  冲突明细见 load_mem_report.md / load_mem_conflicts.json，请人工核对原图")
        except Exception as e:
            print(f"[MEM 管线] 入库失败: {e}")
            print("  （提取结果不受影响，JSON 已在盘上；可手动执行 python database/load_mem.py 重试）")
            sys.exit(1)   # 非零退出码提示有错，但数据安全


def status():
    """查看提取进度（断点状态）。--status 是唯一无副作用模式。
    与 main()/提取链路同口径：按内容哈希键检查（结果键 = {MD5}.json），
    不用图片 basename（文件名与内容键无关）。"""
    images = []
    for root, _dirs, files in os.walk(IMG_DIR):
        for f in sorted(files):
            if os.path.splitext(f)[1].lower() == ".png":
                images.append(os.path.join(root, f))
    images.sort()
    manifest = load_manifest()
    all_done = {os.path.splitext(os.path.basename(p))[0]
                for p in glob.glob(os.path.join(OUT_DIR, "*.json"))}
    # 已提取只统计与当前图片哈希匹配的结果（陈旧结果单独计数，避免偏大）
    png_keys = set()
    for i in images:
        try:
            png_keys.add(content_key(i))
        except Exception:
            pass
    done = all_done & png_keys
    stale = len(all_done) - len(done)
    pending = [i for i in images if content_key(i) not in all_done]
    print(f"[MEM 管线] 图片总数: {len(images)}")
    print(f"已提取: {len(done)}")
    if stale:
        print(f"陈旧结果（哈希不在当前图片中，不占用断点）: {stale}")
    print(f"待提取: {len(pending)}")
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
