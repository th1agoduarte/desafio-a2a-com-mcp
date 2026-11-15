"""Automatiza os passos manuais do "Fluxo do avaliador" (3 e 7 a 13) com os corpos de exemplos/wire/.

Sobe os dois processos do zero (uv run), executa os passos, reinicia o servidor MCP no meio
do passo 12 e derruba tudo no fim. Biblioteca padrão apenas. Requer REQUEST_STATE_SECRET.

    python3 scripts/fluxo_avaliador.py            # saída: docs/evidencias/fluxo-avaliador.txt
"""

from __future__ import annotations

import copy
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
WIRE = RAIZ / "exemplos" / "wire"
EVID = RAIZ / "docs" / "evidencias"
AGENTE, MCP = "http://localhost:7300", "http://localhost:7301/mcp"
falhas = 0


def wire(nome: str) -> dict:
    return json.loads((WIRE / nome).read_text(encoding="utf-8"))["request"]


def http(url: str, corpo: dict | None = None, cabecalhos: dict | None = None) -> tuple[int, dict]:
    dados = json.dumps(corpo).encode() if corpo is not None else None
    req = urllib.request.Request(url, data=dados, headers=cabecalhos or {}, method="POST" if dados else "GET")
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


def check(passo: str, ok: bool, detalhe: str = "") -> None:
    global falhas
    falhas += not ok
    print(f"{'PASS' if ok else 'FAIL'} passo {passo}{': ' + detalhe if detalhe else ''}", flush=True)


def subir(cmd: list[str], log: Path, porta_url: str) -> subprocess.Popen:
    proc = subprocess.Popen(cmd, cwd=RAIZ, stderr=log.open("a"), stdout=subprocess.DEVNULL)
    for _ in range(120):
        try:
            urllib.request.urlopen(porta_url, timeout=1)
            return proc
        except urllib.error.HTTPError:
            return proc  # respondeu (405/406 no GET do /mcp é resposta)
        except Exception:
            time.sleep(0.25)
    raise SystemExit(f"não subiu: {' '.join(cmd)}")


def a2a(metodo: str, params: dict, traceparent: str | None = None) -> dict:
    cab = {"Content-Type": "application/json"}
    if traceparent:
        cab["traceparent"] = traceparent
    return http(f"{AGENTE}/a2a", {"jsonrpc": "2.0", "id": 1, "method": metodo, "params": params}, cab)[1]


def tarefa(resp: dict) -> dict:
    return (resp.get("result") or {}).get("task") or resp.get("result") or {}


def texto_status(t: dict) -> str:
    return " ".join(p.get("text", "") for p in ((t.get("status") or {}).get("message") or {}).get("parts", []))


def continuar(task_id: str, texto: str) -> dict:
    msg = copy.deepcopy(wire("10-a2a-send-message-continuacao.json")["body"]["params"])
    msg["message"]["taskId"] = task_id
    msg["message"]["parts"] = [{"text": texto}]
    msg["message"]["messageId"] = f"msg-{time.time_ns()}"
    return a2a("SendMessage", msg)


def main() -> int:
    if not os.getenv("REQUEST_STATE_SECRET"):
        sys.exit("exporte REQUEST_STATE_SECRET antes (veja o README)")
    EVID.mkdir(parents=True, exist_ok=True)
    log_mcp = EVID / "fluxo-avaliador-servidor-mcp.stderr.txt"
    log_mcp.write_text("")
    (EVID / "fluxo-avaliador-agente.stderr.txt").write_text("")
    cmd_mcp = ["uv", "run", "--project", "servidor-mcp", "python", "servidor-mcp/servidor.py"]
    mcp = subir(cmd_mcp, log_mcp, MCP)
    agente = subir(["uv", "run", "--project", "agente", "python", "agente/agente.py"],
                   EVID / "fluxo-avaliador-agente.stderr.txt", f"{AGENTE}/.well-known/agent-card.json")
    try:
        # 3. card contra o exemplo
        _, card = http(f"{AGENTE}/.well-known/agent-card.json")
        esperado = json.loads((WIRE / "07-a2a-agent-card.json").read_text())["response"]["body"]
        i, e = card["supportedInterfaces"][0], esperado["supportedInterfaces"][0]
        check("3", i["protocolBinding"] == e["protocolBinding"] and i["protocolVersion"] == e["protocolVersion"]
              and i["url"].endswith("/a2a") and [s["id"] for s in card["skills"]] == ["reservar-sala"],
              f"supportedInterfaces={json.dumps(card['supportedInterfaces'])} skills={[s['id'] for s in card['skills']]}")

        # 7. SendMessage de exemplos/wire/08 (sala-garagem 14h-15h, 03/11/2026)
        r8 = wire("08-a2a-send-message.json")
        t = tarefa(a2a("SendMessage", r8["body"]["params"], r8["headers"]["traceparent"]))
        check("7", (t.get("status") or {}).get("state") == "TASK_STATE_INPUT_REQUIRED"
              and texto_status(t) == "alternativas: sala-fusca, sala-mirante",
              f"estado={(t.get('status') or {}).get('state')} mensagem={texto_status(t)!r}")

        # 8. escolha=sala-mirante e GetTask
        continuar(t["id"], "escolha=sala-mirante")
        g = tarefa(a2a("GetTask", {"id": t["id"]}))
        art = (g.get("artifacts") or [{}])[0]
        conteudo = json.loads(" ".join(p.get("text", "") for p in art.get("parts", [])) or "{}")
        check("8", (g.get("status") or {}).get("state") == "TASK_STATE_COMPLETED" and art.get("name") == "reserva"
              and conteudo.get("sala") == "sala-mirante" and conteudo.get("politica") == "2026-11-01",
              f"estado={(g.get('status') or {}).get('state')} artifact={json.dumps(conteudo)}")

        # 9. repete o 7 e recusa
        t9 = tarefa(a2a("SendMessage", {**r8["body"]["params"], "message": {**r8["body"]["params"]["message"], "messageId": "msg-passo-9"}}))
        r = tarefa(continuar(t9["id"], "escolha=recusar"))
        check("9", (r.get("status") or {}).get("state") == "TASK_STATE_CANCELED",
              f"pausa={texto_status(t9)!r} estado final={(r.get('status') or {}).get('state')}")

        # 10. sala inexistente
        p10 = copy.deepcopy(r8["body"]["params"])
        p10["message"]["parts"] = [{"text": "reservar sala=sala-inexistente inicio=2026-11-03T14:00:00-03:00 fim=2026-11-03T15:00:00-03:00 responsavel=Marty"}]
        p10["message"]["messageId"] = "msg-passo-10"
        t10 = tarefa(a2a("SendMessage", p10))
        historico = " ".join(p.get("text", "") for m in t10.get("history", []) for p in m.get("parts", []))
        check("10", (t10.get("status") or {}).get("state") == "TASK_STATE_FAILED" and "Sala inexistente: sala-inexistente" in historico,
              f"estado={(t10.get('status') or {}).get('state')} mensagem={texto_status(t10)!r}")

        # 11. tools/call de conflito direto no MCP; troca um caractere do requestState
        r3 = wire("03-tools-call-conflito-input-required.json")
        _, conflito = http(MCP, r3["body"], r3["headers"])
        res = conflito.get("result") or {}
        chave, estado = next(iter(res.get("inputRequests") or {"": None})), res.get("requestState") or ""
        meio = len(estado) // 2
        adulterado = estado[:meio] + ("A" if estado[meio] != "A" else "B") + estado[meio + 1:]
        r4 = wire("04-tools-call-retry.json")
        retry = copy.deepcopy(r4["body"])
        retry["params"]["inputResponses"] = {chave: {"action": "accept", "content": {"sala": "sala-fusca"}}}
        retry["params"]["requestState"] = adulterado
        status, resp = http(MCP, retry, r4["headers"])
        check("11", res.get("resultType") == "input_required" and (resp.get("error") or {}).get("code") == -32602,
              f"alternativas={res['inputRequests'][chave]['params']['requestedSchema']['properties']['sala'].get('enum')} "
              f"http={status} error={json.dumps(resp.get('error'))}")

        # 12. mesmo requestState, sem adulterar, depois de reiniciar o servidor MCP
        mcp.terminate()
        mcp.wait(timeout=10)
        with log_mcp.open("a") as f:
            f.write("[fluxo] ---- servidor MCP reiniciado aqui (passo 12) ----\n")
        mcp = subir(cmd_mcp, log_mcp, MCP)
        retry["params"]["requestState"] = estado
        retry["id"] = 99
        status, resp = http(MCP, retry, r4["headers"])
        sc = (resp.get("result") or {}).get("structuredContent") or {}
        check("12", (resp.get("result") or {}).get("resultType") == "complete" and sc.get("reservado") is True and sc.get("sala") == "sala-fusca",
              f"http={status} structuredContent={json.dumps(sc)}")

        # 13. o mesmo conflito sem elicitation nas capabilities
        sem = copy.deepcopy(wire("06-erro-32021-sem-elicitation.json"))
        status, resp = http(MCP, sem["body"], sem["headers"])
        err = resp.get("error") or {}
        check("13", status == 400 and err.get("code") == -32021 and "requiredCapabilities" in (err.get("data") or {}),
              f"http={status} error={json.dumps(err)}")
    finally:
        for proc in (mcp, agente):
            proc.terminate()
    print(f"resumo: {falhas} falha(s)")
    return 1 if falhas else 0


if __name__ == "__main__":
    sys.exit(main())
