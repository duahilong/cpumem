# MEM 提取管线文档

本文档专门描述 **MEM（内存）报价图片的专用提取管线**。它与
[`CPU提取管线.md`](CPU提取管线.md) 结构对齐——同样的入口协议、内容寻址、
OCR 通道、缓存分层和数据导入模式，但针对 mem 报价单的特点做了关键差异：

1. **两段裁剪 + 4 块竖向分块**：标题横幅（sheet_date 来源）+ 表格主体两段
   拼接、总代标头区裁掉；主体再按分界竖线切成 4 块（每块 = 型号列 + 价格列），
   每块单独 OCR——分块不含日期信息，日期仅保留在裁剪图中由 LLM 从横幅读取；
2. **OCR 为主、分块转写**：GLM-OCR 对 4 个分块分别转写为 Markdown 表格，
   LLM 以分块 OCR 文本为主要信息源提取，裁剪图（含标题横幅）作为辅助证据
   消歧和 sheet_date 来源；
3. **全类目保留**：mem 报价单通常同时含内存/固态/主板/显卡/显示器等多个
   区块，管线不做类目过滤，全部提取，并为每条记录补齐 `hardware_type`
   类别缩写字段；
4. **内容寻址（方案 B，与 CPU 管线一致）**：key = MD5(源图字节) 贯穿
   裁剪/分块/OCR 缓存/最终 JSON/DB `source_image` 全链路。

数据导入由 `load_mem.py` 承担（管线最后一步，见第 4 节）；
`extract_mem.py --db` 可在提取结束后自动触发导入。

> 说明：文档以撰写时的代码为准；若与代码实际行为不一致，以代码为准。

---

## 1. 最终架构（详细）

整条管线从原始图片到数据库的完整数据流，含每个环节的输入、输出、
缓存与失败分支：

```text
╔══════════════════════════════════════════════════════════════════════╗
║  输入层                                                              ║
║                                                                      ║
║  价格图片/mem/*.png|jpg|jpeg          （人工分类原图）                ║
║  · 近方形多区块版式（内存 / SSD / 主板 / 显卡 / 显示器等报价表并排）  ║
║  · 管线内格式固定 PNG：单文件必须 .png；目录中 jpg/jpeg 跳过并提示    ║
╚═══════════════════════════════┬══════════════════════════════════════╝
                                │
                ┌───────────────▼────────────────┐
                │  S0 入口协议（extract_mem.py）   │
                │  python extract_mem.py          │
                │    <PNG图片或目录>               │
                │    [--workers N]                │
                │    [--out-dir 目录]             │
                │    [--db] [--status]            │
                │  · 必须显式传参；无参数只打印用法 │
                │  · 目录 os.walk 递归扫描 PNG     │
                │  · --status：唯一无副作用模式     │
                │  · --out-dir：产物根目录同步切换  │
                │    （含 MANIFEST_PATH）          │
                └───────────────┬────────────────┘
                                │
                ┌───────────────▼────────────────┐
                │  S1 内容寻址 + 断点续跑          │
                │  key = MD5(源图原始字节)         │
                │  register_key() 登记 manifest    │
                │    （锁互斥 + 原子替换；          │
                │     同内容异名升级 {name,        │
                │     aliases}）                  │
                │  extracted_mem/{key}.json 已存在?│
                │   ├─ 是 → 跳过该图（不调 OCR/LLM）│
                │   └─ 否 → 进入提取流程           │
                └───────────────┬────────────────┘
                                │
      ┌─────────────────────────▼──────────────────────────────┐
      │  S2 裁剪预处理（crop_mem_image()）                       │
      │                                                         │
      │  crop_cache/{key}.png 已存在？                           │
      │   ├─ 是 → 直接复用                                      │
      │   └─ 否 → 两段拼接：                                     │
      │     段1 标题横幅（报价单 XX月XX日，sheet_date 来源）      │
      │     段3 表格主体（从区块表头开始，含全部产品区块）        │
      │     —— 中间"总代标头区"（两行总代重复标头）裁掉          │
      │        （OCR 干扰源：分块后跨列文字产生残字乱码行）       │
      │                                                         │
      │  锚点：y 2%~15% 全宽深色表格线（深色占比>0.9）            │
      │    第1根 = 横幅底线（≈0.027h）                           │
      │    第3根 = 总代区底线（≈0.068h，区块表头上边界）          │
      │    只 2 根线时第 2 根为总代区底（仅一行总代版式）         │
      │    检测失败回退固定比例 0.027/0.068（stderr 警告）        │
      └─────────────────────────┬──────────────────────────────┘
                                │ crop_cache/{key}.png（横幅+主体）
                                │
      ┌─────────────────────────▼──────────────────────────────┐
      │  S3 4 块竖向切分（split_mem_blocks()）                   │
      │                                                         │
      │  crop_cache/{key}_B0~B3.png 全部已存在？                 │
      │   ├─ 是 → 直接复用                                      │
      │   └─ 否 → 只切表格主体区域（不拼回横幅，                 │
      │            分块无日期信息）：                            │
      │    横幅底线检测：y 1%~10% 全宽线（扫描从 1% 起跳过       │
      │      y=0 的横幅顶边框线），回退 0.027h                   │
      │    主体带 detect_vlines()：列深色占比>0.6 聚类成线       │
      │    3 条分界先验 x/w≈0.25/0.50/0.75，各 ±3% 窗口          │
      │      内取最左候选；无候选回退均分                        │
      │    切点落在表格间隙内（型号+价格对不拆散）               │
      │    产物 {key}_B0.png ~ _B3.png                          │
      └─────────────────────────┬──────────────────────────────┘
                                │
      ┌─────────────────────────▼──────────────────────────────┐
      │  S4 分块 OCR（ocr_markdown()，每图共 4 次 GLM-OCR）      │
      │                                                         │
      │  i = 0..3 逐块：                                        │
      │  ocr_cache/{key}_Bi.md 已存在？                          │
      │   ├─ 是 → 直接复用（同图同输出，确定性）                 │
      │   └─ 否 → 分块图 base64 编码                            │
      │        ▼                                                │
      │  POST llama.cpp /v1/chat/completions                    │
      │    地址：llm_config.json 的 ocr_base_url                 │
      │           （默认 127.0.0.1:8080，兼容 OCR_BASE_URL 覆盖） │
      │    model = glm-ocr                                       │
      │    prompt = "识别图片中的所有文字，输出为Markdown格式"    │
      │    temperature = 0（确定性输出关键）                      │
      │    max_tokens = ocr_max_tokens（默认 16384；             │
      │      余量过大会诱发同重复退化，16384 是平衡点）          │
      │    timeout = ocr_timeout_seconds（默认 600s）            │
      │        ▼                                                │
      │  html_table_to_markdown()：                              │
      │    · 展开 rowspan/colspan 到每个占位格                   │
      │    · 剥 HTML 标签、转义竖线（避免破坏管道表格）           │
      │    · 插入 |---| 分隔行                                   │
      │        ▼                                                │
      │  写入 ocr_cache/{key}_Bi.md 缓存                        │
      │                                                         │
      │  合并：各块带【第 N 块（报价单从左数第 N 个产品区块）】   │
      │  标注，块间不拼表（防串价）                              │
      │                                                         │
      │  任一块失败/返回空 → RuntimeError → 该图计失败           │
      │  （不产生结果文件，不降级纯视觉；重跑自动重试，           │
      │  已成功的块命中缓存）                                    │
      └─────────────────────────┬──────────────────────────────┘
                                │ 分块 OCR Markdown（带块标注）
                                │
      ┌─────────────────────────▼──────────────────────────────┐
      │  S5 提示词组装（OCR 为主、图为辅）                       │
      │                                                         │
      │  提示词 = prompts/OCR主_指令_MEM.txt 全文                │
      │           + 分块 OCR Markdown 数据区（追加在指令之后）   │
      │                                                         │
      │  指令结构（对齐 CPU 管线 OCR主_指令.txt 的两步式）：      │
      │   · 角色声明：分块 OCR 表格（主要）+ 裁剪图（辅助裁决    │
      │     + sheet_date 来源）                                 │
      │   · 分块说明：无标题横幅/总代标头（已裁剪）；报价日期    │
      │     从裁剪图顶部标题横幅读取，不从 OCR 表格提取          │
      │   · 提取规则：双价行拆单条/套装两条、空价格行跳过、       │
      │     "数字+****"可取数字部分、严禁跨行/跨列/跨块复制价格  │
      │   · 输出 JSON 契约：sheet_date 年份 2026、               │
      │     category 按区块标题、product_name 内存拼全           │
      │     容量/代际/频率/时序/马甲、price_type 枚举             │
      │     （单条/套装/默认）、price 纯数字、                    │
      │     hardware_type 类别缩写枚举                           │
      │   · 裁剪图消歧规则：OCR 型号名可疑（I/1 混淆等）回裁剪图 │
      │     确认                                                 │
      │   · （✗ 不含 CPU 白名单——mem 图与白名单无关）            │
      │                                                         │
      │  （不拼接 base.txt / mem.txt——新结构自带完整契约，       │
      │    避免 base.txt 星号价格规则与新契约冲突）               │
      └─────────────────────────┬──────────────────────────────┘
                                │
      ┌─────────────────────────▼──────────────────────────────┐
      │  S6 LLM 提取（call_llm()）                              │
      │                                                         │
      │  配置：llm_config.json（base_url / api_key / model）     │
      │  消息：裁剪图 crop_cache/{key}.png（含标题横幅，         │
      │        sheet_date 来源；image_url base64）+ 联合提示词   │
      │  兼容处理：瞬时错误（timeout/connection/rate limit）     │
      │            原参数重试 1 次；                             │
      │            temperature 不被支持 → 去参数重试；           │
      │            剥 ```json 代码围栏后 json.loads 解析          │
      │  超时：timeout_seconds（默认 300s）→ 该图计失败          │
      │  输出：dict（sheet_date + products[]）                   │
      └─────────────────────────┬──────────────────────────────┘
                                │
      ┌─────────────────────────▼──────────────────────────────┐
      │  S7 后处理与落盘（extract_one()）                        │
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
      │  ③ 附溯源字段：source_image = {key}.png（内容寻址，      │
      │     反查原名走 manifest.json）                           │
      │  ④ 原子落盘 output_mem/extracted_mem/{key}.json          │
      │     （临时文件 + os.replace；UTF-8，indent=2）           │
      │     ——即唯一断点续跑检查点（全链路成功才写）             │
      └─────────────────────────┬──────────────────────────────┘
                                │
╔═══════════════════════════════▼══════════════════════════════════════╗
║  数据导入层（load_mem.py，人工触发或 extract_mem.py --db 自动触发）   ║
║                                                                      ║
║  ┌─────────────────────────────────────────────────────────────┐    ║
║  │ 预验证（不通过则跳过该条并记录原因，不入库）                  │    ║
║  │  · source_image 非空（溯源/幂等键；内容寻址结果为 {key}.png）│    ║
║  │  · sheet_date 可解析为 YYYY-MM-DD（norm_date，年份强制 2026）│    ║
║  │  · product_name 非空                                        │    ║
║  │  · price 可转数字、> 0、在 1..200000、不含 * / X             │    ║
║  │  · price_type ∈ {单条, 套装, 默认}                           │    ║
║  │  · hardware_type ∈ 枚举表（非法时兜底推断后再入库）           │    ║
║  └──────────────────────────┬──────────────────────────────────┘    ║
║                             ▼                                       ║
║  ┌─────────────────────────────────────────────────────────────┐    ║
║  │ 幂等入库（先删后插；仅当该文件有通过记录时才删旧）            │    ║
║  │  · DELETE FROM quotes WHERE source_image = ?                 │    ║
║  │  · products：product_key（vendor-型号）不存在时插入，         │    ║
║  │    category 按记录实际值（内存/固态硬盘/主板…），             │    ║
║  │    display_name 仅 CPU 类剥规格后缀（MEM 保留规格）           │    ║
║  │  · dates：INSERT OR IGNORE 日期维表                          │    ║
║  │  · quotes：每条价格类型一行，带 source_image，               │    ║
║  │    hardware_type 写入 quotes.hardware_type 列                │    ║
║  └──────────────────────────┬──────────────────────────────────┘    ║
║                             ▼                                       ║
║  ┌─────────────────────────────────────────────────────────────┐    ║
║  │ 库内去重（全库，破坏性）：同 (date_key, product_key,          │    ║
║  │ price_type) 多条时                                           │    ║
║  │  · 同价 → 保留最早一条，删除后续                             │    ║
║  │  · 异价 → 删除冲突行（保留最早），写入                        │    ║
║  │    output_mem/load_mem_conflicts.json（kept.source 当前      │    ║
║  │    为 null，不完全可溯源）                                    │    ║
║  │    + 人可读报告 load_mem_report.md（仅 CLI 直跑生成，         │    ║
║  │    --db 路径不写报告）                                        ║    ║
║  └─────────────────────────────────────────────────────────────┘    ║
║                                                                      ║
║  目标数据库：database/cpumem.db（SQLite）                             ║
║    products(product_key, display_name, category, vendor)             ║
║    dates(date_key, year, month, day, weekday)                        ║
║    quotes(id, product_key, date_key, price, price_type,              ║
║           source_image, hardware_type)                               ║
╚══════════════════════════════════════════════════════════════════════╝
```

### 1.1 内容寻址规则（方案 B，与 CPU 管线一致）

- key = 源文件**原始字节**的 MD5。字节不变时改名、移动、复制都复用同一 key；
  不同内容或重新保存/转码后字节变化则成为新 key。
- 裁剪图、4 块分块图、OCR 缓存、最终 JSON 和 DB `source_image` 都用该 key；
  原始文件名不参与 key，由 `manifest.json`（key → 原名，同内容异名升级为
  `{name, aliases}`）反查。
- 同内容异名重发 → 相同哈希 → 自动跳过（免费去重）；manifest 读取加锁、
  写入原子替换（临时文件 + os.replace）。
- `--out-dir` 自定义输出时 `MANIFEST_PATH` 同步切换（与 CPU 管线不同，
  MEM 的 manifest 跟随产物目录）。

### 1.2 产物与缓存布局（对齐 CPU 管线的 output_cpu/）

```text
database/output_mem/                      # MEM 管线全部产物（默认；--out-dir 可调）
  ├─ extracted_mem/{key}.json             # 提取结果（source_image={key}.png、
  │                                       #  hardware_type；唯一断点续跑检查点，原子落盘）
  ├─ crop_cache/{key}.png                 # 两段裁剪图（标题横幅+表格主体，LLM 输入）
  ├─ crop_cache/{key}_B0~B3.png           # 4 块分块图（仅表格主体，无横幅）
  ├─ ocr_cache/{key}_B0~B3.md             # 4 块分块 OCR Markdown 缓存
  ├─ manifest.json                        # MD5(key) → 原名映射（随 DB 备份）
  └─ extract_mem_progress.log             # 提取进度日志（追加写）
```

缓存按内容 key 复用，**改逻辑不会自动失效**，须按影响层手动清理：

| 改动 | 需清理 |
| --- | --- |
| 裁剪逻辑（`crop_mem_image`） | `crop_cache/{key}.png` + `{key}_B0~B3.png` + `ocr_cache/{key}_B0~B3.md` |
| 分块逻辑（`split_mem_blocks`） | `{key}_B0~B3.png` + `ocr_cache/{key}_B0~B3.md` |
| OCR 行为（`ocr_markdown`/`html_table_to_markdown`） | `ocr_cache/{key}_B0~B3.md` |
| 主提示词（`OCR主_指令_MEM.txt`） | `extracted_mem/` 对应 `{key}.json` |

### 1.3 与其他管线的隔离

| 维度 | MEM 管线（extract_mem.py） | CPU 管线（extract_cpu.py） | 通用管线（extract.py） |
| --- | --- | --- | --- |
| 输入目录 | 仅 `价格图片/mem/` | 仅 `价格图片/CPU/` | `价格图片/` 全部子目录 |
| 裁剪预处理 | 两段裁剪（横幅+主体，总代区裁掉）+ 4 块竖向分块 | 三段式锚点裁剪 + LL/LR 分块 | 无 |
| OCR 通道 | 有（llama.cpp GLM-OCR，每图 4 次） | 有（每图 2 次） | 无 |
| 内容寻址 | MD5 贯穿（manifest 随 `--out-dir` 切换） | MD5 贯穿（manifest 固定默认路径） | basename |
| 提示词 | `OCR主_指令_MEM.txt` + 分块 OCR Markdown | `OCR主_指令.txt` + 联合 OCR Markdown | base.txt + `<父目录>.txt` |
| CPU 白名单 | **无** | 内嵌在指令中 | cpu_watchlist.json |
| 类目范围 | **全类目保留** | 只保留 CPU | 不过滤 |
| LLM 输入图 | 裁剪图（横幅+主体，sheet_date 来源） | 裁剪图（横幅+CPU 表） | 原图 |
| 输出目录 | `output_mem/extracted_mem/` | `output_cpu/extracted_cpu/` | `extracted/` |
| 数据导入 | `load_mem.py`（或 `--db` 自动触发） | `load_cpu.py` | `clean_load.py` |
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

注意：`OTHER` 是合法枚举值，不会当非法值重推——否则低频内存条可能被
频率启发式误改写。

---

## 3. 提示词（prompts/OCR主_指令_MEM.txt）

- **独立成文**，不拼接 base.txt / mem.txt（新结构自带完整 JSON 契约，
  避免 base.txt "含星号价格无效" 与 "数字+**** 有效" 的规则冲突）；
- 结构对齐 CPU 的 `OCR主_指令.txt`：角色声明 → 分块说明 → 提取规则 →
  JSON 契约（含 hardware_type）→ 裁剪图消歧规则 → OCR Markdown 数据区；
- **sheet_date 契约**：从随消息提供的裁剪图顶部标题横幅"报价单 XX月XX日"
  提取，横幅只含月份和日期，年份固定 2026；OCR 表格中不含日期（分块已裁剪）；
- **分块说明**：分块图无标题横幅、无总代标头（均已裁剪）；同一块内的内容
  属于同一列区块，不要跨块对应价格；分块边缘残字行忽略；
- 无 CPU 白名单；提示词可运行时修改，改后需删 `extracted_mem/`
  对应 JSON 才会重新提取。

---

## 4. 数据导入（load_mem.py，管线最后一步）

```bash
python database/load_mem.py                # 默认导入 output_mem/extracted_mem/
python database/load_mem.py --status       # 查看库内 MEM 数据统计（hardware_type 非空记录）
python database/load_mem.py <单份JSON/目录>
python database/load_mem.py --out-dir DIR  # 对齐 extract_mem.py 的自定义输出目录
```

1. **预验证**（不通过跳过并记录原因）：source_image 非空、sheet_date 可解析、
   product_name 非空、price 可转数字且 1..200000 且无 */X、
   price_type ∈ {单条, 套装, 默认}、hardware_type ∈ 枚举表（非法时兜底推断）；
2. **幂等入库**：仅当文件内有通过记录时先删同 source_image 旧 quotes，再写
   products（category 按记录实际值）/ dates / quotes（含 hardware_type）；
   文件内无效记录被跳过，不是严格全文件 all-or-nothing；
3. **库内去重**（全库，破坏性）：同 (date_key, product_key, price_type)
   同价留最早一条，异价删除并写 `output_mem/load_mem_conflicts.json` +
   人可读报告 `load_mem_report.md`（仅 CLI 直跑生成；`extract_mem.py --db`
   走 `run_import()` 不写报告）；冲突是人工审阅队列，不是已解决问题；
4. 清洗映射复用 `clean_load.py`。

**schema**：`quotes` 表含 `hardware_type TEXT` 列（CPU 管线写入时为 NULL，
`--status` 以此区分 MEM 数据）。

**`extract_mem.py --db`**：提取结束后自动调用 `load_mem.run_import(OUT_DIR)`
（全量、幂等），打印 导入/跳过/去重删除/冲突数；导入失败不影响已落盘的
提取 JSON。

---

## 5. 当前实测效果

- 纯视觉首版（升级前，已废弃）：0cd7c…png（2026-05-29 期）235 条全类目，
  hardware_type 覆盖 100%（DDR3×8 / DDR4×44 / DDR5×54 / SSD×85 / MB×20 /
  GPU×13 / MON×11），与 category 完全对应；
- 裁剪/分块链路（当前版式，2026-09-16 期 9d533a…b5ca.png 实测验证）：
  - 裁剪锚点 detected 分支命中（横幅底线 2.812%、总代区底 7.143%），
    横幅保留、总代区无残留、主体无丢失；
  - 4 块分界线 detected 命中（x/w 24.28% / 49.37% / 74.86%），
    四块型号+价格列完整，分块无横幅碎片；
- 分块 OCR 链路 + 全量结果：待本地服务恢复后单图实测与人工基准建立；
- 提示词与人工基准（对齐 pricebenchmark/ 模式）**稍后建立**，
  建立前批量提取结果属于未验证数据。

---

## 6. 已知注意事项与边界

1. **OCR 是硬依赖**：提示词以 OCR 为主，任一分块 `ocr_markdown()` 失败即抛
   `RuntimeError`，该图计失败。跑批前确认 llama.cpp 服务
   （llm_config.json 的 ocr_base_url，默认 127.0.0.1:8080）已启动；
2. **缓存不自动失效**：按 1.2 节的影响层表格手动清理——裁剪/分块逻辑变更
   会级联影响下游缓存和结果；
3. **分块无日期信息（设计约定）**：日期仅保留在裁剪图（LLM 输入），
   分块 OCR 文本不含 sheet_date 来源；若横幅锚点检测回退固定比例，
   会打印 stderr 警告，应人工抽查该图裁剪结果；
4. **PNG-only**：管线内格式固定 PNG；目录中的 jpg/jpeg 跳过并提示转换，
   之后不要重新编码（会改变内容 key）；
5. **三条入库路径互不干扰**：load_mem.py（extracted_mem）、load_cpu.py
   （output_cpu/extracted_cpu）、clean_load.py（extracted/）；
6. **并发限制**：默认 2（`--workers N` 可调，上限 6）——OCR/LLM 服务
   共同承压，超限自动钳制；第一次 Ctrl+C 优雅停止（不再提交新图、
   等待在跑任务），第二次强制退出；
7. **配额与安全**：批量提取与导入真实消耗 LLM 配额、传输图片、写库，
   执行前需明确授权；`llm_config.json` 含敏感凭据，严禁外泄；
   库内去重是全库破坏性操作，跨 MEM/CPU 共享库，需人工审阅冲突清单；
8. **失败处理原则**：失败图计入清单、重跑重试，绝不伪造成功，
   也不因 LLM 提取不确定而补造价格——宁可漏掉不确定行，也不跨行/
   跨列/跨块推断。

**超时配置**（llm_config.json）：
- `timeout_seconds: 300` —— LLM 调用超时；
- `ocr_timeout_seconds: 600` —— OCR 调用超时；
- `ocr_max_tokens: 16384` —— OCR 输出上限（过大诱发重复退化）；
- 超时抛异常 → 该图计失败，重跑自动重试。

---

## 7. 常用命令速查

```bash
# 仅语法检查（无副作用）
python -m py_compile database/extract_mem.py database/load_mem.py

# 查看进度（不消耗配额；递归扫描默认目录，含陈旧结果单独计数）
python database/extract_mem.py --status

# 提取整个目录（默认 2 并发；真实调用 OCR + LLM，需 llama.cpp 在线，需授权）
python database/extract_mem.py ../价格图片/mem

# 提取单张
python database/extract_mem.py ../价格图片/mem/9d533ab166da4a14b018bf942670b5ca.png

# 指定并发（上限 6）
python database/extract_mem.py ../价格图片/mem --workers 4

# 自定义输出根目录（结果/裁剪/OCR/日志/manifest 同步切换）
python database/extract_mem.py ../价格图片/mem --out-dir ./my_out

# 提取后自动入库（全量 extracted_mem/，幂等；真实写库 + 全库去重，需授权）
python database/extract_mem.py ../价格图片/mem --db

# 数据导入（人工触发：预验证 + hardware_type 落库 + 幂等入库 + 报告）
python database/load_mem.py
python database/load_mem.py --status
python database/load_mem.py --out-dir ./my_out
```
