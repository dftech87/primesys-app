"""Prepara o backup no PC do dono (rodar UMA vez, no terminal do próprio PC).

Cria a pasta %USERPROFILE%\\PrimeSysBackup com:
  - chave-privada.pem ............ abre os backups; protegida pela senha que você digitar (NUNCA sai deste PC)
  - chave-publica-para-railway.txt  vai na variável BACKUP_PUBLIC_KEY do Prime Info no Railway (não é segredo)
  - config.json .................. endereço, token de download (vai na BACKUP_TOKEN do Railway) e pasta dos arquivos

Uso:  python scripts\\backup_chaves.py
"""

import base64
import getpass
import json
import os
import secrets
import subprocess
import sys
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

PASTA = Path(os.environ.get("USERPROFILE", Path.home())) / "PrimeSysBackup"
URL_PADRAO = "https://app.primesysapp.com.br/admin/backup/automatico"


def main() -> None:
    privada = PASTA / "chave-privada.pem"
    if privada.exists() and "--refazer" not in sys.argv:
        sys.exit(f"Já existe uma chave em {privada}.\nSe criar outra, os backups antigos só abrem com a chave antiga. "
                 "Para refazer mesmo assim: python scripts\\backup_chaves.py --refazer")
    PASTA.mkdir(parents=True, exist_ok=True)
    (PASTA / "arquivos").mkdir(exist_ok=True)
    # Só o seu usuário do Windows enxerga a pasta.
    subprocess.run(["icacls", str(PASTA), "/inheritance:r", "/grant:r", f"{os.environ.get('USERNAME', '')}:(OI)(CI)F"],
                   capture_output=True)

    print("Escolha a SENHA DO BACKUP (mínimo 10 caracteres). Ela abre os arquivos de backup.")
    print("Guarde num gerenciador de senhas: sem ela, nenhum backup abre.")
    while True:
        senha = getpass.getpass("Senha do backup: ")
        if len(senha) < 10:
            print("Use pelo menos 10 caracteres.")
            continue
        if senha != getpass.getpass("Repita a senha: "):
            print("As senhas não conferem. Tente de novo.")
            continue
        break

    chave = rsa.generate_private_key(public_exponent=65537, key_size=4096)
    privada.write_bytes(chave.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
        serialization.BestAvailableEncryption(senha.encode())))
    publica_pem = chave.public_key().public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
    (PASTA / "chave-publica.pem").write_bytes(publica_pem)
    (PASTA / "chave-publica-para-railway.txt").write_text(base64.b64encode(publica_pem).decode(), encoding="utf-8")

    config = {"url": URL_PADRAO, "token": secrets.token_urlsafe(48), "pasta": str(PASTA / "arquivos"), "manter": 60}
    (PASTA / "config.json").write_text(json.dumps(config, indent=2, ensure_ascii=False), encoding="utf-8")

    print(f"""
Pronto. Pasta criada: {PASTA}

Próximos passos (no Railway, serviço Prime Info -> Variables -> New Variable):
  1. BACKUP_PUBLIC_KEY = conteúdo do arquivo chave-publica-para-railway.txt (uma linha só)
  2. BACKUP_TOKEN      = o valor de "token" dentro do config.json (sem as aspas)
     -> não mande o token nem a senha para ninguém, nem no chat.
  3. Faça uma cópia de chave-privada.pem num pendrive ou drive pessoal: sem ela, nenhum backup abre.
""")


if __name__ == "__main__":
    main()
