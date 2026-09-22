# AGENTS.md

## 项目定位

这是一个**离线、脚本优先的硬件报价单 ETL 项目**。人工把报价截图按类目放入 `价格图片/`；Python 脚本以多模态 LLM 提取每图 JSON，CPU/MEM 专用路径还使用本地 GLM-OCR；部分结果再写入 SQLite，以支持硬件历史价格查询。

项目没有 Web 服务、前端、包管理清单、CI、迁移框架、`pytest`/`unittest` 测试套件或现成查询 UI。代码、提示词、CLI 和领域枚举主要使用中文。所有文本文件使用 UTF-8。

**以当前代码为准。** 文档含实验过程与已超前的实现设计：`docs/CPU提取管线.md`、`docs/MEM提取管线.md`、`docs/OCR辅助提取实验.md` 和 `docs/数据库设计文档.md` 都必须结合代码阅读，不能只按文档执行。

## 先检查工作区与副作用

1. 开始任务先执行 `git status --short`。本仓库可能已有用户未提交的代码、配置、数据库、原图、缓存、提取结果、冲突清单和报告；只修改任务相关文件，绝不覆盖、回退、删除或暂存无关改动。
2. 原图 `价格图片/` 可能敏感。任何真实 CPU/MEM/通用提取都会 base64 编码图片并发送到配置的 OCR/LLM 服务，消耗配额和时间；批量执行或单图真实调用前均须获得用户明确授权。
3. `clean_load.py`、`load_cpu.py` 会修改 SQLite 并可能删除重复/冲突 quote；`build_db.py` 会无提示删除并重建 DB。批量导入、运行 loader、建库或清缓存前必须先说明影响并获授权。
4. `database/llm_config.json` 含敏感服务配置/凭据。不得打印、复制、提交、上传、记录或在回复中泄露其值。
5. 图片、数据库、缓存、日志、提取 JSON、Excel、报告和实验产物大多**未被 `.gitignore` 保护**；不要将它们当作普通源码变更提交。

## 当前架构与集成边界

```text
价格图片/{CPU, mem, TF, 其他}/                 # 手工分类原图（Git 忽略）
  ├─ CPU 专用：database/extract_cpu.py
  │    → database/output_cpu/{crop_cache, ocr_cache, extracted_cpu}/
  │    → database/load_cpu.py
  │    → database/cpumem.db
  ├─ MEM 专用：database/extract_mem.py
  │    → database/output_mem/{ocr_cache, extracted_mem}/
  │    → （当前没有 load_mem.py；尚不能正式入库）
  └─ 通用/旧路径：database/extract.py
       → database/extracted/
       → database/clean_load.py
       → database/cpumem.db
```

### 最重要的现状

- **CPU 有独立的正式导入器**：`load_cpu.py` 默认读取 `database/output_cpu/extracted_cpu/*.json`，预验证、幂等写入 `cpumem.db`，随后全库去重并输出 CPU 冲突/报告。
- **MEM 当前没有 importer**：尽管 `docs/MEM提取管线.md` 反复描述 `load_mem.py`、`quotes.hardware_type` 和 MEM 导入报告，当前仓库里**不存在 `database/load_mem.py`**；SQLite schema 也没有 `hardware_type` 列。不得按该文档假设 MEM 已可入库。
- **通用 loader 只读 `database/extracted/*.json`**：`clean_load.py` 不读 `output_cpu/extracted_cpu/`、`output_mem/extracted_mem/`。
- `extract.py` 仍递归扫描 `价格图片/` 的所有分类（包括 CPU、mem），不是“只处理其他类目”。它会与专用路径重叠；生产 CPU/MEM 工作优先使用专用脚本。
- 三条路径都使用 basename 作为输出、缓存、进度和/或 `source_image` 键；同名文件、替换同名源图及跨目录合并都有碰撞/覆盖/复用风险。

## 目录与职责

### 输入、代码与配置

- `价格图片/CPU/`、`价格图片/mem/`、`价格图片/TF/`、`价格图片/其他/`：手工分类的原图。禁止未授权删除、移动、批量改名、提交或外传。
- `database/extract_cpu.py`：CPU OCR-first 专用 extractor。使用 `numpy`、Pillow、本地 OpenAI 兼容 GLM-OCR 与配置的多模态 LLM。
- `database/load_cpu.py`：CPU 专用预验证、幂等入库、全库去重、冲突和 Markdown 报告生成器。
- `database/extract_mem.py`：MEM OCR-first 专用 extractor；整图 OCR/LLM、保留全类目、补齐 `hardware_type`。
- `database/extract.py`：通用/旧 extractor；整图 LLM，无 OCR，递归所有分类。
- `database/clean_load.py`：通用/旧 loader；只导入 `database/extracted/*.json`。
- `database/build_db.py`：破坏性空库重建工具。
- `database/llm_config.json`：LLM/OCR endpoint、模型、超时等配置，含凭据；不要暴露。
- `database/cpu_watchlist.json`：通用 extractor 使用的 CPU 型号白名单。当前 CPU 专用路径不读取它，白名单文本内嵌于 `OCR主_指令.txt`；修改型号范围须同步两处。

### 提示词

- `database/prompts/OCR主_指令.txt`：**当前 CPU 生产路径**的完整 OCR-first 指令、JSON 契约、消歧规则和内嵌白名单。
- `database/prompts/OCR主_指令_MEM.txt`：**当前 MEM 生产路径**的完整 OCR-first 指令及 `hardware_type` 契约；不含 CPU 白名单。
- `database/prompts/base.txt`、`mem.txt`、`TF.txt`、`其他.txt`：通用/旧路径按父目录装配的提示词。
- 当前无 `database/prompts/CPU.txt`；旧文档里提到它的部分都是历史描述。

### 运行产物与验证资产

- `database/output_cpu/`：CPU 管线所有默认产物。
  - `extracted_cpu/*.json`：CPU 最终 JSON、断点续跑检查点。
  - `crop_cache/*.png`：三段裁剪图缓存。
  - `ocr_cache/*.md`：OCR HTML 转换成 Markdown 的缓存。
  - `extract_cpu_progress.log`：追加式提取历史。
  - `load_cpu_conflicts.json`：CPU importer 异价冲突清单。
  - `load_cpu_report.md`：CPU importer 人可读报告。
- `database/output_mem/`：MEM 默认产物目录。
  - `extracted_mem/*.json`：MEM 提取结果（当前可能为空）。
  - `ocr_cache/*.md`：整图 OCR 缓存。
  - 代码会写 `extract_mem_progress.log`，但不能仅凭目录/旧文档假定全量已完成。
- `database/extracted/`：旧通用路径输出，且是 `clean_load.py` 的唯一输入。
- `database/conflicts_cpu.json`：旧/历史 CPU 冲突产物；当前 `load_cpu.py` 写到 `output_cpu/load_cpu_conflicts.json`，不要混淆。
- `database/test_noocr_extract.py`：纯视觉无 OCR 的对比实验，会真实调用 LLM，输出 `output_cpu/extracted_cpu_noocr/` 并添加 `_test_noocr: true`。所有 importer 都不会强制拒绝该标记，**不得误把实验目录传给 loader**。它默认扫描 `crop_cache/*.png`，其中可能包含未接入主管线的 `_LL/_LR` 辅助切块；且 docstring 所称的 `--status` 尚未实现，不能把它当无副作用命令使用。
- `database/test_crops/`、`llm_ocr_test.md`：OCR/分块测试材料；不是生产输入。
- `database/pricebenchmark/`：CPU 两期人工基准 Excel。当前 `verify.py` 已删除，文档中提到的旧验证命令不可直接执行；若要重建验证工具，先确认基准口径和需求。
- `database/cpumem.db`：SQLite 生成数据；当前可能已有 CPU 数据和冲突删行历史。禁止未经授权重建或批量改写。

## 依赖与运行环境

- Python 3.10+（代码使用 `str | None`、`list[str]`）。
- 标准库：`sqlite3`、`json`、`glob`、`re`、`datetime`、`base64`、`urllib.request`、`concurrent.futures` 等。
- 外部依赖：
  - `openai`：所有 extractor 调用 OpenAI 兼容多模态 LLM；
  - `numpy`、`Pillow`：CPU 裁剪和线检测。
- CPU/MEM 还依赖可用的 OpenAI 兼容 GLM-OCR 服务；`ocr_base_url` 优先从 `llm_config.json` 取值，环境变量 `OCR_BASE_URL` 仅在配置没有该值时回退使用。

项目没有 `requirements.txt` 或 `pyproject.toml`。缺包时按实际需求安装；不要无确认引入框架、锁文件或大型依赖：

```bash
python -m pip install openai numpy Pillow
```

## JSON 契约

### CPU 专用结果

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
  "source_image": "source.png"
}
```

- CPU extractor 最终只保留 `category == "CPU"`。
- `product_name` 只保留型号本体，不能拼核数、线程、频率或描述文字；`F`、`K`、`KF`、无后缀是不同型号。
- `price_type` 只应为 `散片` / `原盒`。
- OCR-first CPU 契约把 `数字+****`（如 `480/****`）中的数字视为有效；纯 `****`、空格和不确定格无价。不能把通用 `base.txt` 的“任何星号无效”规则应用到 CPU 专用结果。

### MEM 专用结果

MEM 结果保留图片上所有硬件区块，并附加：

```json
{
  "hardware_type": "DDR3 | DDR4 | DDR5 | SSD | HDD | GPU | MB | PSU | MON | CPU | PERIPH | CARD | OTHER"
}
```

- 内存必须保留容量、DDR 代际、频率、时序、套条/单条、颜色/马甲等区分规格；存储保留容量与接口。
- `hardware_type` 缺失/非法时由代码补齐：显式 DDR 优先，频率 `4000..12000` 推断 DDR5、`800..2133` 推断 DDR4，其余按 category 映射或 `OTHER`。
- 此字段**当前不入 SQLite**，不能声称数据库已支持 `quotes.hardware_type`。

### 通用旧结果

通用 JSON 也使用 `sheet_date`、`products`、`source_image`，但可以出现更多类目与价格标签。`clean_load.py::PRICE_TYPE_MAP` 只识别散片/原盒/单条/套装/默认，保固、国行、全新等未映射标签会折叠为 `默认`，因此存在信息损失。

**价格真实性规则：**任何路径都不可补造价格、跨行/跨列/跨区块复制价格或从不确定文本推断。通用 `base.txt` 的“区间价取中间值”旧规则与真实性原则冲突；修改通用提示词时不得据此写入图片未明确给出的中间价。

## CPU 专用管线：`extract_cpu.py`

### 执行模型

```text
显式传入 CPU 图片或目录
  -> output_cpu/extracted_cpu/<basename>.json 是否存在：存在即跳过
  -> crop_cpu_image()：三段式自适应裁剪 -> output_cpu/crop_cache/<basename>.png
  -> ocr_markdown()：本地 GLM-OCR -> HTML 表格转 Markdown -> output_cpu/ocr_cache/<basename>.md
  -> OCR主_指令.txt + OCR Markdown
  -> 多模态 LLM：裁剪图 + 联合提示词
  -> 保留 CPU 类目、添加 source_image
  -> output_cpu/extracted_cpu/<basename>.json
  -> （人工触发）load_cpu.py -> cpumem.db
```

- 仅 `--status` 会默认扫描 `价格图片/CPU/`；其余运行必须显式传图片或目录。目录扫描可递归。
- 默认 2 并发，`--workers N` 最大钳制到 6。
- `--out-dir <目录>` 可将**结果、裁剪、OCR 缓存和进度日志**一并切换到自定义根目录；CPU importer 的 `--out-dir` 需要传同一个根目录中的 `extracted_cpu/`。
- 三段裁剪通过亮区/长竖线检测横幅和 CPU 区右边界，失败时回退固定比例；该策略针对当前 CPU 报价模板，不保证新模板可用。
- OCR 使用 `temperature=0`，CPU 的 `max_tokens` 当前硬编码为 16384；MEM 才读取配置中的 `ocr_max_tokens`。输出通常为 HTML 表格；`html_table_to_markdown()` 仅面向当前预期的小写、带引号属性的 HTML，展开 `rowspan`/`colspan` 为 Markdown 管道表格，不是通用 HTML 解析器。OCR 格式异常时可能原样传给后续 LLM，修改前须以真实 OCR 样本核查。
- OCR 失败、返回空、LLM 失败或 JSON 解析失败都会使该图失败且不写结果；没有生产降级到纯视觉/纯 OCR 的分支。重跑会重试未写结果的图。
- `call_llm()` 仅对 endpoint 拒绝 `temperature` 重试一次；无通用网络/限流/路由重试。
- 缓存/结果均以 basename 为键：改裁剪逻辑清对应裁剪缓存；改 OCR 行为清对应 OCR 缓存；改主提示词删对应 JSON。替换同名原图和跨目录同名图会产生陈旧缓存或碰撞。

### 未接入的分块代码

`split_table_blocks()`、`detect_vlines()` 和 `_find_split()` 已定义，可生成 `_LL.png` / `_LR.png` 子表裁切；但当前 `extract_one()` **没有调用 `split_table_blocks()`**。实际生产 OCR/LLM 接收的是单张三段裁剪图，不是 LL/LR 块。不要因文档声称 S2b 已运行而清理/依赖分块缓存，除非先实现并验证调用接入。

## MEM 专用管线：`extract_mem.py`

```text
显式传入 MEM 图片或目录
  -> output_mem/extracted_mem/<basename>.json 是否存在：存在即跳过
  -> 原整图 -> ocr_markdown() -> output_mem/ocr_cache/<basename>.md
  -> OCR主_指令_MEM.txt + OCR Markdown
  -> 多模态 LLM：原整图 + 联合提示词
  -> 保留全部 category，补齐 hardware_type
  -> output_mem/extracted_mem/<basename>.json
  -> （当前无正式 MEM loader）
```

- 与 CPU 一样，除 `--status` 外必须显式传目标；目录可递归扫描，支持 PNG/JPG/JPEG。
- 不裁剪：MEM 图通常是近方形、多区块报价单，必须保留内存、SSD、主板、显卡、显示器等全部区域。
- 默认 2 并发、上限 6，支持 `--workers` 与 `--out-dir`。
- OCR 也硬依赖；MEM 从配置读取 `ocr_max_tokens`（默认 16384），以避免输出截断/重复退化。OCR-first 结果目前尚无成熟人工基准，不能将批量输出视为已验证生产数据。
- 输出/缓存同样 basename 键控，提示词变更需删对应 JSON，OCR 行为变更需删对应 OCR 缓存。
- 当前结果目录可能为空而 OCR 缓存已存在；缓存存在不代表 LLM 成功或数据已入库。

## CPU 数据导入：`load_cpu.py`

`load_cpu.py` 是 CPU 提取后的正式人工触发入库步骤，默认目标为 `database/output_cpu/extracted_cpu/`。

1. 读取一份 JSON、一个目录或默认目录；`--out-dir` 指向 CPU 自定义输出根目录。
2. 对每条记录预验证：顶层 `source_image` 非空、日期可解析、型号非空、价格可转数字且在 `1..200000`、价格不含 `*`/`X`、`price_type` 规范化后为散片/原盒、`category` 非空。
3. 至少有一条有效记录才删除相同 `source_image` 的旧 quotes，避免空/全坏 JSON 清空旧来源；然后插入 products/dates/quotes。
4. products 以 `clean_load.norm_product_key()` 的 `vendor-型号` 键 insert-once；CPU importer 无论原 category 写什么都新建为 `CPU`。展示名会尝试去除 CPU 规格后缀，但 product key 在该清理前生成，措辞变化仍可能分裂产品键。
5. 对**整个共享数据库**执行 `(date_key, product_key, price_type)` 去重：同价保留最早 id，异价保留最早 id、删除后者并写 `output_cpu/load_cpu_conflicts.json`。保留行的 source 在冲突记录中目前为 `null`，溯源不完整。
6. 写 `output_cpu/load_cpu_report.md`。报告中的“入库价格记录”是预验证通过/尝试写入数，不能等同于去重后保留的 quote 数。

风险与约束：

- 去重是跨来源、全库、破坏性的；它可能删除同日同型号同类型的有效不同报价。当前冲突记录的保留行 `source` 为 `null`，不能完整追溯赢家来源。不得未经授权反复全量导入或把冲突当作已解决。
- `load_cpu.py --status` 的“价格记录”查询的是所有具有 `source_image` 的 quotes，不严格限定 CPU；混入其他 importer 数据后会偏大。
- CPU 当前 SQLite 状态、导入报告和冲突文件是业务数据，不要用测试命令重跑 import 覆盖它们。

## 通用旧路径：`extract.py` / `clean_load.py`

### 通用 extractor

- 递归扫描 `价格图片/` 所有分类，固定 10 并发，整图直送 LLM，无 OCR。
- 按父目录合成 `base.txt + <父目录>.txt`；不存在对应 prompt 时只用 base。
- 当 `cpu_watchlist.json` 的 `enabled` 为真时，它会对**所有类别**拼接 CPU 白名单，不只 CPU 目录；这是当前缺陷，改动时要检查非 CPU 提示词。
- 输出平铺到 `database/extracted/<basename>.json`，同名即跳过；该路径与专用 CPU/MEM 结果重叠但不能混同。

### 通用 loader

- 仅处理 `database/extracted/*.json`，不处理 output_cpu/output_mem。
- 按 `source_image` 先删除旧 quotes，再对每项逐条验证并插入；与 CPU importer 不同，它在验证各条产品**之前**删除来源旧记录，故一个日期可读但 products 全坏的 JSON 可清空同来源旧数据。
- 复用 vendor/category/price type 映射；空/未知分类才调用 `guess_category()`。兜底顺序先 CPU、后通用容量/SSD、最后 DDR/内存，因此 `DDR5 16G` 一类缺分类内存可能被误判为 SSD。
- `norm_date()` 支持带年份的 `YYYY-M-D`（`-` `/` `.`）和 `M月D日`，年份强制 2026；**不支持**旧文档宣称的短点号 `9.16`。
- `--force-conflicts` 没有实际功能：`dedupe_quotes()` 始终删除冲突后行，flag 只影响消息。
- 虽有 `_create_tables()` / `load_db()` 自动建表分支，`main()` 在 DB 不存在时会提前返回，CLI 不能自动建库。首次 generic 导入须先获得授权并执行 `build_db.py`，或先修复行为。

## SQLite schema（当前代码定义）

`build_db.py` 与 `clean_load.py::_create_tables()` 定义：

| 表 | 字段 |
| --- | --- |
| `products` | `product_key` PK、`display_name`、`category`、`vendor` |
| `dates` | `date_key` PK、`year`、`month`、`day`、`weekday` |
| `quotes` | `id`、`product_key`、`date_key`、`price`、`price_type`、`source_image` |

索引：`idx_q_prod_date(product_key, date_key)`、`idx_p_cat_vendor(category, vendor)`。

- schema 没有 `hardware_type`，没有 source/vendor/category/price-type 独立维表，也没有 `(date_key, product_key, price_type)` 数据库唯一约束；去重完全依赖 Python 脚本。
- `products` 只在产品键首次出现时插入；后续提取不会修正既有展示名、分类或品牌。`norm_product_key()` 只统一大小写、空白/下划线和连字符，未全面规范型号别名、拼写、后缀、容量格式或品牌语义，相近型号写法仍可能分裂为多个产品。
- 外键约束只在部分既有 DB 连接启用 `PRAGMA foreign_keys=ON`，新建路径不一致。
- `build_db.py` 会删掉整个现有 `cpumem.db`，绝不能作为普通“初始化检查”执行。

## 测试、验证与质量边界

- 没有自动化测试、CI 或现存通用验证脚本。`database/verify.py` 已删除；旧文档中以它验证 Excel 的命令不可直接运行。
- `database/pricebenchmark/` 中保留两期 CPU Excel 基准。文档记录历史 CPU 试验结果：0a04 为 132/132、0b5a 为 141/148（95.3%）；这不是对当前全量结果的自动保证。
- CPU `output_cpu/extract_cpu_progress.log` 当前可记录 316/316 提取完成，但进度日志是追加历史；结果保留数、导入报告覆盖范围、DB 保留 quote 数和 OCR/LLM 正确性是不同概念，不能用其中任一个代替质量验证。
- `load_cpu.py` 的预验证只验证字段格式/范围，不验证白名单、真实行列对应、供应商/类别语义或模型准确性；冲突文件是待人工审阅队列，不是自动修复。
- MEM OCR-first 路径尚无已建立的人工 benchmark；现有 OCR 测试材料可见重复、错位/错误型号风险。MEM 批量数据未建立质量门槛前，不得宣称已可生产入库。
- Python 修改后至少运行：

```bash
python -m py_compile database/build_db.py database/clean_load.py database/extract.py database/extract_cpu.py database/extract_mem.py database/load_cpu.py database/test_noocr_extract.py
```

不要把该检查生成的 `database/__pycache__/` 作为功能变更提交。

## 常用命令

从仓库根目录执行：

```bash
# 无副作用：查看 CPU/MEM/通用提取进度与 CPU 库状态
python database/extract_cpu.py --status
python database/extract_mem.py --status
python database/extract.py --status
python database/load_cpu.py --status

# 无副作用：Python 语法检查
python -m py_compile database/build_db.py database/clean_load.py database/extract.py database/extract_cpu.py database/extract_mem.py database/load_cpu.py database/test_noocr_extract.py

# CPU：真实调用 OCR + LLM，写 output_cpu/（须先授权）
python database/extract_cpu.py 价格图片/CPU/example.png
python database/extract_cpu.py 价格图片/CPU --workers 4
python database/extract_cpu.py 价格图片/CPU --out-dir ./my_output

# CPU：真实修改 SQLite、全库去重并写报告/冲突（须先授权）
python database/load_cpu.py
python database/load_cpu.py database/output_cpu/extracted_cpu/example.json
python database/load_cpu.py --out-dir ./my_output

# MEM：真实调用 OCR + LLM，写 output_mem/；当前没有 load_mem.py（须先授权）
python database/extract_mem.py 价格图片/mem/example.png
python database/extract_mem.py 价格图片/mem --workers 4
python database/extract_mem.py 价格图片/mem --out-dir ./my_output

# 通用旧路径：真实调用 LLM / 修改通用 DB 数据（须先授权）
python database/extract.py --file TF/example.png
python database/extract.py
python database/clean_load.py

# 破坏性：删除后重建 cpumem.db（须明确授权）
python database/build_db.py

# 实验：真实调用 LLM，非生产输出；不要传给 loader
python database/test_noocr_extract.py database/output_cpu/crop_cache --workers 2 --limit 10
```

不要执行旧文档里的 `extract.py --real`、`extract_mem.py --file ...`、`load_mem.py`、`verify.py` 或 `CPU.txt` 工作流：它们与当前代码不匹配或文件不存在。

## 维护要求

- 路径从 `__file__` 推导；沿用脚本式模块常量、简短中文 docstring、轻量 `sys.argv`、`main()` + `if __name__ == "__main__":`。
- 修改 CPU 提示词应编辑 `OCR主_指令.txt`；修改 MEM 提示词应编辑 `OCR主_指令_MEM.txt`。不要单改 legacy `base.txt`/`mem.txt` 后误认为 OCR-first 专用路径会变化。
- 修改 CPU 裁剪、OCR HTML/Markdown 转换、OCR token/超时、白名单或提示词后，按影响层只清相应缓存/结果，并先以代表图做真实重提验证；清缓存与重提会有副作用，需授权。
- 变更 loader、产品键、日期/分类/价格类型映射、去重策略或 schema 前，必须考虑已入库历史、`source_image` 幂等、CPU/通用路径共享 DB、冲突报告和数据迁移。
- 对 MEM 入库的实现必须同时设计 schema migration、`hardware_type` 持久化、全类目合法 price_type、来源冲突策略与测试/人工基准；不得仅依照当前超前的 MEM 文档添加调用入口。
- 若修复文档与代码不一致，应优先修复/验证行为，再同步更新相关 docs 和本文件；不要只改文档掩盖实现状态。
