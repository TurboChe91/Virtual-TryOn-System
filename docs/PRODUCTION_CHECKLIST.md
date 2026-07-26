# 上线检查清单

## 部署前

- [ ] `.env` 从 `.env.example` 复制并填写；`LUNELLE_ENV=production`
- [ ] `LUNELLE_ADMIN_TOKEN` 已设置为强随机值（如 `openssl rand -hex 24`）
- [ ] `LUNELLE_DEBUG=0`
- [ ] 图片 API 密钥有效且额度充足（`python -m lunelle.cli init` 显示 configuration: OK）
- [ ] `LUNELLE_MAX_CONCURRENCY` 与供应商限流匹配（默认 2 保守可用）
- [ ] `LUNELLE_PRICING_JSON` 按实际供应商单价校准（成本统计准确性）
- [ ] Docker 卷 / 数据目录规划在有足够空间的磁盘（每张 2048px 图约 0.7-1MB）
- [ ] 反向代理 + HTTPS 配置完成；服务本体不直接暴露公网
- [ ] 备份 cron 就位（`scripts/backup.sh` + outputs rsync）

## 部署后验证（每次发版都做）

- [ ] `docker ps` healthy / `curl /health` 200
- [ ] `curl /ready` 全部 checks=true
- [ ] 创建一个测试款式 → 生成两图 → 管理页可见图片
- [ ] 重启服务 → 任务与图片仍在
- [ ] `GET /api/stats` 数字与预期一致
- [ ] 日志无 ERROR（`grep '"level": "ERROR"' logs/lunelle.log`）
- [ ] 无 token 的写请求返回 401

## 业务运行

- [ ] 每批生成后人工过一遍 QA 标记（needs_human_review 项）再导出
- [ ] 导出 zip 交付前抽查 manifest.json 与图片对应关系
- [ ] 每周看一次 /api/stats 的失败分布与成本
- [ ] 每月恢复演练一次（见 BACKUP_AND_RECOVERY.md）

## 已知不做自动化的事

- 视觉终审（水印/畸形/款式还原）→ 人工
- 多机水平扩展 → 当前单机设计（见 ARCHITECTURE.md 演进路径）
