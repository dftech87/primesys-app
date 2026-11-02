"""Data e hora da loja. O servidor roda em UTC: depois das 21h de Brasília o date.today() dele já é amanhã, e o
"hoje" das telas passava a ser tratado como dia encerrado (guardado por 15 minutos em vez de 1)."""

from datetime import date, datetime, timedelta, timezone

FUSO_LOJA = timezone(timedelta(hours=-3))   # horário de Brasília (sem horário de verão desde 2019)


def hoje_loja() -> date:
    return datetime.now(FUSO_LOJA).date()
