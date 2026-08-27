#!/usr/bin/env python3
"""Migra config.json da versao Gmail API/OAuth para SMTP + Keychain."""

from __future__ import annotations

import argparse
import json
from pathlib import Path


BASE_DIR = Path(__file__).resolve().parent


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, default=BASE_DIR / "config.json")
    parser.add_argument("--email")
    args = parser.parse_args()

    path = args.config if args.config.is_absolute() else BASE_DIR / args.config
    if not path.exists():
        print(f"Arquivo nao encontrado: {path}")
        return 2

    try:
        config = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        print(f"Falha ao ler {path}: {exc}")
        return 2
    if not isinstance(config, dict):
        print("config.json precisa conter um objeto JSON.")
        return 2

    email = str(args.email or config.get("smtp_usuario") or "").strip()
    if not email:
        email = input("Conta Gmail que enviara os relatorios: ").strip()
    if not email:
        print("E-mail nao informado. Migracao cancelada.")
        return 2

    backup = path.with_name(path.stem + ".oauth.backup" + path.suffix)
    if not backup.exists():
        backup.write_text(json.dumps(config, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    config.pop("gmail_credentials", None)
    config.pop("gmail_token", None)
    config.update(
        {
            "smtp_usuario": email,
            "smtp_remetente": email,
            "smtp_nome_remetente": config.get(
                "smtp_nome_remetente", "Relatorio Diario de Licitacoes - LFM"
            ),
            "smtp_host": "smtp.gmail.com",
            "smtp_port": 587,
            "smtp_seguranca": "starttls",
            "smtp_timeout_segundos": 30,
            "smtp_keychain_service": "br.mil.marinha.compras765741.smtp",
        }
    )

    path.write_text(json.dumps(config, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"Migracao concluida: {path}")
    print(f"Backup da configuracao OAuth: {backup}")
    print("Proximo passo: python smtp_setup.py")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
