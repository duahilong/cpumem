# AGENTS.md

## 项目定位

这是一个**离线、脚本优先的硬件报价单 ETL 项目**。人工将报价截图按类目放入 `价格图片/`，Python 管线调用多模态 LLM（CPU 还结合本地 OCR）生成每图一个原始 JSON；清洗脚本再将部分结果规范化并写入 SQLite，用于 CPU、内存、存储及其他硬件的历史价格查询。

项目没有 Web 服务、前端、包管理清单、CI、`pytest` 或 `unittest` 测试套件。代码、提示词、CLI 输出和领域枚举以中文为主；文本文件必须保持 UTF-8 编码。

**以当前代码为准。** `docs/CPU提取管线.md` 是当前 CPU 专用管线的主要说明；`docs/OCR辅助提取实验.md` 记录其演化实验；`docs/数据库设计文档.md` 同时包含当前架构、历史实验记录和部分过时描述。修改前应阅读相关代码与文档，不能只按旧文档假设行为。

## 当前整体架构

```text
价格图片/{CPU, mem, TF, 其他}/                 # 人工分类原图（敏感、本地输入）
  ├─ database/extract_cpu.py                   # CPU 专用：裁剪 → OCR → LLM
  │    ├─ database/crop_cache/*.png             # 裁剪缓存
  │    ├─ database/ocr_cache/*.md               # OCR Markdown 缓存
  │    └─ database/extracted_cpu/*.json         # CPU 结果
  ├─ database/extract_mem.py                    # mem 图专用：整图 LLM + hardware_type 补齐
  │    └─ database/extracted_mem/*.json         # MEM/混合图结果
  └─ database/extract.py                        # 旧/通用：递归全部分类图
       └─ database/extracted/*.json             # 通用结果

目前的入库路径：database/extracted/*.json
  -> database/clean_load.py
  -> database/cpumem.db（products / dates / quotes）

CPU 质量验证（独立、只读）：
database/extracted_cpu/<结果>.json + database/pricebenchmark/0a04-pricebenchmark.xlsx
  -> database/verify.py
  -> 准确率及类型错 / 数值错 / 漏提 / 多提 / 清单外报告
```

### 最重要的集成边界

`clean_load.py` **只读取** `database/extracted/*.json`；它不会读取 `extracted_cpu/` 或 `extracted_mem/`。因此当前质量更高的 CPU 专用结果、以及 MEM 结果中的 `hardware_type`，尚未通过正式 loader 入库。扩展入库输入范围、迁移结果或批量合并前，必须先说明会影响数据库去重和 `source_image` 幂等行为，并取得用户明确许可。

通用 `extract.py` 仍会递归扫描 `价格图片/` 的**全部**子目录（包括 CPU 和 mem），会与专用管线产生重叠结果；不要把它误认为只处理 TF/其他。生产性 CPU/MEM 工作优先使用各自专用脚本，除非任务明确要求通用管线。

## 目录与职责

- `价格图片/CPU/`、`价格图片/mem/`、`价格图片/TF/`、`价格图片/其他/`：原始报价截图，已被 `.gitignore` 忽略。不得删除、移动、批量改名、提交或向外部服务传输，除非用户明确授权。
- `database/extract_cpu.py`：当前 CPU 专用生产候选管线；使用 `numpy`、Pillow、本地 GLM-OCR、外部/网关多模态 LLM。
- `database/extract_mem.py`：当前 MEM 专用管线；整图调用 LLM，保留图上所有硬件类目，并给每条结果补齐 `hardware_type`。
- `database/extract.py`：通用/旧式多类目提取器；使用 `base.txt + <父目录>.txt`，递归扫描全部图片。
- `database/prompts/`：运行时读取、可直接调整的提示词资产。
  - `OCR主_指令.txt`：当前 CPU 管线实际使用的完整契约、白名单和两步式提取指令（OCR 为主、图为辅）。
  - `base.txt`：通用路径的基础契约，extract.py / extract_mem.py 引用（CPU 管线不再使用）。
  - `mem.txt`：MEM 专用补充规则，要求 `hardware_type`。
  - `TF.txt`、`其他.txt`：通用路径的类别规则。
- `database/cpu_watchlist.json`：CPU 型号白名单，供通用管线 `extract.py` 使用；CPU 管线的白名单已内嵌在 `OCR主_指令.txt` 中。更新型号策略时须同步两处和基准。
- `database/llm_config.json`：OpenAI Chat Completions 兼容的 LLM 配置及 OCR 服务/超时配置；含敏感凭据，严禁读取后在回复、日志、文档或提交中复述其值。
- `database/crop_cache/`：CPU 三段裁剪缓存。
- `database/ocr_cache/`：CPU OCR 结果缓存（HTML 表格转成 Markdown）。
- `database/extracted/`：通用 extractor 的原始 JSON；也是当前 loader 的唯一输入目录。
- `database/extracted_cpu/`：CPU 专用结果；当前不自动入库。
- `database/extracted_mem/`：MEM 专用结果；当前不自动入库。
- `database/pricebenchmark/`：CPU 人工验证基准，含 `0a04-pricebenchmark.xlsx`（verify.py 默认基准，与旧 jg.xlsx 数据一致）和 `0b5a-pricebenchmark.xlsx`。
- `database/ultra_crops_llm/`（已删除）：OCR/提示词/模型实验产物已清理；复现实验时重建临时目录即可。
- `database/*_progress.log`：提取运行日志；批量脚本会追加写入。
- `database/clean_load.py`：仅对 `extracted/*.json` 清洗、幂等写库、全库去重。
- `database/build_db.py`：无确认、破坏性地重建空库。
- `database/cpumem.db`：SQLite 生成数据；当前可能为空。重建、批量入库、修改或删除前必须获得明确许可。
- `database/verify.py`：CPU 单份提取结果与人工 Excel 的 QA 工具；不是通用数据校验器。
- `docs/CPU提取管线.md`：当前 CPU 管线、缓存、基准结果和常用命令。
- `docs/OCR辅助提取实验.md`：OCR 方案的历史实验、结论和未合入的可选路径。
- `docs/数据库设计文档.md`：全局 ETL/SQLite/MEM 设计与历史记录；注意其中仍包含旧模型、旧 CPU 提示词、`extract.py --real` 等过时描述。

## 依赖与运行环境

- Python 3.10+（代码使用 `str | None`、`list[str]` 等现代类型标注）。
- 标准库：`sqlite3`、`json`、`glob`、`re`、`datetime`、`base64`、`urllib.request`、`concurrent.futures` 等。
- 外部 Python 依赖：
  - `openai`：所有 extractor 调用 OpenAI 兼容的多模态 LLM；
  - `openpyxl`：`verify.py` 读取 Excel 基准；
  - `numpy`、`Pillow`：`extract_cpu.py` 的图像锚点检测和裁剪。
- CPU 管线还依赖可用的本地 OpenAI 兼容 OCR 服务（GLM-OCR/llama.cpp）；默认地址由 `llm_config.json` 的 `ocr_base_url` 决定，环境变量 `OCR_BASE_URL` **仅在配置未提供该项时**才作为回退。

项目暂无 `requirements.txt` / `pyproject.toml`。缺包时按实际需要安装，不要自行引入大型框架、lockfile 或新依赖，除非任务需要且已确认：

```bash
python -m pip install openai openpyxl numpy Pillow
```

## 提取 JSON 契约

通用与 CPU 输出的主要结构为：

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

- `sheet_date`：报价日期；下游会强制年份为 2026。
- `source_image`：由各 extractor 添加，且是 loader 的来源幂等删除键；不得随意移除。
- `product_name`：CPU 专用输出应仅保留型号本体，不能拼入核数、线程、频率或描述文字；内存/存储等产品则必须保留容量、DDR/频率、接口、显存、容量和其他区分规格。
- `price`：必须是明确的纯数值；不确定、空白、跨行/跨列推断出的价应宁可漏掉也不能补造。`base.txt` 中“区间价取中间值”的旧规则与此真实性原则冲突；处理或修订通用提示词时不得据此生成图片未明确给出的中间价。
- `price_type`：CPU 常用 `散片`/`原盒`；内存常用 `单条`/`套装`；实际通用输出还可能含保固、国行、全新等标签，但当前 loader 会将未在映射表内的类型折叠为 `默认`。

MEM 专用输出会额外带：

```json
{"hardware_type": "DDR3 | DDR4 | DDR5 | SSD | HDD | GPU | MB | PSU | MON | CPU | PERIPH | CARD | OTHER"}
```

该字段当前只存在于 JSON，SQLite schema 与 `clean_load.py` 均不会持久化它。

## 三条提取路径

### CPU：`database/extract_cpu.py`（当前专用路径）

CPU 管线只针对 `价格图片/CPU/` 设计，必须传入图片文件或目录；不带参数只打印用法。`--status` 是唯一会使用默认 CPU 目录的无副作用模式。

```text
原始 CPU 图片
  -> extracted_cpu/<basename>.json 是否存在：存在即跳过
  -> crop_cpu_image()：三段式、锚点自适应裁剪 -> crop_cache/<basename>.png
  -> ocr_markdown()：本地 GLM-OCR -> HTML 转 Markdown -> ocr_cache/<basename>.md
  -> build_joint_prompt()：OCR主_指令.txt + OCR Markdown
  -> call_llm()：裁剪图 + OCR-first 联合提示词
  -> 仅保留 category == "CPU"，添加 source_image
  -> extracted_cpu/<basename>.json
```

关键事实：

- `crop_cpu_image()` 检测横幅底部和 CPU 表右边界，再拼接三段裁剪图，以排除右侧硬盘/内存表格对 CPU 提取的干扰；检测失败会回退历史比例。
- `ocr_markdown()` 用 `temperature=0` 请求 OCR，调用失败或返回空会抛异常；**实际代码没有纯视觉或纯 OCR 降级分支**（提示词以 OCR 为主，OCR 失败即报错，该图计失败，重跑自动重试）。
- OCR HTML 的 `rowspan`/`colspan` 会在 `html_table_to_markdown()` 展开成 Markdown 管道表格。
- 当前 CPU 逻辑实际使用 `OCR主_指令.txt`（含白名单），`build_cpu_prompt()`（旧 base.txt + CPU.txt 组装）已删除；不要重建旧提示词路径。
- 默认 2 并发，`--workers N` 可调整，上限 6；CPU OCR 和 LLM 服务共同承压。
- `timeout_seconds` 默认 300 秒、`ocr_timeout_seconds` 默认 600 秒（均可在配置中覆盖）；超时是失败，结果文件不会写出，重跑会重试。
- 当前 OCR-first 提示词规定：含数字的 `数字+****`（例如 `480/****`）可以提取数字部分；纯 `****` 才是无价。这与 `base.txt` 的通用星号规则不同，不能混用两套契约。
- 裁剪、OCR、最终 JSON 都按**文件 basename**缓存。改裁剪逻辑后需要清对应 `crop_cache/`；改 OCR 行为后需要清对应 `ocr_cache/`；改 CPU 主提示词后需要删对应 `extracted_cpu/` JSON 才会重新调用。替换同名源图或不同目录同名图也会误复用缓存。

### MEM：`database/extract_mem.py`

```text
价格图片/mem/ 顶层图片
  -> base.txt + mem.txt
  -> 整图发送给 LLM（无裁剪、无 CPU 白名单）
  -> 保留图中全部类目，不只保留内存
  -> infer_hardware_type() 补齐/纠正 hardware_type
  -> extracted_mem/<basename>.json
```

- 默认 10 并发，支持 `--file`、`--status`；扫描是非递归的顶层 `png/jpg/jpeg` glob。
- `hardware_type` 先接受 LLM 合法值；缺失/非法时，内存优先按显式 `DDR3/4/5` 识别，之后按频率 `4000..12000 -> DDR5`、`800..2133 -> DDR4` 兜底，其他类目根据 `category` 映射。
- MEM 图可能同时包含内存、SSD、主板、显卡、显示器等，不能在 extractor 中过滤为仅“内存”。
- 结果目前不进入 `clean_load.py`。

### 通用：`database/extract.py`

- 递归扫描 `价格图片/` 下所有 `png/jpg/jpeg`，默认 10 并发，输出平铺到 `extracted/`。
- 按图片父目录拼接 `base.txt + <父目录>.txt`；缺少分类提示词时只使用 `base.txt`。
- 每个结果名是 `extracted/<basename>.json`，已存在即跳过；`--status` 也按 basename 比较。
- 代码当前只要 `cpu_watchlist.json` 启用就会为**所有分类**追加 CPU 白名单，而不是仅 CPU 图片；这是现状/缺陷，改动时需要专门验证非 CPU 提示词与输出。
- 通用路径用 `base.txt`（extract.py / extract_mem.py 引用）；CPU 管线的 `CPU.txt` 已删除（其规则已并入 `OCR主_指令.txt`）。

所有 extractor 都处理模型偶发的 Markdown 代码围栏，并在 endpoint 拒绝 `temperature` 参数时重试一次。它们不会为一般网络、限流、路由异常作额外重试；失败项应保留失败状态、检查服务后重跑，绝不可伪造成功。

## 清洗、SQLite 与入库

### 表结构

`database/build_db.py` 与 `clean_load.py::_create_tables()` 定义：

| 表 | 用途 | 关键字段 |
| --- | --- | --- |
| `products` | 产品维表 | `product_key` PK、`display_name`、`category`、`vendor` |
| `dates` | 日期维表 | `date_key` PK、`year`、`month`、`day`、`weekday` |
| `quotes` | 长表价格事实 | `id`、`product_key`、`date_key`、`price`、`price_type`、`source_image` |

索引：`idx_q_prod_date(product_key, date_key)`、`idx_p_cat_vendor(category, vendor)`。

`quotes` 的一条记录代表一种价格类型；同一 CPU 的散片和原盒应是两行。`source_image` 是可追溯性要求，不要删除。

### 规范化与幂等行为

- `norm_date()` 接受带年份的 `YYYY-M-D`（分隔符可为 `-` `/` `.`）和中文 `M月D日`，将年份强制为 2026；当前**不支持**无年份点号日期如 `9.16`，尽管旧文档写过支持。
- `VENDOR_MAP`、`CATEGORY_MAP`、`PRICE_TYPE_MAP` 是集中式映射；新增已知别名优先最小改动补到映射表，而不是散落特判。
- `norm_product_key()` 生成 `小写规范化 vendor + '-' + 小写连字符化 product_name`。它在 CPU 展示名清洗前运行，因此 CPU 提取措辞中的规格差异仍会令 `product_key` 分裂；调整键策略前必须考虑历史数据迁移。
- 价格必须能转为数值，且在 `1..200000`；`<=0`、`*`/`X`、超范围或无效价格不入库。注意当前检查只看 JSON 内的 `price` 值；若上游已把有标记的原始值转成纯数字，loader 无法恢复该标记。
- 分类为空/未知才会调用 `guess_category()`。其顺序先匹配 CPU、后匹配通用容量/SSD、最后匹配 DDR/内存，因此类似带 `16G` 和 `DDR5` 的内存若分类丢失，存在被误归为 SSD 的风险。
- 对每一个输入 JSON，`process_file()` 先删除 `quotes.source_image == source_image` 的旧记录，再插入新记录，形成来源级幂等。
- 这里的来源键也只有 basename；不同目录同名图一旦合并入同一输入目录，会互相删除/覆盖记录。

### 去重与冲突

全库 `dedupe_quotes()` 按 `(date_key, product_key, price_type)` 处理：

- 同价：保留最早记录，删除后续；
- 异价：保留最早记录，删除后续，并把冲突写入 `database/conflicts.json`。

`--force-conflicts` **当前没有实际功能**：它只改变提示文字，`dedupe_quotes()` 依然会删除异价后续行，也不会回填冲突。不要在说明、测试或实现中把它说成已支持“强制保留”。

### 建库危险点

- `build_db.py` 会直接删除已有 `database/cpumem.db` 后新建空表，**没有交互确认**；调试、测试、修复中绝不可随手执行。
- `load_db()` 有建库辅助路径，但 `clean_load.py::main()` 在 DB 不存在时会提前返回，故正常 CLI 下自动建库不可达。首次实际运行需要先获授权后执行 `build_db.py`，或先修复该行为并更新文档/测试。
- 外键只在已有 DB 的 `load_db()` 分支启用 `PRAGMA foreign_keys = ON`，新建连接的实际强制性并不一致。

## CPU 验证与实验质量

`verify.py` 是针对 CPU 人工基准的独立只读工具：

```bash
python database/verify.py database/extracted_cpu/<结果>.json
```

- 默认基准是 `database/pricebenchmark/0a04-pricebenchmark.xlsx`；读取首个 worksheet。
- 它使用验证专用 `norm()`：保留 CPU `F`/`K`/`KF` 区别，处理少数人工笔误与规格后缀。不要把这套规则直接拿去替代全品类数据库键规范化。
- 报告 `类型错`、`数值错`、`漏提`、`多提`、`清单外`。基准仅对应某个报价期，只能对同一期图片验证；跨期运行会产生大量假错误。
- `0b5a-pricebenchmark.xlsx` 不是 CLI 参数，需要在 Python 中临时重设 `verify.XLSX_PATH`，或在改造工具后再提供正式参数；不可假设当前 CLI 已支持 `--benchmark`。
- 当前提取进度应使用各脚本的 `--status` 实时查看；现有 `*_progress.log` 是多次单图/实验运行的追加历史，不能据其中某条 `总进度` 直接推断全量生产完成度。
- CPU 提示词、OCR/裁剪、白名单、模型调用或验证归一化发生改动时，至少验证 0a04 基准，并用另一张不同期/版式图片做泛化核查。文档记录当前 OCR-first 管线在 0a04 达到 132/132、0b5a 达到 141/148；这是实验结果，不代表全量生产数据已验证。

## 常用命令

从仓库根目录执行：

```bash
# 仅 Python 语法检查；不会调用 OCR/LLM，不会修改业务数据
python -m py_compile database/build_db.py database/clean_load.py database/extract.py database/extract_cpu.py database/extract_mem.py database/verify.py

# 只查看进度；不会发送图片或写数据库
python database/extract_cpu.py --status
python database/extract_mem.py --status
python database/extract.py --status

# CPU：单图或指定目录。真实调用本地 OCR + LLM，会产生/复用缓存并写结果
python database/extract_cpu.py 价格图片/CPU/example.png
python database/extract_cpu.py 价格图片/CPU --workers 4

# MEM：单图或整个 mem 目录。真实调用 LLM、写 extracted_mem/
python database/extract_mem.py --file example.png
python database/extract_mem.py

# 通用：单图或全量递归。真实调用 LLM、写 extracted/
python database/extract.py --file TF/example.png
python database/extract.py

# CPU 提取结果与默认人工基准比对（只读）
python database/verify.py database/extracted_cpu/<result>.json

# 清洗并写入数据库；只读取 extracted/*.json，会删旧 source_image quote、去重，可能生成 conflicts.json
python database/clean_load.py

# 破坏性：无提示删除后重建 cpumem.db；必须先明确确认
python database/build_db.py
```

不要使用旧文档中的 `python extract.py --real`：当前 `extract.py` 默认就是真实 LLM 调用，且没有 `--real` 开关。

## 安全、数据与 Git 约束

1. **先检查工作区。** 开始任何任务前执行 `git status --short`。工作区可能含用户未提交的配置、缓存、结果、日志和数据库改动；只改任务相关文件，不覆盖、暂存、回退或删除无关内容。
2. **禁止未授权的副作用。** 批量 CPU/MEM/通用提取会消耗配额、耗时，并将图片发送给配置服务；批量入库会修改 SQLite/冲突文件；建库、删除/移动/批量重命名文件同样有影响。执行前说明影响并取得用户明确许可。
3. **保护原始图片与凭据。** `价格图片/` 可能敏感；`llm_config.json` 包含敏感凭据。不得打印、复制、提交、上传或在回复中泄露它们。
4. **Git 忽略规则不足。** 当前 `.gitignore` 只忽略原始图片和少数系统文件；`cpumem.db`、`llm_config.json`、缓存、提取 JSON、日志、Excel 基准、实验产物都可能被追踪或意外暂存。新增生成物或处理密钥时应评估补充忽略/样例配置策略，但不要擅自删除已有本地文件。
5. **缓存/结果不是无害临时文件。** 当前仓库可能保留 `crop_cache/`、`ocr_cache/`、`extracted_*`、`ultra_crops_llm/` 和 benchmark；不要在无明确任务时清空。若任务确实要求重提，按逻辑层只清理相关单图/单目录缓存，并先确认影响。
6. **价格真实性优先。** 不得因 LLM 或 OCR 不确定而补造价格；不得跨行、跨列、跨子表复制价格。宁可漏提，也不能把错误价格放进下游数据库。

## 代码风格与改动要求

- 延续脚本式结构：模块级路径常量、简短中文 docstring、轻量 `sys.argv` 解析、`main()` 和 `if __name__ == "__main__":` 入口。
- 路径必须从 `__file__` 推导，不依赖启动时工作目录。
- 标准库优先；不要为了简单任务引入框架。
- 提取策略优先外置于 `database/prompts/` 或 JSON 配置；代码负责安全、装配、缓存、调用和格式补救。
- 修改 CPU 主提示词时改 `OCR主_指令.txt`（含内联白名单）；`CPU.txt` 已删除（规则已并入 `OCR主_指令.txt`）。
- 修改通用/MEM 提示词、类别/品牌/价格类型枚举时，联动检查 prompts、`clean_load.py` 映射、SQLite 历史兼容性、样例 JSON 和文档。
- Python 改动后至少运行 `py_compile`；不要把由检查产生的 `__pycache__/` 当作功能变更提交。
- 变更 CPU 裁剪、OCR HTML/Markdown 转换、提示词或白名单，必须结合人工基准运行 `verify.py`，并清除受影响的 CPU 缓存层后才做真实重提。
- 若修复文档—代码不一致，优先修行为并补验证，然后同步更新相关文档和本文件；不要仅改文档掩盖现状。
