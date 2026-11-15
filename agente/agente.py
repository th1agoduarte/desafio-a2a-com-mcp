"""Agente "Central de Salas": servidor A2A v1.0 por fora (:7300), host MCP por dentro.

Sem LLM: o pedido chega em formato fixo e o agente decide por regra. Ele não implementa
regra de sala (conflito, política, alternativas são do servidor MCP): só traduz protocolo.

A ponte (onde o MRTR do MCP encontra a Task do A2A):
  - `_tratar_resultado`: o `tools/call` volta `input_required` -> a Task vai para
    TASK_STATE_INPUT_REQUIRED com a linha "alternativas: ..." e o `requestState` fica
    guardado em PAUSAS[task_id], fora da Task (nunca vai para card, artifact ou mensagem).
  - `_retomar`: o SendMessage de continuação traz `escolha=<valor>` -> o agente repete o
    `tools/call` original (id JSON-RPC novo, gerado pelo SDK) com `inputResponses` na mesma
    chave e o `requestState` ecoado byte a byte.

    MCP_URL=http://localhost:7301/mcp python agente.py
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import secrets
import sys
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass
from datetime import datetime
from typing import Any

# Spans de OpenTelemetry estão fora do escopo; a instrumentação embutida do a2a-sdk só gera ruído no stderr.
os.environ.setdefault("OTEL_INSTRUMENTATION_A2A_SDK_ENABLED", "false")

import uvicorn  # noqa: E402
from a2a.helpers.proto_helpers import new_task, new_text_part
from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.events import EventQueue
from a2a.server.request_handlers import DefaultRequestHandlerV2
from a2a.server.routes.agent_card_routes import create_agent_card_routes
from a2a.server.routes.common import DefaultServerCallContextBuilder
from a2a.server.routes.jsonrpc_routes import create_jsonrpc_routes
from a2a.server.tasks import InMemoryTaskStore, TaskUpdater
from a2a.types import (
    AgentCapabilities,
    AgentCard,
    AgentInterface,
    AgentProvider,
    AgentSkill,
    TaskState,
)
from mcp import types as mcp_types
from mcp.client import Client
from starlette.applications import Starlette
from starlette.requests import Request

HOST = os.getenv("AGENTE_HOST", "127.0.0.1")
PORTA = int(os.getenv("AGENTE_PORT", "7300"))
URL_PUBLICA = os.getenv("AGENTE_URL", f"http://{HOST}:{PORTA}")
MCP_URL = os.getenv("MCP_URL", "http://localhost:7301/mcp")
PROTOCOLO_MCP = "2026-07-28"
TOOL_RESERVA = "reservar_sala"  # a skill reservar-sala é atendida por esta tool, se tools/list a anunciar
RESOURCE_POLITICA = "politica://uso"

# Exatamente o que o enunciado pede no _meta: elicitation em form mode, nada além.
CAPABILITIES = mcp_types.ClientCapabilities(
    elicitation=mcp_types.ElicitationCapability(form=mcp_types.FormElicitationCapability())
)
TRACEPARENT_RE = re.compile(r"^[0-9a-f]{2}-([0-9a-f]{32})-[0-9a-f]{16}-[0-9a-f]{2}$")
PEDIDO_RE = re.compile(r"^\s*reservar((?:\s+\w+=\S+)+)\s*$")
ESCOLHA_RE = re.compile(r"^\s*escolha=(\S+)\s*$")


def log(msg: str) -> None:
    print(f"[agente] {datetime.now().isoformat(timespec='milliseconds')} {msg}", file=sys.stderr, flush=True)


# ---------------------------------------------------------------- host MCP


class ClienteMCP(Client):
    """Cliente MCP do SDK, sem callback de elicitation: o agente precisa ver o input_required cru.

    O SDK só declara elicitation quando há callback, e aí declara form + url. Aqui a capability
    é fixada em {"elicitation": {"form": {}}}, que é o que o agente de fato atende (via A2A).
    """

    async def _build_session(self, exit_stack: AsyncExitStack) -> Any:
        sessao = await super()._build_session(exit_stack)
        sessao._build_capabilities = lambda _versao: CAPABILITIES  # type: ignore[method-assign]
        return sessao


@dataclass
class Pausa:
    """O que o agente guarda de uma Task pausada. O requestState é opaco: guardar e ecoar."""

    tool: str
    argumentos: dict[str, str]
    chave: str
    campo: str  # nome da propriedade do requestedSchema (o servidor decide; o agente não fixa)
    opcoes: list[str]
    request_state: str
    politica: str


PAUSAS: dict[str, Pausa] = {}  # task_id -> pausa; nunca serializado para o cliente A2A
TRACES: dict[str, str] = {}  # task_id -> trace-id W3C, para todos os requests MCP da Task (só enquanto viva)
TRAVAS: dict[str, asyncio.Lock] = {}  # uma continuação por vez em cada Task


def traceparent(trace_id: str) -> str:
    return f"00-{trace_id}-{secrets.token_hex(8)}-01"  # mesmo trace-id, span-id novo por request


class Host:
    def __init__(self, cliente: ClienteMCP) -> None:
        self.cliente = cliente

    def meta(self, trace_id: str) -> dict[str, Any]:
        return {"traceparent": traceparent(trace_id)}

    async def descobrir(self, trace_id: str) -> dict[str, mcp_types.Tool]:
        """tools/list no início de cada execução, com o traceparent da Task.

        Além de descobrir a tool em runtime, alimenta o cache de schemas do cliente do SDK;
        sem isso, o SDK faria um tools/list próprio (sem o nosso _meta) para validar o
        structuredContent do tools/call.
        """
        resultado = await self.cliente.list_tools(meta=self.meta(trace_id))
        return {t.name: t for t in resultado.tools}

    async def versao_politica(self, trace_id: str) -> str:
        resultado = await self.cliente.read_resource(RESOURCE_POLITICA, meta=self.meta(trace_id))
        texto = next((c.text for c in resultado.contents if isinstance(c, mcp_types.TextResourceContents)), "")
        primeira = texto.splitlines()[0] if texto else ""
        return primeira.split(":", 1)[1].strip() if primeira.startswith("versao:") else ""

    async def chamar(self, trace_id: str, tool: str, argumentos: dict[str, str], **retry: Any) -> Any:
        # session.call_tool com allow_input_required=True devolve InputRequiredResult sem resolver nada.
        return await self.cliente.session.call_tool(
            tool, argumentos, meta=self.meta(trace_id), allow_input_required=True, **retry
        )


# ---------------------------------------------------------------- A2A


def texto_de(context: RequestContext) -> str:
    return context.get_user_input().strip()


def argumentos_do_pedido(texto: str, tool: mcp_types.Tool) -> dict[str, str] | None:
    """`reservar sala=<id> inicio=<iso> fim=<iso> responsavel=<nome>` -> argumentos da tool.

    As chaves aceitas e as obrigatórias vêm do inputSchema descoberto, não de lista fixa.
    """
    m = PEDIDO_RE.match(texto)
    if not m:
        return None
    pares = dict(p.split("=", 1) for p in m.group(1).split())
    schema = tool.input_schema or {}
    propriedades = set((schema.get("properties") or {}).keys())
    obrigatorios = set(schema.get("required") or [])
    argumentos = {k: v for k, v in pares.items() if k in propriedades}
    return argumentos if obrigatorios <= set(argumentos) else None


def texto_do_resultado(resultado: mcp_types.CallToolResult) -> str:
    return " ".join(c.text for c in resultado.content if isinstance(c, mcp_types.TextContent)).strip()


def opcoes_da_elicitation(pedido: mcp_types.InputRequest) -> tuple[str, list[str]]:
    """(propriedade, alternativas): o enum (ou const, com uma só) da propriedade do requestedSchema."""
    params = pedido.params
    schema = getattr(params, "requested_schema", None) or {}
    if not isinstance(schema, dict):
        schema = schema.model_dump(by_alias=True, exclude_none=True)
    for nome, prop in (schema.get("properties") or {}).items():
        if "enum" in prop:
            return nome, [str(v) for v in prop["enum"]]
        if "const" in prop:
            return nome, [str(prop["const"])]
    return "", []


def novo_cliente_mcp() -> ClienteMCP:
    return ClienteMCP(
        MCP_URL,
        mode=PROTOCOLO_MCP,  # sem handshake: cada request leva versão e capabilities no _meta
        client_info=mcp_types.Implementation(name="agente-central-de-salas", version="1.0.0"),
        cache=None,
    )


class AgenteSalas(AgentExecutor):
    """Um cliente MCP por execução (aberto e fechado na mesma task assíncrona).

    O protocolo não tem sessão, então isso não custa nada de estado; e uma falha de transporte
    (servidor MCP fora do ar) fica restrita àquela execução, sem inutilizar o agente.
    """

    async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
        atual = context.current_task  # na continuação, ids vêm da Task guardada, não da mensagem
        task_id = atual.id if atual else context.task_id or ""
        context_id = atual.context_id if atual else context.context_id or ""
        updater = TaskUpdater(event_queue, task_id, context_id)
        cabecalho = (context.call_context.state.get("headers") or {}).get("traceparent", "")
        m = TRACEPARENT_RE.match(cabecalho.strip().lower())
        if m:
            TRACES[task_id] = m.group(1)
        trace_id = TRACES.setdefault(task_id, secrets.token_hex(16))

        def msg(texto: str) -> Any:
            return updater.new_agent_message([new_text_part(texto)])

        async with TRAVAS.setdefault(task_id, asyncio.Lock()):
            try:
                novo = context.current_task is None
                if novo:
                    await event_queue.enqueue_event(
                        new_task(task_id, context_id, TaskState.TASK_STATE_SUBMITTED, history=[context.message])
                    )
                    log(f"task={task_id} SUBMITTED trace={trace_id}")
                    await updater.start_work()
                async with novo_cliente_mcp() as cliente:
                    host = Host(cliente)
                    if novo:
                        await self._novo_pedido(host, task_id, texto_de(context), trace_id, updater, msg)
                    else:
                        await self._retomar(host, task_id, texto_de(context), trace_id, updater, msg)
            except Exception as exc:  # erro JSON-RPC do MCP (ex.: requestState expirado) ou servidor fora do ar
                while getattr(exc, "exceptions", None):  # ExceptionGroup do anyio: mostra a causa real
                    exc = exc.exceptions[0]
                PAUSAS.pop(task_id, None)
                log(f"task={task_id} FAILED erro de protocolo/transporte MCP: {exc!r}")
                try:
                    await updater.failed(msg(f"Falha ao falar com o servidor MCP: {exc}"))
                except RuntimeError:  # a Task já tinha chegado a estado terminal
                    pass
            finally:
                if task_id not in PAUSAS:  # terminal: nada mais a propagar nesta Task
                    TRACES.pop(task_id, None)
                    TRAVAS.pop(task_id, None)

    async def _novo_pedido(self, host: Host, task_id: str, texto: str, trace_id: str, updater: TaskUpdater, msg: Any) -> None:
        ferramentas = await host.descobrir(trace_id)
        log(f"task={task_id} tools/list -> {sorted(ferramentas)}")
        tool = ferramentas.get(TOOL_RESERVA)
        if tool is None:
            await updater.failed(msg(f"O servidor MCP nao anuncia a tool {TOOL_RESERVA}"))
            return
        argumentos = argumentos_do_pedido(texto, tool)
        if argumentos is None:
            await updater.failed(
                msg("Pedido invalido: use reservar sala=<id> inicio=<iso8601> fim=<iso8601> responsavel=<nome>")
            )
            return
        politica = await host.versao_politica(trace_id)
        resultado = await host.chamar(trace_id, tool.name, argumentos)
        await self._tratar_resultado(task_id, tool.name, argumentos, politica, resultado, updater, msg)

    async def _tratar_resultado(
        self, task_id: str, tool: str, argumentos: dict[str, str], politica: str, resultado: Any,
        updater: TaskUpdater, msg: Any,
    ) -> None:
        # ---- PONTE, ida: input_required do MCP vira TASK_STATE_INPUT_REQUIRED do A2A.
        if isinstance(resultado, mcp_types.InputRequiredResult):
            pedidos = resultado.input_requests or {}
            if len(pedidos) != 1 or not resultado.request_state:
                await updater.failed(msg("Resposta input_required inesperada do servidor MCP"))
                return
            chave, pedido = next(iter(pedidos.items()))
            campo, opcoes = opcoes_da_elicitation(pedido)
            PAUSAS[task_id] = Pausa(tool, argumentos, chave, campo, opcoes, resultado.request_state, politica)
            log(f"task={task_id} INPUT_REQUIRED opcoes={opcoes} (requestState guardado, {len(resultado.request_state)} bytes)")
            await updater.requires_input(msg("alternativas: " + ", ".join(opcoes)))
            return

        PAUSAS.pop(task_id, None)
        if resultado.is_error:
            erro = texto_do_resultado(resultado)
            log(f"task={task_id} FAILED {erro}")
            await updater.failed(msg(erro))
            return
        dados = resultado.structured_content or {}
        if dados.get("reservado") is False:
            log(f"task={task_id} CANCELED motivo={dados.get('motivo')}")
            await updater.cancel(msg(f"Reserva nao realizada: {dados.get('motivo') or 'recusado'}."))
            return
        reserva = {k: dados.get(k) for k in ("reserva", "sala", "inicio", "fim", "responsavel")}
        reserva["politica"] = politica  # versão lida do resource politica://uso
        await updater.add_artifact([new_text_part(json.dumps(reserva, ensure_ascii=False))], name="reserva")
        log(f"task={task_id} COMPLETED {reserva['reserva']} em {reserva['sala']}")
        await updater.complete(msg(f"Reserva {reserva['reserva']} confirmada na {reserva['sala']}."))

    async def _retomar(self, host: Host, task_id: str, texto: str, trace_id: str, updater: TaskUpdater, msg: Any) -> None:
        pausa = PAUSAS.get(task_id)
        if pausa is None:
            await updater.failed(msg("Task sem pausa pendente"))
            return
        if pausa.tool not in await host.descobrir(trace_id):
            await updater.failed(msg(f"O servidor MCP nao anuncia mais a tool {pausa.tool}"))
            return
        m = ESCOLHA_RE.match(texto)
        valor = m.group(1) if m else None
        if valor == "recusar":
            resposta: dict[str, Any] = {"action": "decline"}
        elif valor in pausa.opcoes:
            resposta = {"action": "accept", "content": {pausa.campo: valor}}
        else:  # fora do enum da elicitation: continua pausada, repetindo a lista
            await updater.requires_input(msg("alternativas: " + ", ".join(pausa.opcoes)))
            return
        await updater.start_work()
        # ---- PONTE, volta: o mesmo tools/call, id novo, inputResponses na mesma chave, requestState ecoado.
        resultado = await host.chamar(
            trace_id,
            pausa.tool,
            pausa.argumentos,
            input_responses={pausa.chave: mcp_types.ElicitResult.model_validate(resposta)},
            request_state=pausa.request_state,
        )
        await self._tratar_resultado(task_id, pausa.tool, pausa.argumentos, pausa.politica, resultado, updater, msg)

    async def cancel(self, context: RequestContext, event_queue: EventQueue) -> None:
        PAUSAS.pop(context.task_id or "", None)
        await TaskUpdater(event_queue, context.task_id or "", context.context_id or "").cancel()


class TaskStoreComHistorico(InMemoryTaskStore):
    """Toda mensagem de status do agente entra no history assim que é emitida.

    O a2a-sdk só move status.message para o history na atualização seguinte, então a
    mensagem de um estado terminal (por exemplo, o erro exato da tool em FAILED) nunca
    apareceria no history. Aqui ela entra na hora, sem duplicar quando o SDK a move depois
    (dedupe por messageId), no mesmo formato de exemplos/wire/08 e 10.
    """

    async def save(self, task: Any, context: Any) -> None:
        vistos: set[str] = set()
        historico = []
        for m in task.history:
            if m.message_id not in vistos:
                vistos.add(m.message_id)
                historico.append(m)
        if task.status.HasField("message") and task.status.message.message_id not in vistos:
            historico.append(task.status.message)
        del task.history[:]
        task.history.extend(historico)
        await super().save(task, context)


CARD = AgentCard(
    name="Central de Salas",
    description="Reserva salas de reuniao da Hill Valley Tech.",
    provider=AgentProvider(organization="Hill Valley Tech", url="https://hillvalley.example"),
    version="1.0.0",
    supported_interfaces=[AgentInterface(url=f"{URL_PUBLICA}/a2a", protocol_binding="JSONRPC", protocol_version="1.0")],
    capabilities=AgentCapabilities(streaming=False, push_notifications=False, extended_agent_card=False),
    default_input_modes=["text/plain"],
    default_output_modes=["text/plain"],
    skills=[
        AgentSkill(
            id="reservar-sala",
            name="Reservar sala",
            description="Reserva uma sala em um intervalo. Se houver conflito, pergunta qual alternativa usar.",
            tags=["salas", "agenda"],
            input_modes=["text/plain"],
            output_modes=["text/plain"],
            examples=[
                "reservar sala=sala-garagem inicio=2026-11-03T14:00:00-03:00 fim=2026-11-03T15:00:00-03:00 responsavel=Marty"
            ],
        )
    ],
)


class ContextoV1(DefaultServerCallContextBuilder):
    """Header A2A-Version ausente vale 1.0 neste endpoint.

    O a2a-sdk segue a regra da spec (ausente = 0.3) e recusaria os requests do validador,
    que usam métodos e enums da v1.0 (SendMessage, ROLE_USER) sem mandar o header.
    Um A2A-Version explícito continua valendo e é validado pelo SDK.
    """

    def build(self, request: Request) -> Any:
        contexto = super().build(request)
        cabecalhos = contexto.state.setdefault("headers", {})
        if not (cabecalhos.get("a2a-version") or cabecalhos.get("A2A-Version")):
            cabecalhos["a2a-version"] = "1.0"
        return contexto


def criar_app() -> Starlette:
    handler = DefaultRequestHandlerV2(
        agent_executor=AgenteSalas(), task_store=TaskStoreComHistorico(), agent_card=CARD
    )

    @asynccontextmanager
    async def ciclo(_app: Starlette):
        log(f"A2A em {URL_PUBLICA}/a2a, card em {URL_PUBLICA}/.well-known/agent-card.json, MCP em {MCP_URL}")
        yield

    rotas = create_agent_card_routes(CARD) + create_jsonrpc_routes(handler, rpc_url="/a2a", context_builder=ContextoV1())
    return Starlette(routes=rotas, lifespan=ciclo)


if __name__ == "__main__":
    uvicorn.run(criar_app(), host=HOST, port=PORTA, log_level="warning")
