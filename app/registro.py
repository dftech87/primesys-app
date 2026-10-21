"""Logs dos dois apps (Prime Info e Prime Vendas) em JSON no stdout, com o nível em cada linha.

O Railway marca como erro tudo o que sai pelo stderr — até "servidor iniciado" do uvicorn — e sem configuração
os logs de informação nem apareciam. Em JSON com o campo "level", ele mostra cada linha no nível certo e o
vermelho fica só para erro de verdade. Chamado ao importar app.main e vendas.main (depois que o uvicorn já
montou os loggers dele, então os dele passam a sair por aqui também).
"""

import json
import logging
import sys

_NIVEIS = {"DEBUG": "debug", "INFO": "info", "WARNING": "warn", "ERROR": "error", "CRITICAL": "error"}


class _Json(logging.Formatter):
    def format(self, registro: logging.LogRecord) -> str:
        mensagem = registro.getMessage()
        if registro.exc_info:
            mensagem += "\n" + self.formatException(registro.exc_info)
        # Acentos escapados (ç): o Railway decodifica o JSON e mostra certo; nenhum terminal embaralha.
        return json.dumps({"message": mensagem, "level": _NIVEIS.get(registro.levelname, "info"), "logger": registro.name})


def configurar(nivel: int = logging.INFO) -> None:
    raiz = logging.getLogger()
    if getattr(raiz, "_primesys_configurado", False):
        return
    saida = logging.StreamHandler(sys.stdout)
    saida.setFormatter(_Json())
    raiz.handlers[:] = [saida]
    raiz.setLevel(nivel)
    raiz._primesys_configurado = True
    for nome in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        logger = logging.getLogger(nome)
        logger.handlers[:] = []
        logger.propagate = True
    # Cada chamada ao ERP geraria uma linha (e a URL pode ter termos de busca): só avisos e erros.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
