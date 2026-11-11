"""Liberação de permissão do caixa pelo Prime Info.

No ERP (Ferramentas > Liberação de permissão) o operador pede uma liberação no caixa (cancelar nota, dar desconto...) e alguém
com autoridade aprova. Aqui o app lê essa fila (GET /api/usuario/liberacoes-pendentes/v1), avisa o celular de quem pode aprovar
e aplica a decisão (POST no mesmo caminho), sempre pela mesma regra do módulo de liberações do ERP.

Pontos de atenção:
- Só quem tem um dos perfis escolhidos no /admin para a empresa aprova (decisão do dono: perfil de administrador). Desligado por
  padrão em cada empresa: é uma função que grava no ERP.
- O ERP grava a decisão em nome do usuário do TOKEN da empresa. Por isso o app guarda o seu próprio registro (quem aprovou, quando)
  e manda o nome de quem decidiu no campo de observação.
- Custo na cota do ERP: 1 chamada a cada `intervalo` segundos por empresa, e só enquanto houver celular inscrito ou alguém com a
  tela aberta, dentro do horário configurado. Se o ERP falhar, o intervalo dobra (até 60 s).
- A tela aberta lê o resultado guardado aqui (sem chamar o ERP): abrir em vários celulares não gasta cota."""

import asyncio
import json
import logging
import random
import time
import uuid
from datetime import datetime
from urllib.parse import urlparse

import httpx

from app import push
from app.database import db_session
from app.datas import FUSO_LOJA
from app.meuerp_client import MeuERPClient

logger = logging.getLogger(__name__)

CAMINHO_ERP = "/api/usuario/liberacoes-pendentes/v1"
INTERVALO_PADRAO, INTERVALO_MINIMO, INTERVALO_MAXIMO = 10, 5, 60
HORA_INICIAL_PADRAO, HORA_FINAL_PADRAO = 6, 23
VALIDADE_DA_CONFIG = 30            # s: configuração por empresa lida do banco no máximo a cada 30 s
PRESENCA_VALE = 30                 # s: tela aberta que perguntou há menos que isso mantém a consulta ligada
NAO_AVISAR_SE_MAIS_VELHO_QUE = 600  # s: pedido velho (o operador já desistiu) entra na lista, mas não vira notificação
GUARDAR_AVISOS_POR = 7 * 24 * 3600
MAX_PAGINAS = 3
ESPERA_MAXIMA_DA_TELA = 3.0   # s: a tela nunca fica esperando o ERP mais que isso (devolve o que já tem)
TESTE_INTERVALO_MINIMO = 20   # s: no modo teste a consulta nunca é mais frequente que isso (3 chamadas por minuto)
TESTE_HORAS_PADRAO, TESTE_HORAS_MAXIMO = 3, 8
TABELAS = (
    """CREATE TABLE IF NOT EXISTS liberacoes_empresas (
        cnpj TEXT PRIMARY KEY,
        ativo INTEGER NOT NULL DEFAULT 0,
        perfis TEXT NOT NULL DEFAULT '[]',
        intervalo INTEGER NOT NULL DEFAULT 10,
        hora_inicial INTEGER NOT NULL DEFAULT 6,
        hora_final INTEGER NOT NULL DEFAULT 23
    )""",
    """CREATE TABLE IF NOT EXISTS liberacoes_avisos (
        cnpj TEXT NOT NULL,
        id_solicitacao BIGINT NOT NULL,
        avisado_em DOUBLE PRECISION NOT NULL,
        PRIMARY KEY (cnpj, id_solicitacao)
    )""",
    """CREATE TABLE IF NOT EXISTS liberacoes_decisoes (
        id TEXT PRIMARY KEY,
        cnpj TEXT NOT NULL,
        id_solicitacao BIGINT NOT NULL,
        email TEXT NOT NULL,
        nome TEXT,
        decisao TEXT NOT NULL,
        motivo TEXT,
        permissao TEXT,
        caixa INTEGER,
        operador TEXT,
        resultado TEXT NOT NULL,
        criado_em TEXT NOT NULL
    )""",
    # Modo teste (decisão do dono, 2026-11-11): a empresa só RECEBE o aviso, uma vez, e a consulta ao ERP para sozinha. Nunca aprova nada.
    """CREATE TABLE IF NOT EXISTS liberacoes_teste (
        cnpj TEXT PRIMARY KEY,
        expira_em DOUBLE PRECISION NOT NULL,
        encerrado_em DOUBLE PRECISION
    )""",
    """CREATE TABLE IF NOT EXISTS push_inscricoes (
        endpoint TEXT PRIMARY KEY,
        cnpj TEXT NOT NULL,
        email TEXT NOT NULL,
        perfil INTEGER,
        p256dh TEXT NOT NULL,
        auth TEXT NOT NULL,
        criado_em TEXT NOT NULL
    )""",
)

# Serviços de notificação dos navegadores. O endereço da inscrição vem do celular, então só se aceita endereço destes serviços:
# senão alguém logado poderia fazer o servidor chamar um endereço qualquer.
_SERVICOS_PUSH = (".googleapis.com", ".mozilla.com", ".push.apple.com", ".notify.windows.com", ".windows.com")


def iniciar_tabelas() -> None:
    with db_session() as conn:
        for comando in TABELAS:
            conn.execute(comando)


def _agora_texto() -> str:
    return datetime.now(FUSO_LOJA).strftime("%Y-%m-%d %H:%M:%S")


# ----------------------------------------------------------------------------- configuração por empresa
_config_cache: dict[str, tuple[float, dict]] = {}
_ativas_cache: tuple[float, list[str]] = (0.0, [])


def _padrao() -> dict:
    return {"ativo": False, "perfis": [], "intervalo": INTERVALO_PADRAO, "hora_inicial": HORA_INICIAL_PADRAO, "hora_final": HORA_FINAL_PADRAO}


def config(cnpj: str) -> dict:
    """Configuração da empresa (desligada se nunca foi ligada ou se o banco falhar)."""
    agora = time.monotonic()
    guardada = _config_cache.get(cnpj)
    if guardada and agora - guardada[0] < VALIDADE_DA_CONFIG:
        return guardada[1]
    cfg = _padrao()
    try:
        with db_session() as conn:
            linha = conn.execute(
                "SELECT ativo, perfis, intervalo, hora_inicial, hora_final FROM liberacoes_empresas WHERE cnpj = ?", (cnpj,)
            ).fetchone()
        if linha is not None:
            try:
                perfis = [int(p) for p in json.loads(linha["perfis"] or "[]")]
            except (ValueError, TypeError):
                perfis = []
            cfg = {"ativo": bool(linha["ativo"]), "perfis": perfis, "intervalo": int(linha["intervalo"]),
                   "hora_inicial": int(linha["hora_inicial"]), "hora_final": int(linha["hora_final"])}
    except Exception:
        logger.exception("não foi possível ler a configuração de liberações")
        if guardada:
            return guardada[1]
    _config_cache[cnpj] = (agora, cfg)
    return cfg


def salvar_config(cnpj: str, ativo: bool, perfis: list[int], intervalo: int, hora_inicial: int, hora_final: int) -> None:
    intervalo = max(INTERVALO_MINIMO, min(INTERVALO_MAXIMO, int(intervalo)))
    hora_inicial = max(0, min(23, int(hora_inicial)))
    hora_final = max(0, min(24, int(hora_final)))
    with db_session() as conn:
        conn.execute(
            "INSERT INTO liberacoes_empresas (cnpj, ativo, perfis, intervalo, hora_inicial, hora_final) VALUES (?, ?, ?, ?, ?, ?)"
            " ON CONFLICT (cnpj) DO UPDATE SET ativo = excluded.ativo, perfis = excluded.perfis, intervalo = excluded.intervalo,"
            " hora_inicial = excluded.hora_inicial, hora_final = excluded.hora_final",
            (cnpj, 1 if ativo else 0, json.dumps(sorted({int(p) for p in perfis})), intervalo, hora_inicial, hora_final),
        )
    _config_cache.pop(cnpj, None)
    global _ativas_cache
    _ativas_cache = (0.0, [])


# ----------------------------------------------------------------------------- modo teste
# Enquanto existir a linha da empresa em liberacoes_teste a empresa está em teste: o app só avisa e NUNCA grava decisão no ERP.
# A consulta só roda enquanto o teste não terminou (um aviso entregue ou prazo vencido) e há celular inscrito.
_teste_cache: dict[str, tuple[float, dict | None]] = {}
_TESTE_FALHA = {"expira_em": 0.0, "encerrado_em": 0.0}   # banco indisponível: trata como teste já encerrado (não consulta, não decide)


def teste(cnpj: str) -> dict | None:
    """Linha do modo teste da empresa (ou None se a empresa não está em teste)."""
    agora = time.monotonic()
    guardado = _teste_cache.get(cnpj)
    if guardado and agora - guardado[0] < VALIDADE_DA_CONFIG:
        return guardado[1]
    try:
        with db_session() as conn:
            linha = conn.execute("SELECT expira_em, encerrado_em FROM liberacoes_teste WHERE cnpj = ?", (cnpj,)).fetchone()
        dados = None if linha is None else {"expira_em": float(linha["expira_em"]), "encerrado_em": None if linha["encerrado_em"] is None else float(linha["encerrado_em"])}
    except Exception:
        logger.exception("não foi possível ler o modo teste das liberações")
        return guardado[1] if guardado else dict(_TESTE_FALHA)
    _teste_cache[cnpj] = (agora, dados)
    return dados


def em_teste(cnpj: str) -> bool:
    return teste(cnpj) is not None


def teste_rodando(cnpj: str) -> bool:
    dados = teste(cnpj)
    return dados is not None and dados["encerrado_em"] is None and dados["expira_em"] > time.time()


def ligar_teste(cnpj: str, horas: int = TESTE_HORAS_PADRAO) -> None:
    """Começa (ou recomeça) o teste. Aviso já enviado antes não se repete: o registro de avisos continua valendo."""
    horas = max(1, min(TESTE_HORAS_MAXIMO, int(horas)))
    with db_session() as conn:
        conn.execute(
            "INSERT INTO liberacoes_teste (cnpj, expira_em, encerrado_em) VALUES (?, ?, NULL)"
            " ON CONFLICT (cnpj) DO UPDATE SET expira_em = excluded.expira_em, encerrado_em = NULL",
            (cnpj, time.time() + horas * 3600),
        )
    _teste_cache.pop(cnpj, None)
    _estado_de(cnpj).update(falhas=0, proxima=0.0)


def sair_do_teste(cnpj: str) -> None:
    with db_session() as conn:
        conn.execute("DELETE FROM liberacoes_teste WHERE cnpj = ?", (cnpj,))
    _teste_cache.pop(cnpj, None)


def _encerrar_teste(cnpj: str) -> None:
    try:
        with db_session() as conn:
            conn.execute("UPDATE liberacoes_teste SET encerrado_em = ? WHERE cnpj = ? AND encerrado_em IS NULL", (time.time(), cnpj))
    except Exception:
        logger.exception("não foi possível encerrar o modo teste das liberações")
    _teste_cache.pop(cnpj, None)
    logger.info("modo teste das liberações encerrado (empresa %s): aviso entregue", cnpj[-4:])


def intervalo(cnpj: str) -> int:
    """Segundos entre consultas ao ERP. No teste nunca menos que TESTE_INTERVALO_MINIMO."""
    base = config(cnpj)["intervalo"]
    return max(base, TESTE_INTERVALO_MINIMO) if em_teste(cnpj) else base


def _empresas_ativas() -> list[str]:
    global _ativas_cache
    agora = time.monotonic()
    if agora - _ativas_cache[0] < VALIDADE_DA_CONFIG:
        return _ativas_cache[1]
    try:
        with db_session() as conn:
            lista = [linha["cnpj"] for linha in conn.execute("SELECT cnpj FROM liberacoes_empresas WHERE ativo = 1").fetchall()]
    except Exception:
        logger.exception("não foi possível listar as empresas com liberações ligadas")
        return _ativas_cache[1]
    _ativas_cache = (agora, lista)
    return lista


def pode(request) -> bool:
    """O usuário da sessão aprova liberações: empresa ligada, perfil entre os escolhidos e e-mail conhecido (vai para o registro)."""
    cnpj, perfil = request.session.get("cnpj"), request.session.get("perfil")
    if not cnpj or perfil is None or not request.session.get("email"):
        return False
    try:
        perfil = int(perfil)
    except (TypeError, ValueError):
        return False
    cfg = config(cnpj)
    return cfg["ativo"] and perfil in cfg["perfis"]


def dentro_do_horario(cfg: dict, agora: datetime | None = None) -> bool:
    hora = (agora or datetime.now(FUSO_LOJA)).hour
    ini, fim = cfg["hora_inicial"], cfg["hora_final"]
    if ini == fim:
        return True   # início = fim: o dia todo
    return ini <= hora < fim if ini < fim else (hora >= ini or hora < fim)


# ----------------------------------------------------------------------------- inscrições de celular
_inscritos_cache: dict[str, tuple[float, int]] = {}


def endereco_de_push_valido(endpoint: str) -> bool:
    try:
        url = urlparse(endpoint)
    except ValueError:
        return False
    host = (url.hostname or "").lower()
    return url.scheme == "https" and len(endpoint) <= 1000 and any(host.endswith(s) for s in _SERVICOS_PUSH) and url.port in (None, 443)


def inscrever(cnpj: str, email: str, perfil: int | None, endpoint: str, p256dh: str, auth: str) -> None:
    with db_session() as conn:
        conn.execute(
            "INSERT INTO push_inscricoes (endpoint, cnpj, email, perfil, p256dh, auth, criado_em) VALUES (?, ?, ?, ?, ?, ?, ?)"
            " ON CONFLICT (endpoint) DO UPDATE SET cnpj = excluded.cnpj, email = excluded.email, perfil = excluded.perfil,"
            " p256dh = excluded.p256dh, auth = excluded.auth, criado_em = excluded.criado_em",
            (endpoint, cnpj, email, perfil, p256dh, auth, _agora_texto()),
        )
    _inscritos_cache.pop(cnpj, None)


def remover_inscricao(endpoint: str, cnpj: str | None = None) -> None:
    with db_session() as conn:
        if cnpj:   # quem cancela só cancela o celular da própria empresa
            conn.execute("DELETE FROM push_inscricoes WHERE endpoint = ? AND cnpj = ?", (endpoint, cnpj))
        else:
            conn.execute("DELETE FROM push_inscricoes WHERE endpoint = ?", (endpoint,))
    _inscritos_cache.clear()


def _inscricoes(cnpj: str) -> list[dict]:
    with db_session() as conn:
        return [dict(l) for l in conn.execute("SELECT endpoint, p256dh, auth, perfil FROM push_inscricoes WHERE cnpj = ?", (cnpj,)).fetchall()]


def quantos_inscritos(cnpj: str) -> int:
    agora = time.monotonic()
    guardado = _inscritos_cache.get(cnpj)
    if guardado and agora - guardado[0] < VALIDADE_DA_CONFIG:
        return guardado[1]
    try:
        with db_session() as conn:
            n = int(conn.execute("SELECT count(*) AS n FROM push_inscricoes WHERE cnpj = ?", (cnpj,)).fetchone()["n"])
    except Exception:
        logger.exception("não foi possível contar os celulares inscritos")
        return guardado[1] if guardado else 0
    _inscritos_cache[cnpj] = (agora, n)
    return n


# ----------------------------------------------------------------------------- fila lida do ERP
_estado: dict[str, dict] = {}
_presenca: dict[str, float] = {}
_travas: dict[str, asyncio.Lock] = {}
_tenants: dict[str, tuple[float, dict | None]] = {}


def _normalizar(bruto: dict) -> dict:
    permissao = str(bruto.get("permissao") or "")
    codigo, _, descricao = permissao.partition(" - ")
    return {
        "id": int(bruto["idSolicitacao"]),
        "codigo": codigo.strip() if descricao else "",
        "permissao": (descricao or permissao).strip()[:120],
        "info": str(bruto.get("informacao") or "").strip()[:300],
        "quando": str(bruto.get("dataHora") or ""),
        "operador": str(bruto.get("usuario") or "").strip()[:60],
        "caixa": bruto.get("numeroCaixa"),
    }


def _idade_s(quando: str) -> int | None:
    try:
        momento = datetime.fromisoformat(quando)
    except (ValueError, TypeError):
        return None
    if momento.tzinfo is not None:
        momento = momento.astimezone(FUSO_LOJA)
    else:
        momento = momento.replace(tzinfo=FUSO_LOJA)   # o ERP manda a hora da loja, sem fuso
    return max(0, int((datetime.now(FUSO_LOJA) - momento).total_seconds()))


async def listar_pendentes(client: MeuERPClient) -> list[dict]:
    itens: list[dict] = []
    for pagina in range(1, MAX_PAGINAS + 1):
        resposta = await client.get(CAMINHO_ERP, params={"page": pagina, "limit": 50})
        for bruto in resposta.get("items") or []:
            try:
                itens.append(_normalizar(bruto))
            except (KeyError, TypeError, ValueError):
                logger.warning("pedido de liberação com formato inesperado ignorado")
        if not resposta.get("hasNext"):
            break
    return sorted(itens, key=lambda i: (i["quando"], i["id"]))


def _estado_de(cnpj: str) -> dict:
    return _estado.setdefault(cnpj, {"itens": [], "em": 0.0, "erro": False, "falhas": 0, "proxima": 0.0})


def resumo(cnpj: str) -> dict:
    """O que a tela mostra, sem chamar o ERP."""
    est = _estado_de(cnpj)
    return {
        "itens": [{**i, "idade_s": _idade_s(i["quando"])} for i in est["itens"]],
        "atualizado_ha": int(time.time() - est["em"]) if est["em"] else None,
        "erro": est["erro"],
        "motivo": est.get("motivo", ""),
        "intervalo": intervalo(cnpj),
        "teste": {"rodando": teste_rodando(cnpj)} if em_teste(cnpj) else None,   # no teste a tela só mostra o pedido (não decide)
    }


def marcar_presenca(cnpj: str) -> None:
    _presenca[cnpj] = time.monotonic()


def _com_presenca(cnpj: str) -> bool:
    visto = _presenca.get(cnpj)
    return visto is not None and time.monotonic() - visto < PRESENCA_VALE


def _tenant(cnpj: str) -> dict | None:
    from app.tenants import get_tenant_by_cnpj   # importado aqui: tenants importa o cliente do ERP e o módulo precisa ser leve de carregar

    agora = time.monotonic()
    guardado = _tenants.get(cnpj)
    if guardado and agora - guardado[0] < 60:
        return guardado[1]
    tenant = get_tenant_by_cnpj(cnpj)
    _tenants[cnpj] = (agora, tenant)
    return tenant


async def atualizar(cnpj: str, tenant: dict) -> dict:
    """Lê a fila no ERP agora, guarda e avisa os celulares dos pedidos novos. Uma leitura por vez por empresa."""
    trava = _travas.setdefault(cnpj, asyncio.Lock())
    est = _estado_de(cnpj)
    if trava.locked():
        return est
    async with trava:
        # reserva a próxima consulta ANTES de chamar o ERP: quem chegar durante a leitura (ou logo depois de uma falha) não repete
        est["proxima"] = time.time() + intervalo(cnpj)
        try:
            itens = await listar_pendentes(MeuERPClient(tenant["api_token"]))
        except Exception as erro:
            # só o tipo/código do erro (nunca o texto, que pode trazer endereço): a tela do /admin mostra para achar a causa
            est["motivo"] = f"HTTP {erro.response.status_code}" if isinstance(erro, httpx.HTTPStatusError) else type(erro).__name__
            est["falhas"] += 1
            est["erro"] = True
            est["proxima"] = time.time() + min(INTERVALO_MAXIMO, intervalo(cnpj) * (2 ** min(est["falhas"], 3)))
            logger.warning("fila de liberações: ERP não respondeu (%s)", est["motivo"])
            return est
        est.update(itens=itens, em=time.time(), erro=False, motivo="", falhas=0, proxima=time.time() + intervalo(cnpj))
        try:
            await _avisar_novos(cnpj, itens)
        except Exception:
            logger.exception("falha ao avisar os celulares sobre pedidos de liberação")
    return est


async def garantir_fresco(cnpj: str, tenant: dict) -> None:
    """Para a primeira abertura da tela: se ainda não há leitura recente, faz uma agora (as seguintes vêm do ciclo).
    Respeita a espera depois de uma falha (senão cada abertura da tela martelaria o ERP e gastaria a cota da empresa) e nunca
    prende a tela por mais de ESPERA_MAXIMA_DA_TELA: a leitura continua por trás."""
    if em_teste(cnpj):
        return   # no modo teste só o ciclo (com celular inscrito) consulta o ERP: abrir a tela nunca gasta cota
    est = _estado_de(cnpj)
    velha = not est["em"] or time.time() - est["em"] > 2 * config(cnpj)["intervalo"] + 5
    if not velha or time.time() < est["proxima"]:
        return
    try:
        await asyncio.wait_for(asyncio.shield(atualizar(cnpj, tenant)), ESPERA_MAXIMA_DA_TELA)
    except asyncio.TimeoutError:
        pass


# ----------------------------------------------------------------------------- avisos
def _filtrar_novos(cnpj: str, ids: list[int]) -> list[int]:
    if not ids:
        return []
    marcas = ",".join("?" for _ in ids)
    with db_session() as conn:
        ja = {int(l["id_solicitacao"]) for l in conn.execute(
            f"SELECT id_solicitacao FROM liberacoes_avisos WHERE cnpj = ? AND id_solicitacao IN ({marcas})", (cnpj, *ids)).fetchall()}
    return [i for i in ids if i not in ja]


def _marcar_avisados(cnpj: str, ids: list[int]) -> None:
    with db_session() as conn:
        for i in ids:
            conn.execute("INSERT INTO liberacoes_avisos (cnpj, id_solicitacao, avisado_em) VALUES (?, ?, ?) ON CONFLICT (cnpj, id_solicitacao) DO NOTHING",
                         (cnpj, i, time.time()))
        if random.random() < 0.01:
            conn.execute("DELETE FROM liberacoes_avisos WHERE avisado_em < ?", (time.time() - GUARDAR_AVISOS_POR,))


def _texto_do_aviso(item: dict) -> dict:
    titulo = f"Caixa {item['caixa']} pede liberação" if item.get("caixa") else "Pedido de liberação"
    corpo = item["permissao"] + (f" · {item['operador']}" if item["operador"] else "")
    return {"titulo": titulo, "texto": corpo[:120], "url": "/liberacoes", "tag": f"lib-{item['id']}"}


async def _avisar_novos(cnpj: str, itens: list[dict]) -> None:
    novos = await asyncio.to_thread(_filtrar_novos, cnpj, [i["id"] for i in itens])
    if not novos:
        return
    await asyncio.to_thread(_marcar_avisados, cnpj, novos)   # marca antes de enviar: uma falha no envio nunca vira aviso repetido
    if not push.ligado():
        return
    perfis = set(config(cnpj)["perfis"])
    destinos = [s for s in await asyncio.to_thread(_inscricoes, cnpj) if s.get("perfil") in perfis]
    if not destinos:
        return
    for item in itens:
        if item["id"] not in novos:
            continue
        idade = _idade_s(item["quando"])
        if idade is not None and idade > NAO_AVISAR_SE_MAIS_VELHO_QUE:
            continue
        conteudo = _texto_do_aviso(item)
        resultados = await asyncio.gather(*(push.enviar(s, conteudo) for s in destinos), return_exceptions=True)
        if teste_rodando(cnpj) and any(not isinstance(r, Exception) for r in resultados):
            await asyncio.to_thread(_encerrar_teste, cnpj)   # teste cumprido: um aviso entregue, a consulta ao ERP para
        for destino, resultado in zip(destinos, resultados):
            if isinstance(resultado, push.InscricaoMorta):
                await asyncio.to_thread(remover_inscricao, destino["endpoint"])
            elif isinstance(resultado, Exception):
                logger.warning("envio de notificação falhou (%s)", type(resultado).__name__)


# ----------------------------------------------------------------------------- decisão
class LiberacaoRecusada(Exception):
    def __init__(self, detalhe: str, status: int = 422):
        super().__init__(detalhe)
        self.detalhe, self.status = detalhe, status


def _registrar(cnpj: str, item: dict | None, id_solicitacao: int, email: str, nome: str, decisao: str, motivo: str, resultado: str) -> None:
    try:
        with db_session() as conn:
            conn.execute(
                "INSERT INTO liberacoes_decisoes (id, cnpj, id_solicitacao, email, nome, decisao, motivo, permissao, caixa, operador, resultado, criado_em)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (uuid.uuid4().hex, cnpj, int(id_solicitacao), email, nome, decisao, motivo, (item or {}).get("permissao"),
                 (item or {}).get("caixa"), (item or {}).get("operador"), resultado, _agora_texto()),
            )
    except Exception:
        logger.exception("DECISÃO DE LIBERAÇÃO SEM REGISTRO: %s %s pedido %s %s (%s)", cnpj, email, id_solicitacao, decisao, resultado)


async def decidir(client: MeuERPClient, cnpj: str, email: str, nome: str, id_solicitacao: int, tipo: str, motivo: str) -> dict:
    if em_teste(cnpj):
        raise LiberacaoRecusada("Modo teste: este cliente só recebe o aviso. Decida o pedido direto no ERP.", 403)
    tipo = (tipo or "").upper()
    if tipo not in ("L", "B"):
        raise LiberacaoRecusada("Escolha liberar ou bloquear.")
    motivo = (motivo or "").strip()
    if tipo == "B" and not motivo:
        raise LiberacaoRecusada("Informe o motivo do bloqueio.")
    item = next((i for i in _estado_de(cnpj)["itens"] if i["id"] == int(id_solicitacao)), None)
    observacao = (motivo or f"Prime Info: liberado por {nome}")[:256] if tipo == "L" else f"{motivo} (Prime Info: {nome})"[:256]
    try:
        resposta = await client.post(CAMINHO_ERP, json={"idSolicitacao": int(id_solicitacao), "tipoLiberacao": tipo, "motivo": observacao})
    except httpx.HTTPStatusError as erro:
        codigo = erro.response.status_code
        mensagem = ""
        try:
            mensagem = str(erro.response.json().get("mensagem") or "")[:200]
        except Exception:
            pass
        _registrar(cnpj, item, id_solicitacao, email, nome, tipo, motivo, f"recusado pelo ERP (HTTP {codigo})")
        if codigo in (404, 409):
            _retirar(cnpj, id_solicitacao)
            raise LiberacaoRecusada("Este pedido já foi tratado (no ERP ou por outra pessoa).", 409)
        if codigo in (401, 403):
            raise LiberacaoRecusada("O ERP não permite que este acesso libere esse pedido.", 403)
        raise LiberacaoRecusada(mensagem or "O ERP não aceitou a decisão. Tente de novo ou faça direto no ERP.", 502 if codigo >= 500 else 422)
    except httpx.HTTPError:
        _registrar(cnpj, item, id_solicitacao, email, nome, tipo, motivo, "sem resposta do ERP")
        raise LiberacaoRecusada("Não foi possível confirmar se o ERP registrou a decisão. Confira o pedido no ERP antes de tentar de novo.", 502)
    if isinstance(resposta, dict) and resposta.get("status") is False:
        _registrar(cnpj, item, id_solicitacao, email, nome, tipo, motivo, "recusado pelo ERP")
        raise LiberacaoRecusada(str(resposta.get("mensagem") or "O ERP não aceitou a decisão.")[:200])
    _registrar(cnpj, item, id_solicitacao, email, nome, tipo, motivo, "ok")
    _retirar(cnpj, id_solicitacao)
    return {"ok": True, "decisao": tipo}


def _retirar(cnpj: str, id_solicitacao: int) -> None:
    est = _estado_de(cnpj)
    est["itens"] = [i for i in est["itens"] if i["id"] != int(id_solicitacao)]


# ----------------------------------------------------------------------------- ciclo em segundo plano
_tarefa: asyncio.Task | None = None


async def _passo() -> None:
    agora = time.time()
    tarefas = []
    for cnpj in list(_empresas_ativas()):
        cfg = config(cnpj)
        if not cfg["ativo"] or not dentro_do_horario(cfg):
            continue
        if em_teste(cnpj):
            # teste: só consulta com o teste em andamento e com celular inscrito (a tela aberta não conta)
            if not teste_rodando(cnpj) or quantos_inscritos(cnpj) == 0:
                continue
        elif not (_com_presenca(cnpj) or quantos_inscritos(cnpj) > 0):
            continue
        est = _estado_de(cnpj)
        if agora < est["proxima"]:
            continue
        est["proxima"] = agora + intervalo(cnpj)   # reserva já: o passo seguinte não repete enquanto esta leitura anda
        tenant = await asyncio.to_thread(_tenant, cnpj)
        if tenant is None:
            continue
        tarefas.append(atualizar(cnpj, tenant))
    if tarefas:
        await asyncio.gather(*tarefas, return_exceptions=True)


async def _ciclo() -> None:
    while True:
        try:
            await _passo()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("ciclo das liberações")
        await asyncio.sleep(1.0)


def iniciar() -> None:
    global _tarefa
    if _tarefa is None or _tarefa.done():
        try:
            iniciar_tabelas()
        except Exception:
            logger.exception("tabelas das liberações não puderam ser criadas")
            return
        _tarefa = asyncio.get_running_loop().create_task(_ciclo())


async def parar() -> None:
    global _tarefa
    if _tarefa is not None:
        _tarefa.cancel()
        try:
            await _tarefa
        except (asyncio.CancelledError, Exception):
            pass
        _tarefa = None
