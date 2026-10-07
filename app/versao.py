"""Identificador da versão do app que está no ar.

O app instalado no celular fica aberto em segundo plano e não recarrega sozinho. Ele pergunta ao servidor
(`/api/versao`) qual é a versão atual; se o número mudou desde que a tela foi carregada, mostra o aviso de
"Nova versão disponível". O número muda a cada deploy:
  - no Railway, vem do commit publicado (RAILWAY_GIT_COMMIT_SHA);
  - fora dele, é um resumo (hash) dos arquivos do app, calculado quando o servidor liga.
O valor devolvido é um hash curto: não revela o commit nem nada sobre o código.
"""

import hashlib
import os
from pathlib import Path

_EXTENSOES = {".py", ".html", ".js", ".css", ".json"}


def _calcular() -> str:
    sha = os.getenv("RAILWAY_GIT_COMMIT_SHA") or os.getenv("GIT_COMMIT_SHA")
    if sha:
        base = sha
    else:
        resumo = hashlib.sha256()
        raiz = Path(__file__).resolve().parent
        for arquivo in sorted(raiz.rglob("*")):
            if arquivo.is_file() and arquivo.suffix in _EXTENSOES and "__pycache__" not in arquivo.parts:
                resumo.update(str(arquivo.relative_to(raiz)).encode())
                resumo.update(arquivo.read_bytes())
        base = resumo.hexdigest()
    return hashlib.sha256(f"primesys:{base}".encode()).hexdigest()[:12]


VERSAO = _calcular()
