#!/usr/bin/env python3
"""Gera config.json para GitHub Actions usando secrets/variaveis de ambiente.

Nao grava a Senha de App. Ela permanece apenas em SMTP_APP_PASSWORD.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
EXAMPLE = BASE_DIR / "config.example.json"
TARGET = BASE_DIR / "config.json"


def lista_env(nome: str) -> list[str]:
    bruto = str(os.environ.get(nome) or "").strip()
    if not bruto:
        return []
    return [item.strip() for item in bruto.replace(";", ",").split(",") if item.strip()]


def main() -> int:
    if not EXAMPLE.exists():
        raise SystemExit(f"Arquivo nao encontrado: {EXAMPLE}")
    config = json.loads(EXAMPLE.read_text(encoding="utf-8"))

    usuario = str(os.environ.get("SMTP_USUARIO") or "").strip()
    destinatarios = lista_env("DESTINATARIOS")
    if not usuario:
        raise SystemExit("Secret/variavel SMTP_USUARIO nao configurado.")
    if not destinatarios:
        raise SystemExit("Secret/variavel DESTINATARIOS nao configurado.")

    config["smtp_usuario"] = usuario
    config["smtp_remetente"] = str(os.environ.get("SMTP_REMETENTE") or usuario).strip()
    config["smtp_nome_remetente"] = str(
        os.environ.get("SMTP_NOME_REMETENTE")
        or config.get("smtp_nome_remetente")
        or "Relatorio Diario de Licitacoes - LFM"
    ).strip()
    config["destinatarios"] = destinatarios
    config["cc"] = lista_env("CC")
    config["bcc"] = lista_env("BCC")
    config["enviar_email"] = True

    # Em CI, o cache e o output ficam dentro do workspace.
    config["pncp_cache_path"] = "cache/pncp_complementos.json"
    config["cache_base_path"] = "cache/dados_operacionais.json"
    config["diretorio_pdf"] = "output"

    TARGET.write_text(json.dumps(config, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print("config.json gerado para GitHub Actions (sem credenciais SMTP).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
