#!/usr/bin/env python3
"""Grava/verifica/remove a Senha de App do Gmail no Keychain do macOS."""

from __future__ import annotations

import argparse
import getpass
import json
import subprocess
from pathlib import Path


BASE_DIR = Path(__file__).resolve().parent
DEFAULT_SERVICE = "br.mil.marinha.compras765741.smtp"


def carregar_config(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise SystemExit(f"Nao foi possivel ler {path}: {exc}") from exc
    return data if isinstance(data, dict) else {}


def executar_security(args: list[str]) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            ["security", *args],
            text=True,
            capture_output=True,
            check=False,
        )
    except FileNotFoundError as exc:
        raise SystemExit("O utilitario 'security' nao foi encontrado. Execute este script no macOS.") from exc


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Armazena a Senha de App do Gmail no Keychain sem grava-la no projeto."
    )
    parser.add_argument("--config", type=Path, default=BASE_DIR / "config.json")
    parser.add_argument("--email")
    parser.add_argument("--service")
    parser.add_argument("--verificar", action="store_true")
    parser.add_argument("--apagar", action="store_true")
    args = parser.parse_args()

    config = carregar_config(args.config)
    email = str(args.email or config.get("smtp_usuario") or "").strip()
    service = str(args.service or config.get("smtp_keychain_service") or DEFAULT_SERVICE).strip()

    if not email:
        email = input("Conta Gmail usada para enviar os relatorios: ").strip()
    if not email:
        print("E-mail nao informado.")
        return 2

    if args.apagar:
        r = executar_security(["delete-generic-password", "-a", email, "-s", service])
        if r.returncode == 0:
            print("Senha de App removida do Keychain.")
            return 0
        print((r.stderr or r.stdout).strip() or "Item nao encontrado no Keychain.")
        return 1

    if args.verificar:
        r = executar_security(["find-generic-password", "-a", email, "-s", service])
        if r.returncode == 0:
            print(f"OK: existe uma credencial no Keychain para {email} / {service}.")
            return 0
        print("Credencial nao encontrada. Execute: python smtp_setup.py")
        return 1

    senha = getpass.getpass(
        "Cole a Senha de App do Google (16 caracteres; nao sera exibida): "
    ).replace(" ", "").strip()
    if not senha:
        print("Senha de App vazia. Nada foi alterado.")
        return 2
    if len(senha) != 16:
        print(
            "Aviso: Senhas de App do Google normalmente possuem 16 caracteres. "
            "A credencial sera salva como informada."
        )

    r = executar_security(
        [
            "add-generic-password",
            "-a",
            email,
            "-s",
            service,
            "-w",
            senha,
            "-U",
        ]
    )
    if r.returncode != 0:
        print(f"Falha ao gravar no Keychain: {(r.stderr or r.stdout).strip()}")
        return 1

    print("Senha de App armazenada no Keychain com sucesso.")
    print(f"Conta: {email}")
    print(f"Servico: {service}")
    print("A senha nao foi gravada em config.json nem em arquivo do projeto.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
