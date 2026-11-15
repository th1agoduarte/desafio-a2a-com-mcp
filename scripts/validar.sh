#!/usr/bin/env bash
# Sobe os dois processos do zero, roda o validador do starter e confere no stderr do
# servidor MCP o que o validador não confere sozinho (traceparent, tools/list antes do
# primeiro tools/call do agente, id do retry diferente do inicial). Derruba tudo no fim.
# Saídas em docs/evidencias/: validador.txt, servidor-mcp.stderr.txt, agente.stderr.txt, conferencia-logs.txt
set -uo pipefail
cd "$(dirname "$0")/.."
EVID=docs/evidencias
mkdir -p "$EVID"
: "${REQUEST_STATE_SECRET:?exporte REQUEST_STATE_SECRET (veja o README: python3 -c \"import secrets; print(secrets.token_hex(32))\")}"

for p in 7300 7301; do fuser -k "$p/tcp" >/dev/null 2>&1 || true; done
sleep 0.5
uv run --project servidor-mcp python servidor-mcp/servidor.py 2> "$EVID/servidor-mcp.stderr.txt" &
MCP_PID=$!
uv run --project agente python agente/agente.py 2> "$EVID/agente.stderr.txt" &
AG_PID=$!
trap 'kill $MCP_PID $AG_PID 2>/dev/null; for p in 7300 7301; do fuser -k "$p/tcp" >/dev/null 2>&1 || true; done' EXIT

for _ in $(seq 60); do
  curl -sf -o /dev/null http://localhost:7300/.well-known/agent-card.json && curl -s -o /dev/null http://localhost:7301/mcp && break
  sleep 0.5
done

python3 validador/validar.py --agente http://localhost:7300 --mcp http://localhost:7301 | tee "$EVID/validador.txt"
STATUS=${PIPESTATUS[0]}
echo "exit=$STATUS" >> "$EVID/validador.txt"
sleep 0.3
python3 scripts/conferir_logs.py "$EVID/validador.txt" "$EVID/servidor-mcp.stderr.txt" | tee "$EVID/conferencia-logs.txt"
LOGS=${PIPESTATUS[0]}
echo "validador exit=$STATUS | conferência de logs exit=$LOGS"
[ "$STATUS" -eq 0 ] && [ "$LOGS" -eq 0 ]
