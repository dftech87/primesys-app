"""Baixa o backup criptografado do PrimeSys (tarefa agendada diária no PC do dono).

Lê %USERPROFILE%\\PrimeSysBackup\\config.json (criado por backup_chaves.py), salva o arquivo na pasta configurada e
apaga os mais antigos, mantendo os últimos N (padrão 60). Registra cada execução em backup.log, na mesma pasta.
Só usa a biblioteca padrão do Python. O arquivo chega criptografado: só abre com a chave privada (backup_abrir.py).

Uso:  python scripts\\backup_baixar.py
"""

import json
import os
import sys
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

PASTA = Path(os.environ.get("USERPROFILE", Path.home())) / "PrimeSysBackup"


def registrar(texto: str) -> None:
    linha = f"{datetime.now():%Y-%m-%d %H:%M:%S}  {texto}"
    print(linha)
    with open(PASTA / "backup.log", "a", encoding="utf-8") as f:
        f.write(linha + "\n")


def main() -> int:
    try:
        config = json.loads((PASTA / "config.json").read_text(encoding="utf-8"))
    except FileNotFoundError:
        print(f"Não encontrei {PASTA / 'config.json'}. Rode antes: python scripts\\backup_chaves.py")
        return 2
    destino = Path(config.get("pasta") or PASTA / "arquivos")
    destino.mkdir(parents=True, exist_ok=True)
    pedido = urllib.request.Request(config["url"], headers={"Authorization": f"Bearer {config['token']}",
                                                            "User-Agent": "PrimeSysBackup/1"})
    try:
        with urllib.request.urlopen(pedido, timeout=180) as resposta:
            conteudo = resposta.read()
    except urllib.error.HTTPError as erro:
        registrar(f"FALHOU: o servidor respondeu {erro.code} ({erro.read()[:200].decode('utf-8', 'replace')})")
        return 1
    except Exception as erro:
        registrar(f"FALHOU: sem resposta do servidor ({type(erro).__name__})")
        return 1

    try:
        envelope = json.loads(conteudo)
        assert envelope.get("formato") == "primesys-backup" and envelope.get("dados")
    except Exception:
        registrar("FALHOU: a resposta não é um arquivo de backup do PrimeSys")
        return 1
    nome = f"primesys-backup-{datetime.now():%Y-%m-%d_%H%M}.psbk"
    (destino / nome).write_bytes(conteudo)

    manter = int(config.get("manter") or 60)
    arquivos = sorted(destino.glob("primesys-backup-*.psbk"))
    for antigo in arquivos[:-manter]:
        antigo.unlink()
    registrar(f"OK: {nome} ({len(conteudo) // 1024} KB) | registros: {envelope.get('contagem')} | guardados: {min(len(arquivos), manter)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
