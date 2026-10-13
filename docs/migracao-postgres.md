# Migração do cadastro de empresas para Postgres (Railway)

O app usa Postgres quando existe a variável `DATABASE_URL`. Sem ela, continua usando o arquivo SQLite
(`DATABASE_PATH`). Na primeira vez que liga com Postgres **vazio**, copia sozinho os cadastros do arquivo SQLite,
uma única vez, sem abrir os tokens (eles vão criptografados como estão) e conferindo tudo no final.

Por que: o arquivo SQLite fica num volume, e serviço com volume no Railway fica alguns segundos fora do ar a cada
publicação. Com Postgres o volume sai, e o banco passa a poder ser usado também pelo Prime Vendas.

## Passo a passo no Railway

1. **Plano pago** ativo no projeto.
2. No projeto: **+ New → Database → Add PostgreSQL**. O Railway cria o serviço `Postgres`.
3. No serviço do PrimeSys Info → **Variables**:
   - Confira que `DATABASE_PATH` está como `/data/primesys.db` (é de onde a cópia lê). **Não apague ainda.**
   - **New Variable**: nome `DATABASE_URL`, valor `${{Postgres.DATABASE_URL}}` (referência; o Railway preenche).
4. Publicar a versão com suporte a Postgres (push na main) — ou, se já publicada, o próprio passo 3 faz o deploy.
5. Nos **logs** do deploy, procurar a linha:
   `N empresa(s) copiadas do arquivo SQLite (/data/primesys.db) para o Postgres.`
   O N tem que ser igual ao número de empresas no `/admin`.
   - Se aparecer `arquivo SQLite ... não encontrado`, o `DATABASE_PATH` está errado: **remova `DATABASE_URL`**
     (volta ao arquivo), corrija e repita. Antes de repetir, apague as empresas do Postgres (tabela `empresas`) ou
     recrie o banco, porque a cópia só acontece com ele vazio.
6. Testar: login de um cliente, `/admin` (lista e "Conferência do token"), `/saude`.

## Depois de alguns dias funcionando

7. Serviço do PrimeSys Info → **Settings → Volumes**: remover o volume `/data`. A partir daí a publicação deixa
   de derrubar o app.
8. Remover a variável `DATABASE_PATH`.
9. **Settings → Healthcheck Path**: `/saude` (o Railway só troca para a versão nova quando ela responde).

## Voltar atrás (antes de remover o volume)

Remover `DATABASE_URL`: o app volta a usar o arquivo. Atenção: o que for cadastrado ou alterado no `/admin`
depois da troca fica só no Postgres e não volta para o arquivo.

## Segurança

- `DATABASE_URL` contém a senha do banco: fica só nas variáveis do Railway, nunca em chat, print ou Git.
- Os tokens continuam criptografados com `TOKEN_ENCRYPTION_KEY`; quem lê o banco sem a chave não abre os tokens.
- Use a referência `${{Postgres.DATABASE_URL}}` (rede interna do Railway), não o endereço público do banco.
