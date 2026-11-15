# Desafio 04 — A Ponte: agente A2A com MCP por dentro. Rode `make help`.
#
# Dois processos Python (uv): servidor-mcp/ (:7301/mcp) e agente/ (:7300/a2a).
# O .env (gitignored) é carregado e exportado; `make setup` o cria com um REQUEST_STATE_SECRET novo.

SHELL := /bin/bash
-include .env
export
EVID := docs/evidencias

.PHONY: upstream help setup env mcp agente validar fluxo extras test evidencias parar clean

help: ## Lista os alvos
	@grep -hE '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | awk 'BEGIN{FS=":.*?## "}{printf "  \033[36m%-11s\033[0m %s\n", $$1, $$2}'

setup: env ## Instala as dependências travadas (uv.lock) dos dois processos
	@cd servidor-mcp && uv sync -q && echo "✓ servidor-mcp/.venv"
	@cd agente && uv sync -q && echo "✓ agente/.venv"

env: ## Cria o .env com um REQUEST_STATE_SECRET aleatório, se ainda não existir (não imprime o valor)
	@[ -f .env ] || { sed "s/^REQUEST_STATE_SECRET=.*/REQUEST_STATE_SECRET=$$(python3 -c 'import secrets; print(secrets.token_hex(32))')/" .env.example > .env && echo "✓ .env criado com REQUEST_STATE_SECRET novo (32 bytes)"; }
	@n=$$(grep -E '^REQUEST_STATE_SECRET=' .env | cut -d= -f2- | tr -d '\n' | wc -c); echo "REQUEST_STATE_SECRET: $$n caracteres hex"

mcp: ## Sobe o servidor MCP em primeiro plano (stderr visível)
	uv run --project servidor-mcp python servidor-mcp/servidor.py

agente: ## Sobe o agente A2A em primeiro plano
	uv run --project agente python agente/agente.py

validar: ## Sobe os dois do zero, roda validador/validar.py e confere o stderr do MCP -> docs/evidencias/
	@scripts/validar.sh

fluxo: ## Passos manuais do Fluxo do avaliador (3, 7-13), com restart do MCP -> docs/evidencias/fluxo-avaliador.txt
	@python3 scripts/fluxo_avaliador.py | tee $(EVID)/fluxo-avaliador.txt

extras: upstream ## Segredo obrigatório, sem segredo no código, -32020, sem SDK de LLM, pastas protegidas
	@scripts/verificacoes_extras.sh | tee $(EVID)/verificacoes-extras.txt

test: validar fluxo extras ## Tudo: validador (36) + conferência de logs + fluxo do avaliador + extras

evidencias: test ## Alias de test (as saídas já ficam em docs/evidencias/)

parar: ## Derruba o que estiver nas portas 7300 e 7301
	-@fuser -k 7300/tcp 7301/tcp 2>/dev/null; true

clean: parar ## Remove caches e venvs
	rm -rf servidor-mcp/.venv agente/.venv servidor-mcp/__pycache__ agente/__pycache__

upstream: ## Garante o remote upstream (repositório base) usado nas comparações; cria no clone limpo
	@git remote get-url upstream >/dev/null 2>&1 || git remote add upstream https://github.com/devfullcycle/desafio-a2a-com-mcp.git
	@git fetch -q upstream
