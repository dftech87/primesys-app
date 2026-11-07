"""Registro de tempos das requisições do Prime Info: onde o tempo foi (fila da cota, ERP, nosso código).

O medidor de consumo (app/consumo.py) soma chamadas por hora e tela; aqui fica o caso a caso, para responder "por que ESTE login
demorou?". Cada pedido leva uma ficha (contextvar) que as chamadas ao ERP preenchem: quantas foram, quanto esperaram na fila da
cota e quanto o ERP levou. Os passos nomeados (token, senha, perfil, permissões...) dizem em qual etapa o tempo foi.

Só grava o que interessa: todo login, e qualquer pedido lento (3 s ou mais), com muitas chamadas ao ERP (8 ou mais) ou com
erro 5xx. Cada registro vai para a tabela `desempenho` (90 dias) e para o log do Railway. Só números e nomes de passo: nunca
e-mail, senha, token, texto de busca ou dado de cliente. A empresa é identificada pelo CNPJ. Nada aqui pode quebrar um pedido.
Só o Prime Info grava (o Vendas e o Estoque importam o cliente do ERP, mas sem ficha o registro não faz nada).
"""

import asyncio
import contextvars
import logging
import time
from contextlib import contextmanager
from datetime import datetime, timedelta

from app import consumo
from app.datas import FUSO_LOJA

logger = logging.getLogger(__name__)

LENTO_A_PARTIR_DE = 3.0      # segundos
MUITAS_CHAMADAS = 8
ROTAS_SEMPRE_REGISTRADAS = ("/login",)
RETENCAO_DIAS = 90
MAX_PASSOS = 12

ativo = False
_ficha: contextvars.ContextVar["Ficha | None"] = contextvars.ContextVar("ficha_desempenho", default=None)


class Ficha:
    __slots__ = ("cnpj", "chamadas", "espera", "erp", "passos", "inicio")

    def __init__(self) -> None:
        self.cnpj = ""
        self.chamadas = 0
        self.espera = 0.0      # tempo total na fila da cota
        self.erp = 0.0         # tempo total esperando o ERP responder
        self.passos: list[list] = []   # [nome, segundos, chamadas]
        self.inicio = time.monotonic()


def abrir() -> contextvars.Token:
    """Começa a ficha do pedido (o middleware chama). A ficha é um objeto: o que se anota dentro vale para o pedido todo."""
    return _ficha.set(Ficha())


def fechar(marca: contextvars.Token) -> None:
    _ficha.reset(marca)


def empresa(cnpj: str) -> None:
    """Diz de qual empresa é o pedido (as telas sabem depois de ler a sessão)."""
    f = _ficha.get()
    if f is not None and cnpj:
        f.cnpj = str(cnpj)[:14]


def somar_chamada(espera: float, erp: float) -> None:
    """Uma chamada ao ERP: quanto esperou na fila da cota e quanto o ERP levou. Nunca levanta erro."""
    f = _ficha.get()
    if f is not None:
        f.chamadas += 1
        f.espera += max(0.0, espera)
        f.erp += max(0.0, erp)


@contextmanager
def passo(nome: str):
    """Marca uma etapa do pedido (ex.: 'perfil'): guarda o tempo e quantas chamadas ao ERP ela gastou."""
    f = _ficha.get()
    if f is None:
        yield
        return
    inicio, antes = time.monotonic(), f.chamadas
    try:
        yield
    finally:
        if len(f.passos) < MAX_PASSOS:
            f.passos.append([nome[:20], round(time.monotonic() - inicio, 1), f.chamadas - antes])


def _texto_dos_passos(f: Ficha) -> str:
    return ", ".join(f"{n}={s}s/{c}" for n, s, c in f.passos)


def concluir(metodo: str, caminho: str, status: int) -> None:
    """Fim do pedido: decide se vale registrar e grava sem atrasar a resposta. Nunca levanta erro."""
    try:
        f = _ficha.get()
        if f is None or not ativo:
            return
        rota = consumo.normalizar(caminho)
        duracao = time.monotonic() - f.inicio
        importante = (metodo == "POST" and rota in ROTAS_SEMPRE_REGISTRADAS) or duracao >= LENTO_A_PARTIR_DE \
            or f.chamadas >= MUITAS_CHAMADAS or status >= 500
        if not importante:
            return
        linha = (datetime.now(FUSO_LOJA).strftime("%Y-%m-%d %H:%M:%S"), f.cnpj, metodo, rota, int(status), int(duracao * 1000),
                 f.chamadas, int(f.espera * 1000), int(f.erp * 1000), _texto_dos_passos(f))
        (logger.warning if duracao >= 10 else logger.info)(
            "pedido %s %s: %d, %.1f s (fila da cota %.1f s, ERP %.1f s, %d chamadas) passos: %s%s", metodo, rota, status,
            duracao, f.espera, f.erp, f.chamadas, _texto_dos_passos(f) or "-", f" empresa {f.cnpj}" if f.cnpj else "")
        try:
            asyncio.get_running_loop().run_in_executor(None, _gravar, linha)
        except RuntimeError:
            _gravar(linha)
    except Exception:   # o registro jamais pode atrapalhar uma resposta
        logger.debug("falha ao registrar o tempo do pedido", exc_info=True)


def _gravar(linha: tuple) -> None:
    from app.database import db_session   # importado aqui: o Prime Vendas importa o cliente do ERP e não precisa do banco

    try:
        with db_session() as conn:
            conn.execute("INSERT INTO desempenho (em, cnpj, metodo, rota, status, ms, chamadas, espera_ms, erp_ms, passos) "
                         "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", linha)
    except Exception:
        logger.exception("não foi possível gravar o registro de tempo do pedido")


def limpar_antigos() -> None:
    from app.database import db_session

    limite = (datetime.now(FUSO_LOJA) - timedelta(days=RETENCAO_DIAS)).strftime("%Y-%m-%d %H:%M:%S")
    try:
        with db_session() as conn:
            conn.execute("DELETE FROM desempenho WHERE em < ?", (limite,))
    except Exception:
        logger.exception("não foi possível limpar os registros de tempo antigos")


def listar(dias: int = 7, so_lentos: bool = False, limite: int = 200) -> list[dict]:
    """Registros mais recentes para a tela do /admin."""
    from app.database import db_session

    desde = (datetime.now(FUSO_LOJA) - timedelta(days=max(1, min(int(dias), RETENCAO_DIAS)))).strftime("%Y-%m-%d %H:%M:%S")
    minimo = int(LENTO_A_PARTIR_DE * 1000) if so_lentos else 0
    with db_session() as conn:
        linhas = conn.execute(
            "SELECT em, cnpj, metodo, rota, status, ms, chamadas, espera_ms, erp_ms, passos FROM desempenho "
            "WHERE em >= ? AND ms >= ? ORDER BY em DESC LIMIT ?", (desde, minimo, int(max(1, min(limite, 1000))))).fetchall()
    return [dict(l) for l in linhas]


def iniciar() -> None:
    global ativo
    ativo = True
    limpar_antigos()
