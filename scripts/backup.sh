#!/usr/bin/env bash
# MemOS 数据备份脚本（生产用）
# 用法:
#   bash scripts/backup.sh              # 备份到默认目录 ./backups
#   BACKUP_DIR=/mnt/backup bash scripts/backup.sh
set -euo pipefail

BACKUP_DIR="${BACKUP_DIR:-./backups}"
STAMP="$(date +%Y%m%d_%H%M%S)"
TARGET="${BACKUP_DIR}/memos_${STAMP}"
mkdir -p "$TARGET"

echo "==> 备份目录: $TARGET"

# 1. PostgreSQL（API key 存储）
echo "==> 备份 PostgreSQL..."
docker exec memos-postgres pg_dump -U "${POSTGRES_USER:-memos}" -d "${POSTGRES_DB:-memos}" \
  > "${TARGET}/postgres.sql" 2>/dev/null || echo "    ⚠️  PostgreSQL 备份失败（容器未运行？）"

# 2. Neo4j 数据卷（图记忆）
echo "==> 备份 Neo4j 数据卷..."
if docker ps --format '{{.Names}}' | grep -q '^neo4j-docker$'; then
  docker run --rm -v memos-dev_neo4j_data:/data:ro -v "$(pwd)/${TARGET}":/backup alpine \
    tar czf /backup/neo4j_data.tar.gz -C /data . 2>/dev/null
  echo "    neo4j_data.tar.gz OK"
else
  echo "    ⚠️  Neo4j 容器未运行，跳过"
fi

# 3. Qdrant 数据卷（向量库）
echo "==> 备份 Qdrant 数据卷..."
if docker ps --format '{{.Names}}' | grep -q '^qdrant-docker$'; then
  docker run --rm -v memos-dev_qdrant_data:/qdrant/storage:ro -v "$(pwd)/${TARGET}":/backup alpine \
    tar czf /backup/qdrant_data.tar.gz -C /qdrant/storage . 2>/dev/null
  echo "    qdrant_data.tar.gz OK"
else
  echo "    ⚠️  Qdrant 容器未运行，跳过"
fi

# 4. 压缩整包
echo "==> 压缩..."
cd "$BACKUP_DIR" && tar czf "memos_${STAMP}.tar.gz" "memos_${STAMP}" && rm -rf "memos_${STAMP}"
echo "==> ✅ 备份完成: ${BACKUP_DIR}/memos_${STAMP}.tar.gz"

# 5. 保留最近 N 份，删除旧备份
KEEP="${BACKUP_KEEP:-7}"
echo "==> 清理旧备份（保留 ${KEEP} 份）..."
ls -1t "${BACKUP_DIR}"/memos_*.tar.gz 2>/dev/null | tail -n +$((KEEP + 1)) | xargs -r rm -f
echo "==> 完成。"
