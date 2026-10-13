# Liberar o Prime Vendas para um cliente

## 1. No admin do PrimeSys Info (DFTECH)

1. O cliente precisa estar cadastrado e **Ativo** (CNPJ + token conferido).
2. Coluna **Prime Vendas** → **Liberar**.
   - **Bloquear** desconecta na hora todos os celulares da empresa.

## 2. No ERP do cliente (cada vendedor)

O funcionário precisa estar **ativo** e ter **um** destes:

- **Usuário do ERP vinculado** no cadastro do funcionário (melhor: o app lê o perfil e as permissões de desconto); ou
- o **e-mail de login** dele no **contato** do cadastro do funcionário (o vendedor entra, mas sem permissão de desconto
  se o usuário não estiver vinculado).

A comissão vem do cadastro do funcionário. Mudança no ERP pode levar até 10 minutos para valer no app.

## 3. No celular do vendedor

1. Instalar o APK do Prime Vendas.
2. Entrar com CNPJ, e-mail e senha do ERP (primeiro acesso precisa de internet).
3. Criar o PIN de 4 números e aguardar baixar produtos, clientes e estoque.

## Mensagens do app e o que fazer

| Mensagem | O que fazer |
|---|---|
| "O Prime Vendas não está liberado para esta empresa" | Passo 1 (Liberar no admin) |
| E-mail ou senha inválidos (mensagem do ERP) | Conferir o login no próprio ERP |
| "Seu usuário do ERP não está ligado a um funcionário ativo" | Passo 2 e aguardar até 10 minutos |
| "Acesso expirado" | Entrar de novo com e-mail e senha |

## Atualizações

Quando o Prime Vendas é publicado, aparece no app a faixa **"Nova versão disponível"** (igual ao PrimeSys Info).
Tocar em **Atualizar** recarrega e volta para a mesma tela.
