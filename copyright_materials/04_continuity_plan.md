# 穿戴甲AIGC商品视觉生产管理系统软著源码连续性方案

软件简称：穿戴甲视觉生产系统  
版本号：V1.0

> **第二轮替代声明：以下正文保留为第一轮连续性设计记录，不再用于最终排版。** 第二轮已将客户部署适配文件移到完整候选中部；当前前 30 页为候选物理行 1–1500，后 30 页为 10976–12475，目标客户标识扫描均为 0。最终方案见 `../copyright_materials_v2/final_deposit_plan.md`，逐项命中见 `../copyright_materials_v2/brand_scan.md`。

## 1. 第一轮设计目标（存档）

本方案把 49 个真实首方文件组织为一套可解释的连续程序，而不是从几十个文件各摘少量代码。完整候选含 12,871 行原始源码，按每页 50 行估算约 258 页；计入路径标记和分隔空行的候选文本为 13,019 行，约 261 页。实际页数取决于最终字号、页眉、页脚和登记机构排版要求。

普通交存排版时应坚持以下原则：

- 固定使用 `03_source_manifest.md` 的顺序。
- 每个文件从第一行开始连续排到最后一行，不在文件内部跳行。
- 前 30 页从候选第一行起连续取；后 30 页从完整候选末尾反向确定起点后连续取到结尾。
- 文件路径标记只说明来源，不用于凑行数。
- 不把测试、Mock、依赖库、HTML/CSS 模板或 `.env` 混入页数。

## 2. 推荐文件排列顺序

### A. 请求入口与业务建模

1. `server.py`
2. `schemas.py`
3. `styles.py`
4. `vocab.py`
5. `prompts.py`

这部分从 HTTP 请求、文件上传和权限校验开始，进入款式/SKU 输入模型、受控词汇与 Prompt 构造。阅读者可以看到系统如何把用户输入转化为可信业务数据和 AI 请求文本。

### B. 任务创建、状态与执行

6. `tasks.py`
7. `models.py`
8. `policy.py`
9. `budget.py`
10. `worker.py`

这部分形成核心调用链：创建批次和任务、计算幂等键、固化依赖、应用状态机与预算，然后由 Worker 领取、执行、记录和失败处理。

### C. AI 通道

11. `providers/base.py`
12. `providers/openai_compat.py`
13. `providers/__init__.py`
14. `profiles.py`
15. `llm.py`

这部分展示 Worker 如何解析当前通道、构造兼容式图片/Chat 请求、发送参考图、解析响应和分类错误；源码中只有参数和字段名，没有真实凭证值。

### D. 输入、素材与 QA

16. `inputs.py`
17. `snapshots.py`
18. `assets.py`
19. `splits.py`
20. `nailslots.py`
21. `planview.py`
22. `geometry.py`
23. `qa.py`
24. `gating.py`

这部分解释生成任务如何冻结真实素材、在调用边界复核实际输入、把款式图切成十甲、把十甲映射到手模几何位置，并对结果进行自动检查和人工发布门禁。

### E. 运行保障

25. `config.py`
26. `urlguard.py`
27. `logging_setup.py`
28. `build.py`
29. `errors.py`

配置、安全、日志脱敏、构建指纹和业务异常支撑主链，但不抢占主要业务逻辑的位置。

### F. 结果管理、发布与持久层

30. `stats.py`
31. `export.py`
32. `publish.py`
33. `cloudflare.py`
34. `db.py`
35–49. `migrations/0001` 至 `0015`

这部分从结果统计、本地交付进入远端对象存储发布，最后落到 SQLite 事务和完整数据表演进。它非常适合作为候选程序末段，因为结尾不是零散工具，而是对整套业务状态的持久化定义。

## 3. 为什么构成连续程序逻辑

```text
API Route
  → Schema / Style / SKU
  → Prompt
  → TaskService / State Machine / Budget
  → SQLite Queue
  → Worker
  → AI Provider / LLM
  → Input Snapshot / Asset / Split / Nail Slot
  → QA / Human Gate
  → Stats / Export / R2+D1 Publish
  → Database / Migrations
```

相邻模块之间存在真实 import、函数调用或数据库契约关系。例如 `server.py` 调用 `TaskService.create_generation`；TaskService 调用 Prompt、Snapshot、Asset 和 Budget；Worker 调用 `claim_next`、Provider、QA；Publish 和 Export 共用 Gate；这些服务最终依赖 `db.py` 和迁移表。因此该顺序可以解释为同一软件从入口到持久化和交付的连续源程序。

## 4. 预计总代码行数

- 原始源码：12,871 行。
- 路径标记和分隔空行：148 行。
- 候选文本总计：13,019 行。
- 按每页 50 行粗估：原始源码约 258 页；候选文本约 261 页。
- 项目核心运行包（包括未进入候选的 CLI、Mock、Web 页面和 JSON 契约）约 14,963 行。

## 5. 前 30 页建议

若按“每页 50 行原始程序行”排版，前 30 页需要 1,500 行源码，建议连续使用：

- `server.py` 全部 1,070 行；
- `schemas.py` 全部 277 行；
- `styles.py` 第 1–153 行。

在候选文本中的覆盖范围约为第 1–1508 行（其中含 3 个文件路径标记和分隔空行）。这 30 页从真实 FastAPI 入口开始，完整展示 54 个 API 路由所在文件、全部输入模型，并进入款式/SKU 解析逻辑，业务识别度高。

最终排版时应把文件路径放在程序页眉或文件首行，并确保每页实际程序行数符合受理要求；不要简单按候选文本每 50 个物理行分页，因为空行和路径标记会占行。

## 6. 后 30 页建议

完整候选最后 1,500 行原始源码恰好由以下连续内容组成：

- `publish.py` 第 6–404 行，共 399 行；
- `cloudflare.py` 全部 162 行；
- `db.py` 全部 197 行；
- `migrations/0001` 至 `0015` 全部 742 行。

合计 1,500 行。候选文本中的覆盖范围约为第 11467–13017 行，期间包含正常文件路径标记和分隔空行。

该末段从审核通过结果的版本化发布继续到 R2/D1 API 封装、SQLite 事务和全部数据库表，形成“Result → Storage → Database”的完整结尾。它同时覆盖任务、QA、素材、谱系、预算、发布版本、切图和候选选择的数据结构，适合作为后 30 页。

## 7. 排版前检查

1. 由开发者确认登记机构当期对页眉、页码、字体、字号、空行和每页行数的具体要求。
2. 最终打印前再次扫描真实凭证特征。
3. 检查每页顶部的名称、版本号与申请表一致。
4. 将 `FINGLOW`、`api.finglow.cn` 和 `Lunelle Nails` 作为客户项目历史部署标识处理，避免进入普通交存的前后 30 页；`lunelle` / `Lunelle Studio` 仅按源码内部历史技术标识如实记录。
5. 确认最后一页确实是候选固定顺序的末尾，不要为了视觉整齐重排迁移。
6. 保存最终 PDF 的行号映射，以便证明每一页来自哪个真实文件。
