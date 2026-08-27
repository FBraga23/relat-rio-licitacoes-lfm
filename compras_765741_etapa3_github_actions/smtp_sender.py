#!/usr/bin/env python3
"""Envio de e-mail por SMTP do Gmail.

No macOS, a Senha de App pode ser lida do Keychain. Em ambientes de CI/CD
(como GitHub Actions), ela pode ser fornecida pela variavel de ambiente
SMTP_APP_PASSWORD. A senha nunca e gravada em config.json.
"""

from __future__ import annotations

import logging
import os
import smtplib
import ssl
import subprocess
from email.message import EmailMessage
from email.utils import formataddr, make_msgid
from pathlib import Path
from typing import Iterable


class SMTPError(RuntimeError):
    """Falha de configuracao, credencial, autenticacao ou envio SMTP."""


def _normalizar_lista(valores: Iterable[str] | None) -> list[str]:
    return [str(v).strip() for v in (valores or []) if str(v).strip()]


def obter_senha_keychain(*, usuario: str, servico: str) -> str:
    """Le a Senha de App do Keychain do usuario logado no macOS."""
    usuario = str(usuario).strip()
    servico = str(servico).strip()
    if not usuario:
        raise SMTPError("smtp_usuario nao foi configurado.")
    if not servico:
        raise SMTPError("smtp_keychain_service nao foi configurado.")

    try:
        resultado = subprocess.run(
            [
                "security",
                "find-generic-password",
                "-a",
                usuario,
                "-s",
                servico,
                "-w",
            ],
            text=True,
            capture_output=True,
            check=False,
        )
    except FileNotFoundError as exc:
        raise SMTPError(
            "O utilitario 'security' do macOS nao foi encontrado e nenhuma "
            "Senha de App foi fornecida por variavel de ambiente."
        ) from exc

    if resultado.returncode != 0:
        detalhe = (resultado.stderr or resultado.stdout).strip()
        raise SMTPError(
            "Senha de App nao encontrada no Keychain para "
            f"usuario={usuario!r}, servico={servico!r}. "
            "No macOS, execute 'python smtp_setup.py'. No GitHub Actions, "
            "configure o secret SMTP_APP_PASSWORD."
            + (f" Detalhe: {detalhe}" if detalhe else "")
        )

    senha = resultado.stdout.strip().replace(" ", "")
    if not senha:
        raise SMTPError("A Senha de App recuperada do Keychain esta vazia.")
    return senha


def obter_senha_app(*, usuario: str, servico: str) -> str:
    """Obtém a Senha de App por env (CI) ou, como fallback, pelo Keychain."""
    senha_env = str(os.environ.get("SMTP_APP_PASSWORD") or "").strip().replace(" ", "")
    if senha_env:
        return senha_env
    return obter_senha_keychain(usuario=usuario, servico=servico)


def construir_mensagem(
    *,
    remetente: str,
    nome_remetente: str | None,
    destinatarios: Iterable[str],
    assunto: str,
    corpo: str,
    anexos: Iterable[Path],
    cc: Iterable[str] | None = None,
    bcc: Iterable[str] | None = None,
) -> tuple[EmailMessage, list[str]]:
    para = _normalizar_lista(destinatarios)
    cc_lista = _normalizar_lista(cc)
    bcc_lista = _normalizar_lista(bcc)
    remetente = str(remetente).strip()

    if not remetente:
        raise SMTPError("Nenhum remetente SMTP foi configurado.")
    if not para:
        raise SMTPError("Nenhum destinatario foi configurado.")

    mensagem = EmailMessage()
    mensagem["From"] = (
        formataddr((str(nome_remetente).strip(), remetente))
        if nome_remetente and str(nome_remetente).strip()
        else remetente
    )
    mensagem["To"] = ", ".join(para)
    if cc_lista:
        mensagem["Cc"] = ", ".join(cc_lista)
    mensagem["Subject"] = assunto
    mensagem["Message-ID"] = make_msgid()
    mensagem.set_content(corpo)

    for anexo in anexos:
        caminho = Path(anexo)
        if not caminho.exists():
            raise SMTPError(f"Anexo nao encontrado: {caminho}")
        conteudo = caminho.read_bytes()
        if caminho.suffix.lower() == ".pdf":
            maintype, subtype = "application", "pdf"
        else:
            maintype, subtype = "application", "octet-stream"
        mensagem.add_attachment(
            conteudo,
            maintype=maintype,
            subtype=subtype,
            filename=caminho.name,
        )

    destinos_smtp = list(dict.fromkeys(para + cc_lista + bcc_lista))
    return mensagem, destinos_smtp


def enviar_email(
    *,
    usuario: str,
    keychain_service: str,
    destinatarios: Iterable[str],
    assunto: str,
    corpo: str,
    anexos: Iterable[Path],
    cc: Iterable[str] | None = None,
    bcc: Iterable[str] | None = None,
    remetente: str | None = None,
    nome_remetente: str | None = None,
    host: str = "smtp.gmail.com",
    port: int = 587,
    seguranca: str = "starttls",
    timeout: float = 30.0,
) -> str:
    """Envia por SMTP e retorna o Message-ID local da mensagem."""
    usuario = str(usuario).strip()
    remetente = str(remetente or usuario).strip()
    host = str(host).strip()
    seguranca = str(seguranca).strip().casefold()
    port = int(port)

    if seguranca not in {"starttls", "ssl"}:
        raise SMTPError("smtp_seguranca deve ser 'starttls' ou 'ssl'.")

    senha = obter_senha_app(usuario=usuario, servico=keychain_service)
    mensagem, destinos = construir_mensagem(
        remetente=remetente,
        nome_remetente=nome_remetente,
        destinatarios=destinatarios,
        cc=cc,
        bcc=bcc,
        assunto=assunto,
        corpo=corpo,
        anexos=anexos,
    )

    contexto = ssl.create_default_context()
    try:
        if seguranca == "ssl":
            with smtplib.SMTP_SSL(host, port, timeout=timeout, context=contexto) as smtp:
                smtp.login(usuario, senha)
                smtp.send_message(mensagem, from_addr=remetente, to_addrs=destinos)
        else:
            with smtplib.SMTP(host, port, timeout=timeout) as smtp:
                smtp.ehlo()
                smtp.starttls(context=contexto)
                smtp.ehlo()
                smtp.login(usuario, senha)
                smtp.send_message(mensagem, from_addr=remetente, to_addrs=destinos)
    except smtplib.SMTPAuthenticationError as exc:
        raise SMTPError(
            "O Gmail recusou a autenticacao SMTP. Confirme a conta configurada e "
            "gere uma Senha de App valida. No GitHub Actions, salve-a no secret "
            "SMTP_APP_PASSWORD."
        ) from exc
    except (smtplib.SMTPException, OSError, TimeoutError) as exc:
        raise SMTPError(f"Falha ao enviar pelo SMTP do Gmail: {exc}") from exc

    message_id = str(mensagem.get("Message-ID") or "")
    logging.info("E-mail enviado por SMTP do Gmail. message_id=%s", message_id or "nao informado")
    return message_id
