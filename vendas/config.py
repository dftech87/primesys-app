"""Configuração do Prime Vendas (só variáveis de ambiente; nada de segredo no código)."""

import os

from app.security import normalize_cnpj

# A liberação de cada empresa é o botão "Prime Vendas" no /admin do PrimeSys Info (coluna vendas_liberado).
# Opcional: CNPJs liberados também por variável (separados por vírgula), útil em testes locais.
CNPJS_LIBERADOS = {normalize_cnpj(c) for c in os.getenv("VENDAS_CNPJS_LIBERADOS", "").split(",") if normalize_cnpj(c)}


def empresa_liberada(tenant: dict | None) -> bool:
    return bool(tenant) and (tenant["cnpj"] in CNPJS_LIBERADOS or bool(tenant.get("vendas_liberado")))

# O ERP deixa 20 chamadas por minuto por token, somando os dois apps. O Vendas fica com uma parte menor
# (quase tudo dele é sincronização em segundo plano) para nunca deixar o PrimeSys Info lento.
COTA_ERP_POR_MINUTO = int(os.getenv("VENDAS_COTA_ERP_POR_MINUTO", "10"))

# Acesso do aparelho no servidor: vence se o celular ficar esse tempo sem usar (aí entra de novo com e-mail e senha).
APARELHO_VENCE_DIAS = int(os.getenv("VENDAS_APARELHO_VENCE_DIAS", "30"))
# No celular: o PIN deixa de abrir o app depois de tantos dias sem conseguir falar com o servidor.
PIN_OFFLINE_DIAS = int(os.getenv("VENDAS_PIN_OFFLINE_DIAS", "7"))
# De quanto em quanto tempo o servidor confere no ERP se o vendedor continua ativo e vinculado.
RECONFERIR_VENDEDOR_HORAS = 6

# Permissões do perfil do usuário no ERP (módulo POS) que valem para o app.
PERMISSAO_DESCONTO_DOCUMENTO = 10037
PERMISSAO_DESCONTO_ITEM = 10038
PERMISSAO_DESCONTO_ACIMA_DO_LIMITE = 10042
PERMISSAO_ACRESCIMO_DOCUMENTO = 10096

# Política de privacidade (uma só para os dois apps, publicada no Prime Info).
URL_PRIVACIDADE = os.getenv("VENDAS_URL_PRIVACIDADE", "https://app.primesysapp.com.br/privacidade")
