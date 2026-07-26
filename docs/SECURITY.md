# 安全说明

## 密钥管理

- 密钥仅从环境变量/.env 读取（`config.py` 是唯一入口）；`.env` 在 `.gitignore`。
- 日志层全局脱敏：`sk-...` 与 `Bearer ...` 模式自动替换为 `<redacted>`（logging_setup.redact，含异常堆栈）。
- 日志/接口/统计中只出现密钥指纹（sha256 前 8 位），不可逆。
- 启动校验会拒绝 `.env.example` 的占位密钥。
- **历史事项**：前身项目 `Lunelle_Nails_fork_polish-contract/scripts/run_seedance_video.py`
  曾硬编码一枚 wisech API key（已于 2026-07-26 从源码移除；该目录非 git 仓库，无历史泄露面，
  但该 key 曾长期存在于本地磁盘，建议轮换）。本仓库从未包含真实密钥。

## 接口安全

- 写操作（创建款式/生成/重试/取消/导出/上传/重跑 QA）在设置 `LUNELLE_ADMIN_TOKEN`
  后要求 `X-Admin-Token` 头，恒定时间比较（hmac.compare_digest）。生产必设。
- 读接口无敏感数据（无密钥、无用户隐私）。
- 无默认账号/密码、无测试后门、无无保护删除接口（系统根本没有删除接口 —— 资产不可变，重生成走新版本）。
- 重试有状态守卫：success 任务不可重试/取消；重复生成默认幂等去重，force 是显式动作。

## 输入与文件安全

- 所有 API 入参过 pydantic Schema（extra="forbid"、长度/数量上限、enum 校验）。
- 上传：≤ `LUNELLE_MAX_UPLOAD_MB`、Pillow verify 魔数校验、仅 PNG/JPEG/WEBP；
  存储文件名由服务端生成（`ref-<sha256前12>.<ext>`），用户文件名不落盘 → 无路径穿越。
- 图片下载（供应商返回 URL 时）：仅 https、50MB 上限、流式读取。
- 出图接口回文件前校验路径 resolve 后仍在 `LUNELLE_OUTPUT_DIR` 内。

## 运行安全

- 生产 `LUNELLE_DEBUG=0`（validate_for_serve 强制）；500 响应只含 error_id，堆栈仅入日志。
- `/docs` (OpenAPI UI) 在 production 关闭。
- Docker 以非 root 用户（uid 10001）运行；服务默认绑定 127.0.0.1，公网必须经反代+HTTPS。
- 依赖全部 `==` 锁定（requirements.txt / requirements-dev.txt），无未记录全局依赖。

## 合规提示

`LUNELLE_DISABLE_PROVIDER_WATERMARK=1`（默认）会请求供应商去掉「AI生成」角标。
部分司法辖区/平台要求对 AI 生成内容进行标识 —— 面向消费者发布前请确认贵司所在
市场的标识义务，必要时设为 0 或在商品页自行披露。
