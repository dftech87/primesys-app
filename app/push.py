"""Notificação no celular (Web Push) para o Prime Info. Só o envio e as chaves; quem decide o que avisar é app/liberacoes.py.

Precisa de um par de chaves VAPID (variáveis de ambiente VAPID_PRIVATE_KEY, VAPID_PUBLIC_KEY e VAPID_SUBJECT). Sem elas o envio fica desligado e o restante continua funcionando (o aviso dentro do app aberto
não depende disto). O conteúdo da notificação nunca leva dado sensível: só o título e uma linha curta."""

import json
import logging
import os

logger = logging.getLogger(__name__)

TTL_SEGUNDOS = 120        # um pedido de liberação velho não adianta: o celular descarta se não conseguir entregar nesse prazo
TIMEOUT_SEGUNDOS = 10


def _chave_privada() -> str | None:
    return (os.getenv("VAPID_PRIVATE_KEY") or "").strip() or None


def chave_publica() -> str | None:
    """Chave pública (base64 url) que o navegador do celular usa para se inscrever."""
    return (os.getenv("VAPID_PUBLIC_KEY") or "").strip() or None


def _contato() -> str | None:
    """Contato que o serviço de notificação do navegador recebe junto com cada envio (exigência do padrão). Vem da variável
    VAPID_SUBJECT: um "mailto:" de suporte ou o endereço público do app. Não existe valor padrão de propósito."""
    return (os.getenv("VAPID_SUBJECT") or "").strip() or None


def ligado() -> bool:
    return bool(_chave_privada() and chave_publica() and _contato())


class InscricaoMorta(Exception):
    """O serviço de notificação disse que este celular não existe mais (app desinstalado, permissão retirada)."""


async def enviar(inscricao: dict, conteudo: dict) -> bool:
    """Envia uma notificação para um celular inscrito. True se o serviço aceitou. Levanta InscricaoMorta quando o celular
    saiu (a inscrição deve ser apagada); qualquer outra falha só devolve False (tenta no próximo aviso)."""
    if not ligado():
        return False
    from pywebpush import WebPushException, webpush_async   # só carrega se as chaves existirem

    try:
        await webpush_async(
            subscription_info={"endpoint": inscricao["endpoint"], "keys": {"p256dh": inscricao["p256dh"], "auth": inscricao["auth"]}},
            data=json.dumps(conteudo, ensure_ascii=False),
            vapid_private_key=_chave_privada(),
            vapid_claims={"sub": _contato()},
            ttl=TTL_SEGUNDOS,
            headers={"Urgency": "high"},
            timeout=TIMEOUT_SEGUNDOS,
        )
        return True
    except WebPushException as erro:
        resposta = getattr(erro, "response", None)
        codigo = getattr(resposta, "status", None) or getattr(resposta, "status_code", None)
        if codigo in (404, 410):
            raise InscricaoMorta() from erro
        logger.warning("notificação não entregue (HTTP %s)", codigo)
        return False
    except Exception:
        logger.warning("notificação não entregue (falha ao falar com o serviço de notificação)", exc_info=True)
        return False
