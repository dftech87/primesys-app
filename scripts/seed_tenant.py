"""Registers a client tenant (CNPJ + Meu ERP Online API token).

Usage:
    .venv/Scripts/python.exe scripts/seed_tenant.py --cnpj 29970763000147 --nome DFTECH --token "..."

Without --cnpj/--token, falls back to MEUERP_CNPJ / MEUERP_TOKEN from .env
(used to seed the first DFTECH test tenant). Login itself uses the client's own
Meu ERP Online e-mail/password, validated live against their API — no password
is stored here.
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import settings
from app.database import init_db
from app.tenants import create_tenant


def main() -> None:
    parser = argparse.ArgumentParser(description="Cadastra ou atualiza uma empresa (tenant) do dashboard.")
    parser.add_argument("--cnpj", default=settings.meuerp_cnpj)
    parser.add_argument("--nome", default="DFTECH")
    parser.add_argument("--token", default=settings.meuerp_token)
    args = parser.parse_args()

    if not (args.cnpj and args.token):
        parser.error("--cnpj e --token são obrigatórios (ou configure MEUERP_CNPJ/MEUERP_TOKEN no .env).")

    init_db()
    create_tenant(cnpj=args.cnpj, nome_fantasia=args.nome, api_token=args.token)
    print(f"Tenant cadastrado/atualizado: cnpj={args.cnpj} nome={args.nome}")


if __name__ == "__main__":
    main()
