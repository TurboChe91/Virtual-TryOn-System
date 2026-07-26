# 项目当前状态（2026-07-26）

## 一句话

核心链路（款式→双图真实生成→质检→管理→导出）已实现并经真实 API 与全新 Docker
环境验证；单机生产可用，视觉终审保留人工环节。

## 已实现并验证 ✅

- 款式结构化：结构化字段 / 中英自然语言（确定性词表）/ LLM 可选（强校验+回退）
- 提示词 pv-2：网格 2×5 合同、佩戴场景锁、身份块、负向词；版本随任务持久化
- 真实生成：wisech 聚合端点 + doubao-seedream-4-5；grid→wearing 参考图一致性机制生效
- 供应商水印关闭（watermark:false）；提示词标题渲染问题已修（pv-2）
- 任务状态机/幂等/退避重试/崩溃恢复/手动重试留痕 —— 单元+集成+真实环境全验证
- QA：8 项确定性检查 + 自适应 2×5 计数（3 张真实图全部正确识别 10 枚）
- 导出：webp + manifest + products.csv + generation-report.csv（真实数据验证）
- 全新环境：git archive 干净源 → --no-cache 构建 → 新卷新库 → 真实双图 → 重启数据保留
- 质量门：pytest 119 通过 / ruff 0 / mypy 0

## 已实现未完全验证 ⚠️

- docker-compose.yml：标准语法但本机 compose 二进制损坏，用等价纯 docker 命令验证
- Linux 裸机部署：仅在 macOS 验证（Docker 路径可视为跨平台等价）
- LLM 款式解析走真实 chat 端点：单测覆盖协议与回退，未做真实调用（避免额外成本；
  开启方式 LUNELLE_TEXT_MODEL=doubao-seed-2-0-mini-260215）

## 未实现 ❌

- 参考图驱动的款式识别（上传图→自动 StyleSpec）：上传与存储已就绪，识别未接
- 多肤色×多视角矩阵（前身项目的 4×4 matrix）：当前每款一张佩戴图（skin_tone 可选）
- 视觉级自动 QA（水印 OCR、手部关键点）：设计为人工复核项

## 外部阻塞

- 无。密钥可用（wisech）。若切官方 OpenAI 需自备 gpt-image key。

## 下一步建议（按价值排序）

1. 参考图→StyleSpec（用 doubao-seed-2.0 视觉模型，产出仍过 StyleSpec 校验）
2. 佩戴图多版本挑选（一次 n>1 出图 + 管理页勾选终稿）
3. Shopify Admin API 直传（当前为 CSV+文件包人工上传）
