# Validação dos critérios de aceite: passo a passo

Roteiro para conferir cada critério do [enunciado](DESAFIO.md) a partir de um clone limpo. Pré-requisitos: `python3` ≥ 3.10, `uv`, `curl` e o remote `upstream` apontando para o starter:

```bash
git remote add upstream https://github.com/devfullcycle/desafio-a2a-com-mcp.git && git fetch upstream
```

## 0. Tudo de uma vez

| # | Comando | Esperado |
|---|---|---|
| 0.1 | `make setup` | `✓ .env criado com REQUEST_STATE_SECRET novo (32 bytes)`, `✓ servidor-mcp/.venv`, `✓ agente/.venv` |
| 0.2 | `make test` | Validador `36 passaram, 0 falharam` (exit 0); conferência de logs `0 falha(s)`; fluxo do avaliador `0 falha(s)`; extras `0 falha(s)`. Saídas em `docs/evidencias/` |

Os passos abaixo detalham cada critério e o comando que o comprova.

## 1. Servidor MCP

| Critério | Como conferir | Esperado |
|---|---|---|
| Porta 7301, Streamable HTTP, `tools/list` com 3 tools | validador 01 | `PASS 01` |
| `inputSchema` de objeto em toda tool | validador 02 | `PASS 02` |
| `listar_salas` com `structuredContent` conforme `outputSchema` e o mesmo JSON em texto | validador 03 | `PASS 03` |
| `_meta` sem `protocolVersion` ou sem `clientCapabilities` → -32602, HTTP 400 | validador 04 e 05 | `PASS 04`, `PASS 05` |
| tool inexistente recusada | validador 06 | `PASS 06` |
| `politica://uso` devolve a política; URI inexistente → -32602 | validador 07 e 08 | `PASS 07`, `PASS 08` |
| Sala inexistente, janela, duração, intervalo invertido: `isError` com a mensagem exata | validador 09 a 12 | `PASS 09` a `PASS 12` |
| stderr registra método, id e traceparent de cada request | `docs/evidencias/conferencia-logs.txt` (gerado por `make validar`) | `PASS todas as N linhas do stderr têm method, id e traceparent` |

## 2. MRTR

| Critério | Como conferir | Esperado |
|---|---|---|
| Conflito → `input_required`, uma entrada em form mode, `requestState` presente | validador 13 | `PASS 13` |
| `requestedSchema` plano, enum na ordem da regra | validador 14; `make fluxo`, passo 7 | `PASS 14`; `alternativas: sala-fusca, sala-mirante` |
| Sem form elicitation → -32021 com `data.requiredCapabilities`, HTTP 400 | validador 15; `make fluxo`, passo 13 (corpo de `exemplos/wire/06`) | `PASS 15`; `http=400 error={"code": -32021, ...}` |
| Retry com `inputResponses` e `requestState` conclui com `complete` | validador 16 | `PASS 16` |
| `requestState` adulterado → -32602 | validador 17; `make fluxo`, passo 11 (um caractere trocado no meio) | `PASS 17`; `http=400 error={"code": -32602, ...}` |
| Argumentos adulterados no retry não tomam efeito | validador 18 | `PASS 18` (o boundary rejeita: digest dos argumentos não bate) |
| Retry válido depois de reiniciar o servidor MCP | `make fluxo`, passo 12 | `PASS passo 12: processo novo=True http=200 structuredContent={... "reservado": true, "sala": "sala-fusca" ...}` |
| Chave de `REQUEST_STATE_SECRET`, ≥ 32 bytes, sem segredo no código | `make extras`, itens 1 a 3 | servidor sai com erro sem a variável e com 16 bytes; nenhum hex de 32+ bytes no código |
| `decline` conclui sem reservar e sem `isError` | validador 19 | `PASS 19` |
| Sem alternativa → `isError` com a mensagem exata | validador 20 | `PASS 20` |
| Expiração entre 5 e 30 min | `grep -n "TTL_REQUEST_STATE =" servidor-mcp/servidor.py` | `600` segundos (10 min), passado a `RequestStateSecurity(ttl=...)` |

## 3. Agente como host MCP

| Critério | Como conferir | Esperado |
|---|---|---|
| `tools/list` antes do primeiro `tools/call` do agente | `conferencia-logs.txt` | `PASS tools/list do agente (id=1) antes do primeiro tools/call (id=3)` |
| traceparent com o trace-id do validador | `conferencia-logs.txt`; ou `grep <trace-id> docs/evidencias/servidor-mcp.stderr.txt` com o valor da 1ª linha de `validador.txt` | `PASS N requests do agente levam o trace-id ...` |
| Capability de elicitation em form mode em todo request | `conferencia-logs.txt` | `PASS todo request MCP do agente declara clientCapabilities = {"elicitation":{"form":{}}}` |
| Headers `MCP-Protocol-Version`, `Mcp-Method`, `Mcp-Name` espelhando o corpo; divergência → -32020 | `conferencia-logs.txt`; `make extras`, item 4 | `PASS todo request MCP do agente leva ...`; `-32020 ... http=400` |
| `isError` → Task `FAILED` com a mensagem exata no histórico | validador 35; `make fluxo`, passo 10 | `PASS 35`; `Sala inexistente: sala-inexistente` |

## 4. Agente como servidor A2A

| Critério | Como conferir | Esperado |
|---|---|---|
| Card v1.0 em `/.well-known/agent-card.json` | `curl -s localhost:7300/.well-known/agent-card.json`; validador 21 a 23; `make fluxo`, passo 3 | `supportedInterfaces[0]` = `{"url": ".../a2a", "protocolBinding": "JSONRPC", "protocolVersion": "1.0"}`, skill `reservar-sala` |
| Sala livre → `COMPLETED` com artifact `reserva` e `politica` | validador 24 e 25 | `PASS 24`, `PASS 25` |
| `GetTask` com id, contextId, estado e artifact | validador 26; `make fluxo`, passo 8 | `PASS 26`; `estado=TASK_STATE_COMPLETED artifact={... "politica": "2026-11-01"}` |
| `SendMessage` em Task terminal → erro | validador 31 | `PASS 31` |
| SUBMITTED → WORKING antes de terminar | `grep -n "SUBMITTED\|start_work" agente/agente.py` | a Task é criada em `TASK_STATE_SUBMITTED` e passa a `WORKING` (`updater.start_work()`) antes de qualquer chamada MCP |

## 5. A ponte

| Critério | Como conferir | Esperado |
|---|---|---|
| Sala ocupada → `INPUT_REQUIRED` com `alternativas: ...` na ordem do enum | validador 27 e 28; `make fluxo`, passo 7 | `PASS 27`, `PASS 28` |
| `escolha=<id>` → `COMPLETED` na sala escolhida | validador 30; `make fluxo`, passo 8 | `PASS 30` |
| `escolha=recusar` → `CANCELED` | validador 32; `make fluxo`, passo 9 | `PASS 32` |
| Escolha fora do enum mantém a pausa | validador 29 | `PASS 29` |
| Duas Tasks pausadas, cada uma com a sua reserva | validador 33 | `PASS 33` |
| `requestState` nunca em resposta A2A | validador 34 | `PASS 34` |
| Erro do MCP não deixa a Task presa; o agente se recupera | `make fluxo`, passos `extra-1` e `extra-2` | servidor MCP fora do ar com Task pausada → `TASK_STATE_FAILED`; servidor de volta → novo pedido `TASK_STATE_COMPLETED`, sem reiniciar o agente |
| Retry com id JSON-RPC diferente do inicial | `conferencia-logs.txt` | `PASS retries com id diferente do request inicial (inicial->retry): [('5', '6'), ...]` |

## 6. Validação e entrega

| Critério | Como conferir | Esperado |
|---|---|---|
| Validador 36/36, exit 0, processos recém-iniciados | `make validar` | `resumo: 36 passaram, 0 falharam`; `exit=0` no fim de `docs/evidencias/validador.txt` |
| `dados/`, `validador/`, `exemplos/` intactos | `git diff --stat upstream/main -- dados validador exemplos` (também `make extras`, item 6) | saída vazia |
| Determinístico, sem SDK de LLM | validador 36; `make extras`, item 5 | `PASS 36`; nenhum `openai`/`anthropic`/`google-genai`/`langchain`/`litellm` nos `pyproject.toml` e `uv.lock` |
| README com as 4 seções, comandos funcionando de um clone limpo | `grep -n "^## " README.md`; seguir "Como rodar" num clone novo | `Como rodar`, `Onde a ponte acontece`, `Decisões técnicas`, `Saída do validador` |

## 7. Prova negativa (as verificações detectam erro de verdade)

```bash
# 1) requestState sem integridade seria aceito? Troque um caractere: precisa dar -32602 (make fluxo, passo 11).
# 2) Sem a chave o servidor não sobe:
env -u REQUEST_STATE_SECRET uv run --project servidor-mcp python servidor-mcp/servidor.py; echo "exit=$?"   # exit=1
# 3) O validador acusa falha quando o estado não é fresco: rode-o duas vezes seguidas sem reiniciar.
set -a; . ./.env; set +a
uv run --project servidor-mcp python servidor-mcp/servidor.py 2>/dev/null &
uv run --project agente python agente/agente.py 2>/dev/null &
sleep 5; python3 validador/validar.py | tail -1; python3 validador/validar.py | tail -1; make parar
```

Esperado em 3: a primeira dá `36 passaram`; a segunda, `20 passaram, 16 falharam` (a 13 em diante: as alternativas das 14h foram ocupadas pela primeira execução e o servidor responde `Sem alternativas disponiveis no intervalo`), como o README do validador avisa. Isso mostra que o 36/36 depende de comportamento real, não de respostas fixas.
