"""Gera valores novos para as variáveis secretas do Railway (troca após incidente de segurança).

Rodar no terminal do PRÓPRIO PC:  .venv\\Scripts\\python.exe scripts\\gerar_segredos.py
Os valores vão para um arquivo em %USERPROFILE%\\PrimeSysBackup (pasta só do seu usuário do Windows) e NÃO são
mostrados na tela. Abra o arquivo, copie cada valor para o Railway e apague o arquivo no fim da troca.
"""

import os
import secrets
import string
from datetime import datetime
from pathlib import Path

from cryptography.fernet import Fernet

PASTA = Path(os.environ.get("USERPROFILE", Path.home())) / "PrimeSysBackup"


def senha_forte(tamanho: int = 20) -> str:
    alfabeto = string.ascii_letters + string.digits + "-_.!@#"
    while True:
        s = "".join(secrets.choice(alfabeto) for _ in range(tamanho))
        if any(c.islower() for c in s) and any(c.isupper() for c in s) and any(c.isdigit() for c in s):
            return s


def main() -> None:
    PASTA.mkdir(parents=True, exist_ok=True)
    arquivo = PASTA / f"segredos-novos-{datetime.now():%Y%m%d-%H%M}.txt"
    valores = {
        "SESSION_SECRET": secrets.token_urlsafe(48),
        "ADMIN_PASSWORD": senha_forte(),
        "BACKUP_TOKEN": secrets.token_urlsafe(48),
        "TOKEN_ENCRYPTION_KEY": Fernet.generate_key().decode(),
    }
    linhas = [
        "VALORES NOVOS PARA O RAILWAY — não mande este arquivo nem estes valores para ninguém (nem no chat).",
        "Apague este arquivo quando a troca terminar. Guarde só a ADMIN_PASSWORD no seu gerenciador de senhas.",
        "",
        "=== Serviço Prime Sys Infor ===",
        f"SESSION_SECRET = {valores['SESSION_SECRET']}",
        f"ADMIN_PASSWORD = {valores['ADMIN_PASSWORD']}",
        f"BACKUP_TOKEN = {valores['BACKUP_TOKEN']}",
        f"TOKEN_ENCRYPTION_KEY = {valores['TOKEN_ENCRYPTION_KEY']}",
        "TOKEN_ENCRYPTION_KEY_ANTERIOR = (copie o valor ATUAL da TOKEN_ENCRYPTION_KEY, antes de trocar)",
        "",
        "=== Serviço Vendas ===",
        f"TOKEN_ENCRYPTION_KEY = {valores['TOKEN_ENCRYPTION_KEY']}   (a MESMA do Prime Sys Infor)",
        "TOKEN_ENCRYPTION_KEY_ANTERIOR = (o mesmo valor antigo usado no Prime Sys Infor)",
        "SESSION_SECRET = (só se o serviço Vendas tiver essa variável: use o mesmo valor acima)",
        "",
        "=== No PC, depois que o Prime Sys Infor publicar ===",
        "Em PrimeSysBackup\\config.json, troque o valor de \"token\" pelo BACKUP_TOKEN novo.",
    ]
    arquivo.write_text("\n".join(linhas) + "\n", encoding="utf-8")
    print(f"Arquivo criado: {arquivo}")
    print("Os valores não foram mostrados aqui. Abra o arquivo no Bloco de Notas para copiar.")


if __name__ == "__main__":
    main()
