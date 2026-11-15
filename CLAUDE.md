# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

Desafio 04 (módulo Desenvolvimento de Agentes com IA). Clone of `devfullcycle/desafio-a2a-com-mcp`, delivered as a **public GitHub fork** on `main`.
- Remotes: `upstream` = devfullcycle (read-only), `origin` = `git@github.com:th1agoduarte/desafio-a2a-com-mcp.git`.
- Statement: `DESAFIO.md` (the starter's original README, verbatim).
- `README.md` is the deliverable doc. It must keep exactly four sections: Como rodar, Onde a ponte acontece, Decisões técnicas, Saída do validador.
- Criterion-by-criterion check: `VALIDACAO.md`.

## Commands

```bash
make setup      # uv sync for servidor-mcp/ and agente/ + .env with a fresh REQUEST_STATE_SECRET (never printed)
make mcp        # servidor-mcp/servidor.py on :7301/mcp (stderr = one line per request)
make agente     # agente/agente.py on :7300 (/a2a, /.well-known/agent-card.json)
make validar    # scripts/validar.sh: fresh processes + validador/validar.py (36 checks) + scripts/conferir_logs.py on the MCP stderr
make fluxo      # scripts/fluxo_avaliador.py: evaluator steps 3, 7-13 with exemplos/wire bodies, restarts the MCP server mid-MRTR
make extras     # scripts/verificacoes_extras.sh: secret required, no hardcoded secret, -32020, no LLM SDK, protected dirs untouched
make test       # all three; outputs land in docs/evidencias/ (commit them)
make parar      # kill whatever holds ports 7300/7301
```

- The validator needs **freshly started processes**: reservations from one run change the next.
- Evidence files are `.txt` because the starter's `.gitignore` ignores `*.log`.

## Architecture

There are two processes, and the agent talks to the MCP server only over HTTP. Never import the server code into the agent.

**`servidor-mcp/servidor.py`** uses `mcp==2.3.0` `MCPServer`. `streamable_http_app(json_response=True)` serves protocol revision 2026-07-28: stateless, with `_meta` on every request.
- Domain: `dados/*.json` is loaded read-only. Reservations live in memory.
- **MRTR is driven by the tool body, not by `Resolve`/`Elicit`.**
  - `reservar_sala` returns `InputRequiredResult` (`pedir_escolha`). Its plaintext `requestState` is `{"pedido","opcoes"}`.
  - On retry (`ctx.request_state` set), `retomar` rebuilds the request from that state.
  - The SDK `RequestStateBoundary` seals and verifies it: AES-256-GCM, a TTL of 600 s, and binding to the tool name and an args digest. The key comes from `REQUEST_STATE_SECRET` (hex, at least 32 bytes) through `RequestStateSecurity`.
  - The resolver approach was dropped because it recomputes alternatives each round, so a retry after a restart (empty reservations) became a new question.
- The missing form elicitation capability raises `MCPError(-32021)`, which the transport maps to HTTP 400.
- `com_log` is an ASGI wrapper that logs every POST to stderr: method, id, name/uri, `retry=sim`, the Mcp-* headers, `client=`, `caps=` and `traceparent`. `scripts/conferir_logs.py` parses that format, so keep them in sync.

**`agente/agente.py`** uses `a2a-sdk==1.3.0` (A2A v1.0, JSON-RPC), with `DefaultRequestHandlerV2`, `AgenteSalas(AgentExecutor)` and `TaskUpdater`.
- **Bridge, outbound:** `_tratar_resultado`.
  - An `InputRequiredResult` gets stored in `PAUSAS[task_id]`, never in the Task.
  - The Task then goes to `TASK_STATE_INPUT_REQUIRED` with the exact text `alternativas: a, b`.
- **Bridge, return:** `_retomar`.
  - `escolha=<id|recusar>` repeats `session.call_tool(..., allow_input_required=True, input_responses=..., request_state=...)`. The SDK gives it a new JSON-RPC id.
- MCP client:
  - `ClienteMCP` subclasses `mcp.client.Client` with `mode="2026-07-28"` and no elicitation callback, so the raw `input_required` reaches the agent.
  - It forces the capabilities to exactly `{"elicitation":{"form":{}}}`. The SDK would otherwise declare form plus url, and only when a callback exists.
- SDK workarounds, documented in the README:
  - `ContextoV1` defaults a missing `A2A-Version` header to 1.0, because the SDK treats a missing header as 0.3 and the validator sends none.
  - `TaskStoreComHistorico` puts each status message into `history` right away, so a terminal message (the FAILED tool error) shows up in history.
- `OTEL_INSTRUMENTATION_A2A_SDK_ENABLED=false` silences a2a-sdk span noise. OpenTelemetry spans are out of scope.

## Constraints (from DESAFIO.md)

- **Never modify** `dados/`, `validador/`, `exemplos/`. `make extras` diffs them against `upstream/main`.
- No LLM anywhere and no LLM SDK in `pyproject.toml`/`uv.lock`. The workspace multi-provider/gateway pattern **does not apply**: LLMs are forbidden, and container/compose/gateway are out of scope.
- **No business rules in the agent.** Conflicts, policy and alternatives belong to the server. The agent only parses the two fixed formats and checks the choice against the enum the server sent.
- **No session state.** The `requestState` is opaque to the agent: store and echo it only.
- **Exact error strings:** the five `ERRO_*` constants in `servidor.py`.
- **Default ports:** 7300 and 7301. Paths: `/a2a`, `/mcp`.
- **Pinned versions:** `mcp==2.3.0`, `a2a-sdk==1.3.0`, `uvicorn==0.54.0`, plus the `uv.lock` files.
- `.env` is gitignored. Never commit or print `REQUEST_STATE_SECRET`.

## Plano / status (2026-11-15)

1. Read wire examples and validator. ✓
2. MCP server: tools, resource, errors, MRTR, sealed requestState. ✓ Validator 1–20.
3. Agent: MCP host plus A2A server and the bridge. ✓ Validator 21–36.
4. Evaluator-flow automation (restart, tamper, -32021) and extra checks. ✓
5. README, VALIDACAO, evidence. ✓ Then commit, push, and a clean-clone run following only the README.
