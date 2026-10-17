"""Abre um backup do PrimeSys no PC do dono e mostra o resumo (para conferir que o backup está bom).

Pede a senha do backup (a da chave privada). Com --salvar, grava o conteúdo aberto num JSON: esse arquivo tem os
dados dos clientes, então apague depois de usar (só é preciso numa restauração, que fazemos juntos).

Uso:  python scripts\\backup_abrir.py                      (abre o backup mais recente)
      python scripts\\backup_abrir.py ARQUIVO.psbk
      python scripts\\backup_abrir.py ARQUIVO.psbk --salvar aberto.json
"""

import base64
import getpass
import gzip
import json
import os
import sys
from pathlib import Path

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

PASTA = Path(os.environ.get("USERPROFILE", Path.home())) / "PrimeSysBackup"


def abrir(arquivo: bytes, chave_privada) -> dict:
    envelope = json.loads(arquivo)
    if envelope.get("formato") != "primesys-backup":
        raise ValueError("este arquivo não é um backup do PrimeSys")
    chave_aes = chave_privada.decrypt(
        base64.b64decode(envelope["chave_cifrada"]),
        padding.OAEP(mgf=padding.MGF1(algorithm=hashes.SHA256()), algorithm=hashes.SHA256(), label=None))
    aberto = AESGCM(chave_aes).decrypt(base64.b64decode(envelope["nonce"]), base64.b64decode(envelope["dados"]),
                                       b"primesys-backup-v1")
    return json.loads(gzip.decompress(aberto))


def main() -> int:
    args = [a for a in sys.argv[1:] if a != "--salvar"]
    saida = None
    if "--salvar" in sys.argv:
        i = sys.argv.index("--salvar")
        saida = sys.argv[i + 1] if i + 1 < len(sys.argv) else "backup-aberto.json"
        args = [a for a in args if a != saida]
    if args:
        caminho = Path(args[0])
    else:
        config = json.loads((PASTA / "config.json").read_text(encoding="utf-8"))
        arquivos = sorted(Path(config["pasta"]).glob("primesys-backup-*.psbk"))
        if not arquivos:
            print("Nenhum backup encontrado na pasta.")
            return 1
        caminho = arquivos[-1]

    senha = getpass.getpass("Senha do backup: ")
    try:
        chave = serialization.load_pem_private_key((PASTA / "chave-privada.pem").read_bytes(), password=senha.encode())
    except ValueError:
        print("Senha errada.")
        return 1
    conteudo = abrir(caminho.read_bytes(), chave)
    print(f"Backup: {caminho.name}")
    print(f"Criado em (UTC): {conteudo['criado_em']} | origem: {conteudo.get('origem')}")
    for nome, linhas in conteudo["tabelas"].items():
        print(f"  {nome}: {len(linhas)} registro(s)")
    if conteudo.get("ausentes"):
        print(f"  (não existiam no banco: {', '.join(conteudo['ausentes'])})")
    empresas = conteudo["tabelas"].get("empresas", [])
    if empresas:
        print("Clientes no backup:", ", ".join(sorted(str(e.get("nome_fantasia")) for e in empresas)))
    if saida:
        Path(saida).write_text(json.dumps(conteudo, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"Conteúdo aberto salvo em {saida}. APAGUE este arquivo depois de usar (tem dados dos clientes).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
