#!/usr/bin/env python3
"""Pré-coleta os complementos essenciais do PNCP para uso de contingência às 08:00."""

from __future__ import annotations

import argparse
import json
import logging
import os
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from main import FUSO_BRASILIA, atualizar_snapshot_pncp, configurar_logs

BASE_DIR = Path(__file__).resolve().parent


def carregar_config(caminho: Path) -> dict[str, Any]:
    if not caminho.exists():
        raise ValueError(
            f"Configuracao nao encontrada: {caminho}. Copie config.example.json para config.json."
        )
    try:
        dados = json.loads(caminho.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"Falha ao ler {caminho}: {exc}") from exc
    if not isinstance(dados, dict):
        raise ValueError("config.json precisa conter um objeto JSON.")
    return dados


def resolver(caminho: str | Path) -> Path:
    p = Path(caminho).expanduser()
    return p if p.is_absolute() else BASE_DIR / p


def determinar_periodo(config: dict[str, Any], agora: datetime) -> tuple[date, date]:
    fim = agora.date()
    modo = str(config.get("periodo_relatorio", "ano_corrente")).strip().casefold()
    if modo == "janela_dias":
        janela_dias = max(0, int(config.get("janela_dias", 370)))
        return fim - timedelta(days=janela_dias), fim
    return date(fim.year, 1, 1), fim


def argumentos() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Atualiza o snapshot local dos complementos PNCP sem gerar PDF nem enviar e-mail."
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=BASE_DIR / "config.json",
        help="Caminho do arquivo config.json.",
    )
    parser.add_argument("--verbose", action="store_true")
    return parser.parse_args()


def main() -> int:
    os.chdir(BASE_DIR)
    args = argumentos()
    configurar_logs(BASE_DIR / "logs", args.verbose)

    try:
        caminho_config = args.config if args.config.is_absolute() else BASE_DIR / args.config
        config = carregar_config(caminho_config)
    except ValueError as exc:
        logging.error("Configuracao invalida: %s", exc)
        return 2

    agora = datetime.now(FUSO_BRASILIA)
    inicio, fim = determinar_periodo(config, agora)
    cache_path = resolver(config.get("pncp_cache_path", "cache/pncp_complementos.json"))
    tentativas = max(1, int(config.get("pncp_snapshot_tentativas", config.get("pncp_tentativas", 3))))
    espera = max(
        0.0,
        float(
            config.get(
                "pncp_snapshot_espera_base_segundos",
                config.get("pncp_espera_base_segundos", 4),
            )
        ),
    )
    timeout_leitura = max(5.0, float(config.get("pncp_snapshot_timeout_leitura_segundos", 20)))

    logging.info(
        "Iniciando pre-coleta PNCP de madrugada | cache=%s | periodo-base=%s a %s",
        cache_path,
        inicio,
        fim,
    )
    atualizadas, falhas = atualizar_snapshot_pncp(
        inicio=inicio,
        fim=fim,
        agora=agora,
        cache_pncp_path=cache_path,
        tentativas_pncp=tentativas,
        espera_base_pncp=espera,
        timeout_leitura=timeout_leitura,
    )

    if falhas:
        logging.error(
            "Snapshot PNCP concluido parcialmente: %d/4 recortes atualizados. Falhas: %s",
            atualizadas,
            "; ".join(falhas),
        )
        return 4

    logging.info("Snapshot PNCP concluido com sucesso: 4/4 recortes atualizados.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
