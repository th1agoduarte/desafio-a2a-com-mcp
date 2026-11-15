#!/usr/bin/env bash
# Itens do enunciado que nem o validador nem o fluxo do avaliador exercitam:
#   1-2  o servidor MCP não sobe sem REQUEST_STATE_SECRET ou com menos de 32 bytes
#   3    nenhum segredo hardcoded no código
#   4    header Mcp-Method divergente do corpo é recusado com -32020
#   5    nenhuma dependência de SDK de LLM nos pyproject.toml / uv.lock
#   6    dados/, validador/ e exemplos/ idênticos ao upstream
set -uo pipefail
cd "$(dirname "$0")/.."
falhas=0
ok() { echo "PASS $1"; }
nok() { echo "FAIL $1"; falhas=$((falhas + 1)); }
for p in 7300 7301; do fuser -k "$p/tcp" >/dev/null 2>&1 || true; done

saida=$(env -u REQUEST_STATE_SECRET timeout 30 uv run --project servidor-mcp python servidor-mcp/servidor.py 2>&1 </dev/null); rc=$?
[ $rc -ne 0 ] && grep -q "REQUEST_STATE_SECRET ausente" <<<"$saida" && ok "1 sem REQUEST_STATE_SECRET o servidor não sobe (exit=$rc)" || nok "1 sem REQUEST_STATE_SECRET (exit=$rc)"
saida=$(REQUEST_STATE_SECRET=$(python3 -c "import secrets; print(secrets.token_hex(16))") timeout 30 uv run --project servidor-mcp python servidor-mcp/servidor.py 2>&1 </dev/null); rc=$?
[ $rc -ne 0 ] && grep -q "curta" <<<"$saida" && ok "2 com 16 bytes o servidor não sobe (exit=$rc)" || nok "2 segredo curto (exit=$rc)"

if grep -rnE "token_hex\(32\)\)?\s*$|[0-9a-f]{64}" servidor-mcp/*.py agente/*.py | grep -v "python3 -c" ; then nok "3 possível segredo no código"; else ok "3 nenhum segredo hex de 32+ bytes no código; a chave só vem de os.getenv(\"REQUEST_STATE_SECRET\")"; fi

: "${REQUEST_STATE_SECRET:?exporte REQUEST_STATE_SECRET}"
uv run --project servidor-mcp python servidor-mcp/servidor.py 2>/dev/null &
PID=$!
trap 'kill $PID 2>/dev/null; fuser -k 7301/tcp >/dev/null 2>&1' EXIT
for _ in $(seq 40); do curl -s -o /dev/null http://localhost:7301/mcp && break; sleep 0.3; done
corpo='{"jsonrpc":"2.0","id":1,"method":"tools/list","params":{"_meta":{"io.modelcontextprotocol/protocolVersion":"2026-07-28","io.modelcontextprotocol/clientCapabilities":{}}}}'
resp=$(curl -s -w ' http=%{http_code}' http://localhost:7301/mcp -H 'Content-Type: application/json' -H 'Accept: application/json, text/event-stream' \
  -H 'MCP-Protocol-Version: 2026-07-28' -H 'Mcp-Method: tools/call' -d "$corpo")
grep -q '"code":-32020' <<<"$resp" && ok "4 Mcp-Method divergente do corpo -> $resp" || nok "4 header divergente -> $resp"

if grep -iE '"?(openai|anthropic|google-genai|google-generativeai|langchain|litellm|mistralai|cohere|ollama)' servidor-mcp/pyproject.toml agente/pyproject.toml servidor-mcp/uv.lock agente/uv.lock; then
  nok "5 dependência de SDK de LLM encontrada"; else ok "5 nenhum SDK de LLM nos pyproject.toml nem nos uv.lock"; fi

if git rev-parse -q --verify upstream/main >/dev/null; then
  d=$(git diff --stat upstream/main -- dados validador exemplos); [ -z "$d" ] && ok "6 dados/, validador/ e exemplos/ idênticos a upstream/main" || nok "6 diferenças: $d"
else nok "6 remote upstream ausente"; fi
echo "resumo: $falhas falha(s)"
exit $falhas
