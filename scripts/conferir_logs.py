"""Confere no stderr do servidor MCP os itens do Fluxo do avaliador (passos 5 e 6).

    python3 scripts/conferir_logs.py docs/evidencias/validador.txt docs/evidencias/servidor-mcp.stderr.txt
"""

import re
import sys

saida, stderr = open(sys.argv[1]).read(), open(sys.argv[2]).read().splitlines()
achado = re.search(r"trace-id desta execucao: ([0-9a-f]{32})", saida)
if not achado:
    sys.exit("FAIL saída do validador sem a linha do trace-id")
trace_id = achado.group(1)
linhas = [l for l in stderr if l.startswith("[mcp] ") and " method=" in l]
campos = [dict(re.findall(r"([\w.-]+)=(\S+)", l)) for l in linhas]
# O agente se identifica no _meta (clientInfo); o validador não manda clientInfo.
do_agente = [c for c in campos if c.get("client") == "agente-central-de-salas"]
falhas = 0


def check(ok: bool, texto: str) -> None:
    global falhas
    falhas += not ok
    print(("PASS " if ok else "FAIL ") + texto)


check(bool(do_agente), f"{len(do_agente)} requests do agente (clientInfo agente-central-de-salas) no stderr")
check(bool(campos) and all("method" in c and "id" in c and "traceparent" in c for c in campos), f"todas as {len(campos)} linhas do stderr têm method, id e traceparent")
primeiro_call = next((i for i, c in enumerate(do_agente) if c["method"] == "tools/call"), None)
primeiro_list = next((i for i, c in enumerate(do_agente) if c["method"] == "tools/list"), None)
check(primeiro_list is not None and primeiro_call is not None and primeiro_list < primeiro_call,
      f"tools/list do agente (id={do_agente[primeiro_list]['id'] if primeiro_list is not None else '-'}) antes do primeiro tools/call (id={do_agente[primeiro_call]['id'] if primeiro_call is not None else '-'})")
com_trace = [c for c in do_agente if trace_id in c.get("traceparent", "")]
check(bool(com_trace), f"{len(com_trace)} requests do agente levam o trace-id {trace_id} do validador no traceparent")
spans = {c["traceparent"].split("-")[2] for c in com_trace}
check(len(spans) == len(com_trace), "cada request MCP do agente tem span-id próprio, com o mesmo trace-id")
check(all(c.get("traceparent", "-") != "-" for c in do_agente), "todo request MCP do agente leva traceparent")
check(all(c.get("caps") == '{"elicitation":{"form":{}}}' for c in do_agente),
      'todo request MCP do agente declara clientCapabilities = {"elicitation":{"form":{}}}')
espelho = all(
    c.get("h.mcp-method") == c["method"] and c.get("h.mcp-protocol-version") == "2026-07-28"
    and (c.get("h.mcp-name") == (c.get("name") or c.get("uri")) if c["method"] in ("tools/call", "resources/read") else True)
    for c in do_agente
)
check(espelho, "todo request MCP do agente leva MCP-Protocol-Version, Mcp-Method e (tools/call, resources/read) Mcp-Name iguais ao corpo")
pares = []
for i, c in enumerate(do_agente):
    if c.get("retry") == "sim":
        inicial = next((d for d in reversed(do_agente[:i]) if d["method"] == "tools/call" and d.get("retry") != "sim"
                        and d["traceparent"].split("-")[1] == c["traceparent"].split("-")[1]), None)
        if inicial:
            pares.append((inicial["id"], c["id"]))
check(bool(pares) and all(a != b for a, b in pares), f"retries com id diferente do request inicial (inicial->retry): {pares}")
print(f"resumo: {falhas} falha(s)")
sys.exit(1 if falhas else 0)
