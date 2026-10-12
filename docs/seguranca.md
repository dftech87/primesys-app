# Segurança do PrimeSys

Como os dados ficam protegidos e o passo a passo para trocar cada chave. Valores de chaves e senhas
**nunca** vão para o GitHub, para chat, e-mail ou print: só para as variáveis do Railway e para o
gerenciador de senhas de quem administra.

## O que é sensível
| Dado | Onde fica | Proteção |
|---|---|---|
| Token da API de cada cliente | Banco de clientes (volume do Railway) | Criptografado (Fernet/AES) com `TOKEN_ENCRYPTION_KEY`; a chave fica só na variável de ambiente |
| Senha dos usuários | Em lugar nenhum | O login é validado direto no ERP do cliente; o app não guarda senha |
| Sessão de login | Cookie no aparelho | Assinado com `SESSION_SECRET`, só por HTTPS, `SameSite=lax`, validade de 7 dias |
| Dados do ERP (vendas, estoque...) | Só na memória do servidor (cache curto) | Separados por CNPJ; respostas `/api/*` com `Cache-Control: no-store` |

## Variáveis do Railway
| Variável | Para quê | Se faltar |
|---|---|---|
| `SESSION_SECRET` | Assinar o cookie de login | O app usa um segredo aleatório por processo (todos saem a cada reinício) |
| `ADMIN_USER` / `ADMIN_PASSWORD` | Entrar no `/admin` | Login do `/admin` desativado (também se a senha for a padrão `1608`) |
| `TOKEN_ENCRYPTION_KEY` | Criptografar os tokens | Tokens gravados em texto simples (aviso no log) |
| `TOKEN_ENCRYPTION_KEY_ANTERIOR` | Só durante a troca da chave | — (não deve existir fora da troca) |

`MEUERP_CNPJ` e `MEUERP_TOKEN` não são mais necessárias (serviam para criar o primeiro cliente) e devem
ser removidas: um valor errado ali já causou um token trocado.

## Proteções automáticas
- **Conferência do token**: no login, na abertura do app e no cadastro do `/admin`, o servidor pergunta ao
  ERP (`/api/empresa/v1`) de que empresa é o token. Token de outra empresa: acesso bloqueado e cadastro
  recusado. O `/admin` mostra a conferência de cada cliente (inclusive token repetido).
- **Limite de tentativas de login**: 5 erros por CNPJ+e-mail e 30 por rede em 15 minutos; 5 por rede no
  `/admin`. Reiniciar o serviço zera os bloqueios.
- **Cabeçalhos HTTP**: HTTPS obrigatório (HSTS), app proibido dentro de outro site, sem adivinhação de
  tipo de arquivo, endereço não vaza para outros sites, formulários só para o próprio app, câmera só para
  o próprio app.
- **Chart.js com verificação de integridade (SRI)**: se o arquivo da CDN for alterado, o navegador recusa.
  Ao atualizar a versão do Chart.js, recalcule o `integrity` (ver abaixo).

## Trocar a chave de criptografia (`TOKEN_ENCRYPTION_KEY`)
Faça fora do horário de uso (o deploy tem uma queda curta).
1. Gere a chave nova no seu terminal e guarde no gerenciador de senhas:
   `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"`
2. No Railway, crie `TOKEN_ENCRYPTION_KEY_ANTERIOR` com o valor **atual** de `TOKEN_ENCRYPTION_KEY`.
3. Troque `TOKEN_ENCRYPTION_KEY` pela chave **nova** e faça o deploy.
4. No log, confira: `N token(s) de API regravados com a chave nova`. Entre no app com um cliente.
5. Apague `TOKEN_ENCRYPTION_KEY_ANTERIOR` e faça o deploy de novo. Pode descartar a chave antiga.

Se a chave atual for **perdida**, os tokens ficam ilegíveis: cadastre cada cliente de novo no `/admin`
(o token vem do ERP do cliente). Nenhum dado de venda se perde, porque eles ficam no ERP.

## Trocar `SESSION_SECRET`
Gere `python -c "import secrets; print(secrets.token_urlsafe(48))"`, troque a variável e faça o deploy.
Todos os usuários precisam entrar de novo uma vez.

## Trocar a senha do `/admin`
Troque `ADMIN_PASSWORD` (20+ caracteres; evite `# $ " ' \` e espaço) e faça o deploy. Use o campo simples
da variável, não o editor "Raw".

## Se um token de cliente vazar
1. Gere um token novo no ERP do cliente (isso invalida o antigo).
2. Cadastre o token novo no `/admin` (ele confere se é da empresa certa).

## Rotina
- Conferir vulnerabilidades das dependências:
  `pip install pip-audit` e `pip-audit -r requirements.txt` (última verificação: 2026-10-12, nenhuma).
- Recalcular o SRI do Chart.js ao trocar de versão:
  `curl -sL <url do arquivo> | openssl dgst -sha384 -binary | openssl base64 -A` (prefixe com `sha384-`).
