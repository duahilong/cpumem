# AGENTS.md

## 项目定位

这是一个**离线、脚本优先的硬件报价单 ETL 项目**。人工将报价截图按 `CPU`、`mem`、`TF`、`其他` 分类放入 `价格图片/`；Python 脚本调用多模态 LLM 提取结构化 JSON。CPU 与 MEM 专用路径还依赖本地 OpenAI 兼容 GLM-OCR；CPU 结果可由专用 importer 写入 SQLite。

项目没有 Web 服务、前端、包管理清单、迁移框架、CI、自动化测试套件或现成查询 UI。领域术语、提示词、CLI 输出均主要为中文，文本文件应使用 UTF-8。

**始终以当前代码为准。** `docs/CPU提取管线.md` 跟踪 CPU 的新内容寻址和分块 OCR 实现；`docs/MEM提取管线.md` 的 `load_mem.py` / `quotes.hardware_type` 内容超前于代码；`docs/OCR辅助提取实验.md` 和 `docs/数据库设计文档.md` 包含大量历史流程与过时命令。任何实现前均须结合代码验证文档描述。

## 操作前的约束

1. 开始任务先运行 `git status --short`。本项目经常有用户尚未提交的脚本、文档、DB、原图、结果、缓存、报告及冲突清单；只修改任务相关文件，绝不覆盖、回退、删除、暂存无关内容。
2. `价格图片/` 是敏感原始数据。真实 CPU/MEM/通用提取会 base64 编码图片并发送到配置的 OCR/LLM 服务，消耗配额和时间；单图或批量真实调用前都必须取得用户明确授权。
3. `database/build_db.py` 会无确认删除重建 SQLite；`database/load_cpu.py` 与 `database/clean_load.py` 会删旧 quote、全库去重、生成/覆盖报告和冲突文件。运行 loader、建库、清缓存、批量改名/移动前先说明影响并取得授权。
4. `database/llm_config.json` 包含敏感 endpoint/凭据；不得打印、复制、上传、提交、在日志记录或在回复中透露其值。
5. `.gitignore` 当前忽略 `价格图片/`、`database/output_cpu/`、`database/output_mem/` 和系统文件；但仍**不忽略** `llm_config.json`、`cpumem.db`、`database/extracted/`、冲突文件、基准 Excel 和 `__pycache__/`。不要把生成物或密钥误当普通源码提交。

## 当前架构

```text
价格图片/{CPU, mem, TF, 其他}/              # 手工分类原图（Git 忽略）
  ├─ CPU 专用：database/extract_cpu.py
  │    -> database/output_cpu/
  │       ├─ manifest.json                  # MD5 内容键 -> 原始 basename
  │       ├─ crop_cache/{key}.png / _LL / _LR
  │       ├─ ocr_cache/{key}_LL.md / _LR.md
  │       └─ extracted_cpu/{key}.json
  │    -> database/load_cpu.py
  │    -> database/cpumem.db
  ├─ MEM 专用：database/extract_mem.py
  │    -> database/output_mem/{ocr_cache, extracted_mem}/
  │    -> （当前没有 load_mem.py；不能正式入库）
  └─ 通用/旧路径：database/extract.py
       -> database/extracted/{basename}.json
       -> database/clean_load.py
       -> database/cpumem.db
```

### 当前最重要的边界

- **CPU 有正式 importer**：`load_cpu.py` 默认读取 `database/output_cpu/extracted_cpu/*.json`，先预验证，再按 `source_image` 幂等替换，最后对整个共享数据库去重，并写 CPU 冲突/报告。
- **MEM 没有 importer**：仓库没有 `database/load_mem.py`，实际 SQLite schema 没有 `quotes.hardware_type`；MEM JSON 的 `hardware_type` 目前不会通过任何正式路径持久化。不得采信 MEM 文档中“已导入”/“三条 importer”/`quotes.hardware_type` 的说法。
- **通用 loader 只读取** `database/extracted/*.json`，不会读取 `output_cpu/extracted_cpu/` 或 `output_mem/extracted_mem/`。
- 通用 `extract.py` 仍递归扫描 `价格图片/` 的**所有**类别，包括 CPU 与 mem，会和专用路径重叠；生产 CPU/MEM 工作优先专用脚本。
- CPU 新路径以文件内容 MD5 做内部键；MEM 与通用路径仍以 basename 做结果、缓存和/或来源键，后两者仍有同名冲突风险。

## 文件和目录职责

### 代码与配置

- `database/extract_cpu.py`：CPU OCR-first extractor；使用 `numpy`、Pillow、GLM-OCR、OpenAI SDK。当前已接入 MD5 内容寻址、三段裁剪、LL/LR 分块 OCR、优雅 Ctrl+C 停止。
- `database/load_cpu.py`：CPU 专用预验证、幂等导入、全库去重、Markdown 报告与冲突 JSON 写入器；复用 `clean_load.py` 的标准化函数。
- `database/extract_mem.py`：MEM OCR-first extractor；整图 OCR/LLM，保留所有硬件区块，补齐 `hardware_type`。
- `database/extract.py`：旧/通用 extractor；整图视觉 LLM，固定 10 并发，递归全部分类。
- `database/clean_load.py`：旧/通用 loader；只读 `database/extracted/*.json`。
- `database/build_db.py`：破坏性空库重建脚本。
- `database/test_noocr_extract.py`：CPU 无 OCR 对比实验，真实调用 LLM；不属于生产路径。
- `database/cpu_watchlist.json`：仅由通用路径读取的 CPU 白名单。CPU 专用 OCR 路径的白名单内嵌在 `OCR主_指令.txt`；范围变动时须同步两处。
- `database/llm_config.json`：LLM/OCR 服务配置及凭据；严禁泄露。

### 提示词

- `database/prompts/OCR主_指令.txt`：**当前 CPU 专用生产路径**的完整 OCR-first 指令、JSON 契约、分块子表说明、消歧规则和内嵌白名单。
- `database/prompts/OCR主_指令_MEM.txt`：**当前 MEM 专用路径**的 OCR-first 指令和 `hardware_type` 契约。
- `database/prompts/base.txt`、`mem.txt`、`TF.txt`、`其他.txt`：通用/历史路径按父目录装配的提示词；单改它们不会改变 CPU/MEM OCR-first 专用路径。
- 当前没有 `database/prompts/CPU.txt`；任何文档/历史实验中涉及它的说明都不是当前运行行为。

### 产物与验证资产

- `database/output_cpu/`（Git 忽略）：CPU 默认的全部运行产物。
  - `manifest.json`：`MD5(file bytes) -> 原始 basename` 映射；同内容异名会升级记录为 `{name, aliases}`。它是从 hash 型 JSON/DB `source_image` 反查原名的重要资产，应与 DB 备份。
  - `crop_cache/{key}.png`：三段裁剪图；`{key}_LL.png` / `{key}_LR.png`：两个 CPU 子表切块。
  - `ocr_cache/{key}_LL.md` / `{key}_LR.md`：分别 OCR 后的 Markdown 缓存。
  - `extracted_cpu/{key}.json`：最终 CPU JSON 和唯一成功检查点；其中 `source_image` 是 `{key}.png`，而不是人类原始文件名。
  - `extract_cpu_progress.log`：追加式日志；当前进度日志、状态统计、保留结果数可能因重构/中断而互不相等，不要将任一数字单独当作全量质量结论。
  - `load_cpu_conflicts.json`、`load_cpu_report.md`：CPU 导入业务产物。当前可能不存在，不能假设其必有或反映最新 316 图结果。
- `database/output_mem/`（Git 忽略）：MEM 默认运行产物。可能有 OCR 缓存但 `extracted_mem/` 为空；OCR 缓存存在不表示 LLM 成功或已入库。
- `database/extracted/`：通用旧路径结果，也是 `clean_load.py` 的唯一输入。
- `database/conflicts_cpu.json`：旧/历史 CPU 冲突清单；不要和 `output_cpu/load_cpu_conflicts.json` 混为同一产物。
- `database/pricebenchmark/`：两期 CPU 人工 Excel 基准。`verify.py` 当前不存在；旧文档中的验证命令不可直接运行。
- `database/test_crops/`、`llm_ocr_test.md`：分块/OCR 手工测试材料，非生产输入。
- `database/cpumem.db`：生成数据；目前可能已有 CPU 数据和删冲突历史。未经明确授权不得重建或批量改写。

## 依赖和服务

- Python 3.10+。
- 标准库：`sqlite3`、`json`、`glob`、`re`、`datetime`、`base64`、`urllib.request`、`threading`、`concurrent.futures`、`signal` 等。
- 外部包：`openai`、`numpy`、`Pillow`。当前没有依赖清单；缺包时按实际需要安装：

```bash
python -m pip install openai numpy Pillow
```

- 所有 extractor 使用配置的 OpenAI Chat Completions 兼容多模态服务。
- CPU/MEM OCR 还需要 OpenAI 兼容 GLM-OCR 服务。`ocr_base_url` 优先从 `llm_config.json` 读取；环境变量 `OCR_BASE_URL` **仅当配置没有该项时**才作为回退。
- 典型可选配置包括 `temperature`、`timeout_seconds`、`extra_body`、`ocr_timeout_seconds`；MEM OCR 额外读取 `ocr_max_tokens`。不要复述配置中的实际值或凭据。

## JSON 契约与真实性

### CPU 专用 JSON

```json
{
  "sheet_date": "2026-03-11",
  "products": [
    {
      "category": "CPU",
      "vendor": "Intel",
      "product_name": "i5-12400F",
      "price_type": "散片",
      "price": 613
    }
  ],
  "source_image": "<content-md5>.png"
}
```

- 结果仅保留 `category == "CPU"`。
- `product_name` 应是型号本体；不能带核数、线程、频率和描述文字。`F`、`K`、`KF`、无后缀是不同型号。
- `price_type` 应为 `散片` 或 `原盒`。
- CPU OCR-first 契约把 `数字+****`（例如 `480/****`）中的数字视为有效；纯 `****`、空格、无法确认的格无价。

### MEM 专用 JSON

MEM 保留图上的所有类别，且每条含：

```json
{"hardware_type": "DDR3 | DDR4 | DDR5 | SSD | HDD | GPU | MB | PSU | MON | CPU | PERIPH | CARD | OTHER"}
```

- 内存产品必须保留容量、DDR 代际、频率、时序、套条/单条、颜色/马甲等区分规格；存储必须保留容量、接口等规格。
- 代码会在 `hardware_type` 缺失/非法时兜底：显式 DDR 优先；频率 `4000..12000` 推断 DDR5、`800..2133` 推断 DDR4；其余按 category 映射或 `OTHER`。
- 该字段目前仅存在 JSON，不入 SQLite。

### 通用旧 JSON

通用输出也含 `sheet_date`、`products`、`source_image`，但会出现更多类别和标签。`clean_load.py::PRICE_TYPE_MAP` 只保留散片、原盒、单条、套装、默认；保固、国行、全新等未映射标签会折叠为 `默认`。

**真实性优先：**不得补造价格、跨行/跨列/跨区块复制价格，或根据不确定文本推断。通用 `base.txt` 中“区间价取中间值”的历史规则与此冲突，修改通用提示词时不得据它写入图片未明确给出的中间价格。

## CPU 专用管线：`extract_cpu.py`

### 当前实际流程

```text
显式指定 CPU 图片或目录
  -> 文件扩展名：collect_images 接受 png/jpg/jpeg；extract_one 实际只处理 PNG
  -> key = MD5(源文件原始字节)，register_key() 登记 manifest
  -> output_cpu/extracted_cpu/{key}.json 存在则跳过
  -> crop_cpu_image(img, key)：三段锚点裁剪 -> crop_cache/{key}.png
  -> split_table_blocks(crop, key)：二级竖线切块
       -> crop_cache/{key}_LL.png / {key}_LR.png
  -> ocr_markdown(img, key)：LL/LR 分别 OCR
       -> ocr_cache/{key}_LL.md / {key}_LR.md
       -> 带【左子表】/【右子表】标注的纯文本拼接
  -> OCR主_指令.txt + 合并 OCR Markdown
  -> 多模态 LLM：完整裁剪图（含日期横幅）+ 联合提示词
  -> 仅 CPU 类目，source_image = {key}.png
  -> output_cpu/extracted_cpu/{key}.json
  -> （人工触发）load_cpu.py -> cpumem.db
```

### 内容寻址规则

- key 是源文件**原始字节**的 MD5。字节不变时改名、移动、复制都复用同一 key；不同内容或重新保存/转码后字节变化则成为新 key。
- CPU 管线内部的裁剪、分块、OCR 缓存、JSON 结果和 DB `source_image` 都使用该 key；同名不同图不会碰撞，同内容异名会自动跳过。
- 原始文件名不参与 CPU key；由 `manifest.json` 反查。丢失 manifest 可由原图重算 hash 重建基本映射，但别名线索会丢失。
- 当前 CPU 源文件名大多本身为哈希样式，但不要依赖此偶然事实；仍应通过 `content_key()` 得到 key。
- CPU 管线收集 JPG/JPEG 但 `extract_one()` 会拒绝非 PNG 并记失败；输入必须预先转为 PNG，且之后不要重新编码以免改变 key。

### 裁剪、分块与 OCR

- `crop_cpu_image()` 用顶部亮区识别横幅底部、在 30–50% 图宽范围找长暗竖线识别 CPU 区右边界，失败回退约 9% 高度和 40% 宽度；三段拼图保留横幅日期、CPU 表头和 CPU 主体，排除右侧硬盘/内存干扰。
- `split_table_blocks()` 已**真正接入** `ocr_markdown()`：先把 CPU 区与右侧非 CPU 区分开（中间不落盘），再把左侧分为 LL（Intel 老款/11–14 代子表）和 LR（15/14 代 U 系 + AMD 子表）。两块都应保留各自完整型号/散片/原盒列。
- `_find_split()` 在多个候选竖线中取最左候选，避免误把 15/14 子表左边界作为前一子表右边界；不要擅自恢复旧的“最后候选”逻辑。
- `_ocr_image_to_md()` 对每个分块单独请求 GLM-OCR；CPU `max_tokens` 硬编码为 16384，`temperature=0`。
- `html_table_to_markdown()` 仅针对预期的、小写且属性带引号的 OCR HTML，展开简单 `rowspan`/`colspan`；它不是通用 HTML parser。格式异常时可能原样交给后续 LLM，改动前需核查真实样本。
- LL/LR OCR 结果是**文本拼接**，各自带子表头；不跨块并成同一 Markdown 表，避免按行强行对齐而串价。
- 分块图不含标题横幅；因此 LLM 仍必须接收完整裁剪图，以从横幅读取 `sheet_date` 并在 OCR 可疑时作视觉裁决。

### 执行语义和缓存

- 只有 `--status` 使用默认 `价格图片/CPU/` 且不调用服务。其他运行必须显式传文件/目录；目录递归扫描。
- 默认 2 并发，`--workers N` 上限 6。
- `--out-dir DIR` 将结果、裁剪、OCR、日志路径切到自定义根目录；**当前没有同步更新 `MANIFEST_PATH`**，manifest 仍使用默认 `database/output_cpu/manifest.json`。自定义输出运行前先确认这会造成 manifest 与产物分离。
- OCR 失败/空响应、LLM 失败、JSON 解析失败都会使该图失败且不写最终 JSON；没有生产纯视觉/纯 OCR 降级。重跑会重试。
- 改裁剪逻辑：清对应 `{key}.png`，并清 `_LL/_LR` 分块及其 OCR 缓存；改分块逻辑：清对应 `_LL/_LR.png` 和 `_LL/_LR.md`；改 OCR 行为：清对应 md；改主提示词：删对应 JSON。上述操作有成本和数据风险，须授权。
- 结果/checkpoint 写入和 manifest 写入不是原子事务；`register_key()` 在多 worker 下 read-modify-write manifest，缺乏锁/原子替换，可能丢失并发登记项。
- `main()` 的启动 `done`/末尾 `total_done` 仍混用 basename、manifest 值与 hash，可能误报进度；以 `extract_cpu.py --status` 的 MD5 比对为更接近实际的状态，但仍应把产物数、日志和 DB 行数看作不同指标。
- 已实现第一次 Ctrl+C 的优雅停止：不再提交新图、等待在跑任务；第二次 Ctrl+C 强退。未完成图应重跑。

## MEM 专用管线：`extract_mem.py`

```text
显式指定 MEM 图片或目录
  -> output_mem/extracted_mem/{basename}.json 是否存在则跳过
  -> 整张原图 -> GLM-OCR -> output_mem/ocr_cache/{basename}.md
  -> OCR主_指令_MEM.txt + OCR Markdown
  -> 多模态 LLM：完整原图 + 联合提示词
  -> 保留所有类别，补齐 hardware_type
  -> output_mem/extracted_mem/{basename}.json
  -> （当前没有 importer）
```

- MEM 不裁剪，因为近方形多区块报价单必须保留内存、SSD、主板、显卡、显示器等区域。
- 除 `--status` 外必须显式传目标；目录扫描可递归，支持 PNG/JPG/JPEG。默认 2 并发、上限 6，支持 `--workers`、`--out-dir`。
- OCR 是硬依赖；它读取配置中的 `ocr_max_tokens`（默认 16384）以限制重复退化，和 CPU 的硬编码 token 行为不同。
- 输出/缓存仍按 basename 键控；不同目录同名图、替换同名图片均可能复用陈旧缓存/结果。
- MEM `status()` 只扫描默认目录顶层，而真实指定目录提取可递归，故对子目录输入它可能低估/误报状态。
- 当前 `output_mem/extracted_mem/` 可为空但已有 OCR 缓存；缓存不等于提取完成，MEM OCR-first 输出也尚无成熟人工 benchmark，不能宣称可生产入库。

## CPU 导入：`load_cpu.py`

`load_cpu.py` 是 CPU 结果的正式人工触发入库步骤。默认输入 `database/output_cpu/extracted_cpu/`，可指定单 JSON、目录或 `--out-dir` 对齐自定义 CPU 输出根。

1. 顶层 `source_image` 必须非空；CPU 内容寻址结果中应为 `{md5}.png`。
2. 每条预验证：日期可解析、型号非空、价格可转数字且在 `1..200000`、价格字符串不含 `*`/`X`、价格类型规范化后为 `散片`/`原盒`、category 非空。
3. 仅当一个文件至少有一条有效记录时才删除该 `source_image` 的旧 quotes；该文件中无效记录会被跳过，**不是严格的全文件 all-or-nothing**。
4. 有效项写 products/dates/quotes。产品键为 `clean_load.norm_product_key()` 的 `vendor-型号`；新 products 的 category 无论输入类别写成什么都会强制为 `CPU`。展示名清洗发生在 product key 创建之后，措辞变化仍可能拆分产品键。
5. 对整个共享 DB 按 `(date_key, product_key, price_type)` 去重：同价留最早 id；异价也保留最早 id、删除后者，写 `output_cpu/load_cpu_conflicts.json`。
6. 写 `output_cpu/load_cpu_report.md`；“入库价格记录”是预验证通过/尝试插入数量，**不等于**全局去重后保留 quote 数。

风险：

- 去重跨来源且全库破坏性，可能删掉同日同型号同类型的合法不同报价；冲突是人工审阅队列，不是已解决问题。
- 冲突 JSON 的 `kept.source` 当前为 `null`，不能完全溯源赢家。
- `load_cpu.py --status` 的“价格记录”计数是所有具有 `source_image` 的 quotes，并非严格仅 CPU；共享 DB 混入其他结果后会偏大。
- 每次导入都会写报告/冲突清单并执行全库去重，不得在测试中无授权反复运行。

## 通用旧路径：`extract.py` / `clean_load.py`

### 通用 extractor

- 递归扫描 `价格图片/` 全部分类，整图直送 LLM，无 OCR，固定 10 并发。
- 组合 `base.txt + <父目录>.txt`；缺 prompt 时只使用 base。
- 只要 `cpu_watchlist.json.enabled` 为真，就会给**所有类别**追加 CPU 白名单，非 CPU 图也受无关上下文影响；这是现存缺陷。
- 结果平铺为 `database/extracted/<basename>.json`，同名即跳过。它会和 CPU/MEM 专用路径重叠，但不可互相混用。
- `extract.py` 默认就是真实 LLM 调用；没有 `--real` 开关。

### 通用 loader

- 仅处理 `database/extracted/*.json`。
- 在逐产品验证**前**删除相同 `source_image` 的旧 records；若 JSON 日期有效但 products 全坏，可能清空旧来源。
- 日期解析支持带年份的 `YYYY-M-D`（`-` `/` `.`）和 `M月D日`，并把年份强制 2026；不支持文档仍宣称的 `9.16`。
- `guess_category()` 先 CPU、再容量/SSD、最后 DDR/内存；`DDR5 16G` 这类缺类目内存可被误判为 SSD。
- `--force-conflicts` 没有实际功能；异价后续记录仍会被删除，flag 只影响提示文字。
- 虽有自动建表函数，`main()` 在 DB 不存在时提前返回，CLI 不能自动创建 DB；首次 generic 导入须先获授权并执行 `build_db.py` 或先修行为。

## SQLite schema（当前代码）

`build_db.py` 与 `clean_load.py::_create_tables()` 定义：

| 表 | 字段 |
| --- | --- |
| `products` | `product_key` PK、`display_name`、`category`、`vendor` |
| `dates` | `date_key` PK、`year`、`month`、`day`、`weekday` |
| `quotes` | `id`、`product_key`、`date_key`、`price`、`price_type`、`source_image` |

索引：`idx_q_prod_date(product_key, date_key)`、`idx_p_cat_vendor(category, vendor)`。

- 没有 `hardware_type` 列；没有 source/vendor/category/price-type 独立维表；没有 `(date_key, product_key, price_type)` DB 唯一约束。去重仅靠 Python。
- `products` 仅首次插入，后续结果不会改正其展示名、类别或品牌。
- `norm_product_key()` 只标准化大小写、空格/下划线与连字符，未全面统一别名、拼写、规格后缀、容量格式或品牌语义；相近写法仍可能形成不同 product。
- 既有 DB 的某些 loader 连接启用外键，创建路径不一致。
- `build_db.py` 删除整个 DB，绝不作为普通检查命令使用。

## 验证、实验与质量边界

- 没有测试 runner、CI、依赖锁或当前可运行的 `verify.py`。Python 改动后至少执行 `py_compile`。
- `database/pricebenchmark/` 保留两期 CPU Excel 人工基准；文档记录的 0a04 132/132 与 0b5a 141/148（95.3%）是历史/整图 OCR 阶段结果，不自动证明当前 LL/LR 分块链路、全量结果或 DB 正确。
- LL/LR 分块已接入代码和已有缓存，但其真实服务链路+人工基准的量化复验仍应在获授权后完成；不要将实验预期当生产准确率。
- MEM OCR-first 没有已建立基准；现有 OCR 材料可见重复、错位和模型名误识风险。未建立质量门槛前不得把 MEM 批量结果当可入库生产数据。
- `test_noocr_extract.py` 是配额消耗型实验：使用 CPU prompt 但不提供 OCR 文本，输出 `output_cpu/extracted_cpu_noocr/` 并标记 `_test_noocr: true`。没有 importer 会强制阻止这类结果，绝不可传给 loader。
- 无参数 no-OCR 实验会扫描 `crop_cache/*.png`，其中包含 `{key}_LL/_LR.png` 辅助块，可能把子表当成独立测试图；其 docstring 声称的 `--status` 并未实现，不能作为无副作用命令运行。

## 常用命令

从仓库根执行：

```bash
# 无副作用状态/语法检查
python database/extract_cpu.py --status
python database/extract_mem.py --status
python database/extract.py --status
python database/load_cpu.py --status
python -m py_compile database/build_db.py database/clean_load.py database/extract.py database/extract_cpu.py database/extract_mem.py database/load_cpu.py database/test_noocr_extract.py

# CPU：真实 OCR + LLM，会写 output_cpu/（须授权）
python database/extract_cpu.py 价格图片/CPU/example.png
python database/extract_cpu.py 价格图片/CPU --workers 4
python database/extract_cpu.py 价格图片/CPU --out-dir ./my_output

# CPU：真实 DB 修改、全库去重、报告/冲突更新（须授权）
python database/load_cpu.py
python database/load_cpu.py database/output_cpu/extracted_cpu/<key>.json
python database/load_cpu.py --out-dir ./my_output

# MEM：真实 OCR + LLM，写 output_mem/；没有 load_mem.py（须授权）
python database/extract_mem.py 价格图片/mem/example.png
python database/extract_mem.py 价格图片/mem --workers 4
python database/extract_mem.py 价格图片/mem --out-dir ./my_output

# 通用旧路径：真实 LLM / 通用 DB 修改（须授权）
python database/extract.py --file TF/example.png
python database/extract.py
python database/clean_load.py

# 破坏性 DB 重建（必须明确授权）
python database/build_db.py

# 实验：真实 LLM，不能交给 loader
python database/test_noocr_extract.py database/output_cpu/crop_cache --workers 2 --limit 10
```

不要按旧文档执行 `extract.py --real`、`load_mem.py`、`verify.py` 或 `CPU.txt` 工作流；它们不存在或与当前代码不匹配。

## 维护要求

- 延续脚本式实现：从 `__file__` 推导路径、模块常量、简短中文 docstring、轻量 `sys.argv`、`main()` + `if __name__ == "__main__":`。
- 改 CPU 提示词须编辑 `OCR主_指令.txt`；改 MEM 专用提示词须编辑 `OCR主_指令_MEM.txt`。不要只改 legacy prompt 后误以为专用路径会变化。
- 修改 CPU 内容键、manifest、裁剪、分块、OCR HTML/Markdown、OCR token/超时、白名单或 prompt 时，必须先考虑已有内容键缓存、DB `source_image` 和 manifest 兼容性；按影响层清对应缓存，并先以代表图真实重提验证（需授权）。
- 变更 loader、产品键、日期/类别/价格类型映射、去重或 schema 时，必须考虑 CPU/通用共享 DB、历史数据、冲突报告及迁移；不能只改文档。
- 若实现 MEM importer，必须同时设计 schema migration、`hardware_type` 持久化、全类目合法 `price_type`、冲突策略、历史 DB 兼容、测试与人工基准；不得只依据当前超前 MEM 文档添加命令入口。
- 不要把 `__pycache__/`、临时图、OCR 缓存、提取 JSON、日志、DB、报告或敏感配置作为普通代码变更提交，除非用户明确要求。
