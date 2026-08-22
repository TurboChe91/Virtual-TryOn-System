# 穿戴甲AIGC商品视觉生产管理系统软著源码候选清单

软件简称：穿戴甲视觉生产系统  
版本号：V1.0

> **第二轮状态说明：本文件记录的是第一轮原样候选，现仅作为审计存档，不再作为普通交存排版依据。** 当前提交净化副本、完整文件顺序和精确前后 30 页边界，以 `../copyright_materials_v2/source_submission_candidate.txt`、`../copyright_materials_v2/final_deposit_plan.md` 和 `../copyright_materials_v2/brand_scan.md` 为准。

## 1. 第一轮候选版本说明（已被第二轮方案替代）

候选文件：`03_source_candidate.txt`  
生成依据：当前工作区中的真实首方源码  
原始源码行数：12,871 行  
候选文本总行数：13,019 行（包含 49 个文件路径标记和文件间空行）  
处理方式：未改写业务逻辑，未格式化源文件，未删除源码行；仅在每个文件前加入 `# File:` 或 `-- File:` 路径标记。

候选明确排除了 `.env`、`data/`、测试、探测脚本、Mock Provider、HTML/CSS 页面模板、部署配置、文档、图片和第三方依赖源码。

## 2. 排列顺序与准确行号

下表的“候选起止行”只覆盖该文件的原始内容，不含路径标记和文件间空行。

| 顺序 | 真实文件 | 原文件行数 | 候选起止行 |
|---:|---|---:|---:|
| 1 | `lunelle/server.py` | 1070 | 3–1072 |
| 2 | `lunelle/schemas.py` | 277 | 1076–1352 |
| 3 | `lunelle/styles.py` | 270 | 1356–1625 |
| 4 | `lunelle/vocab.py` | 212 | 1629–1840 |
| 5 | `lunelle/prompts.py` | 479 | 1844–2322 |
| 6 | `lunelle/tasks.py` | 2114 | 2326–4439 |
| 7 | `lunelle/models.py` | 211 | 4443–4653 |
| 8 | `lunelle/policy.py` | 51 | 4657–4707 |
| 9 | `lunelle/budget.py` | 534 | 4711–5244 |
| 10 | `lunelle/worker.py` | 910 | 5248–6157 |
| 11 | `lunelle/providers/base.py` | 51 | 6161–6211 |
| 12 | `lunelle/providers/openai_compat.py` | 296 | 6215–6510 |
| 13 | `lunelle/providers/__init__.py` | 82 | 6514–6595 |
| 14 | `lunelle/profiles.py` | 236 | 6599–6834 |
| 15 | `lunelle/llm.py` | 437 | 6838–7274 |
| 16 | `lunelle/inputs.py` | 449 | 7278–7726 |
| 17 | `lunelle/snapshots.py` | 411 | 7730–8140 |
| 18 | `lunelle/assets.py` | 239 | 8144–8382 |
| 19 | `lunelle/splits.py` | 410 | 8386–8795 |
| 20 | `lunelle/nailslots.py` | 250 | 8799–9048 |
| 21 | `lunelle/planview.py` | 427 | 9052–9478 |
| 22 | `lunelle/geometry.py` | 77 | 9482–9558 |
| 23 | `lunelle/qa.py` | 365 | 9562–9926 |
| 24 | `lunelle/gating.py` | 99 | 9930–10028 |
| 25 | `lunelle/config.py` | 314 | 10032–10345 |
| 26 | `lunelle/urlguard.py` | 188 | 10349–10536 |
| 27 | `lunelle/logging_setup.py` | 102 | 10540–10641 |
| 28 | `lunelle/build.py` | 440 | 10645–11084 |
| 29 | `lunelle/errors.py` | 21 | 11088–11108 |
| 30 | `lunelle/stats.py` | 90 | 11112–11201 |
| 31 | `lunelle/export.py` | 254 | 11205–11458 |
| 32 | `lunelle/publish.py` | 404 | 11462–11865 |
| 33 | `lunelle/cloudflare.py` | 162 | 11869–12030 |
| 34 | `lunelle/db.py` | 197 | 12034–12230 |
| 35 | `lunelle/migrations/0001_init.sql` | 90 | 12234–12323 |
| 36 | `lunelle/migrations/0002_profiles_hero.sql` | 71 | 12327–12397 |
| 37 | `lunelle/migrations/0003_llm_settings.sql` | 10 | 12401–12410 |
| 38 | `lunelle/migrations/0004_tryon_publish.sql` | 7 | 12414–12420 |
| 39 | `lunelle/migrations/0005_review_closure.sql` | 68 | 12424–12491 |
| 40 | `lunelle/migrations/0006_lineage_and_reservations.sql` | 78 | 12495–12572 |
| 41 | `lunelle/migrations/0007_snapshots_and_assets.sql` | 62 | 12576–12637 |
| 42 | `lunelle/migrations/0008_lineage_and_dependencies.sql` | 43 | 12641–12683 |
| 43 | `lunelle/migrations/0009_publish_versions.sql` | 51 | 12687–12737 |
| 44 | `lunelle/migrations/0010_nail_slots.sql` | 68 | 12741–12808 |
| 45 | `lunelle/migrations/0011_identity_provenance.sql` | 27 | 12812–12838 |
| 46 | `lunelle/migrations/0012_build_identity.sql` | 58 | 12842–12899 |
| 47 | `lunelle/migrations/0013_execution_inputs.sql` | 54 | 12903–12956 |
| 48 | `lunelle/migrations/0014_crop_revisions.sql` | 41 | 12960–13000 |
| 49 | `lunelle/migrations/0015_candidate_selections.sql` | 14 | 13004–13017 |

## 3. 排列逻辑

1. 先放 API Route，使阅读者从真实业务入口进入。
2. 接着是输入 Schema、款式结构化、业务词表和 Prompt。
3. 再进入 TaskService、状态机、策略、预算和 Worker，形成完整异步执行主链。
4. 随后放 AI Provider、运行时通道和多模态 LLM 封装。
5. 然后放实际输入清单、快照、素材、切图、逐甲槽位、视图计划和 QA。
6. 再放配置、安全、日志和构建追溯等运行保障。
7. 末段依次是统计、结果导出、R2/D1 发布、Cloudflare 客户端、SQLite 数据层和全部迁移，形成结果交付与数据模型闭环。

## 4. 完整性与限制

- 该文本是“申请候选材料”，不是可独立运行的源码包；原仓库仍是唯一真实开发源码。
- 为降低 Mock 和 UI 样板占比，候选没有包含 `providers/mock.py` 和 `web/*.html`，因此不以单文件文本直接运行作为目标。
- Prompt、历史迁移注释、品牌文字、公开域名及通道字段仍保持真实源码原貌，正式排版前应按 `07_submission_risk_audit.md` 再人工审核。
- 若最终只交存前后各 30 页，应从该固定顺序取连续前段和连续末段，不应重排或从中间零散摘行。
