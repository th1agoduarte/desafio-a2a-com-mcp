# A Ponte: agente A2A com MCP por dentro

Entrega do desafio do módulo **Desenvolvimento de Agentes com IA** (enunciado completo em [DESAFIO.md](DESAFIO.md)). Dois processos Python, sem LLM:

| Processo | Pasta | Porta / endpoint | Papel |
|---|---|---|---|
| Servidor MCP `central-de-salas` | [`servidor-mcp/`](servidor-mcp/servidor.py) | `7301` · Streamable HTTP em `/mcp` · revisão `2026-07-28` | 3 tools, resource `politica://uso`, os dois tipos de erro e o MRTR na reserva |
| Agente `Central de Salas` | [`agente/`](agente/agente.py) | `7300` · JSON-RPC em `/a2a` · card em `/.well-known/agent-card.json` | servidor A2A v1.0 por fora, host MCP (cliente HTTP) por dentro |

Stack, com versões travadas em `pyproject.toml` + `uv.lock`: SDK oficial do MCP para Python **`mcp==2.3.0`** (v2, revisão 2026-07-28), **`a2a-sdk==1.3.0`** (A2A v1.0, binding JSON-RPC 2.0) e `uvicorn==0.54.0`.

## Como rodar

Pré-requisitos: `git`, `python3` ≥ 3.10 (só para o validador) e [`uv`](https://docs.astral.sh/uv/getting-started/installation/). O `uv` baixa um Python compatível para os processos se precisar.

```bash
git clone https://github.com/th1agoduarte/desafio-a2a-com-mcp.git
cd desafio-a2a-com-mcp
```

**Chave do `requestState`.** O servidor MCP não sobe sem ela. Gere uma chave de 32 bytes aleatórios e exporte no terminal do servidor. Nunca versione o valor.

```bash
export REQUEST_STATE_SECRET=$(python3 -c "import secrets; print(secrets.token_hex(32))")
```

**Terminal 1: servidor MCP.** Na primeira vez, o `uv` instala as dependências do `uv.lock`. O stderr mostra cada request recebido.

```bash
uv run --project servidor-mcp python servidor-mcp/servidor.py
```

**Terminal 2: agente.**

```bash
uv run --project agente python agente/agente.py
```

**Terminal 3: card e validador.**

```bash
curl http://localhost:7300/.well-known/agent-card.json
python3 validador/validar.py --agente http://localhost:7300 --mcp http://localhost:7301
```

Rode o validador com os dois processos recém-iniciados, porque as reservas de uma execução mudam a seguinte.

Para reiniciar o servidor MCP no meio de um MRTR (passo 12 do fluxo do avaliador), use `Ctrl+C` no terminal 1 e suba de novo **no mesmo terminal**, onde `REQUEST_STATE_SECRET` continua exportada.

<details>
<summary>Sem <code>uv</code>, com <code>pip</code></summary>

```bash
python3 -m venv servidor-mcp/.venv && servidor-mcp/.venv/bin/pip install "mcp==2.3.0" "uvicorn==0.54.0"
python3 -m venv agente/.venv && agente/.venv/bin/pip install "a2a-sdk[http-server]==1.3.0" "mcp==2.3.0" "uvicorn==0.54.0"
servidor-mcp/.venv/bin/python servidor-mcp/servidor.py      # terminal 1, com REQUEST_STATE_SECRET exportada
agente/.venv/bin/python agente/agente.py                    # terminal 2
```
</details>

**Atalhos (`make help`).**
- `make setup` instala as dependências e cria um `.env` (gitignored) com um `REQUEST_STATE_SECRET` novo.
- `make mcp` e `make agente` sobem cada processo.
- `make test` roda tudo e grava as saídas em [`docs/evidencias/`](docs/evidencias/):
  - `make validar`: sobe os dois do zero, roda o validador e confere o stderr do servidor MCP;
  - `make fluxo`: passos manuais 3 e 7 a 13 do fluxo do avaliador, com o restart do servidor MCP;
  - `make extras`: segredo obrigatório, nenhum segredo no código, -32020, nenhum SDK de LLM e pastas protegidas intactas.

Variáveis opcionais, todas com os padrões que o validador exige (detalhes em [.env.example](.env.example)):
- servidor MCP: `MCP_HOST`, `MCP_PORT`;
- agente: `AGENTE_HOST`, `AGENTE_PORT`, `AGENTE_URL`, `MCP_URL`.

## Onde a ponte acontece

Tudo em [`agente/agente.py`](agente/agente.py), nos dois pontos marcados `# ---- PONTE`.

**Ida.** Em `AgenteSalas._tratar_resultado` ([agente/agente.py:261](agente/agente.py#L261)):
- O agente chama a tool por `session.call_tool(..., allow_input_required=True)` (linha 140). Assim o cliente do SDK devolve o `InputRequiredResult` cru e não tenta responder a elicitation sozinho; o agente nem registra callback de elicitation.
- Quando o resultado é `input_required`, o agente lê do `requestedSchema` a propriedade e o seu `enum` (ou `const`).
- Guarda `Pausa(tool, argumentos, chave, opcoes, request_state, politica)` em `PAUSAS[task_id]` (linha 273).
- Põe a Task em `TASK_STATE_INPUT_REQUIRED` com a mensagem exata `alternativas: <ids>` (linha 275).

**Volta.** Em `AgenteSalas._retomar` ([agente/agente.py:295](agente/agente.py#L295)):
- O `SendMessage` de continuação traz `taskId` e `escolha=<valor>`.
- `escolha=recusar` vira `{"action": "decline"}`. Um id que está no enum vira `{"action": "accept", "content": {<propriedade do schema>: id}}`.
- Qualquer outro valor mantém a Task pausada e repete a lista.
- Para aceite ou recusa, o agente repete o mesmo `tools/call` com `inputResponses` na mesma chave e o `requestState` de `PAUSAS[task_id]` ecoado sem modificação (linha 319). O SDK emite cada request com um id JSON-RPC novo.
- No log do servidor MCP aparecem pares como `id=5` (inicial) e `id=6 ... retry=sim` (retry).
- A resposta fecha a Task:
  - `complete` → `COMPLETED`, com o artifact `reserva`;
  - `reservado: false` → `CANCELED`;
  - `isError` → `FAILED`, com o texto da tool.

**Do lado do servidor**, o par equivalente está em [`servidor-mcp/servidor.py`](servidor-mcp/servidor.py):
- `reservar_sala` (linha 257) devolve `pedir_escolha(...)` (linha 205), um `InputRequiredResult` com uma única entrada `escolha_de_sala` em form mode. A resposta termina pedindo informação; nunca é um request do servidor para o cliente.
- No retry, `ctx.request_state is not None` (linha 258) leva a `retomar(...)` (linha 239), que reconstrói o pedido a partir do próprio `requestState`.

## Decisões técnicas

**Proteção do `requestState`.** Uso o utilitário do próprio SDK, o `RequestStateSecurity` + `RequestStateBoundary` de `mcp.server.request_state`. A tool produz um JSON em texto puro, `{"v":1,"pedido":{sala,inicio,fim,responsavel},"opcoes":[...]}`, e nunca toca no codec. O boundary embrulha esse JSON num envelope com:
- `iat` e `exp`;
- método e nome da tool;
- digest SHA-256 dos argumentos;
- audiência (`central-de-salas`).

O envelope é cifrado e autenticado com **AES-256-GCM**, numa chave derivada por HKDF-SHA256. No fio vai `v1.<base64url>`, com integridade e confidencialidade.

**Verificação na volta.** O boundary verifica tudo antes de a tool rodar. Tag GCM inválida (um caractere trocado), envelope expirado, tool diferente ou argumentos diferentes dos selados recebem `-32602 Invalid or expired requestState`; o motivo real vai só para o log, por exemplo `requestState rejected on tools/call: seal`. Por isso um retry com argumentos adulterados é rejeitado em vez de produzir reserva. Mesmo que passasse, a reserva usa os valores selados (`retomar` lê `pedido` do estado, não dos argumentos).

**Validade.** 10 minutos (`TTL_REQUEST_STATE = 600`), dentro da faixa de 5 a 30 pedida.

**Chave.** Vem só de `REQUEST_STATE_SECRET`: hex com pelo menos 32 bytes, ou o servidor sai com erro (`chave_request_state()`). Não há chave no código. Como o estado viaja inteiro no token e a chave é a mesma entre execuções, um retry funciona depois de reiniciar o servidor MCP, que não guarda nada em memória entre o `input_required` e o retry.

**MRTR conduzido pelo corpo da tool, não por resolver.** Testei primeiro o `Resolve(...)` + `Elicit(...)` do SDK, que é o desenho dos exemplos de wire (chave `__main__:escolha_de_sala`). Ele passa no validador, mas recalcula as alternativas a cada rodada e só aceita a resposta se a pergunta sair idêntica. Depois de um restart, as reservas em memória somem, as alternativas mudam e o retry viraria uma nova pergunta. Com as alternativas seladas no `requestState`, o retry após restart conclui; o passo 12 de `make fluxo` comprova.

**Capability de elicitation (-32021).** É lida do `_meta` de cada request (`ctx.client_capabilities`), sem sessão. Sem form elicitation declarada, a tool levanta `MCPError(-32021, data.requiredCapabilities={"elicitation":{"form":{}}})`, e o transport do SDK responde HTTP 400. O mesmo vale para `_meta` sem `protocolVersion` ou sem `clientCapabilities` (-32602, HTTP 400) e para header que não bate com o corpo (-32020).

**Estado das Tasks.**
- Tasks: `InMemoryTaskStore` do a2a-sdk, com `id`, `contextId`, `status`, `history` e `artifacts`. O SDK recusa `SendMessage` em Task terminal.
- Pausas: um dicionário separado do store, `PAUSAS[task_id] -> Pausa`. É por Task, então duas pausas simultâneas não trocam de `requestState`. O `requestState` nunca entra em card, artifact, mensagem ou metadata da Task, e o validador confere que ele não vaza.
- Trace: `TRACES[task_id]` guarda o trace-id do `traceparent` do cliente A2A. Todo request MCP daquela Task leva `traceparent` com o mesmo trace-id e um span-id novo.

**Host MCP.**
- Um cliente MCP por execução do agente, aberto e fechado na mesma task assíncrona. O protocolo não tem sessão, então isso não custa estado, e uma falha de transporte fica restrita àquela execução.
- Erro de protocolo do MCP (por exemplo `requestState` expirado depois de 10 minutos) ou servidor fora do ar termina a Task em `FAILED` com a causa ([agente/agente.py:230](agente/agente.py#L230)). A Task nunca fica presa em `WORKING`, e o agente volta a atender assim que o servidor MCP volta; os passos `extra-1` e `extra-2` de `make fluxo` comprovam.
- O agente usa o `Client` do SDK no modo `2026-07-28`, sem handshake: cada request carrega `protocolVersion`, `clientInfo` e `clientCapabilities` no `_meta`, e o SDK espelha `MCP-Protocol-Version`, `Mcp-Method` e `Mcp-Name` nos headers.
- O SDK só declara elicitation quando há callback, e aí declara `form` + `url`. A subclasse `ClienteMCP` fixa exatamente `{"elicitation": {"form": {}}}`, que é o que o agente atende.
- As ferramentas vêm de `tools/list`, feito no início de cada execução, antes de qualquer `tools/call`. As chaves do pedido são filtradas pelo `inputSchema` descoberto. Esse `tools/list` leva o `traceparent` da Task e alimenta o cache de schemas do SDK; sem ele, o SDK faria um `tools/list` próprio, sem o nosso `_meta`.
- A versão da política sai da primeira linha do resource `politica://uso`, lido a cada pedido, e vai no campo `politica` do artifact.

**Reenvio do mesmo `requestState`.** Dentro da validade, o servidor aceita o mesmo token mais de uma vez: não há marcação de "usado". É uma escolha consciente, porque marcar exigiria memória no servidor entre o `input_required` e o retry, que o enunciado proíbe, e o próprio passo 12 do avaliador reusa o token depois de um restart. Do lado do agente, a pausa é consumida no primeiro retry, e uma trava por Task serializa continuações simultâneas.

**Sem regra de negócio no agente.** Conflito, janela, duração e alternativas são decididos só pelo servidor MCP. O agente interpreta dois formatos fixos (`reservar ...` e `escolha=...`) por regex e valida a escolha contra o enum que o próprio servidor mandou. Mesmo pedido, mesmo resultado.

**Divergências de SDK, documentadas em vez de contornadas no protocolo.**
1. **`A2A-Version`.** O `a2a-sdk` interpreta a ausência do header `A2A-Version` como v0.3 e recusa `SendMessage`. O validador e os exemplos de wire mandam métodos da v1.0 sem o header. O `ContextoV1` ([agente/agente.py:376](agente/agente.py#L376)) assume `1.0` quando o header falta; um header explícito continua valendo.
2. **`history`.** O `a2a-sdk` só move `status.message` para o `history` na atualização seguinte, então a mensagem de um estado terminal (o erro exato da tool em `FAILED`) nunca apareceria no histórico. O `TaskStoreComHistorico` (linha 328) inclui cada mensagem de status no `history` assim que ela é emitida, deduplicada por `messageId`, no mesmo formato de `exemplos/wire/08` e `10`.
3. **Prefixo do erro.** O SDK do MCP prefixa erros de execução com `Error executing tool reservar_sala: `. O enunciado aceita.

**Log no stderr do servidor MCP.** Um middleware ASGI registra todo POST, inclusive os que o SDK rejeita antes da tool, com:
- método, id, nome ou URI e `retry=sim`;
- os headers `Mcp-Method`, `Mcp-Name` e `MCP-Protocol-Version`;
- `clientInfo`, `clientCapabilities` e `traceparent`.

Exemplo: `[mcp] ... method=tools/call id=6 name=reservar_sala retry=sim h.mcp-method=tools/call h.mcp-name=reservar_sala ... client=agente-central-de-salas caps={"elicitation":{"form":{}}} traceparent=00-<trace-id>-<span>-01`.

## Saída do validador

Última execução (`make validar`), com os dois processos recém-iniciados. A mesma saída está em [docs/evidencias/validador.txt](docs/evidencias/validador.txt). O stderr correspondente do servidor MCP está em [servidor-mcp.stderr.txt](docs/evidencias/servidor-mcp.stderr.txt), onde o trace-id da primeira linha aparece no `traceparent` dos requests do agente. A conferência automática desse stderr está em [conferencia-logs.txt](docs/evidencias/conferencia-logs.txt).

```text
trace-id desta execucao: 089a80869a9926ff372139672c72c1d5
procure esse valor no stderr do servidor MCP para conferir a propagacao do traceparent.

PASS 01 tools/list traz as tres tools
PASS 02 toda tool tem inputSchema de objeto
PASS 03 listar_salas devolve structuredContent e o mesmo JSON em texto
PASS 04 _meta sem protocolVersion devolve -32602 e HTTP 400
PASS 05 _meta sem clientCapabilities devolve -32602 e HTTP 400
PASS 06 tool inexistente e recusada, por -32602 ou por isError
PASS 07 resources/read de politica://uso devolve a politica
PASS 08 resources/read de URI inexistente devolve -32602
PASS 09 sala inexistente devolve isError com a mensagem exata
PASS 10 fora da janela devolve isError com a mensagem exata
PASS 11 duracao acima de 2h devolve isError com a mensagem exata
PASS 12 intervalo invertido devolve isError com a mensagem exata
PASS 13 conflito devolve input_required com inputRequests e requestState
PASS 14 a elicitation e form mode e oferece as alternativas na ordem certa
PASS 15 conflito sem a capability elicitation devolve -32021 e HTTP 400
PASS 16 retry com inputResponses e requestState conclui a reserva
PASS 17 requestState adulterado e rejeitado com -32602
PASS 18 argumentos adulterados no retry nao tomam efeito
PASS 19 recusa conclui sem reservar e sem isError
PASS 20 conflito sem alternativa possivel devolve isError com a mensagem exata

PASS 21 agent card responde 200 no well-known com JSON
PASS 22 o card declara a interface JSON-RPC com url e versao 1.0
PASS 23 o card declara a skill reservar-sala
PASS 24 SendMessage com sala livre conclui a Task
PASS 25 o artifact chama reserva e traz a versao da politica
PASS 26 GetTask devolve id, contextId e estado corrente
PASS 27 SendMessage com sala ocupada pausa a Task
PASS 28 a Task pausada lista as alternativas na ordem certa
PASS 29 escolha fora do enum mantem a Task pausada
PASS 30 a continuacao conclui a Task na sala escolhida
PASS 31 SendMessage em Task terminal e recusado
PASS 32 a recusa termina a Task em CANCELED
PASS 33 duas Tasks pausadas ao mesmo tempo concluem cada uma com a sua reserva
PASS 34 nenhuma resposta A2A carrega o requestState
PASS 35 sala inexistente termina a Task em FAILED com a mensagem da tool
PASS 36 o agente e deterministico: o mesmo pedido produz a mesma pausa

resumo: 36 passaram, 0 falharam, de 36 verificacoes
exit=0
```

Outras evidências em [docs/evidencias/](docs/evidencias/):
- [fluxo-avaliador.txt](docs/evidencias/fluxo-avaliador.txt): passos 3 e 7 a 13, incluindo o `requestState` adulterado, o retry depois de reiniciar o servidor MCP (com prova de que o processo é novo) e o -32021, mais `extra-1` e `extra-2` (servidor MCP fora do ar com Task pausada, e recuperação);
- [verificacoes-extras.txt](docs/evidencias/verificacoes-extras.txt);
- o roteiro de conferência de cada critério em [VALIDACAO.md](VALIDACAO.md).
