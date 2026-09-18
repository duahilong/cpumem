# -*- coding: utf-8 -*-
"""
步骤1：提取 —— 逐张调用多模态 LLM 读取报价图片，输出原始 JSON 存档。

配置：编辑 llm_config.json，填入 base_url / api_key / model（OpenAI 兼容格式）。

用法：
    python extract.py                # 模拟模式（不调 LLM，生成模拟数据验证链路）
    python extract.py --real         # 真实模式（调用 llm_config.json 配置的 LLM）
    python extract.py --real --file 图片文件名   # 只提取指定图片（单张测试）

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

# ============ 提取提示词（决定数据质量的核心） ============
# 公共基础规则（所有分类通用）
BASE_PROMPT = """你是硬件报价单数据提取助手。请仔细阅读这张报价单图片，把其中的产品价格信息提取为 JSON。

要求：
1. 输出一个 JSON 对象，格式如下：
{
  "sheet_date": "YYYY-MM-DD",
  "products": [
    {
      "category": "大类",
      "vendor": "品牌",
      "product_name": "型号名",
      "price_type": "价格类型",
      "price": 613
    }
  ]
}
字段说明：
- sheet_date: 从图片标题提取报价日期。图片中只包含月份和日期，年份固定为 2026 年，直接输出 "2026-MM-DD"
- vendor: 品牌，如 Intel / AMD / 金士顿 / 三星 / 罗技（以表头区块标题为准）
- product_name: 型号名。容量/规格必须完整拼入型号名（如 "TF卡 SDCS3 16G"、"990 PRO 1TB"），不能只写系列名；同一系列不同容量的产品必须各自一条记录
- price_type: 价格类型
- price: 纯数字价格。如果价格含星号(*)、字母X、或无法辨认（如"****"、"****/853"），该价格视为无效：
  * 整行价格都无效 -> 不要输出该条记录
  * 只有一个价格无效（如"****/853"中的第一个） -> 只输出有效的那个价格（如 原盒 853）
  * 区间价（如"695--1320"）取中间值或只取能确认的那个数

2. 严格按图片内容提取，不要推断、不要编造；看不清的记录直接跳过。
3. 价格不明确的记录（含星号、X、空）直接剔除，不要编造价格；只有价格明确的产品才输出。
   【严禁跨行复制价格】每行的价格只能来自本行。如果某行的价格栏是空白（如缺货/未报价），该行直接跳过，绝对不能把相邻行的价格复制给它。例：图中 "Ryzen-9 9950X 3D 2" 行价格栏为空白，则不要为它输出任何记录。
4. 只输出 JSON，不要其他文字。每个产品只输出 category/vendor/product_name/price_type/price 五个字段，不要输出 spec/price_raw/note 等其他字段。
"""

# 分类专用规则：按图片所在文件夹自动附加
CATEGORY_PROMPTS = {
    "CPU": """
本图是 CPU 报价单，专用规则：
- category 统一为 "CPU"
【拆行总则 —— 先判断行结构，再拆分】
拆分前必须先数清楚：这一行有 N 个型号、M 个价格。然后按以下规则对应：
- 情况A：1个型号 + 2个价格（如 "U5 230F  12/238"）→ 按散片/原盒拆两条（散片12、原盒238）
- 情况B：N个型号 + N个价格（如 "i3 6100-I5 6500-I7 6700  12/85/238"，或 "U5 230F-245KF-245K  12/85/238"）→ 按型号拆，每个型号一个价（230F=12、245KF=85、245K=238），price_type 根据该区域的列头判断（散片区则为散片）
- 情况C：2个型号 + 3个价格（如 "U5 250K PLUS-U7 270K PLUS  330/383/389"）→ 说明散片/原盒与多型号交织，需结合表格列头判断：可能是"型号1散片/型号1原盒/型号2散片"或"型号1散片/型号2散片/型号2原盒"，按列头位置严格对应
- 【逐行对齐校验 —— 防止价格串行】提取多型号交织区域时，必须逐行校验：
  * 每个价格必须能说出它对应的型号名和位置（第几个价格、哪一列），说不出来就跳过
  * 同一型号不能出现在多条记录里且价格不同（如 "U9 285K 散片 89" 和 "U9 285K 散片 1255" 同时出现 = 对齐错误，说明价格串行了）
  * 价格从上到下逐行对应，上一行的价格绝对不能给到下一行的型号
  * 价格的量级要合理：i3 散片应几百元、i9 散片上千元、U系 CPU 不可能 10~90 元（10~90 是隔壁内存条/表带价格）
  * 如果某区域价格对不上号（如型号数量与价格数量不符、价格量级明显错误），宁可跳过该行，也不要猜测分配
- 【行结构识别】型号边界识别：U5/U7/U9、i3/i5/i7/i9、Ryzen、G 开头（奔腾）是新型号的标志；型号名里的"-"连接的是同一型号的规格（如 i5-13400F），不是型号分隔符；只有当"-"两边都是独立型号名（如 230F-245KF）才是多型号行
- 关键：型号名里含"-"不一定是多型号（如 "i5 13400F" 是型号本身），要先识别型号的完整边界（U5/U7/U9、i3/i5/i7/i9、Ryzen 开头为新型号的标志）
- 对应关系不确定时，直接跳过该行，不要随意猜测分配价格
- 型号名保留完整规格（如 "i5 13400 10核16线程 2.5/4.6"），不要省略
- 同一个型号在不同区块出现（如散片栏和原盒栏）时，分别提取为多条记录
""",
    "mem": """
本图是 内存 价格单，专用规则：
- category 统一为 "内存"
- 规格（容量/频率/时序/颗粒/马甲）必须完整拼入型号名，如 "FURY 16G 6000 C30 马甲"、"银爵 8G/3200 银三星"，不能只写系列名
- 价格如 "1800单条 3600套价" 是单条价+套装价，拆成两条记录：price_type 分别为 "单条" 和 "套装"
- 区分 DDR3/DDR4/DDR5（从型号名中识别，拼入型号名或 spec）
- 台式机/笔记本内存（如 "8G/1600 台式/NB笔记本"）区分清楚，同一行两个价格时拆成两条记录并注明用途
""",
    "TF": """
本图以 TF卡/存储卡 价格为主，专用规则：
- TF卡/SD卡/存储卡/内存卡类产品的 category 统一为 "TF卡"，容量必须完整拼入型号名（如 "TF卡 SDCS3 16G"、"TF卡 SDCG4 128G"），同一系列不同容量各自一条记录，TF版和SD版分开
- 注意：图中如果还包含其他区块（如固态硬盘、机械硬盘、U盘等），也必须一并完整提取，按实际内容标注 category（固态硬盘 / 机械硬盘 / TF卡 等），不要遗漏
- 品牌、价格、备注等其他字段遵循通用规则
""",
    "其他": """
本图是混合类报价单，专用规则：
- category 根据图片内容判断，只能是：主板 / 显卡 / 电源 / 显示器 / 固态硬盘 / 机械硬盘 / 外设
- 主板（如 微星 H410M A PRO）、显卡（如 影驰 RTX 5070）、电源（航嘉）、显示器（航嘉/微星）各自单独分类，不要归入"外设"
- 外设仅指鼠标/键盘/摄像头/耳麦等输入输出设备
- 固态硬盘/机械硬盘按原有规则提取
- 产品型号保留完整（如 "RTX 5070 12G 魔刃 OC"），显存容量拼入型号名
""",
}


def build_prompt(img_path: str) -> str:
    """按图片所在文件夹选择分类专用规则，并附加 CPU 白名单过滤规则"""
    parent = os.path.basename(os.path.dirname(img_path))
    category_rule = CATEGORY_PROMPTS.get(parent, CATEGORY_PROMPTS["其他"])
    prompt = BASE_PROMPT + category_rule

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


def mock_extract(img_path: str) -> dict:
    """模拟模式：生成与真实输出同构的 JSON，用于验证清洗入库链路。"""
    name = os.path.basename(img_path)
    return {
        "sheet_date": "2026-09-16",
        "products": [
            {"category": "CPU", "vendor": "Intel", "product_name": "i5-12400F",
             "spec": "", "price_type": "散片", "price": 613, "price_raw": "613", "note": ""},
            {"category": "CPU", "vendor": "Intel", "product_name": "i5-12400F",
             "spec": "", "price_type": "原盒", "price": 720, "price_raw": "720", "note": ""},
            {"category": "固态硬盘", "vendor": "金士顿", "product_name": "NV3-1TB",
             "spec": "NVMe", "price_type": "默认", "price": 533, "price_raw": "533", "note": ""},
        ],
        "_mock": True,
        "_source": name,
    }


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


def extract_one(img_path: str, extract_fn) -> tuple[str, bool, str]:
    """提取单张图片，返回 (图片名, 成功, 消息)。线程安全：每张图独立落盘。"""
    name = os.path.splitext(os.path.basename(img_path))[0]
    out_path = os.path.join(OUT_DIR, name + ".json")
    if os.path.exists(out_path):
        return (name, True, "跳过（已存在）")
    try:
        t0 = time.time()
        data = extract_fn(img_path)
        data["_source_image"] = os.path.basename(img_path)
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        n = len(data.get("products", []))
        return (name, True, f"完成 ({n} 条, {time.time()-t0:.0f}s)")
    except Exception as e:
        return (name, False, str(e))


def main():
    real = "--real" in sys.argv
    extract_fn = real_extract if real else mock_extract

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

    print(f"共 {len(images)} 张图片，模式: {'真实(LLM)' if real else '模拟'}，并发数: {MAX_WORKERS if real else 1}")
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

    if real:
        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
            futures = {pool.submit(extract_one, img, extract_fn): img for img in images}
            for fut in as_completed(futures):
                name, success, msg = fut.result()
                report(name, success, msg)
    else:
        for i, img in enumerate(images, 1):
            name, success, msg = extract_one(img, extract_fn)
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
        f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} real={real} 本次新提取={ok} 失败={fail} 总进度={total_done}/{len(images)}\n")


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
