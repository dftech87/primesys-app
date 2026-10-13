# Prime Vendas no Railway

Serviço próprio (`vendas`) no mesmo projeto do PrimeSys Info (`efficient-rebirth`), usando o mesmo Postgres.
Mesmo repositório do GitHub; cada serviço só publica quando muda o código dele (Watch Paths).

| Serviço | Código | Comando |
|---|---|---|
| `Prime Sys Infor` (PrimeSys Info, antes `web`) | `app/` | `Procfile` (uvicorn app.main:app) |
| `vendas` (Prime Vendas) | `vendas/` + peças compartilhadas de `app/` | `vendas/railway.json` (uvicorn vendas.main:app) |

## 1. Watch Paths do PrimeSys Info (antes de publicar o Vendas)

Serviço **Prime Sys Infor → Settings → Build → Watch Paths**, um por linha:

```
/app/**
/requirements.txt
/Procfile
```

Assim, mudar só o Vendas não reinicia o PrimeSys Info.

## 2. Criar o serviço `vendas`

1. No projeto: **+ Add → GitHub Repo →** o mesmo repositório do PrimeSys.
2. Renomear o serviço novo para `vendas` (Settings → nome no topo).
3. **Settings → Config-as-code → Railway Config File:** `/vendas/railway.json`
   (define o comando de início, o `/saude` e as pastas observadas).
4. **Variables** do `vendas`:
   - `DATABASE_URL` = `${{Postgres.DATABASE_URL}}`
   - `TOKEN_ENCRYPTION_KEY` = referência ao serviço do PrimeSys Info (digite `${{` e escolha na lista; mesma chave, é ela que abre os tokens do ERP)
   - `VENDAS_CNPJS_LIBERADOS` = `29970763000147` (fase de testes: só a DFTECH; separar CNPJs por vírgula)
5. **Settings → Networking → Generate Domain.** Se der, editar o nome para algo como `prime-vendas`.
6. Deploy. Nos logs não pode haver erro vermelho; `https://<domínio>/saude` deve responder `{"ok": true}`.

## Segurança

- O `vendas` não tem tela de administração; liberação de empresas e (depois) aparelhos ficam no `/admin` do web.
- `DATABASE_URL` e `TOKEN_ENCRYPTION_KEY` só nas variáveis do Railway (nunca em print, chat ou Git).
- O banco guarda só o hash do token de cada celular; desconectar um aparelho vale na hora.

## Variáveis opcionais

| Variável | Padrão | Para que |
|---|---|---|
| `VENDAS_COTA_ERP_POR_MINUTO` | 10 | Parte da cota de 20/min do ERP que o Vendas pode usar |
| `VENDAS_APARELHO_VENCE_DIAS` | 30 | Aparelho sem uso por esse tempo precisa entrar de novo |
| `VENDAS_PIN_OFFLINE_DIAS` | 7 | Dias que o PIN funciona sem falar com o servidor |
