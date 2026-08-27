#!/usr/bin/env python3
"""Instala dois LaunchAgents no macOS: snapshot PNCP às 01:00 e relatório às 08:00."""

from __future__ import annotations

import argparse
import json
import os
import plistlib
import subprocess
from pathlib import Path


LABEL_RELATORIO = "br.mil.marinha.compras765741"
LABEL_SNAPSHOT = "br.mil.marinha.compras765741.snapshot"
PROJECT_DIR = Path(__file__).resolve().parents[1]
LAUNCHAGENTS = Path.home() / "Library" / "LaunchAgents"
PLIST_RELATORIO = LAUNCHAGENTS / f"{LABEL_RELATORIO}.plist"
PLIST_SNAPSHOT = LAUNCHAGENTS / f"{LABEL_SNAPSHOT}.plist"
PYTHON = PROJECT_DIR / ".venv" / "bin" / "python"
RUNNER_RELATORIO = PROJECT_DIR / "run_daily.py"
RUNNER_SNAPSHOT = PROJECT_DIR / "snapshot_pncp.py"
LOG_DIR = PROJECT_DIR / "logs"


def executar(cmd: list[str], *, aceitar_falha: bool = False) -> None:
    resultado = subprocess.run(cmd, text=True, capture_output=True)
    if resultado.returncode and not aceitar_falha:
        detalhe = (resultado.stderr or resultado.stdout).strip()
        raise SystemExit(f"Falha em {' '.join(cmd)}: {detalhe}")


def validar_ambiente() -> dict:
    if os.uname().sysname != "Darwin":
        raise SystemExit("Este instalador so deve ser executado no macOS.")
    if not PYTHON.exists():
        raise SystemExit(
            f"Python do ambiente virtual nao encontrado: {PYTHON}\n"
            "Crie o .venv e instale requirements.txt antes de instalar o agendamento."
        )
    if not RUNNER_RELATORIO.exists() or not RUNNER_SNAPSHOT.exists():
        raise SystemExit("run_daily.py ou snapshot_pncp.py nao foi encontrado no projeto.")

    config_path = PROJECT_DIR / "config.json"
    if not config_path.exists():
        raise SystemExit("config.json nao encontrado. Configure os destinatarios primeiro.")
    try:
        config = json.loads(config_path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise SystemExit(f"Nao foi possivel ler config.json: {exc}") from exc

    smtp_usuario = str(config.get("smtp_usuario") or "").strip()
    smtp_service = str(
        config.get("smtp_keychain_service")
        or "br.mil.marinha.compras765741.smtp"
    ).strip()
    if not smtp_usuario:
        raise SystemExit("smtp_usuario nao foi configurado em config.json.")
    credencial = subprocess.run(
        ["security", "find-generic-password", "-a", smtp_usuario, "-s", smtp_service],
        text=True,
        capture_output=True,
    )
    if credencial.returncode != 0:
        raise SystemExit(
            "Senha de App SMTP nao encontrada no Keychain. "
            "Execute 'python smtp_setup.py' primeiro."
        )
    return config


def gravar_plist(
    caminho: Path,
    label: str,
    runner: Path,
    hora: int,
    minuto: int,
    stdout: str,
    stderr: str,
) -> None:
    plist = {
        "Label": label,
        "ProgramArguments": [str(PYTHON), str(runner)],
        "WorkingDirectory": str(PROJECT_DIR),
        "StartCalendarInterval": {"Hour": hora, "Minute": minuto},
        "RunAtLoad": False,
        "ProcessType": "Background",
        "EnvironmentVariables": {"PYTHONUNBUFFERED": "1"},
        "StandardOutPath": str(LOG_DIR / stdout),
        "StandardErrorPath": str(LOG_DIR / stderr),
    }
    with caminho.open("wb") as arquivo:
        plistlib.dump(plist, arquivo, fmt=plistlib.FMT_XML, sort_keys=False)


def instalar() -> None:
    config = validar_ambiente()
    hora_snapshot = int(config.get("hora_snapshot_pncp", 1))
    hora_relatorio = int(config.get("hora_relatorio", 8))
    minuto_snapshot = int(config.get("minuto_snapshot_pncp", 0))
    minuto_relatorio = int(config.get("minuto_relatorio", 0))
    for valor, nome in [
        (hora_snapshot, "hora_snapshot_pncp"),
        (hora_relatorio, "hora_relatorio"),
    ]:
        if not 0 <= valor <= 23:
            raise SystemExit(f"{nome} precisa estar entre 0 e 23.")
    for valor, nome in [
        (minuto_snapshot, "minuto_snapshot_pncp"),
        (minuto_relatorio, "minuto_relatorio"),
    ]:
        if not 0 <= valor <= 59:
            raise SystemExit(f"{nome} precisa estar entre 0 e 59.")

    LOG_DIR.mkdir(parents=True, exist_ok=True)
    LAUNCHAGENTS.mkdir(parents=True, exist_ok=True)

    gravar_plist(
        PLIST_SNAPSHOT,
        LABEL_SNAPSHOT,
        RUNNER_SNAPSHOT,
        hora_snapshot,
        minuto_snapshot,
        "launchd_snapshot_stdout.log",
        "launchd_snapshot_stderr.log",
    )
    gravar_plist(
        PLIST_RELATORIO,
        LABEL_RELATORIO,
        RUNNER_RELATORIO,
        hora_relatorio,
        minuto_relatorio,
        "launchd_stdout.log",
        "launchd_stderr.log",
    )

    dominio = f"gui/{os.getuid()}"
    for label, plist in [
        (LABEL_SNAPSHOT, PLIST_SNAPSHOT),
        (LABEL_RELATORIO, PLIST_RELATORIO),
    ]:
        executar(["launchctl", "bootout", dominio, str(plist)], aceitar_falha=True)
        executar(["launchctl", "bootstrap", dominio, str(plist)])
        executar(["launchctl", "enable", f"{dominio}/{label}"], aceitar_falha=True)

    print(f"LaunchAgent de snapshot instalado: {PLIST_SNAPSHOT}")
    print(f"Horario do snapshot PNCP: {hora_snapshot:02d}:{minuto_snapshot:02d}.")
    print(f"LaunchAgent do relatorio instalado: {PLIST_RELATORIO}")
    print(f"Horario do relatorio/e-mail: {hora_relatorio:02d}:{minuto_relatorio:02d}.")
    print("Teste snapshot: python scripts/install_launchd.py --executar-snapshot-agora")
    print("Teste relatorio: python scripts/install_launchd.py --executar-agora")


def desinstalar() -> None:
    dominio = f"gui/{os.getuid()}"
    for plist in [PLIST_SNAPSHOT, PLIST_RELATORIO]:
        executar(["launchctl", "bootout", dominio, str(plist)], aceitar_falha=True)
        if plist.exists():
            plist.unlink()
    print("LaunchAgents de snapshot e relatorio removidos.")


def executar_agora(label: str) -> None:
    dominio = f"gui/{os.getuid()}"
    executar(["launchctl", "kickstart", "-k", f"{dominio}/{label}"])
    print("Execucao solicitada ao launchd. Consulte logs/ para acompanhar.")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--desinstalar", action="store_true")
    parser.add_argument("--executar-agora", action="store_true")
    parser.add_argument("--executar-snapshot-agora", action="store_true")
    args = parser.parse_args()
    if args.desinstalar:
        desinstalar()
    elif args.executar_snapshot_agora:
        executar_agora(LABEL_SNAPSHOT)
    elif args.executar_agora:
        executar_agora(LABEL_RELATORIO)
    else:
        instalar()


if __name__ == "__main__":
    main()
