"""Servidor MCP "central-de-salas": Streamable HTTP em :7301/mcp, revisão 2026-07-28.

Tools: listar_salas, consultar_disponibilidade, reservar_sala (com MRTR).
Resource: politica://uso.

O MRTR é conduzido pelo corpo de reservar_sala, que devolve `InputRequiredResult` quando
há conflito: a resposta termina pedindo informação (não existe canal de volta). O
`requestState` leva o pedido original e as alternativas oferecidas; o `RequestStateBoundary`
do SDK sela esse texto (AES-256-GCM, chave de REQUEST_STATE_SECRET, expiração de 10 min,
vínculo com a tool e com o digest dos argumentos) e, no retry, só entrega à tool o que ele
mesmo selou. Nada fica em memória entre o input_required e o retry, e um retry depois de
reiniciar o processo funciona, porque o estado viaja no próprio requestState.

    REQUEST_STATE_SECRET=$(python3 -c "import secrets; print(secrets.token_hex(32))") python servidor.py
"""

from __future__ import annotations

import itertools
import json
import os
import sys
import threading
from datetime import datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any

import uvicorn
from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.request_state import RequestStateSecurity
from mcp.shared.exceptions import MCPError
from mcp_types import MISSING_REQUIRED_CLIENT_CAPABILITY, ElicitResult, InputRequiredResult
from pydantic import BaseModel

DADOS = Path(os.getenv("DADOS_DIR", Path(__file__).resolve().parent.parent / "dados"))
HOST = os.getenv("MCP_HOST", "127.0.0.1")
PORTA = int(os.getenv("MCP_PORT", "7301"))
TTL_REQUEST_STATE = 600  # segundos (10 min): o enunciado pede entre 5 e 30 minutos

SP = timezone(timedelta(hours=-3))
JANELA_INICIO, JANELA_FIM = time(8, 0), time(20, 0)
DURACAO_MAXIMA = timedelta(hours=2)

ERRO_SALA = "Sala inexistente: {}"
ERRO_JANELA = "Fora da janela de uso: a politica permite reservas entre 08:00 e 20:00"
ERRO_DURACAO = "Duracao acima do limite: a politica permite no maximo 2 horas"
ERRO_INTERVALO = "Intervalo invalido: fim deve ser posterior a inicio"
ERRO_SEM_ALTERNATIVA = "Sem alternativas disponiveis no intervalo"


def log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


# ---------------------------------------------------------------- chave do requestState


def chave_request_state() -> bytes:
    """REQUEST_STATE_SECRET em hex (>= 32 bytes). Nunca há chave no código."""
    bruto = os.getenv("REQUEST_STATE_SECRET", "").strip()
    try:
        chave = bytes.fromhex(bruto)
    except ValueError:
        chave = b""
    if len(chave) < 32:
        sys.exit(
            "REQUEST_STATE_SECRET ausente ou curta: exporte pelo menos 32 bytes aleatórios em hex, por exemplo\n"
            '  export REQUEST_STATE_SECRET=$(python3 -c "import secrets; print(secrets.token_hex(32))")'
        )
    return chave


# ---------------------------------------------------------------- domínio (dados/ é só leitura)

SALAS: list[dict[str, Any]] = json.loads((DADOS / "salas.json").read_text(encoding="utf-8"))
SALAS_POR_ID = {s["id"]: s for s in SALAS}
POLITICA_TEXTO = (DADOS / "politica-de-uso.md").read_text(encoding="utf-8")
POLITICA_VERSAO = POLITICA_TEXTO.splitlines()[0].split(":", 1)[1].strip()

# Reservas em memória: as criadas durante a execução valem para o mesmo processo, sem disco.
RESERVAS: list[dict[str, str]] = json.loads((DADOS / "reservas.json").read_text(encoding="utf-8"))
_TRAVA = threading.Lock()
_SEQ = itertools.count(len(RESERVAS) + 1)


def instante(valor: str) -> datetime:
    try:
        dt = datetime.fromisoformat(valor)
    except (TypeError, ValueError):
        raise ToolError(f"Data invalida: {valor}") from None
    return dt if dt.tzinfo else dt.replace(tzinfo=SP)


def validar(sala: str, inicio: str, fim: str) -> tuple[datetime, datetime]:
    """Mesmas validações de sala e de política para consulta e reserva."""
    if sala not in SALAS_POR_ID:
        raise ToolError(ERRO_SALA.format(sala))
    ini, fi = instante(inicio), instante(fim)
    if fi <= ini:
        raise ToolError(ERRO_INTERVALO)
    ini_sp, fim_sp = ini.astimezone(SP), fi.astimezone(SP)
    dentro = (
        ini_sp.date() == fim_sp.date()
        and JANELA_INICIO <= ini_sp.time() <= JANELA_FIM
        and JANELA_INICIO <= fim_sp.time() <= JANELA_FIM
    )
    if not dentro:
        raise ToolError(ERRO_JANELA)
    if fi - ini > DURACAO_MAXIMA:
        raise ToolError(ERRO_DURACAO)
    return ini, fi


def conflitos(sala: str, ini: datetime, fi: datetime) -> list[dict[str, str]]:
    return [
        r for r in RESERVAS
        if r["sala"] == sala and instante(r["inicio"]) < fi and ini < instante(r["fim"])
    ]


def alternativas(sala: str, ini: datetime, fi: datetime) -> list[str]:
    """Salas livres no intervalo, capacidade >= a pedida, no máximo 3, por (capacidade, id)."""
    minima = SALAS_POR_ID[sala]["capacidade"]
    livres = [
        s for s in SALAS
        if s["id"] != sala and s["capacidade"] >= minima and not conflitos(s["id"], ini, fi)
    ]
    livres.sort(key=lambda s: (s["capacidade"], s["id"]))
    return [s["id"] for s in livres[:3]]


# ---------------------------------------------------------------- schemas de saída


class SalaOut(BaseModel):
    id: str
    nome: str
    capacidade: int
    recursos: list[str]


class ListaDeSalas(BaseModel):
    salas: list[SalaOut]


class ConflitoOut(BaseModel):
    id: str
    inicio: str
    fim: str
    responsavel: str


class Disponibilidade(BaseModel):
    sala: str
    livre: bool
    conflitos: list[ConflitoOut]


class ReservaOut(BaseModel):
    reserva: str | None = None
    reservado: bool = True
    sala: str | None = None
    inicio: str | None = None
    fim: str | None = None
    responsavel: str | None = None
    politica: str | None = None
    motivo: str | None = None


# ---------------------------------------------------------------- servidor MCP

mcp = MCPServer(
    name="central-de-salas",
    version="1.0.0",
    request_state_security=RequestStateSecurity(keys=[chave_request_state()], ttl=TTL_REQUEST_STATE),
    log_level="WARNING",
)


@mcp.tool(description="Lista todas as salas com capacidade e recursos.")
def listar_salas() -> ListaDeSalas:
    return ListaDeSalas(salas=[SalaOut(**s) for s in SALAS])


@mcp.tool(description="Diz se uma sala esta livre no intervalo, e quais reservas conflitam.")
def consultar_disponibilidade(sala: str, inicio: str, fim: str) -> Disponibilidade:
    ini, fi = validar(sala, inicio, fim)
    em_conflito = [ConflitoOut(**{k: r[k] for k in ("id", "inicio", "fim", "responsavel")}) for r in conflitos(sala, ini, fi)]
    return Disponibilidade(sala=sala, livre=not em_conflito, conflitos=em_conflito)


CHAVE_ELICITATION = "escolha_de_sala"  # chave atribuída pelo servidor em inputRequests
MENSAGEM_ELICITATION = "A sala pedida esta ocupada nesse intervalo. Escolha uma alternativa."
FORM_ELICITATION = {"elicitation": {"form": {}}}


def declarou_form_elicitation(ctx: Context) -> bool:
    """Capability lida do _meta deste request (sem sessão). `elicitation: {}` vale como form."""
    caps = ctx.client_capabilities
    e = caps.elicitation if caps is not None else None
    return e is not None and (e.form is not None or e.url is None)


def pedir_escolha(pedido: dict[str, str], opcoes: list[str]) -> InputRequiredResult:
    estado = {"v": 1, "pedido": pedido, "opcoes": opcoes}
    return InputRequiredResult.model_validate({
        "inputRequests": {
            CHAVE_ELICITATION: {
                "method": "elicitation/create",
                "params": {
                    "mode": "form",
                    "message": MENSAGEM_ELICITATION,
                    "requestedSchema": {
                        "type": "object",
                        "properties": {
                            "sala": {"type": "string", "title": "Sala", "description": "Sala alternativa escolhida", "enum": opcoes},
                        },
                        "required": ["sala"],
                    },
                },
            }
        },
        # Texto puro aqui; o RequestStateBoundary do SDK sela antes de ir para o fio.
        "requestState": json.dumps(estado, separators=(",", ":")),
    })


def gravar(sala: str, inicio: str, fim: str, responsavel: str) -> ReservaOut:
    ini, fi = validar(sala, inicio, fim)
    with _TRAVA:
        if conflitos(sala, ini, fi):
            raise ToolError(f"Sala ocupada no intervalo: {sala}")
        reserva = {"id": f"res-{next(_SEQ):04d}", "sala": sala, "inicio": inicio, "fim": fim, "responsavel": responsavel}
        RESERVAS.append(reserva)
    return ReservaOut(reserva=reserva["id"], sala=sala, inicio=inicio, fim=fim, responsavel=responsavel, politica=POLITICA_VERSAO)


def retomar(estado_selado: str, respostas: dict[str, Any]) -> ReservaOut | InputRequiredResult:
    """Retry: o pedido é reconstruído do requestState (já verificado), não dos argumentos reenviados."""
    estado = json.loads(estado_selado)
    pedido, opcoes = estado["pedido"], estado["opcoes"]
    resposta = respostas.get(CHAVE_ELICITATION)
    if not isinstance(resposta, ElicitResult):  # retry sem a resposta: pergunta de novo, a mesma coisa
        return pedir_escolha(pedido, opcoes)
    if resposta.action == "decline":
        return ReservaOut(reservado=False, motivo="recusado")
    if resposta.action == "cancel":
        return ReservaOut(reservado=False, motivo="cancelado")
    escolhida = (resposta.content or {}).get("sala")
    if escolhida not in opcoes:
        raise ToolError(f"Escolha fora das alternativas oferecidas: {escolhida}")
    return gravar(escolhida, pedido["inicio"], pedido["fim"], pedido["responsavel"])


@mcp.tool(description="Reserva uma sala. Se o intervalo estiver ocupado, pergunta qual alternativa usar.")
def reservar_sala(sala: str, inicio: str, fim: str, responsavel: str, ctx: Context) -> ReservaOut | InputRequiredResult:
    if ctx.request_state is not None:
        return retomar(ctx.request_state, dict(ctx.input_responses or {}))
    ini, fi = validar(sala, inicio, fim)
    if not conflitos(sala, ini, fi):
        return gravar(sala, inicio, fim, responsavel)
    opcoes = alternativas(sala, ini, fi)
    if not opcoes:
        raise ToolError(ERRO_SEM_ALTERNATIVA)
    if not declarou_form_elicitation(ctx):
        raise MCPError(
            code=MISSING_REQUIRED_CLIENT_CAPABILITY,
            message="Client did not declare the form elicitation capability required by reservar_sala",
            data={"requiredCapabilities": FORM_ELICITATION},
        )
    return pedir_escolha({"sala": sala, "inicio": inicio, "fim": fim, "responsavel": responsavel}, opcoes)


@mcp.resource("politica://uso", name="politica-de-uso", description="Politica de uso das salas.", mime_type="text/markdown")
def politica_de_uso() -> str:
    return POLITICA_TEXTO


# ---------------------------------------------------------------- log de cada request no stderr


def com_log(app: Any) -> Any:
    """ASGI: registra método, id e traceparent de todo POST, inclusive os que o SDK rejeita."""

    async def wrapper(scope: dict, receive: Any, send: Any) -> None:
        if scope["type"] != "http" or scope.get("method") != "POST":
            return await app(scope, receive, send)
        corpo, mensagens = b"", []
        while True:
            m = await receive()
            mensagens.append(m)
            corpo += m.get("body", b"")
            if not m.get("more_body"):
                break
        try:
            req = json.loads(corpo)
        except ValueError:
            req = {}
        params = req.get("params") if isinstance(req, dict) else None
        params = params if isinstance(params, dict) else {}
        meta = params.get("_meta") if isinstance(params.get("_meta"), dict) else {}
        extra = f" name={params['name']}" if "name" in params else (f" uri={params['uri']}" if "uri" in params else "")
        if "requestState" in params:
            extra += " retry=sim"
        cab = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope.get("headers", [])}
        caps = meta.get("io.modelcontextprotocol/clientCapabilities")
        info = meta.get("io.modelcontextprotocol/clientInfo")
        extra += f" client={info.get('name', '-') if isinstance(info, dict) else '-'}"
        extra += (
            f" h.mcp-method={cab.get('mcp-method', '-')} h.mcp-name={cab.get('mcp-name', '-')}"
            f" h.mcp-protocol-version={cab.get('mcp-protocol-version', '-')}"
            f" caps={json.dumps(caps, separators=(',', ':')) if caps is not None else '-'}"
        )
        linha = (
            f"[mcp] {datetime.now().isoformat(timespec='milliseconds')} method={req.get('method') if isinstance(req, dict) else None}"
            f" id={req.get('id') if isinstance(req, dict) else None}{extra} traceparent={meta.get('traceparent', '-')}"
        )
        # Valores vêm do cliente: escapa quebras de linha para ninguém forjar uma linha de log.
        log(linha.replace("\\", "\\\\").replace("\r", "\\r").replace("\n", "\\n"))
        fila = iter(mensagens)

        async def replay() -> dict:
            return next(fila, None) or await receive()

        await app(scope, replay, send)

    return wrapper


def main() -> None:
    app = mcp.streamable_http_app(streamable_http_path="/mcp", json_response=True, host=HOST)
    log(f"[mcp] central-de-salas em http://{HOST}:{PORTA}/mcp (politica {POLITICA_VERSAO}, requestState ttl {TTL_REQUEST_STATE}s)")
    uvicorn.run(com_log(app), host=HOST, port=PORTA, log_level="warning")


if __name__ == "__main__":
    main()
