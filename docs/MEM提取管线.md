# MEM 提取管线文档

本文档专门描述 **MEM（内存）报价图片的专用提取管线**。它与
[`CPU提取管线.md`](CPU提取管线.md) 结构对齐——同样的入口协议、OCR 通道、
缓存分层和数据导入模式，但针对 mem 报价单的特点做了三处关键差异：

1. **无裁剪预处理**：图片不做任何预处理（不裁剪、不缩放、不增强），
   整张原图直送 OCR 和 LLM；
2. **OCR 为主**：GLM-OCR 先把整图转写为 Markdown 表格，LLM 以 OCR 文本
   为主要信息源提取，原图作为辅助证据消歧；
3. **全类目保留**：mem 报价单通常同时含内存/固态/主板/显卡/显示器等多个
   区块，管线不做类目过滤，全部提取，并为每条记录补齐 `hardware_type`
   类别缩写字段。

数据导入由 `load_mem.py` 承担（管线最后一步，见第 4 节）。

> 说明：文档中的行号与实现细节以撰写时的代码为准；若与代码实际行为
> 不一致，以代码为准。

---

## 1. 最终架构（详细）

整条管线从原始图片到数据库的完整数据流，含每个环节的输入、输出、
缓存与失败分支：

```text
╔══════════════════════════════════════════════════════════════════════╗
║  输入层                                                              ║
║                                                                      ║
║  价格图片/mem/*.png|jpg|jpeg          （人工分类原图，171 张）         ║
║  · 29 种分辨率，长宽比 1.04~1.12（近方形版式，与 CPU 宽扁图不同）      ║
║  · 版式：多区块并存（内存 / SSD / 主板 / 显卡 / 显示器等报价表）       ║
║  · 不做任何预处理（无裁剪、无缩放），原图字节直接进入下一层            ║
╚═══════════════════════════════┬══════════════════════════════════════╝
                                │
                ┌───────────────▼────────────────┐
                │  S0 入口协议（extract_mem.py）   │
                │  python extract_mem.py          │
                │    <图片文件或目录>              │
                │    [--workers N]                │
                │    [--out-dir 目录]             │
                │    [--status]                   │
                │  · 必须显式传参；无参数只打印用法 │
                │  · 图片文件：校验扩展名 → 单张    │
                │  · 目录：os.walk 扫描 → 全部     │
                │  · 路径无效/无图片 → 报错退出     │
                │  · --status：唯一无副作用模式     │
                └───────────────┬────────────────┘
                                │
                ┌───────────────▼────────────────┐
                │  S1 断点续跑检查                 │
                │  output_mem/extracted_mem/      │
                │    <图片名>.json 已存在？        │
                │   ├─ 是 → 跳过该图（不调 OCR/LLM）│
                │   └─ 否 → 进入提取流程           │
                └───────────────┬────────────────┘
                                │
      ┌─────────────────────────▼──────────────────────────────┐
      │  S2 OCR 通道（ocr_markdown()）                          │
      │                                                         │
      │  ocr_cache/<图片名>.md 已存在？                          │
      │   ├─ 是 → 直接复用缓存（同图同输出，确定性）              │
      │   └─ 否 → 整图 base64 编码                               │
      │        │                                                │
      │        ▼                                                │
      │  POST llama.cpp /v1/chat/completions                    │
      │    地址：llm_config.json 的 ocr_base_url                 │
      │           （默认 127.0.0.1:8080，兼容 OCR_BASE_URL 覆盖） │
      │    model = glm-ocr                                       │
      │    prompt = "识别图片中的所有文字，输出为Markdown格式"     │
      │    temperature = 0（确定性输出关键）                      │
      │    max_tokens = 16384（不足会截断）                       │
      │        │                                                │
      │        ▼                                                │
      │  模型输出 HTML 表格（含 rowspan/colspan 合并单元格）      │
      │        │                                                │
      │        ▼                                                │
      │  html_table_to_markdown()：                              │
      │    · 展开 rowspan/colspan 到每个占位格                   │
      │    · 剥 HTML 标签、转义竖线（避免破坏管道表格）           │
      │    · 插入 |---| 分隔行                                   │
      │        │                                                │
      │        ▼                                                │
      │  Markdown 写入 ocr_cache/ 缓存                           │
      │                                                         │
      │  失败/返回空 → RuntimeError → 该图计失败（不产生结果文件， │
      │  不降级纯视觉；重跑自动重试）                             │
      └─────────────────────────┬──────────────────────────────┘
                                │ OCR Markdown（结构化转写文本）
                                │
      ┌─────────────────────────▼──────────────────────────────┐
      │  S3 提示词组装（OCR 为主、图为辅）                       │
      │                                                         │
      │  提示词 = prompts/OCR主_指令_MEM.txt 全文                │
      │           + OCR Markdown 数据区（追加在指令之后）        │
      │                                                         │
      │  指令结构（对齐 CPU 管线 OCR主_指令.txt 的两步式）：      │
      │   · 角色声明：OCR 转写表格（主要）+ 报价单原图（辅助裁决）│
      │   · 提取规则：双价行拆单条/套装两条、空价格行跳过、       │
      │     "数字+****"可取数字部分、严禁跨行/跨列/跨区块复制价格 │
      │   · 输出 JSON 契约：sheet_date 年份 2026、               │
      │     category 按区块标题、product_name 内存拼全           │
      │     容量/代际/频率/时序/马甲、price_type 枚举             │
      │     （单条/套装/默认）、price 纯数字、                    │
      │     hardware_type 类别缩写枚举                           │
      │   · 原图消歧规则：OCR 型号名可疑（I/1 混淆等）回原图确认  │
      │   · （✗ 不含 CPU 白名单——mem 图与白名单无关）            │
      │                                                         │
      │  （不再拼接 base.txt / mem.txt——新结构自带完整契约，     │
      │    避免 base.txt 星号价格规则与新契约冲突）               │
      └─────────────────────────┬──────────────────────────────┘
                                │
      ┌─────────────────────────▼──────────────────────────────┐
      │  S4 LLM 提取（call_llm()）                              │
      │                                                         │
      │  配置：llm_config.json（base_url / api_key / model）     │
      │  消息：原整图（image_url base64）+ 联合提示词            │
      │        （多模态双内容，OCR 为主、原图辅助消歧）           │
      │  兼容处理：temperature 不被支持 → 去参数重试一次；        │
      │            剥 ```json 代码围栏后 json.loads 解析          │
      │  超时：timeout_seconds（默认 300s）→ 该图计失败          │
      │  输出：dict（sheet_date + products[]）                   │
      └─────────────────────────┬──────────────────────────────┘
                                │
      ┌─────────────────────────▼──────────────────────────────┐
      │  S5 后处理与落盘（extract_one()）                        │
      │                                                         │
      │  ① 保留全类目：不过滤 category（内存/固态/主板/显卡/     │
      │     显示器等全部保留）                                   │
      │  ② hardware_type 补齐（兜底链）：                        │
      │     LLM 已填合法值 → 放行                                │
      │     缺失/非法 → infer_hardware_type()：                  │
      │       内存 → infer_ddr_type()：                          │
      │         显式 DDR3/4/5 标记（容忍空格/大小写）优先         │
      │         → 频率启发式（4000~12000 → DDR5，                │
      │                     800~2133 → DDR4）                    │
      │         → OTHER                                          │
      │       固态硬盘→SSD / 机械硬盘→HDD / 显卡→GPU /           │
      │       主板→MB / 电源→PSU / 显示器→MON / CPU→CPU /        │
      │       外设→PERIPH / TF/SD/U盘→CARD / 其他→OTHER          │
      │  ③ 附溯源字段：source_image = 原始图片文件名              │
      │  ④ 写入 output_mem/extracted_mem/<图片名>.json           │
      │     （UTF-8，indent=2）——即断点续跑检查点                 │
      └─────────────────────────┬──────────────────────────────┘
                                │
╔═══════════════════════════════▼══════════════════════════════════════╗
║  数据导入层（load_mem.py，人工触发，管线最后一步）                     ║
║                                                                      ║
║  ┌─────────────────────────────────────────────────────────────┐    ║
║  │ 预验证（不通过则跳过该条并记录原因，不入库）                  │    ║
║  │  · source_image 非空（溯源/幂等键）                          │    ║
║  │  · sheet_date 可解析为 YYYY-MM-DD（norm_date，年份强制 2026）│    ║
║  │  · product_name 非空                                        │    ║
║  │  · price 可转数字、> 0、在 1..200000、不含 * / X             │    ║
║  │  · price_type ∈ {单条, 套装, 默认}                           │    ║
║  │  · hardware_type ∈ 枚举表（非法时兜底推断后再入库）           │    ║
║  └──────────────────────────┬──────────────────────────────────┘    ║
║                             ▼                                       ║
║  ┌─────────────────────────────────────────────────────────────┐    ║
║  │ 幂等入库（先删后插）                                          │    ║
║  │  · DELETE FROM quotes WHERE source_image = ?                 │    ║
║  │  · products：product_key（vendor-型号）不存在时插入，         │    ║
║  │    category 按记录实际值（内存/固态硬盘/主板…），             │    ║
║  │    display_name 剥离规格后缀                                 │    ║
║  │  · dates：INSERT OR IGNORE 日期维表                          │    ║
║  │  · quotes：每条价格类型一行，带 source_image，               │    ║
║  │    hardware_type 写入 quotes.hardware_type 列                │    ║
║  └──────────────────────────┬──────────────────────────────────┘    ║
║                             ▼                                       ║
║  ┌─────────────────────────────────────────────────────────────┐    ║
║  │ 库内去重：同 (date_key, product_key, price_type) 多条时       │    ║
║  │  · 同价 → 保留最早一条，删除后续                             │    ║
║  │  · 异价 → 删除冲突行，写入                                    │    ║
║  │    output_mem/load_mem_conflicts.json                        │    ║
║  │    + 人可读报告 load_mem_report.md（待人工核对原图）          │    ║
║  └─────────────────────────────────────────────────────────────┘    ║
║                                                                      ║
║  目标数据库：database/cpumem.db（SQLite）                             ║
║    products(product_key, display_name, category, vendor)             ║
║    dates(date_key, year, month, day, weekday)                        ║
║    quotes(id, product_key, date_key, price, price_type,              ║
║           source_image, hardware_type)                               ║
╚══════════════════════════════════════════════════════════════════════╝
```

### 1.1 产物与缓存布局（对齐 CPU 管线的 output_cpu/）

```text
database/output_mem/                      # MEM 管线全部产物（默认；--out-dir 可调）
  ├─ extracted_mem/<图片名>.json          # 提取结果（含 source_image、hardware_type，
  │                                       #  断点续跑检查点）
  ├─ ocr_cache/<图片名>.md                # OCR Markdown 转写缓存（同图同输出，安全复用）
  └─ extract_mem_progress.log             # 提取进度日志（追加写）
```

三层缓存都按**文件 basename** 复用：改 OCR 行为 → 清 `ocr_cache/`；
改提示词 → 删 `extracted_mem/` 对应 JSON。

### 1.2 与其他管线的隔离

| 维度 | MEM 管线（extract_mem.py） | CPU 管线（extract_cpu.py） | 通用管线（extract.py） |
| --- | --- | --- | --- |
| 输入目录 | 仅 `价格图片/mem/` | 仅 `价格图片/CPU/` | `价格图片/` 全部子目录 |
| 裁剪预处理 | **无** | 三段式锚点裁剪 | 无 |
| OCR 通道 | 有（llama.cpp GLM-OCR） | 有 | 无 |
| 提示词 | `OCR主_指令_MEM.txt` + OCR Markdown | `OCR主_指令.txt` + OCR Markdown | base.txt + `<父目录>.txt` |
| CPU 白名单 | **无** | 内嵌在指令中 | cpu_watchlist.json |
| 类目范围 | **全类目保留** | 只保留 CPU | 不过滤 |
| 输出目录 | `output_mem/extracted_mem/` | `output_cpu/extracted_cpu/` | `extracted/` |
| 数据导入 | `load_mem.py` | `load_cpu.py` | `clean_load.py` |
| 进度日志 | `output_mem/extract_mem_progress.log` | `output_cpu/extract_cpu_progress.log` | `extract_progress.log` |

三条入库路径互不干扰：`load_mem.py`（extracted_mem）、
`load_cpu.py`（output_cpu/extracted_cpu）、`clean_load.py`（extracted/）。

---

## 2. hardware_type 类别缩写字段

每条记录带 `hardware_type`，表示该硬件的类别缩写（入库到
`quotes.hardware_type` 列）：

| category | hardware_type |
| --- | --- |
| 内存 | DDR3 / DDR4 / DDR5（按 DDR 代际细分） |
| 固态硬盘 | SSD |
| 机械硬盘 | HDD |
| 显卡 | GPU |
| 主板 | MB |
| 电源 | PSU |
| 显示器 | MON |
| CPU | CPU |
| 外设 | PERIPH |
| TF卡/SD卡/U盘 | CARD |
| 其他/无法判断 | OTHER |

**DDR 代际兜底识别**（LLM 漏填时）：显式 `DDR3/4/5` 标记优先
（容忍空格/大小写）→ 频率启发式（4000~12000 → DDR5，800~2133 → DDR4）→ OTHER。

---

## 3. 提示词（prompts/OCR主_指令_MEM.txt）

- **独立成文**，不拼接 base.txt / mem.txt（新结构自带完整 JSON 契约，
  避免 base.txt "含星号价格无效" 与 "数字+**** 有效" 的规则冲突）；
- 结构对齐 CPU 的 `OCR主_指令.txt`：角色声明 → 提取规则 → JSON 契约
  （含 hardware_type）→ 原图消歧规则 → OCR Markdown 数据区；
- 无 CPU 白名单；提示词可运行时修改，改后需删 `extracted_mem/`
  对应 JSON 才会重新提取。

---

## 4. 数据导入（load_mem.py，管线最后一步）

```bash
python database/load_mem.py                # 默认导入 output_mem/extracted_mem/
python database/load_mem.py --status       # 查看库内 MEM 数据统计
python database/load_mem.py <单份JSON/目录>
```

1. **预验证**（不通过跳过并记录原因）：source_image 非空、sheet_date 可解析、
   product_name 非空、price 可转数字且 1..200000 且无 */X、
   price_type ∈ {单条, 套装, 默认}、hardware_type ∈ 枚举表（非法时兜底推断）；
2. **幂等入库**：先删同 source_image 旧 quotes，再写
   products（category 按记录实际值）/ dates / quotes（含 hardware_type）；
3. **库内去重**：同 (date_key, product_key, price_type) 同价留最早一条，
   异价删除并写 `output_mem/load_mem_conflicts.json` + 人可读报告
   `load_mem_report.md`；
4. 清洗映射复用 `clean_load.py`。

**schema**：`quotes` 表含 `hardware_type TEXT` 列（CPU 管线写入时为 NULL）。

---

## 5. 当前实测效果

- 纯视觉首版（升级前，已废弃）：0cd7c…png（2026-05-29 期）235 条全类目，
  hardware_type 覆盖 100%（DDR3×8 / DDR4×44 / DDR5×54 / SSD×85 / MB×20 /
  GPU×13 / MON×11），与 category 完全对应；
- OCR-first 版：待单图实测与人工基准建立后补充；
- 提示词与人工基准（对齐 pricebenchmark/ 模式）**稍后建立**，
  建立前批量提取结果属于未验证数据。

---

## 6. 已知注意事项与边界

1. **OCR 是硬依赖**：提示词以 OCR 为主，`ocr_markdown()` 失败即抛
   `RuntimeError`，该图计失败。跑批前确认 llama.cpp 服务
   （llm_config.json 的 ocr_base_url，默认 127.0.0.1:8080）已启动；
2. **缓存不自动失效**：改 OCR 行为 → 清 `ocr_cache/`；改提示词 → 删
   `extracted_mem/` 对应 JSON；
3. **无裁剪的前提**：整图提取依赖 LLM 的多区块表格理解；若实测出现
   跨区块串价（如内存表价格串给 SSD），再考虑分区块裁剪（远期优化）；
4. **三条入库路径互不干扰**：load_mem.py（extracted_mem）、load_cpu.py
   （output_cpu/extracted_cpu）、clean_load.py（extracted/）；
5. **并发限制**：默认 2（`--workers N` 可调，上限 6）——OCR/LLM 服务
   共同承压，超限自动钳制；
6. **配额与安全**：批量提取与导入真实消耗 LLM 配额、传输图片、写库，
   执行前需明确授权；`llm_config.json` 含敏感凭据，严禁外泄；
7. **失败处理原则**：失败图计入清单、重跑重试，绝不伪造成功，
   也不因 LLM 提取不确定而补造价格——宁可漏掉不确定行，也不跨行/
   跨列/跨区块推断。

**超时配置**（llm_config.json）：
- `timeout_seconds: 300` —— LLM 调用超时；
- `ocr_timeout_seconds: 600` —— OCR 调用超时；
- 超时抛异常 → 该图计失败，重跑自动重试。

---

## 7. 常用命令速查

```bash
# 仅语法检查（无副作用）
python -m py_compile database/extract_mem.py database/load_mem.py

# 查看进度（不消耗配额）
python database/extract_mem.py --status

# 提取整个目录（默认 2 并发；真实调用 OCR + LLM，需 llama.cpp 在线，需授权）
python database/extract_mem.py ../价格图片/mem

# 提取单张
python database/extract_mem.py ../价格图片/mem/0cd7c32940974ecba18fcb159d4c5568.png

# 指定并发（上限 6）
python database/extract_mem.py ../价格图片/mem --workers 4

# 数据导入（管线最后一步：预验证 + hardware_type 落库 + 幂等入库）
python database/load_mem.py
python database/load_mem.py --status
```
