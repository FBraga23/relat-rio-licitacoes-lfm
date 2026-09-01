#!/usr/bin/env python3
"""Executa coleta, gera o PDF e, opcionalmente, envia-o por SMTP do Gmail."""

from __future__ import annotations

import argparse
import json
import logging
import os
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from smtp_sender import SMTPError, enviar_email
from main import (
    ApiError,
    FUSO_BRASILIA,
    ORDEM_SITUACOES,
    UASG,
    coletar,
    configurar_logs,
    gerar_pdf,
)


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


def montar_resumo_email(resultado: Any, agora: datetime) -> str:
    contagens = {
        situacao: sum(1 for p in resultado.processos if p.situacao_gerencial == situacao)
        for situacao in ORDEM_SITUACOES
    }
    linhas = [
        f"Segue anexo o Relatorio Diario de Contratacoes da UASG {UASG},",
        f"gerado em {agora.strftime('%d/%m/%Y as %H:%M')} (horario de Brasilia).",
        "",
        "Resumo:",
    ]
    for situacao in ORDEM_SITUACOES:
        if contagens[situacao]:
            linhas.append(f"- {situacao}: {contagens[situacao]}")
    if not any(contagens.values()):
        linhas.append("- Nenhum registro classificado.")
    if resultado.nao_classificados:
        linhas.append(f"- Nao classificados por dados insuficientes: {resultado.nao_classificados}")
    if getattr(resultado, "cache_pncp_utilizado", False):
        timestamps = getattr(resultado, "cache_pncp_timestamps", []) or []
        mais_antigo = min(timestamps) if timestamps else None
        if mais_antigo is not None:
            linhas.append(
                "- PNCP: contingencia com snapshot local de "
                + mais_antigo.astimezone(FUSO_BRASILIA).strftime("%d/%m/%Y as %H:%M")
                + "."
            )
        else:
            linhas.append("- PNCP: contingencia com snapshot local recente.")
    if getattr(resultado, "cache_base_utilizado", False):
        timestamps = getattr(resultado, "cache_base_timestamps", []) or []
        mais_antigo = min(timestamps) if timestamps else None
        if mais_antigo is not None:
            linhas.append(
                "- Base operacional: contingencia com cache de "
                + mais_antigo.astimezone(FUSO_BRASILIA).strftime("%d/%m/%Y as %H:%M")
                + "."
            )
        else:
            linhas.append("- Base operacional: contingencia com cache recente.")
    linhas.extend(
        [
            "",
            "Observacao: o relatorio nao infere lances, habilitacao, recurso ou outra subfase operacional nao fornecida pela fonte publica.",
            "",
            "Mensagem gerada automaticamente.",
        ]
    )
    return "\n".join(linhas)


def motivo_bloqueio_envio(resultado: Any) -> str | None:
    """Retorna o motivo que impede o envio automatico, ou None se estiver integro."""
    if bool(getattr(resultado, "dados_completos", True)):
        return None
    falhas = getattr(resultado, "falhas_essenciais", []) or []
    return "; ".join(str(f) for f in falhas) or "falha de fonte essencial"


def argumentos() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Gera o PDF da UASG 765741 e envia por SMTP do Gmail."
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=BASE_DIR / "config.json",
        help="Caminho do arquivo config.json.",
    )
    parser.add_argument(
        "--sem-email",
        action="store_true",
        help="Gera o PDF, mas nao envia e-mail. Util para testar a coleta e o PDF.",
    )
    parser.add_argument("--verbose", action="store_true")
    return parser.parse_args()


def determinar_periodo(config: dict[str, Any], agora: datetime) -> tuple[date, date]:
    """Define o período-base do relatório.

    Por padrão usa o ano corrente, que corresponde ao escopo do relatório-amostra.
    Para uso excepcional de janela móvel, defina "periodo_relatorio": "janela_dias"
    no config.json e informe "janela_dias".
    """
    fim = agora.date()
    modo = str(config.get("periodo_relatorio", "ano_corrente")).strip().casefold()
    if modo == "janela_dias":
        janela_dias = max(0, int(config.get("janela_dias", 370)))
        return fim - timedelta(days=janela_dias), fim
    return date(fim.year, 1, 1), fim


def tentar_enviar_alerta_falha(
    config: dict[str, Any],
    agora: datetime,
    detalhe: str,
) -> None:
    """Envia aviso simples quando o relatório não pode ser entregue por falha de fonte."""
    if not bool(config.get("enviar_alerta_falha", True)):
        return
    destinatarios = config.get("destinatarios") or []
    if not destinatarios:
        return
    try:
        smtp_usuario = str(config.get("smtp_usuario") or "").strip()
        if not smtp_usuario:
            return
        assunto = (
            f"ALERTA - Relatorio de Contratacoes nao enviado - UASG {UASG} - "
            f"{agora.strftime('%d/%m/%Y')}"
        )
        corpo = (
            f"O Relatorio Diario de Contratacoes da UASG {UASG} nao foi enviado "
            "porque as fontes publicas necessarias estavam indisponiveis ou os dados "
            "nao puderam ser validados com seguranca.\n\n"
            f"Data/hora: {agora.strftime('%d/%m/%Y as %H:%M')} (Brasilia)\n"
            f"Detalhe: {detalhe}\n\n"
            "O sistema bloqueou o relatorio para evitar o envio de informacoes incompletas. "
            "Uma nova execucao podera ser feita quando as fontes forem normalizadas."
        )
        enviar_email(
            usuario=smtp_usuario,
            remetente=str(config.get("smtp_remetente") or smtp_usuario),
            nome_remetente=str(config.get("smtp_nome_remetente") or "Relatorio Diario de Licitacoes - LFM"),
            keychain_service=str(config.get("smtp_keychain_service") or "br.mil.marinha.compras765741.smtp"),
            host=str(config.get("smtp_host") or "smtp.gmail.com"),
            port=int(config.get("smtp_port") or 587),
            seguranca=str(config.get("smtp_seguranca") or "starttls"),
            timeout=float(config.get("smtp_timeout_segundos") or 30),
            destinatarios=destinatarios,
            cc=config.get("cc") or [],
            bcc=config.get("bcc") or [],
            assunto=assunto,
            corpo=corpo,
            anexos=[],
        )
        logging.info("Alerta de falha enviado por e-mail.")
    except SMTPError as exc:
        logging.error("Nao foi possivel enviar o alerta de falha por e-mail: %s", exc)


def main() -> int:
    # launchd pode iniciar o processo com outro cwd; fixamos a pasta do projeto.
    os.chdir(BASE_DIR)
    args = argumentos()
    configurar_logs(BASE_DIR / "logs", args.verbose)

    try:
        config = carregar_config(args.config if args.config.is_absolute() else BASE_DIR / args.config)
    except ValueError as exc:
        logging.error("Configuracao invalida: %s", exc)
        return 2

    agora = datetime.now(FUSO_BRASILIA)
    workers = int(config.get("workers", 5))
    tentativas_pncp = max(1, int(config.get("pncp_tentativas", 3)))
    espera_base_pncp = max(0.0, float(config.get("pncp_espera_base_segundos", 4)))
    cache_pncp_path = resolver(config.get("pncp_cache_path", "cache/pncp_complementos.json"))
    cache_pncp_max_age_hours = max(0.0, float(config.get("pncp_cache_max_age_hours", 24)))
    cache_base_path = resolver(config.get("cache_base_path", "cache/dados_operacionais.json"))
    cache_base_max_age_hours = max(0.0, float(config.get("cache_base_max_age_hours", 30)))
    inicio, fim = determinar_periodo(config, agora)

    diretorio_pdf = resolver(config.get("diretorio_pdf", "output"))
    pdf = diretorio_pdf / f"relatorio_compras_{UASG}_{agora.date().isoformat()}.pdf"

    try:
        resultado = coletar(
            inicio,
            fim,
            agora,
            workers,
            tentativas_pncp=tentativas_pncp,
            espera_base_pncp=espera_base_pncp,
            cache_pncp_path=cache_pncp_path,
            cache_pncp_max_age_hours=cache_pncp_max_age_hours,
            cache_base_path=cache_base_path,
            cache_base_max_age_hours=cache_base_max_age_hours,
        )
        gerar_pdf(pdf, resultado, agora, inicio, fim)
    except (ApiError, OSError, ValueError) as exc:
        logging.error("Execucao abortada antes do envio: %s", exc)
        if not args.sem_email and bool(config.get("enviar_email", True)):
            tentar_enviar_alerta_falha(config, agora, str(exc))
        return 1

    logging.info(
        "PDF diario gerado: %s | %d classificado(s) de %d consultado(s)",
        pdf,
        len(resultado.processos),
        resultado.total_consultado,
    )

    motivo_bloqueio = motivo_bloqueio_envio(resultado)

    if args.sem_email or not bool(config.get("enviar_email", True)):
        if motivo_bloqueio:
            logging.warning("Relatorio potencialmente incompleto: %s", motivo_bloqueio)
        logging.info("Envio de e-mail desativado nesta execucao.")
        return 0

    if motivo_bloqueio:
        logging.error("Relatorio potencialmente incompleto: %s", motivo_bloqueio)
        logging.error(
            "Envio de e-mail cancelado por seguranca. O PDF foi mantido apenas para diagnostico."
        )
        tentar_enviar_alerta_falha(config, agora, motivo_bloqueio)
        return 4

    destinatarios = config.get("destinatarios") or []
    cc = config.get("cc") or []
    bcc = config.get("bcc") or []
    assunto_modelo = str(
        config.get(
            "assunto",
            "Relatorio Diario de Contratacoes - UASG 765741 - {data}",
        )
    )
    assunto = assunto_modelo.format(
        data=agora.strftime("%d/%m/%Y"),
        data_iso=agora.date().isoformat(),
        uasg=UASG,
    )
    corpo = montar_resumo_email(resultado, agora)

    try:
        smtp_usuario = str(config.get("smtp_usuario") or "").strip()
        if not smtp_usuario:
            raise SMTPError("smtp_usuario nao foi configurado em config.json.")

        enviar_email(
            usuario=smtp_usuario,
            remetente=str(config.get("smtp_remetente") or smtp_usuario),
            nome_remetente=str(config.get("smtp_nome_remetente") or "Relatorio Diario de Licitacoes - LFM"),
            keychain_service=str(
                config.get("smtp_keychain_service")
                or "br.mil.marinha.compras765741.smtp"
            ),
            host=str(config.get("smtp_host") or "smtp.gmail.com"),
            port=int(config.get("smtp_port", 587)),
            seguranca=str(config.get("smtp_seguranca") or "starttls"),
            timeout=float(config.get("smtp_timeout_segundos", 30)),
            destinatarios=destinatarios,
            cc=cc,
            bcc=bcc,
            assunto=assunto,
            corpo=corpo,
            anexos=[pdf],
        )
    except SMTPError as exc:
        logging.error("O PDF foi gerado, mas o e-mail nao foi enviado: %s", exc)
        return 3

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
