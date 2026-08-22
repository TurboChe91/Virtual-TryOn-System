# 部署指南

以下命令均在 2026-07-26 实际验证（macOS + OrbStack Docker 28；镜像 python:3.12-slim）。

## A. Docker（推荐）

```bash
git clone <repo> lunelle-studio && cd lunelle-studio
cp .env.example .env
vi .env            # 必填: LUNELLE_IMAGE_API_BASE_URL / _KEY / _MODEL；生产再设 LUNELLE_ADMIN_TOKEN

# 有 compose 插件：
docker compose up -d
# 无 compose（纯 docker，与上等价）：
docker build -t lunelle-studio .
docker volume create lunelle-data
docker run -d --name lunelle-studio --restart unless-stopped --env-file .env \
  -e LUNELLE_DATA_DIR=/data -e LUNELLE_HOST=0.0.0.0 -e LUNELLE_ENV=production \
  -v lunelle-data:/data -p 127.0.0.1:8300:8300 lunelle-studio
```

容器启动即自动：创建 /data 子目录 → 应用数据库迁移 → 校验配置（缺项直接退出并打印原因）→ 起服务。

验证：

```bash
docker ps                                   # STATUS 应含 (healthy)
curl -s http://127.0.0.1:8300/health        # {"status":"ok",...}
curl -s http://127.0.0.1:8300/ready         # 全部 checks 为 true
docker logs lunelle-studio                  # JSON 结构化日志
```

升级：`git pull && docker compose build && docker compose up -d`（卷数据保留，迁移自动跑）。
停止：`docker compose down`；连数据一起删除（危险）：`docker compose down -v`。

## B. 裸机（venv + systemd）

```bash
./scripts/setup.sh && vi .env && ./scripts/setup.sh   # 第二次确认 configuration: OK
./scripts/start.sh                                     # 前台运行
```

systemd 单元示例（`/etc/systemd/system/lunelle.service`）：

```ini
[Unit]
Description=穿戴甲视觉生产系统
After=network-online.target
[Service]
WorkingDirectory=/opt/lunelle-studio
ExecStart=/opt/lunelle-studio/.venv/bin/python -m lunelle.cli serve
Restart=always
User=lunelle
[Install]
WantedBy=multi-user.target
```

## C. 反向代理（对外服务必须）

服务默认只监听 127.0.0.1（容器映射也绑 127.0.0.1）。对外通过 Nginx：

```nginx
server {
    listen 443 ssl;
    server_name studio.example.com;
    ssl_certificate     /etc/letsencrypt/live/studio.example.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/studio.example.com/privkey.pem;

    client_max_body_size 12m;          # ≥ LUNELLE_MAX_UPLOAD_MB
    proxy_read_timeout   330s;         # ≥ LUNELLE_REQUEST_TIMEOUT_S

    add_header X-Content-Type-Options nosniff always;
    add_header X-Frame-Options DENY always;
    add_header Referrer-Policy no-referrer always;

    location / {
        proxy_pass http://127.0.0.1:8300;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-For $remote_addr;
    }
}
```

- HTTPS 用 certbot/lets encrypt；HTTP 强制跳转。
- 生产必须设置 `LUNELLE_ADMIN_TOKEN`，写操作要求 `X-Admin-Token` 头。
- CORS：默认未开放跨域（同源管理页无需）；如需第三方前端接入，在反代层按需放行。
- SQLite 无网络端口，不存在数据库端口暴露问题。
- `.env` 权限必须 600（内含真实密钥）。`scripts/setup.sh` 会 chmod，
  `python -m lunelle.cli init` 会在权限过宽时告警。
- 生产启动校验强制项（`config.validate_for_serve`，不满足则拒绝启动）：
  `LUNELLE_ADMIN_TOKEN` 非空、`LUNELLE_DEBUG=0`、`LUNELLE_DAILY_BUDGET_USD > 0`、
  `LUNELLE_ALLOW_PRIVATE_API_HOSTS=0`、镜像 API 走 https、不使用 mock provider。

## D. 全新环境验证记录（2026-07-26）

1. `git archive HEAD` 导出干净工作区（不含 .env/data/venv）
2. `docker build --no-cache` 全新构建成功
3. `cp .env.example .env` + 填入密钥；全新命名卷
4. 启动 → healthy；`/ready` 全绿；数据库从零初始化（0001_init.sql 自动应用）
5. 创建款式 `nail-sage-leaf-001` → 真实 API 生成 grid+wearing 两图成功（人工目检通过）
6. `docker restart` → 任务记录/图片/统计全部保留
7. 坏模型容器复用同一数据卷 → 任务 `config_error` 失败 → 手动重试留痕 → 状态正确
8. 本地路径同样验证：干净副本 `./scripts/setup.sh` + `start.sh` + `/ready` 通过

限制说明：验证在 macOS/OrbStack 上进行；Linux 服务器行为一致性依赖 Docker 标准化，
未在真实 Linux 主机重复（无服务器权限）。本机 compose 二进制损坏，Docker 路径用
纯 docker 命令验证，compose 文件为标准 v2 语法未实测。
