#!/usr/bin/env python3
"""Cache local para os complementos de atualidade do PNCP.

O cache guarda apenas respostas JSON públicas do PNCP e nunca credenciais.
Cada recorte é salvo com timestamp próprio para permitir validação de idade.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

VERSAO_CACHE = 1


def _ler_cache(caminho: Path) -> dict[str, Any]:
    if not caminho.exists():
        return {"version": VERSAO_CACHE, "entries": {}}
    try:
        dados = json.loads(caminho.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logging.warning("Cache PNCP invalido em %s; ignorando: %s", caminho, exc)
        return {"version": VERSAO_CACHE, "entries": {}}
    if not isinstance(dados, dict):
        return {"version": VERSAO_CACHE, "entries": {}}
    if not isinstance(dados.get("entries"), dict):
        dados["entries"] = {}
    dados["version"] = VERSAO_CACHE
    return dados


def salvar_entrada(
    caminho: Path,
    chave: str,
    registros: list[dict[str, Any]],
    salvo_em: datetime,
) -> None:
    """Salva uma entrada de forma atômica."""
    caminho.parent.mkdir(parents=True, exist_ok=True)
    dados = _ler_cache(caminho)
    dados["entries"][chave] = {
        "saved_at": salvo_em.isoformat(),
        "data": registros,
    }
    temporario = caminho.with_suffix(caminho.suffix + ".tmp")
    temporario.write_text(
        json.dumps(dados, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    temporario.replace(caminho)


def carregar_entrada_recente(
    caminho: Path,
    chave: str,
    agora: datetime,
    max_age_hours: float,
) -> tuple[list[dict[str, Any]], datetime] | None:
    """Retorna (registros, timestamp) se a entrada existir e estiver dentro da idade máxima."""
    dados = _ler_cache(caminho)
    entrada = dados.get("entries", {}).get(chave)
    if not isinstance(entrada, dict):
        return None
    bruto_data = entrada.get("data")
    bruto_timestamp = entrada.get("saved_at")
    if not isinstance(bruto_data, list) or not bruto_timestamp:
        return None
    try:
        salvo_em = datetime.fromisoformat(str(bruto_timestamp))
    except ValueError:
        return None
    if salvo_em.tzinfo is None and agora.tzinfo is not None:
        salvo_em = salvo_em.replace(tzinfo=agora.tzinfo)
    idade = agora - salvo_em
    if idade < timedelta(0):
        # Relógio ajustado para trás; trata como entrada atual, mas não futura por mais de 5 min.
        if abs(idade) > timedelta(minutes=5):
            return None
    elif idade > timedelta(hours=max(0.0, float(max_age_hours))):
        return None
    registros = [r for r in bruto_data if isinstance(r, dict)]
    return registros, salvo_em


def status_cache(caminho: Path) -> dict[str, datetime]:
    """Retorna os timestamps válidos existentes, útil para diagnóstico."""
    dados = _ler_cache(caminho)
    retorno: dict[str, datetime] = {}
    for chave, entrada in dados.get("entries", {}).items():
        if not isinstance(entrada, dict) or not entrada.get("saved_at"):
            continue
        try:
            retorno[str(chave)] = datetime.fromisoformat(str(entrada["saved_at"]))
        except ValueError:
            continue
    return retorno
