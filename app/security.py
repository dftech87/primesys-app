import re


def normalize_cnpj(cnpj: str) -> str:
    """Strips formatting, keeping only digits, so '12.345/0001-99' and '12345000199' match."""
    return re.sub(r"\D", "", cnpj or "")
