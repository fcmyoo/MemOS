# MemOS 私有部署维护手册（阿樂）

> 本手册说明如何长期维护本仓库：**main 纯上游跟踪** + **私有分支持鉴权改动**。

## 分支结构（当前）

| 分支 | 内容 | 用途 |
|---|---|---|
| `main` | 纯上游代码（8d310a7a = MemTensor/MemOS main） | 只做上游同步，**永不直接提交/推送私有改动** |
| `feat/api-auth-hardening` | main + 鉴权加固（20 文件改动） | 自托管部署分支，含 API key 鉴权、postgres、加固配置 |

## Remote 说明

```text
origin  https://ghfast.top/https://github.com/MemTensor/MemOS.git   ← 上游（代理加速）
fork    https://github.com/fcmyoo/MemOS.git                          ← 你的 fork（备份/部署源）
```

- `main` 跟踪 `origin/main`（上游）
- 私有分支推送到 `fork`（你的 GitHub fork）

## 日常操作

### 1. 跟上上游迭代（重点，每月/不定期执行）

```bash
cd /d/code/invest/MemOS
git checkout feat/api-auth-hardening   # 确保在私有分支
bash scripts/sync-upstream.sh          # 自动：main拉上游 → 合并进私有分支
```

脚本做的事：
1. `git checkout main && git pull origin main`（main 拿到上游最新）
2. `git checkout feat/api-auth-hardening`
3. `git merge main`（把上游改动合进私有分支）

**冲突处理**：若上游改动了鉴权相关文件（auth.py、server_api*.py、compose 等），
合并会冲突。解决原则：**保留我们的鉴权逻辑，采纳上游的非鉴权改动**。逐个文件
解决后 `git add` + `git commit`。

**回归验证**：
```bash
python -m py_compile src/memos/api/middleware/auth.py src/memos/api/server_api*.py
MEMOS_BASE_PATH=/tmp/memos-test PYTHONPATH=src python -m pytest tests/api/ -q
```

### 2. 部署到服务器

```bash
git push fork feat/api-auth-hardening
# 服务器上：git fetch origin feat/api-auth-hardening && git checkout feat/api-auth-hardening
```

### 3. 绝不做的事

- ❌ 在 `main` 上 `git commit` 私有改动（会污染上游同步基线）
- ❌ 把私有分支直接 push 到 `origin`（上游仓库，无权限也不应推）
- ❌ 用 `git push fork main` 覆盖 fork 的 main（除非你想让 fork main 与上游不同步）

## 同步失败排查

| 现象 | 原因 | 处理 |
|---|---|---|
| `git pull origin main` 超时 | ghfast 代理波动 | 重试；或 `git remote set-url origin https://github.com/MemTensor/MemOS.git` 直连 |
| 合并冲突 | 上游改了同文件 | 按"保留鉴权、采纳上游"原则解决 |
| `AUTH_ENABLED` 行为异常 | 上游改动了 auth 逻辑 | diff 对比：`git diff main..HEAD -- src/memos/api/middleware/auth.py` |
