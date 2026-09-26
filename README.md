# PrimeSys Dashboard

Dashboard gerencial (BI) multi-tenant que consome a API pública do **Meu ERP Online**
(`https://api.meuerponline.com.br/publica`, Swagger em `swagger.json`).

Cada empresa cliente (tenant) é identificada pelo **CNPJ** e tem seu próprio token de
API do Meu ERP Online, cadastrado localmente. O primeiro tenant de teste é a **DFTECH**.

## Arquitetura

- **FastAPI** (`app/main.py`) com sessão via cookie assinado (`SessionMiddleware`).
- **SQLite** (`primesys.db`) guarda os tenants: CNPJ, nome, token da API do ERP e senha
  de acesso ao dashboard (hash bcrypt). Tabela `empresas` em [app/database.py](app/database.py).
- **`app/meuerp_client.py`**: cliente HTTP assíncrono para a API do Meu ERP Online, com
  rate limiting client-side (token bucket) respeitando o limite documentado: 20
  requisições por token, repondo 20/min. Em caso de HTTP 429 faz retry com backoff.
- **Login** (`app/routers/auth.py`): `GET/POST /login` por CNPJ + senha → sessão.
- **Dashboard** (`app/routers/dashboard.py`): `GET /` (página) e
  `GET /api/dashboard/summary` (dados: contas a receber/pagar pendentes e mercadorias
  mais vendidas nos últimos 30 dias, via NF-e modelo 55).

## Rodando localmente

```bash
.venv/Scripts/pip.exe install -r requirements.txt
.venv/Scripts/python.exe -m uvicorn app.main:app --reload --port 8000
```

Acesse `http://127.0.0.1:8000`.

## Cadastrando um novo cliente (tenant)

Cada cliente gera seu próprio token de API dentro do painel do Meu ERP Online e informa
o CNPJ. Cadastre com:

```bash
.venv/Scripts/python.exe scripts/seed_tenant.py --cnpj 00000000000191 --nome "Nome do Cliente" --token "TOKEN_DO_CLIENTE" --senha "senha-de-acesso-ao-dashboard"
```

O primeiro tenant (DFTECH) é seedado automaticamente a partir de `.env`
(`MEUERP_CNPJ`, `MEUERP_TOKEN`, `DASHBOARD_PASSWORD`) rodando o script sem argumentos.

## Configuração (`.env`, não versionado)

```
MEUERP_BASE_URL=https://api.meuerponline.com.br/publica
MEUERP_CNPJ=...
MEUERP_TOKEN=...
DASHBOARD_PASSWORD=...
SESSION_SECRET=...
```

## Descobertas da API (Meu ERP Online)

- Autenticação: header `Authorization: Authentication {token}` (um token por empresa).
- Rate limit por token: 20 requisições, repondo 20/min, fila de 10, HTTP 429 se exceder.
- 258 endpoints públicos: Financeiro, Mercadoria, Fiscal, Pessoa, Documento, Animal,
  Agenda, Inventário, Ordem de Serviço, Tributação, Usuário, Veículo, etc.
- `GET /api/conta-receber(-pagar)/pendentes/v1` exige `inicio`/`fim` na prática, mesmo
  não marcados como obrigatórios no Swagger — usamos uma janela ampla (-2 anos a +1 ano).
- `GET /api/documento/mercadorias-vendidas/v1` exige `Modelo` do documento fiscal
  (ex.: `55` = NF-e, `65` = NFC-e). Hoje fixo em `55`; próximo passo é tornar
  configurável por tenant (alguns clientes emitem majoritariamente NFC-e).

## Próximos passos sugeridos

- Tornar o `Modelo` de documento fiscal configurável por tenant.
- Mais indicadores: DRE (`/api/dre/v1`), estoque por local, ordens de serviço.
- Tela de administração para cadastrar/editar tenants sem usar o script CLI.
- Criptografar `api_token` em repouso no SQLite (hoje fica em texto plano).
