# AGENTS.md

## 项目概览

这是一个**离线、脚本优先的硬件报价单 ETL 项目**：将手工归类的报价截图交给支持视觉的 LLM 提取为原始 JSON，再清洗并写入 SQLite，最终用于查询 CPU、内存和存储等产品的历史价格。

项目没有 Web 服务、前端、包管理清单、CI 或自动化测试套件；主要操作入口均为 `database/` 下的 Python 脚本。项目领域文字、CLI 输出、数据枚举和提示词均以中文为主，所有文本文件应保持 UTF-8 编码。

### 数据流

```text
价格图片/{CPU, mem, TF, 其他}/       # 手工分类的原始截图（Git 忽略）
  -> database/extract.py             # 多模态 LLM：每张图生成原始 JSON
  -> database/extracted/*.json       # 原始提取结果（断点续跑的检查点）
  -> database/clean_load.py          # 规范化、过滤、去重、入库
  -> database/cpumem.db              # SQLite：products / dates / quotes
```

CPU 提取质量的独立验证路径：

```text
database/extracted/<候选结果>.json + database/jg.xlsx
  -> database/verify.py
  -> 终端准确率及类型错 / 数值错 / 漏提 / 多提报告
```

详细的领域设计、模型实验和提示词迭代记录在 [`docs/数据库设计文档.md`](docs/数据库设计文档.md)。修改代码前先阅读相应脚本；文档中的历史描述若与当前代码不一致，以代码实际行为为准。

## 目录与职责

- `价格图片/`：原始报价截图，当前按 `CPU/`、`mem/`、`TF/`、`其他/` 分类。它被 `.gitignore` 忽略，属于本地原始数据，**不得删除、移动、批量改名或提交**。
- `database/extract.py`：步骤 1。递归扫描图片、组合提示词、调用 OpenAI Chat Completions 兼容的视觉接口，并发生成每图一份 JSON。
- `database/prompts/`：可运行时修改的提取策略：`base.txt` 为通用 JSON 契约，`CPU.txt`、`mem.txt`、`TF.txt`、`其他.txt` 为目录名对应的规则。提示词是提取质量的核心资产。
- `database/cpu_watchlist.json`：CPU 型号白名单（开发基准源为 `database/jg.txt`）。
- `database/llm_config.json`：本地视觉 LLM 网关配置；含敏感凭据，严禁在日志、文档、提交或回复中复述其值。
- `database/clean_load.py`：步骤 2+3。标准化原始 JSON 并幂等写入数据库，记录同日同型号异价冲突。
- `database/build_db.py`：显式、**破坏性**建库/重置工具。
- `database/cpumem.db`：SQLite 数据库。当前应将其视为生成物/本地数据；修改或重建前先确认用户意图。
- `database/verify.py`：针对 CPU 人工基准的提取结果 QA 工具，不是通用数据库测试器。
- `database/jg.xlsx`、`database/jg.txt`：CPU 开发集人工基准及其型号清单。
- `database/ultra_crops/`：图片裁切、比例试验和提取实验产物；除非任务明确涉及视觉预处理实验，不要将其当成生产输入。
- `docs/数据库设计文档.md`：架构、表设计、运行方式、已知问题与实验记录。

## 技术栈与依赖

- Python 3（代码使用 `str | None`，建议 Python 3.10+）。
- 标准库：`sqlite3`、`json`、`glob`、`re`、`datetime`、`concurrent.futures` 等。
- 外部依赖：
  - `openai`：`extract.py` 使用 `OpenAI` SDK 调用 OpenAI 兼容的多模态接口。
  - `openpyxl`：`verify.py` 读取人工基准 Excel。
- 存储：单文件 SQLite；没有 ORM、HTTP API 或 Python 包结构。

若缺少依赖，按实际脚本需要安装，例如：

```bash
python -m pip install openai openpyxl
```

不要擅自新增依赖、lockfile 或大型框架，除非任务需要且已获确认。

## 常用命令

以下命令从仓库根目录执行；也可切换到 `database/` 后去掉前缀。

```bash
# 仅进行 Python 语法检查（不会调用 LLM、不会修改业务数据）
python -m py_compile database/build_db.py database/clean_load.py database/extract.py database/verify.py

# 查看原图相对于 extracted/ 的提取进度
python database/extract.py --status

# 仅提取一张图；参数可为文件名，或相对“价格图片/”的路径
python database/extract.py --file CPU/example.png

# 批量真实调用 LLM；最多 10 个并发请求，会跳过已有同名 JSON
python database/extract.py

# 将 extracted/*.json 清洗并入库
python database/clean_load.py

# 使用 CPU 人工基准验证一份提取 JSON
python database/verify.py database/extracted/<result>.json

# 重建空数据库：会删除现有 database/cpumem.db，执行前必须明确确认
python database/build_db.py
```

`extract.py` 的批量运行会真实消耗接口配额/时间，且向配置的外部服务传输图片；除非用户明确要求运行提取，不要执行无参数的批量命令。`clean_load.py` 会修改数据库和可能生成 `conflicts.json`，同样不应作为无副作用检查运行。

## 提取层约定（`extract.py`）

### 输入、输出和断点续跑

- 输入根目录通过脚本位置推导为 `价格图片/`，递归匹配 `png`、`jpg`、`jpeg`。
- 输出为 `database/extracted/<图片 basename>.json`；存在同名 JSON 即视为已完成并跳过。
- 这意味着**不同分类目录下的同 basename 图片会相互冲突**；改动命名/检查点逻辑时必须同时考虑 `extract_one()`、`status()` 和既有结果的兼容性。
- 每张图在完成后单独写出，失败不会产生结果；重跑会重试失败项。批量结束后会追加 `database/extract_progress.log`。
- `MAX_WORKERS = 10`；遇到供应商限流或不稳定时应降低并发或重跑失败项，而不是把失败结果伪造为成功。

### 提示词与 LLM 契约

`build_prompt()` 从图片父目录装配：`base.txt + <父目录>.txt`，并读取启用状态下的 `cpu_watchlist.json`。`base.txt` 约束模型只输出如下结构：

```json
{
  "sheet_date": "YYYY-MM-DD",
  "products": [
    {
      "category": "CPU",
      "vendor": "Intel",
      "product_name": "i5-12400F",
      "price_type": "散片",
      "price": 613
    }
  ]
}
```

重要规则：

- 日期年份是 2026；只保留价格明确的行。含 `*`、`X`、空白或无法辨认的价格要跳过，不能猜测或从相邻行复制。
- `product_name` 必须保留容量、频率、型号后缀、显存等会区分产品的规格。
- CPU 的 `F`、`K`、`KF`、无后缀是不同型号；一型号双价与多型号多价必须按 `CPU.txt` 的行列规则区分。
- JSON 仅应有定义的字段；源文件名由程序附加 `_source_image`。
- `real_extract()` 接受模型偶尔加上的 Markdown 代码围栏，并对不支持 `temperature` 的端点重试一次。

**当前实现注意：**虽然注释和设计文档将 CPU 白名单描述为 CPU 专用，`build_prompt()` 目前只要白名单启用就会附加它，并未检查父目录是否为 `CPU`。修改该行为时需要补充验证，避免无意改变非 CPU 图片的提取范围。

## 清洗与数据库约定（`clean_load.py` / `build_db.py`）

### SQLite 模型

| 表 | 职责 | 关键字段 |
| --- | --- | --- |
| `products` | 产品维度 | `product_key`（PK）、`display_name`、`category`、`vendor` |
| `dates` | 日期维度 | `date_key`（`YYYY-MM-DD`，PK）、`year`、`month`、`day`、`weekday` |
| `quotes` | 长表价格事实 | `id`、`product_key`、`date_key`、`price`、`price_type`、`source_image` |

每种价格类型是一条 `quotes` 记录：CPU 通常为 `散片`/`原盒`，内存通常为 `单条`/`套装`，其余可为 `默认`。保留 `source_image` 是溯源要求，不能随意移除。

已有索引：

- `idx_q_prod_date`：`quotes(product_key, date_key)`；
- `idx_p_cat_vendor`：`products(category, vendor)`。

### 标准化和入库规则

- 规范化逻辑集中在 `VENDOR_MAP`、`CATEGORY_MAP`、`PRICE_TYPE_MAP` 与 `norm_*` 函数中。新增别名优先在映射表中以最小改动补充，而非分散地写特判。
- `product_key` 是 `规范化 vendor + '-' + 小写、连字符化的 product_name`。它不会去除完整规格；提取措辞漂移可能造成不同键，改动键策略前须考虑已有历史数据迁移。
- `norm_date()` 当前接受完整 `YYYY-M-D` 日期和中文 `M月D日`，并强制年份为 2026；不要仅根据文档假定 `9.16` 这类无年份点号日期已被支持。
- 无法转换、`<= 0`、含 `*`/`X`、或不在 `1..200000` 的价格不入库。
- 若分类未知，`guess_category()` 使用正则兜底；不要把该兜底当作高置信分类器。当前顺序会先把含容量标记（如 `16G`）的名称识别为固态硬盘、后识别 `DDR3`/`DDR4`/`DDR5` 内存，因此在 LLM 漏填分类时，内存产品存在被误归为固态硬盘的风险；调整时应添加覆盖该情形的验证。
- 幂等性以 `source_image` 实现：处理每个 JSON 前先删除该来源的旧 `quotes`，再插入新结果。
- 全库去重键为 `(date_key, product_key, price_type)`：同价保留最早记录；异价保留最早记录、删除后续记录，并写入 `database/conflicts.json` 供人工核图。

### 当前实现与文档不一致处

在修改相关代码、说明行为或编写自动化时必须保留这些事实：

1. `load_db()` 虽可在库不存在时建表，但 `main()` 会先检测 `cpumem.db` 不存在并直接返回；首次运行实际应先执行 `build_db.py`，或修复后再更新文档和测试。
2. `--force-conflicts` 目前只影响提示信息；`dedupe_quotes()` 仍会删除冲突记录，且不会把它们重新入库。不要把它描述成已经生效的“强制保留”。
3. `build_db.py` 会无提示删除已有数据库；绝不可在调试、测试或修复中随手运行。

## 验证与质量要求

- 仓库没有 `pytest`/`unittest`/CI。改动 Python 后至少运行 `py_compile`。
- 改动 CPU 提取提示词、白名单、CPU 解析或验证规则时，应使用 `jg.xlsx` 和对应单图 JSON 运行 `verify.py`；检查报告中的 `类型错`、`数值错`、`漏提`、`多提`、`清单外`。
- `verify.py` 的 `norm()` 是为 CPU 开发集设计的独立归一化规则，和数据库的 `norm_product_key()` 不同；不要误把验证归一化直接用于全品类入库。
- 改动提示词时，除开发图外应至少选另一张不同期、不同版式/比例的 CPU 图进行人工抽检，避免对单图过拟合。
- 不能因 LLM 提取不确定而补造价格。宁可漏掉不确定行，也不要跨行、跨列或跨产品推断。

## 数据、安全与 Git 工作约束

- `.gitignore` 当前只忽略 `价格图片/` 和少量系统文件；数据库、提取结果、日志、Excel、实验裁切和 LLM 配置并不都被忽略。新增生成物前应评估是否需要补充忽略规则，但不要未经确认删除用户已有本地文件。
- `database/llm_config.json` 当前可被 Git 追踪且包含敏感配置。不要打印其内容、复制凭据、在测试中提交请求配置，或把密钥写入代码/文档。建议修改时支持环境变量或本地未追踪配置，但须兼顾现有工作流。
- 原始报价图片可能包含敏感或受限数据，禁止上传、外发或批量处理，除非用户明确授权。
- 开始任务前先执行 `git status --short`；工作区可能已有用户未提交改动、日志、实验结果和数据库变化。只修改任务所需文件，不覆盖、暂存或回退无关改动。
- 运行所有数据库重建、批量提取、批量入库、删除或重命名操作前，说明影响并取得明确许可。
- 不要将 `__pycache__/`、临时裁切图、调试 JSON、日志或含密钥配置作为代码变更的一部分提交，除非用户明确要求保存这些产物。

## 代码风格

- 延续现有脚本式结构：模块级路径常量、带简短中文 docstring 的小函数、`main()` 与 `if __name__ == "__main__":` 入口。
- 路径以 `__file__` 推导，不依赖调用时的当前工作目录。
- 使用标准库优先；CLI 保持轻量，现有代码以 `sys.argv` 而非 CLI 框架解析参数。
- 用户可调的提取策略应优先外置到 `database/prompts/` 或 JSON 配置；程序只负责安全、明确地装配与校验。
- 保持数据库值和提示词中的既有中文枚举（如 `内存`、`固态硬盘`、`散片`、`原盒`），修改枚举时同步检查映射、提示词、SQL、历史数据兼容性和文档。
