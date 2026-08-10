#!/usr/bin/env bash
# MemOS 自托管部署验证脚本（P0 鉴权加固验收）
# 用法: bash scripts/deploy-verify.sh
set -uo pipefail

BASE="http://127.0.0.1:8000"
MASTER_KEY="mk_70f164dbc4193626a369625c569c28114ae7cb8244522b697ad038138e3b3934"
PASS=0; FAIL=0

check() {
  local name="$1" expected="$2" actual="$3"
  if [ "$expected" = "$actual" ]; then
    echo "✅ $name (got $actual)"
    PASS=$((PASS+1))
  else
    echo "❌ $name: expected $expected, got $actual"
    FAIL=$((FAIL+1))
  fi
}

echo "=== 1. 健康检查（无需鉴权）==="
R=$(curl -s -o /dev/null -w '%{http_code}' "$BASE/health")
check "/health 公开可达" 200 "$R"

echo ""
echo "=== 2. 无 key 访问业务端点 → 401 ==="
R=$(curl -s -o /dev/null -w '%{http_code}' -X POST "$BASE/product/search" -H 'Content-Type: application/json' -d '{"query":"test"}')
check "无 key 调 /product/search" 401 "$R"

echo ""
echo "=== 3. 错误 key → 401 ==="
R=$(curl -s -o /dev/null -w '%{http_code}' -X POST "$BASE/product/search" -H "Authorization: Bearer krlk_wrongkey" -H 'Content-Type: application/json' -d '{"query":"test"}')
check "错误 key" 401 "$R"

echo ""
echo "=== 4. Master key 调业务端点 ==="
R=$(curl -s -o /dev/null -w '%{http_code}' -X POST "$BASE/product/search" -H "Authorization: Bearer $MASTER_KEY" -H 'Content-Type: application/json' -d '{"query":"test","user_id":"test-user"}')
check "master key 调 search（特权放行）" 200 "$R"

echo ""
echo "=== 5. Master key 签发普通 API key ==="
R=$(curl -s -X POST "$BASE/admin/keys" -H "Authorization: Bearer $MASTER_KEY" -H 'Content-Type: application/json' -d '{"user_name":"alice","scopes":["read","write"],"description":"deploy test"}')
echo "  响应: $(echo "$R" | head -c 200)"
API_KEY=$(echo "$R" | python -c "import sys,json; print(json.load(sys.stdin).get('key',''))" 2>/dev/null)
if [ -n "$API_KEY" ]; then
  echo "✅ 签发 API key 成功 (prefix: ${API_KEY:0:12}...)"
  PASS=$((PASS+1))
else
  echo "❌ 签发 API key 失败"
  FAIL=$((FAIL+1))
fi

echo ""
echo "=== 6. 新签发的 key 调业务端点 ==="
if [ -n "$API_KEY" ]; then
  R=$(curl -s -o /dev/null -w '%{http_code}' -X POST "$BASE/product/search" -H "Authorization: Bearer $API_KEY" -H 'Content-Type: application/json' -d '{"query":"test","user_id":"alice"}')
  check "新 key 调 search" 200 "$R"
fi

echo ""
echo "=== 7. 列出 API keys（只返回 prefix，不泄明文）==="
R=$(curl -s -X GET "$BASE/admin/keys" -H "Authorization: Bearer $MASTER_KEY")
echo "  响应: $(echo "$R" | head -c 300)"
if echo "$R" | grep -q "alice" && ! echo "$R" | grep -q "krlk_"; then
  echo "✅ 列表含 alice 且无明文 key"
  PASS=$((PASS+1))
else
  echo "⚠️ 列表检查：$(echo "$R" | head -c 150)"
  FAIL=$((FAIL+1))
fi

echo ""
echo "=== 8. 越权 Cube 访问 → 403（多用户隔离）==="
if [ -n "$API_KEY" ]; then
  R=$(curl -s -o /dev/null -w '%{http_code}' -X POST "$BASE/product/search" -H "Authorization: Bearer $API_KEY" -H 'Content-Type: application/json' -d '{"query":"test","user_id":"alice","readable_cube_ids":["someone-elses-cube"]}')
  check "alice 访问他人 cube" 403 "$R"
  R=$(curl -s -o /dev/null -w '%{http_code}' -X POST "$BASE/product/search" -H "Authorization: Bearer $API_KEY" -H 'Content-Type: application/json' -d '{"query":"test","user_id":"alice","readable_cube_ids":["alice"]}')
  check "alice 访问自己默认 cube" 200 "$R"
fi

echo ""
echo "=== 结果: $PASS 通过, $FAIL 失败 ==="
exit $FAIL
