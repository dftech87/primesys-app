"""Comunicação com a API do ERP, para o /admin mostrar se está funcionando e, se não estiver, o MOTIVO.

Em 25/11/2026 o ERP passou a recusar (403) as chaves de clientes sem um plano da API ativo, e as telas diziam só "não foi
possível validar" ou "não reconheceu esse token". Aqui cada recusa vira um estado com texto claro. O teste é uma leitura
(GET /api/empresa/v1), gasta 1 chamada da cota do cliente e nunca devolve nem registra o token.
"""

import asyncio
import logging
import re
import time
from datetime import datetime

import httpx

from app.cache import cached, descartar
from app.datas import FUSO_LOJA
from app.meuerp_client import MeuERPClient
from app.security import normalize_cnpj

logger = logging.getLogger(__name__)

ESPERA_MAXIMA = 8.0     # a página do /admin nunca espera mais que isso pelo ERP
VALE = 120              # a lista reaproveita o último teste por 2 minutos
INTERVALO_MINIMO = 10   # "Testar agora" repetido antes disso devolve o teste que acabou de ser feito (não queima a cota do cliente)

TEXTOS = {
    "ok": ("API do ERP funcionando", ""),
    "licenca": ("API do ERP sem licença", "Ative a licença da API pública deste CNPJ no Meu ERP. Enquanto isso, o cliente não consegue entrar nos apps."),
    "chave": ("Chave da API recusada", "A chave está errada, incompleta ou foi trocada no ERP. Copie de novo e use Trocar token."),
    "recusada": ("API do ERP recusou o acesso", "Veja o motivo informado pelo ERP."),
    "outra": ("Chave de outra empresa", "A chave cadastrada é de outro CNPJ. Use Trocar token."),
    "limite": ("Limite de chamadas do ERP atingido", "A comunicação existe, mas a cota do minuto acabou. Teste de novo em 1 minuto."),
    "lento": ("ERP sem resposta a tempo", "O ERP demorou para responder. Teste de novo em instantes."),
    "fora": ("ERP fora do ar ou com erro", "Teste de novo em instantes. Se continuar, fale com o suporte do ERP."),
}


def _motivo(resposta: httpx.Response, token: str) -> str:
    """Texto curto que o próprio ERP devolveu. Página de erro em HTML não ajuda e fica de fora."""
    texto = resposta.text
    if "<html" in texto.lower() or "<!doctype" in texto.lower():
        return ""
    return re.sub(r"\s+", " ", texto.replace(token, "") if token else texto).strip()[:200]


async def testar(token: str) -> dict:
    """Pergunta ao ERP de quem é o token. Devolve o estado, o motivo do ERP e (se funcionou) a empresa dona do token."""
    r = {"estado": "fora", "motivo": "", "http": 0, "cnpj": "", "fantasia": "", "razao": ""}
    if not re.fullmatch(r"[\x21-\x7e]+", token or ""):   # vazia, com espaço ou acento: nem chega a ir para o ERP
        r["estado"] = "chave"
        return _pronto(r)
    try:
        # shield: se passar do tempo, a chamada termina sozinha e a conexão compartilhada com os clientes não é interrompida
        chamada = asyncio.ensure_future(MeuERPClient(token).get("/api/empresa/v1"))
        chamada.add_done_callback(lambda t: t.cancelled() or t.exception())   # erro que chega depois do tempo não vira alerta solto no log
        dados = await asyncio.wait_for(asyncio.shield(chamada), ESPERA_MAXIMA)
        r["http"], r["cnpj"] = 200, normalize_cnpj(dados.get("cnpjCpf"))
        r["fantasia"], r["razao"] = (dados.get("fantasia") or "").strip(), (dados.get("razaoSocial") or "").strip()
        r["estado"] = "ok" if r["cnpj"] else "fora"
    except (asyncio.TimeoutError, httpx.TimeoutException):
        r["estado"] = "lento"
    except httpx.HTTPStatusError as erro:
        r["http"], r["motivo"] = erro.response.status_code, _motivo(erro.response, token)
        if r["http"] == 401:
            r["estado"] = "chave"
        elif r["http"] == 403:
            r["estado"] = "licenca" if "licen" in r["motivo"].lower() else "recusada"
        elif r["http"] == 429:
            r["estado"] = "limite"
    except Exception as erro:   # sem rede, resposta que não é JSON, token com caractere inválido...
        logger.warning("teste da API do ERP falhou: %s", type(erro).__name__)
    return _pronto(r)


def _pronto(r: dict) -> dict:
    r["titulo"], r["dica"] = TEXTOS[r["estado"]]
    r["em"] = time.monotonic()
    r["quando"] = datetime.now(FUSO_LOJA).strftime("%d/%m às %H:%M:%S")
    return r


def explicar(r: dict) -> str:
    """Uma frase para os avisos do /admin (trocar token, cliente novo)."""
    motivo = f' O ERP respondeu: "{r["motivo"]}"' if r["motivo"] else ""
    return f"{r['titulo']}.{motivo} {r['dica']}".strip()


def _chave(cnpj: str) -> tuple:
    return (normalize_cnpj(cnpj), "estado-da-api")


async def do_cliente(cliente: dict, agora: bool = False) -> dict:
    """Estado da API de um cliente cadastrado. `agora` refaz o teste (respeitando o intervalo mínimo)."""
    async def produzir() -> dict:
        if not cliente["api_token"]:
            return {**_sem_chave(), "em": time.monotonic()}
        r = await testar(cliente["api_token"])
        if r["estado"] == "ok" and r["cnpj"] != normalize_cnpj(cliente["cnpj"]):
            r["estado"] = "outra"
            r["titulo"], r["dica"] = TEXTOS["outra"]
        if r["estado"] != "ok":
            logger.warning("API do ERP do CNPJ %s: %s (HTTP %s)", cliente["cnpj"], r["estado"], r["http"])
        return r

    chave = _chave(cliente["cnpj"])
    r = await cached(chave, VALE, produzir)
    if agora and time.monotonic() - r["em"] > INTERVALO_MINIMO:
        descartar(chave)
        r = await cached(chave, VALE, produzir)
    return r


def _sem_chave() -> dict:
    return {"estado": "chave", "motivo": "", "http": 0, "cnpj": "", "fantasia": "", "razao": "", "titulo": "Chave ilegível no cadastro",
            "dica": "A chave de criptografia do servidor está ausente ou diferente.", "quando": datetime.now(FUSO_LOJA).strftime("%d/%m às %H:%M:%S")}


def esquecer(cnpj: str) -> None:
    """Token trocado: o teste guardado era do token antigo."""
    descartar(_chave(cnpj))


def para_tela(r: dict) -> dict:
    """Só o que o navegador precisa (nunca o token nem dados da empresa)."""
    return {"estado": r["estado"], "ok": r["estado"] == "ok", "titulo": r["titulo"], "dica": r["dica"], "motivo": r["motivo"], "quando": r["quando"]}
