# 备份与恢复

需要备份的内容 = `LUNELLE_DATA_DIR` 整个目录：

| 内容 | 路径 | 方式 |
|---|---|---|
| 数据库 | `lunelle.db`(+`-wal`) | **必须**用在线备份命令（直接 cp 运行中的库可能不一致） |
| 生成图片 | `outputs/` | 文件级复制（rsync/快照） |
| **输入资产（内容寻址）** | `assets/` | 同上，**必须备份** |
| 上传参考图 | `uploads/` | 同上 |
| 导出产物 | `exports/` | 可重导出，可选 |
| 日志 | `logs/` | 可选 |

`assets/` 是按内容寻址的输入资产库（手模底图等），任务快照按 digest 引用它。
**丢了它，过往任务就无法再解析出自己的输入**——快照仍会诚实地记录 digest，但字节没了。
文件名即哈希，所以它是仅追加的：增量 rsync 永远不会重写已有文件。

## 本地/裸机

```bash
./scripts/backup.sh                       # → data/backups/lunelle-<UTC时间戳>.db（SQLite 在线备份 API，服务无需停）
rsync -a data/outputs/ /backup/outputs/
rsync -a data/assets/  /backup/assets/   # 输入资产：仅追加，增量很快
```

恢复：

```bash
# 停服务
cp /backup/lunelle-20260726-101112.db data/lunelle.db
rm -f data/lunelle.db-wal data/lunelle.db-shm      # 丢弃旧 WAL，备份文件本身是完整一致的
rsync -a /backup/outputs/ data/outputs/
rsync -a /backup/assets/  data/assets/
# 启服务 → /ready 应全绿；抽查 GET /api/tasks/{id}/image
```

## Docker 卷

```bash
# 备份（容器可运行中；数据库用容器内在线备份保证一致性）
docker exec lunelle-studio python -m lunelle.cli backup --dest /data/backups/manual.db
docker run --rm -v lunelle-data:/data -v "$PWD":/dump alpine \
  tar czf /dump/lunelle-data-$(date +%Y%m%d).tgz -C / data

# 恢复到全新卷
docker volume create lunelle-data-restored
docker run --rm -v lunelle-data-restored:/data -v "$PWD":/dump alpine \
  tar xzf /dump/lunelle-data-20260726.tgz -C /
# 然后用 -v lunelle-data-restored:/data 启动容器
```

## 建议策略

- 数据库：每日 `backup.sh` + 保留 14 天（cron 一行即可）
- outputs / assets：对象存储/NAS 每日增量（assets 仅追加，永不重写已有文件）
- 每月做一次恢复演练（新卷/新目录起服务 → /ready → 抽查任务与图片）
