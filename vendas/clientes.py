"""Cadastro de cliente pelo app do vendedor.

  - CEP -> endereço e código IBGE do município (ViaCEP). O ERP exige o município (IBGE) no endereço.
  - CNPJ -> dados públicos da Receita (BrasilAPI). Só o CNPJ sai do servidor; CPF nunca é enviado a serviço externo.
  - Antes de cadastrar, confere no ERP se o CPF/CNPJ já existe (não duplica cliente).
  - Cada cadastro tem um identificador criado no celular: reenviar o mesmo cadastro nunca cria dois clientes.
O cliente novo entra na hora na cópia do catálogo do servidor, para os pedidos dele serem aceitos e os outros
celulares da empresa receberem na próxima sincronização.
"""

import json
import logging
import re

import httpx

from app.cache import cached
from app.database import db_session
from app.meuerp_client import MeuERPClient
from vendas import catalogo
from vendas.db import agora_texto, carimbo_agora

logger = logging.getLogger(__name__)

_UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
TTL_PUBLICO = 7 * 24 * 3600


async def _obter(url: str) -> httpx.Response:
    """Uma conexão por consulta (são poucas e com cache): nada fica preso a um loop/conexão antiga."""
    async with httpx.AsyncClient(timeout=10.0, headers={"User-Agent": "PrimeVendas/1.0"}) as cliente:
        return await cliente.get(url)


class CadastroRecusado(Exception):
    """Dado do cadastro precisa ser corrigido pelo vendedor."""


class ServicoIndisponivel(Exception):
    """ERP ou serviço externo fora do ar: tentar depois."""


def so_digitos(texto) -> str:
    return re.sub(r"\D", "", str(texto or ""))


def cpf_valido(cpf: str) -> bool:
    d = so_digitos(cpf)
    if len(d) != 11 or d == d[0] * 11:
        return False
    for n in (9, 10):
        soma = sum(int(d[i]) * (n + 1 - i) for i in range(n))
        if (soma * 10 % 11) % 10 != int(d[n]):
            return False
    return True


def cnpj_valido(cnpj: str) -> bool:
    d = so_digitos(cnpj)
    if len(d) != 14 or d == d[0] * 14:
        return False
    for n in (12, 13):
        pesos = list(range(n - 7, 1, -1)) + list(range(9, 1, -1))
        soma = sum(int(d[i]) * pesos[i] for i in range(n))
        dv = 11 - soma % 11
        if (0 if dv >= 10 else dv) != int(d[n]):
            return False
    return True


# ---- Consultas públicas (servidor chama; o celular nunca fala direto com esses serviços) ----------------------------
async def _viacep(cep: str) -> dict | None:
    r = await _obter(f"https://viacep.com.br/ws/{cep}/json/")
    r.raise_for_status()
    d = r.json()
    if d.get("erro"):
        return None
    return {"cep": cep, "logradouro": d.get("logradouro") or "", "bairro": d.get("bairro") or "", "cidade": d.get("localidade") or "",
            "uf": d.get("uf") or "", "ibge": int(d["ibge"]) if str(d.get("ibge") or "").isdigit() else None}


async def buscar_cep(cnpj_empresa: str, cep) -> dict | None:
    cep = so_digitos(cep)
    if len(cep) != 8:
        raise CadastroRecusado("CEP inválido.")
    try:
        return await cached((cnpj_empresa, "publico-cep", cep), TTL_PUBLICO, lambda: _viacep(cep))
    except httpx.HTTPError as erro:
        raise ServicoIndisponivel() from erro


def _num_ibge(v) -> int | None:
    return int(v) if str(v or "").isdigit() else None


def _fone(*partes) -> str:
    f = so_digitos("".join(str(p or "") for p in partes))
    return f if len(f) in (10, 11) else ""


async def _brasilapi(cnpj: str) -> dict | None:
    r = await _obter(f"https://brasilapi.com.br/api/cnpj/v1/{cnpj}")
    if r.status_code == 404:
        return None
    r.raise_for_status()
    d = r.json()
    return {"razao_social": d.get("razao_social") or "", "fantasia": d.get("nome_fantasia") or "", "cep": so_digitos(d.get("cep")),
            "logradouro": " ".join(x for x in (d.get("descricao_tipo_de_logradouro") or "", d.get("logradouro") or "") if x).strip(),
            "numero": d.get("numero") or "", "complemento": d.get("complemento") or "", "bairro": d.get("bairro") or "",
            "cidade": d.get("municipio") or "", "uf": d.get("uf") or "", "ibge": _num_ibge(d.get("codigo_municipio_ibge")),
            "fone": _fone(d.get("ddd_telefone_1")), "email": (d.get("email") or "").lower(), "situacao": d.get("descricao_situacao_cadastral") or ""}


async def _cnpj_ws(cnpj: str) -> dict | None:
    r = await _obter(f"https://publica.cnpj.ws/cnpj/{cnpj}")
    if r.status_code == 404:
        return None
    r.raise_for_status()
    d = r.json()
    e = d.get("estabelecimento") or {}
    return {"razao_social": d.get("razao_social") or "", "fantasia": e.get("nome_fantasia") or "", "cep": so_digitos(e.get("cep")),
            "logradouro": " ".join(x for x in (e.get("tipo_logradouro") or "", e.get("logradouro") or "") if x).strip(),
            "numero": e.get("numero") or "", "complemento": e.get("complemento") or "", "bairro": e.get("bairro") or "",
            "cidade": (e.get("cidade") or {}).get("nome") or "", "uf": (e.get("estado") or {}).get("sigla") or "",
            "ibge": _num_ibge((e.get("cidade") or {}).get("ibge_id")), "fone": _fone(e.get("ddd1"), e.get("telefone1")),
            "email": (e.get("email") or "").lower(), "situacao": e.get("situacao_cadastral") or ""}


async def _receitaws(cnpj: str) -> dict | None:
    r = await _obter(f"https://receitaws.com.br/v1/cnpj/{cnpj}")
    r.raise_for_status()
    d = r.json()
    if d.get("status") == "ERROR":
        return None
    telefone = (d.get("telefone") or "").split("/")[0]
    return {"razao_social": d.get("nome") or "", "fantasia": d.get("fantasia") or "", "cep": so_digitos(d.get("cep")),
            "logradouro": d.get("logradouro") or "", "numero": d.get("numero") or "", "complemento": d.get("complemento") or "",
            "bairro": d.get("bairro") or "", "cidade": d.get("municipio") or "", "uf": d.get("uf") or "", "ibge": None,
            "fone": _fone(telefone), "email": (d.get("email") or "").lower(), "situacao": d.get("situacao") or ""}


async def _dados_publicos(cnpj: str) -> dict | None:
    """Tenta 3 serviços públicos, nesta ordem (algum pode estar fora ou bloquear o servidor). Sem o código IBGE,
    completa pelo CEP (o ERP exige o município no endereço)."""
    for nome, consulta in (("BrasilAPI", _brasilapi), ("CNPJ.ws", _cnpj_ws), ("ReceitaWS", _receitaws)):
        try:
            dados = await consulta(cnpj)
        except Exception as erro:
            logger.warning("consulta de CNPJ em %s falhou: %s", nome, type(erro).__name__)
            continue
        if dados and dados.get("razao_social"):
            if not dados.get("ibge") and len(dados.get("cep") or "") == 8:
                try:
                    cep = await _viacep(dados["cep"])
                    if cep:
                        dados.update(ibge=cep["ibge"], cidade=dados["cidade"] or cep["cidade"], uf=dados["uf"] or cep["uf"])
                except Exception:
                    pass
            dados["fonte"] = nome
            return dados
    return None


async def ja_no_erp(tenant: dict, documento: str) -> dict | None:
    """Cliente com este CPF/CNPJ já cadastrado no ERP da empresa (ou None)."""
    try:
        p = await MeuERPClient(tenant["api_token"]).get("/api/pessoa/cnpjcpf/v1", params={"cnpjCpf": documento})
    except httpx.HTTPStatusError as erro:
        if erro.response.status_code in (400, 404):
            return None
        raise ServicoIndisponivel() from erro
    except httpx.HTTPError as erro:
        raise ServicoIndisponivel() from erro
    if not p or not p.get("codigo"):
        return None
    return {"codigo": int(p["codigo"]), "nome": p.get("fantasia") or p.get("razaoSocial") or "", "ativo": p.get("ativo", "S") == "S",
            "tipo": p.get("tipo")}


async def buscar_cnpj(aparelho: dict, cnpj) -> dict:
    cnpj = so_digitos(cnpj)
    if not cnpj_valido(cnpj):
        raise CadastroRecusado("CNPJ inválido. Confira os números.")
    existente = await ja_no_erp(aparelho["tenant"], cnpj)
    try:
        dados = await cached((aparelho["cnpj"], "publico-cnpj", cnpj), TTL_PUBLICO, lambda: _dados_publicos(cnpj),
                             guardar_se=lambda v: v is not None)
    except Exception:
        dados = None   # serviços da Receita fora: o vendedor digita
    return {"existente": existente, "dados": dados}


# ---- Cadastro ---------------------------------------------------------------------------------------------------------
def _texto(v, maximo: int) -> str:
    return " ".join(str(v or "").split())[:maximo]


def _no_catalogo(cnpj: str, codigo: int, nome: str, fantasia: str, documento: str, bairro: str, cidade: str, uf: str, fone: str) -> None:
    """Põe o cliente na cópia do catálogo do servidor já com carimbo novo (os celulares baixam na próxima sincronização)."""
    valores = catalogo._converter("clientes", {"nome": nome, "fantasia": fantasia, "documento": documento, "bairro": bairro,
                                               "cidade": cidade, "uf": uf, "fone": fone, "ativo": 1})
    colunas = catalogo.COLUNAS["clientes"]
    with db_session() as conn:
        conn.execute(
            f"INSERT INTO pv_clientes (cnpj, id, {', '.join(colunas)}, resumo, carimbo) VALUES ({', '.join(['?'] * (len(colunas) + 4))}) "
            f"ON CONFLICT (cnpj, id) DO UPDATE SET {', '.join(f'{c} = excluded.{c}' for c in colunas)}, resumo = excluded.resumo, carimbo = excluded.carimbo",
            (cnpj, codigo, *[valores[c] for c in colunas], catalogo._resumo(valores), carimbo_agora()))


def _para_celular(codigo: int, nome: str, fantasia: str, documento: str, bairro: str, cidade: str, uf: str, fone: str, existente: bool) -> dict:
    v = catalogo._converter("clientes", {"nome": nome, "fantasia": fantasia, "documento": documento, "bairro": bairro, "cidade": cidade,
                                         "uf": uf, "fone": fone, "ativo": 1})
    return {"cliente": {"id": codigo, **v}, "existente": existente}


async def cadastrar(aparelho: dict, dados: dict) -> dict:
    tenant, cnpj_empresa = aparelho["tenant"], aparelho["cnpj"]
    id_cadastro = str(dados.get("id") or "").lower()
    if not _UUID.match(id_cadastro):
        raise CadastroRecusado("Identificador do cadastro inválido.")
    with db_session() as conn:
        feito = conn.execute("SELECT resultado FROM pv_cadastros WHERE id = ? AND cnpj = ?", (id_cadastro, cnpj_empresa)).fetchone()
    if feito:   # reenvio do mesmo cadastro (ex.: a resposta se perdeu na volta): devolve o mesmo cliente
        return json.loads(feito["resultado"])

    tipo = "J" if dados.get("tipo_pessoa") == "J" else "F"
    documento = so_digitos(dados.get("documento"))
    nome = _texto(dados.get("nome"), 120)
    fantasia = _texto(dados.get("fantasia"), 120)
    if len(nome) < 2:
        raise CadastroRecusado("Informe o nome / razão social.")
    if tipo == "J" and not cnpj_valido(documento):
        raise CadastroRecusado("CNPJ inválido. Confira os números.")
    if tipo == "F" and documento and not cpf_valido(documento):
        raise CadastroRecusado("CPF inválido. Confira os números.")
    celular = so_digitos(dados.get("celular"))
    if celular and len(celular) not in (10, 11):
        raise CadastroRecusado("Celular inválido (DDD + número).")
    email = _texto(dados.get("email"), 120).lower()
    if email and not _EMAIL.match(email):
        raise CadastroRecusado("E-mail inválido.")

    if documento:
        existente = await ja_no_erp(tenant, documento)
        if existente:
            with db_session() as conn:   # já na cópia do catálogo: devolve como está (não apaga endereço/telefone dela)
                linha = conn.execute("SELECT * FROM pv_clientes WHERE cnpj = ? AND id = ?", (cnpj_empresa, existente["codigo"])).fetchone()
            if linha:
                atual = dict(linha)
                return {"cliente": {"id": atual["id"], **{c: atual[c] for c in catalogo.COLUNAS["clientes"]}}, "existente": True}
            nome_erp = existente["nome"] or nome
            _no_catalogo(cnpj_empresa, existente["codigo"], nome_erp, "", documento, "", "", "", "")
            return _para_celular(existente["codigo"], nome_erp, "", documento, "", "", "", "", True)

    corpo = {"razaoSocial": nome, "fantasia": fantasia, "tipo": "C", "ativo": "S"}
    if documento:
        corpo["cnpjCpf"] = documento
    if celular or email:
        corpo["contato"] = {k: v for k, v in (("celular", celular), ("email", email)) if v}
    cep = so_digitos(dados.get("cep"))
    logradouro, bairro = _texto(dados.get("logradouro"), 120), _texto(dados.get("bairro"), 60)
    cidade, uf, ibge = _texto(dados.get("cidade"), 60), _texto(dados.get("uf"), 2).upper(), dados.get("ibge")
    if cep and not str(ibge or "").isdigit():   # cadastro feito sem internet: o servidor completa pelo CEP
        achado = await buscar_cep(cnpj_empresa, cep) if len(cep) == 8 else None
        if achado:
            ibge, cidade, uf = achado["ibge"], achado["cidade"], achado["uf"]
            logradouro = logradouro or achado["logradouro"]
            bairro = bairro or achado["bairro"]
    if logradouro and str(ibge or "").isdigit():
        corpo["enderecoPrincipal"] = {"logradouro": logradouro, "numero": _texto(dados.get("numero"), 20) or "S/N",
                                      "complemento": _texto(dados.get("complemento"), 60), "bairro": bairro,
                                      "idIbgeMunicipio": int(ibge), "uf": uf, "cep": cep}

    try:
        resposta = await MeuERPClient(tenant["api_token"]).post("/api/pessoa/v1", json=corpo)
    except httpx.HTTPStatusError as erro:
        try:
            mensagem = erro.response.json().get("mensagem")
        except Exception:
            mensagem = None
        if erro.response.status_code in (400, 404, 409, 422):
            raise CadastroRecusado(mensagem or "O ERP recusou o cadastro.")
        raise ServicoIndisponivel() from erro
    except httpx.HTTPError as erro:
        raise ServicoIndisponivel() from erro
    pessoa = resposta.get("pessoa") or {}
    if not resposta.get("status") or not pessoa.get("codigo"):
        raise CadastroRecusado(resposta.get("mensagem") or "O ERP recusou o cadastro.")
    codigo = int(pessoa["codigo"])
    _no_catalogo(cnpj_empresa, codigo, nome, fantasia, documento, bairro, cidade, uf, celular)
    resultado = _para_celular(codigo, nome, fantasia, documento, bairro, cidade, uf, celular, False)
    with db_session() as conn:
        conn.execute("INSERT INTO pv_cadastros (id, cnpj, id_funcionario, id_pessoa, resultado, criado_em) VALUES (?, ?, ?, ?, ?, ?) "
                     "ON CONFLICT (id) DO NOTHING",
                     (id_cadastro, cnpj_empresa, aparelho["id_funcionario"], codigo, json.dumps(resultado, ensure_ascii=False), agora_texto()))
    logger.info("cliente %s cadastrado pelo app (empresa %s, funcionário %s)", codigo, cnpj_empresa, aparelho["id_funcionario"])
    return resultado
