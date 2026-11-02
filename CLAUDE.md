# PrimeSys (Prime Info + Prime Vendas)

Produto da DFTECH (CNPJ 29.970.763/0001-47) vendido a lojas que usam o ERP **Meu ERP Online**. Dois apps web (PWA, instalados no Android como TWA), publicados no Railway, mais um Postgres compartilhado.

## Como trabalhar aqui
- **Responda sempre em português do Brasil**, inclusive notas de andamento e mensagens de commit.
- Fluxo de publicação: alterar o código, **testar localmente**, mostrar ao dono, **esperar o "pode publicar"** e só então dar push na `main` (o Railway publica sozinho). Depois do push, **pare**: o dono acompanha o deploy.
- Mudança que mexe em pedido, login, acesso ou dado de cliente: dizer isso antes e propor o desenho antes de construir.
- **Não use `/team`, vários agentes ou revisores sem perguntar** (gasta muito uso). Trabalho comum se faz direto.
- Mantenha o código modular: uma mudança numa tela não deve exigir mexer em rotas de outras telas. Prefira adicionar a reescrever código compartilhado.

## Segurança (regras fixas)
- Nunca expor, ler ou mostrar segredos: tokens do ERP, `DATABASE_URL`, `TOKEN_ENCRYPTION_KEY`, `SESSION_SECRET`, `ADMIN_PASSWORD`, `BACKUP_TOKEN`, keystores, `signing-key-info.txt`, a chave privada do backup, a pasta `APK/` e `primesys.db`. Nada disso vai para o Git, o chat ou prints.
- Nunca rodar `scripts/backup_chaves.py` pelo assistente: o dono roda no próprio terminal.
- O token do ERP de cada cliente dá acesso amplo ao ERP dele (inclusive ao certificado digital). Fica criptografado (`app/cripto.py`) e **cache, consultas e sessões são sempre isolados por CNPJ** (toda chave de cache começa pelo CNPJ).
- O login valida e-mail e senha no próprio ERP; não guardamos senha. Não criar atalho de "admin entra como cliente".
- SQL no ERP (`app/sqlquery.py`) só com modelos fixos e valores por `i()` / `d()` / validadores. Nunca interpolar texto do usuário.
- Conferir no servidor tudo o que o celular manda (preço, desconto, permissões, ponto de equilíbrio). A tela só facilita.
- Sem `confirm`/`alert` do navegador no Prime Vendas: usar `confirmar()`/`avisar()` do app.

## Dados de clientes e testes
- Testes e escritas no Prime Vendas acontecem **só no DFTECH** (CNPJ 29970763000147).
- Ler dados de outro cliente exige autorização do dono. Todo script de teste contra o ERP define `CNPJ_TESTE` **antes** de importar e confere que `/api/empresa/v1` devolve o CNPJ esperado antes de qualquer consulta.
- Cliente fiscal de teste (NFC-e e pré-venda): 68022887000152, autorizado para leitura.

## Cota do ERP
- O ERP aceita **20 chamadas por minuto por token**, somando os dois apps: Prime Info 12 (`ERP_COTA_POR_MINUTO`) + Prime Vendas 8 (`VENDAS_COTA_ERP_POR_MINUTO`). A soma não pode passar de 20.
- Ao criar ou mudar uma tela, conte as chamadas ao ERP que ela gasta, use o cache (`app/cache.py`) e prefira uma consulta SQL a várias chamadas REST.

## Estrutura
- `app/` Prime Info: `routers/` (`auth`, `admin`, `produtos`, `painel_mobile`), `acessos.py` (abas e ficha por perfil do ERP), `meuerp_client.py`, `sqlquery.py`, `cache.py`, `datas.py` (`hoje_loja()`: sempre o horário de Brasília), tela em `templates/produtos.html`.
- `vendas/` Prime Vendas: `main.py`, `catalogo.py` (cópia do catálogo por empresa), `pedidos.py`, `vendedores.py`, `sessao.py`, `clientes.py`, `pdf_pedido.py`, tela em `static/index.html` (funciona offline: IndexedDB e fila de envio).
- O Vendas importa módulos de `app/`. A lista de arquivos que republica o Vendas está em `vendas/railway.json` (`watchPatterns`): ao fazer o Vendas depender de um módulo novo de `app/`, acrescente-o lá.
- Documentação em `docs/` (permissões do perfil, liberar o Prime Vendas, Railway, segurança, roteiro de cliente novo). `swagger.json` é a documentação da API do ERP.

## Executar localmente
- Info: `.venv/Scripts/python.exe -m uvicorn app.main:app --port 8000`
- Vendas: `.venv/Scripts/python.exe -m uvicorn vendas.main:app --port 8020`
- Sem `DATABASE_URL`, os dois usam SQLite local (testes).

## Pontos de atenção
- Cada serviço roda com **1 réplica e 1 worker**: balde de cota, cache e limites de login ficam na memória do processo.
- Mexer nas permissões do ERP usadas pelos apps: atualizar `docs/permissoes-perfil-erp.md`.
- Não usar a marca ou o layout da IT WORKS, nem imagens com dados pessoais, em materiais de divulgação.
