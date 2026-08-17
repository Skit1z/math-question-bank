# 结构化来源系统与通用教辅导入 CLI 设计

日期：2026-08-17
状态：已获用户批准（关键决策经问答确认）

## 目标

1. 题目来源从自由文本升级为**独立实体**（来源库），UI 可管理（增删改查）、编辑器可选择、侧栏可筛选。
2. 提供**通用**的教辅/真题 PDF 导入 CLI（`scripts/import_paper_book.py`），以 profile 描述书册结构，分阶段执行 ocr → build → import → report。
3. 首个应用：导入李林 880 数一高数篇（做题本 98 页 + 解析分册高数部分），解析来自解析分册并按题匹配。

## 已确认的关键决策

| 决策点 | 结论 |
|--------|------|
| 题号语义 | 保留**书内原始编号**（章内×题型内从 1 重新计数），显示带完整定位 |
| 来源粒度 | **每篇/每卷一条来源**（880基础篇、880综合篇、1991数一 各一条） |
| 旧字段 | **彻底替换**：迁移后删除 `questions.source` 文本列 |
| UI 范围 | 管理（设置内来源库控制台）+ 选择（编辑器）+ 筛选（侧栏）一步到位 |
| 识别引擎 | PaddleOCR-VL-1.6（官方托管 API），页面级缓存断点续跑 |
| 分类 | 章→考点固定映射（K 大纲数学一·高等数学 9 考点与 880 高数 9 章一一对应），零 AI 调用 |

## 数据模型（schema v4）

新表 `sources`：

```sql
CREATE TABLE sources (
    id INTEGER NOT NULL PRIMARY KEY,
    name VARCHAR(100) NOT NULL UNIQUE,   -- 显示名：880基础篇 / 1991数一
    series VARCHAR(100) DEFAULT '',      -- 系列：李林880 / 考研数学真题（可选，用于聚合）
    note TEXT DEFAULT '',
    created_at DATETIME
);
```

`questions` 表改造（表重建迁移 v3→v4，快照优先、单事务、user_version=4）：

- 删除 `source VARCHAR(200)` 列；
- 新增 `source_id INTEGER REFERENCES sources(id)`（可空、索引，删除来源时被引用则拒绝）；
- 新增 `source_number INTEGER`（书内原始题号/卷内题号，可空）；
- 新增 `source_scope VARCHAR(100) DEFAULT ''`（编号作用域，如「第二章·选择」，真题卷为空）。

存量数据迁移：v3 库中每个非空 `source` 字符串创建一条来源记录并回填 `source_id`；空串置空。

显示标签由后端统一计算：`source_label = 名称[·作用域](题号)`，
如 `880基础篇·第二章·选择(10)`、`1991数一(8)`、`880基础篇`（无题号）。

## API

- `GET /api/sources`：列表 + 引用计数（读）。
- `POST /api/sources` / `PUT /api/sources/{id}` / `DELETE /api/sources/{id}`：需 `X-Local-Token`；重名 409；被题目引用时删除 409 并返回引用数。
- `POST/PUT /api/questions`：`source` Form 参数替换为 `source_id` / `source_number` / `source_scope`（空串→NULL，source_id 须存在）。
- `GET /api/questions`：新增 `source_id` 与 `series` 筛选参数（与分页/排序契约兼容）。
- 序列化（to_dict/to_summary_dict）：输出 `source_id/source_number/source_scope/source_label`，不再输出 `source`。
- JSON 同步导出与 `questions_library.md` 同步替换为结构化字段 + `source_label`。

## 前端（纯 HTML/原生 JS，无编译）

- **编辑器**：原「来源」文本框替换为 来源下拉（/api/sources，含"无来源"）+ 题号输入 + 作用域输入；保存载荷改结构化三字段。
- **题库卡片/列表**：显示 `source_label`。
- **侧栏**：新增来源筛选下拉（全部 + 来源列表，含系列分组显示）。
- **设置齿轮**：新增「来源库」控制台（弹窗）：列表（名称/系列/备注/引用数）+ 增改删；删除被引用来源给出高对比错误反馈。
- 遵循现有规范：`MathBankSafe`/DOMPurify 净化、Tooltip、a11y（焦点陷阱、Esc 关闭）、375px 可用。

## CLI（scripts/import_paper_book.py）

- `ocr`：已实现。PyMuPDF 150DPI 渲染 → PaddleOCR-VL-1.6（并发 4、重试 2）→ 逐页 Markdown 缓存于 `.system_generated/book_import/ocr/<key>/pXXXX.md`；`--stop-marker` 在合订分册中截取单科范围（目录点线行防误判）。
- `build --profile <json>`：解析两侧 Markdown 流为题目与解析，按（章，难度块，题型，题号）四元组匹配；产出 `questions.json` + 对账报告（两侧题数、未匹配清单、解析异常）。
  - 选择题选项 → `\begin{choices}\item` 环境；题干尾部空括号清洗；水印/页眉页脚过滤；`\fillin` 规范化（复用 main.py 同款规则）。
  - 难度：基础→basic / 综合→comprehensive / 拓展→advanced。
- `import --profile <json> [--apply]`：默认 dry-run；确保来源记录存在（按 name 幂等创建）；按（source_id, source_scope, source_number）幂等去重；逐题一事务写 `questions` + K 版分类镜像；完成后刷新 JSON 同步导出。
- `report`：插图题清单（供 PDF 手动截图补图）、匹配失败清单、抽查样本。
- profile 示例 `scripts/profiles/880-math1-gaoshu.json`：PDF 路径/缓存键/页范围、难度块→来源与难度映射、题型→question_type 映射、章→考点映射。

## 错误处理与边界

- OCR 单页失败不阻断（不落盘，重跑补齐）；`completed/error` 语义与日志 ASCII 安全。
- 解析守恒校验：每个（章，块，题型）做题本与解析分册题数对账，缺漏进报告而非静默丢弃。
- 编号回退检测（如 (5) 出现在 (10) 后）标记 OCR 疑似丢标题，进报告。
- 服务器运行期迁移：版本化迁移在启动/CLI 首次访问时执行，快照优先；本次线上库 0 题，另做手工完整备份兜底。
- 插图题（做题本仅 8 页含图）导入文本不含图，报告列出，走系统既有 manual-crop 补图。

## 测试与验证

- `tests/test_sources.py`：来源 CRUD、token 守卫、删除保护、题目结构化来源写入/更新/筛选、label 格式。
- `tests/test_database_migrations.py`：v3→v4 迁移（source 字符串→来源记录回填、列删除、外键/索引校验）。
- `tests/test_import_paper_book.py`：标题状态机、选项转 choices、括号清洗、水印过滤、四元组匹配、作用域标签、幂等跳过、边界检测。
- 全量：`python3 -m pytest tests/`、`node --check static/js/*.js`、`python3 -m pip check`。
- 完成后按 AGENTS.md 单一来源规则同步更新根目录 AGENTS.md。
