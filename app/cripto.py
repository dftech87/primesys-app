"""Criptografia dos tokens de API guardados no banco (Fernet: AES + autenticação).

A chave vem da variável de ambiente TOKEN_ENCRYPTION_KEY, nunca do banco: quem levar só o arquivo do banco
não consegue usar os tokens. Sem a variável, tudo segue como antes (texto simples) e o app avisa no log;
isso permite publicar o código antes de definir a chave sem derrubar ninguém.

Valores protegidos começam com "enc:v1:". Valores sem esse prefixo são texto simples (bancos antigos) e
continuam sendo lidos normalmente; a migração os converte quando a chave existir.
"""

import logging
from functools import lru_cache

from cryptography.fernet import Fernet, InvalidToken, MultiFernet

from app.config import settings

logger = logging.getLogger(__name__)

PREFIXO = "enc:v1:"


class CriptoErro(Exception):
    """Não foi possível ler um valor protegido (chave ausente ou diferente da usada para gravar)."""


def _carregar(nome: str, valor: str) -> Fernet:
    try:
        return Fernet(valor.encode())
    except ValueError as erro:   # a mensagem não inclui a chave
        raise RuntimeError(f"{nome} inválida: use uma chave gerada por Fernet.generate_key().") from erro


@lru_cache(maxsize=1)
def _fernet() -> MultiFernet | None:
    """Chave atual (grava e lê) e, só durante uma troca, a anterior (só lê). Ver docs/seguranca.md."""
    chave = (settings.token_encryption_key or "").strip()
    anterior = (settings.token_encryption_key_anterior or "").strip()
    if not chave:
        if anterior:
            raise RuntimeError("TOKEN_ENCRYPTION_KEY_ANTERIOR definida sem TOKEN_ENCRYPTION_KEY.")
        logger.warning("TOKEN_ENCRYPTION_KEY não definida: os tokens continuam gravados em texto simples.")
        return None
    chaves = [_carregar("TOKEN_ENCRYPTION_KEY", chave)]
    if anterior and anterior != chave:
        chaves.append(_carregar("TOKEN_ENCRYPTION_KEY_ANTERIOR", anterior))
    return MultiFernet(chaves)


def em_troca_de_chave() -> bool:
    anterior = (settings.token_encryption_key_anterior or "").strip()
    return bool(anterior) and anterior != (settings.token_encryption_key or "").strip()


def regravar_com_chave_atual(valor: str) -> str:
    """Token protegido (com a chave atual ou a anterior) regravado com a chave atual."""
    return PREFIXO + _fernet().rotate(valor[len(PREFIXO):].encode()).decode()


def ativa() -> bool:
    return _fernet() is not None


def protegido(valor: str) -> bool:
    return valor.startswith(PREFIXO)


def proteger(texto: str) -> str:
    """Criptografa para gravar. Sem chave (ou já protegido), devolve o valor como está."""
    f = _fernet()
    if f is None or protegido(texto):
        return texto
    return PREFIXO + f.encrypt(texto.encode()).decode()


def revelar(valor: str) -> str:
    """Devolve o texto original para usar na chamada ao ERP."""
    if not protegido(valor):
        return valor
    f = _fernet()
    if f is None:
        raise CriptoErro("há tokens criptografados, mas TOKEN_ENCRYPTION_KEY não está definida")
    try:
        return f.decrypt(valor[len(PREFIXO):].encode()).decode()
    except InvalidToken as erro:
        raise CriptoErro("TOKEN_ENCRYPTION_KEY não corresponde à chave usada para gravar os tokens") from erro
