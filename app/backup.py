"""Backup do banco para fora do Railway, criptografado para o dono (DFTECH).

O servidor só tem a CHAVE PÚBLICA (BACKUP_PUBLIC_KEY): consegue fechar o arquivo, mas não abrir. A chave privada
fica só no PC do dono, protegida por senha (scripts/backup_chaves.py). Quem pegar o arquivo, o token de download ou
até o servidor não lê o conteúdo.

Vai no backup o que não dá para refazer: empresas (com o token já criptografado pela TOKEN_ENCRYPTION_KEY, como está
no banco), pedidos, cadastros e celulares do Prime Vendas. O catálogo e os caches ficam de fora (relidos do ERP).

Formato (JSON): a chave AES-256-GCM sorteada a cada backup vai cifrada com RSA-OAEP-SHA256; os dados (JSON
compactado com gzip) vão cifrados com essa chave.
"""

import base64
import gzip
import json
import logging
import os
from datetime import date, datetime, timezone
from decimal import Decimal

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from app.config import settings
from app.database import db_session

logger = logging.getLogger(__name__)

FORMATO = "primesys-backup"
VERSAO = 1
DADOS_ASSOCIADOS = b"primesys-backup-v1"
TABELAS = ("empresas", "pv_pedidos", "pv_cadastros", "pv_aparelhos")


class BackupIndisponivel(Exception):
    pass


def _chave_publica():
    texto = (settings.backup_public_key or "").strip().replace("\\n", "\n")
    if not texto:
        raise BackupIndisponivel("BACKUP_PUBLIC_KEY não definida")
    if "BEGIN" not in texto:   # aceita também o PEM inteiro em base64 (uma linha só, mais fácil de colar)
        texto = base64.b64decode(texto).decode()
    chave = serialization.load_pem_public_key(texto.encode())
    if not isinstance(chave, rsa.RSAPublicKey) or chave.key_size < 3072:
        raise BackupIndisponivel("BACKUP_PUBLIC_KEY precisa ser uma chave RSA de 3072 bits ou mais")
    return chave


def configurado() -> bool:
    try:
        _chave_publica()
        return True
    except Exception:
        return False


def _valor(v):
    if isinstance(v, (datetime, date)):
        return v.isoformat()
    if isinstance(v, Decimal):
        return float(v)
    if isinstance(v, (bytes, memoryview)):
        return base64.b64encode(bytes(v)).decode()
    return v


def _ler_tabelas() -> dict:
    tabelas, ausentes = {}, []
    for nome in TABELAS:   # nomes fixos deste módulo
        try:
            with db_session() as conn:
                tabelas[nome] = [{k: _valor(v) for k, v in dict(r).items()} for r in conn.execute(f"SELECT * FROM {nome}")]
        except Exception:
            ausentes.append(nome)   # ex.: tabela do Prime Vendas ainda não criada neste banco
    return {"tabelas": tabelas, "ausentes": ausentes}


def gerar() -> tuple[bytes, str]:
    """Arquivo de backup já criptografado e o nome sugerido. Os dados abertos nunca saem da memória."""
    chave_publica = _chave_publica()
    agora = datetime.now(timezone.utc)
    conteudo = _ler_tabelas()
    conteudo.update({"criado_em": agora.isoformat(), "origem": os.getenv("RAILWAY_ENVIRONMENT") or "local"})
    aberto = gzip.compress(json.dumps(conteudo, ensure_ascii=False).encode())

    chave_aes, nonce = AESGCM.generate_key(bit_length=256), os.urandom(12)
    cifrado = AESGCM(chave_aes).encrypt(nonce, aberto, DADOS_ASSOCIADOS)
    chave_cifrada = chave_publica.encrypt(
        chave_aes, padding.OAEP(mgf=padding.MGF1(algorithm=hashes.SHA256()), algorithm=hashes.SHA256(), label=None))
    envelope = {
        "formato": FORMATO, "versao": VERSAO, "criado_em": agora.isoformat(),
        "algoritmo": "RSA-OAEP-SHA256 + AES-256-GCM",
        "contagem": {n: len(v) for n, v in conteudo["tabelas"].items()},
        "chave_cifrada": base64.b64encode(chave_cifrada).decode(),
        "nonce": base64.b64encode(nonce).decode(),
        "dados": base64.b64encode(cifrado).decode(),
    }
    logger.info("backup gerado: %s", envelope["contagem"])
    return json.dumps(envelope).encode(), f"primesys-backup-{agora.strftime('%Y-%m-%d_%H%M')}.psbk"

