#!/usr/bin/env python3
"""Gera um relatorio gerencial em PDF para a UASG 765741.

Fontes publicas:
- Base historica: API de Dados Abertos do Compras.gov.br.
- Complemento de atualidade: APIs publicas do PNCP (propostas abertas e atualizacoes recentes).

A classificacao e deliberadamente conservadora: nenhuma subfase operacional
(lances, habilitacao, recurso etc.) e inferida.
"""

from __future__ import annotations

import argparse
import html
import logging
import re
import sys
import threading
import time
import unicodedata
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Iterable
from zoneinfo import ZoneInfo

import requests
from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_LEFT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import cm
from reportlab.platypus import (
    LongTable,
    PageBreak,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from pncp_cache import carregar_entrada_recente, salvar_entrada


UASG = "765741"
CNPJ_ORGAO_FALLBACK = "00394502000144"
FUSO_BRASILIA = ZoneInfo("America/Sao_Paulo")

COMPRAS_BASE = "https://dadosabertos.compras.gov.br"
PNCP_CONSULTA_BASE = "https://pncp.gov.br/api/consulta"
PNCP_DADOS_BASE = "https://pncp.gov.br/api/pncp"

MODALIDADES_PNCP = {
    6: "Pregao",
    8: "Dispensa",
}

# O endpoint do Compras.gov usa codigos internos diferentes dos codigos PNCP:
# 05 = Pregao; 06 = Dispensa de Licitacao.
MODALIDADES_COMPRAS = {
    5: "Pregao",
    6: "Dispensa",
}

MODALIDADES = {
    "Pregao": {"compras": 5, "pncp": 6},
    "Dispensa": {"compras": 6, "pncp": 8},
}

ORDEM_SITUACOES = [
    "Aguardando abertura de propostas",
    "Recebendo propostas",
    "Fase externa em andamento",
    "Suspenso",
    "Homologado",
    "Fracassado/Deserto",
    "Revogado/Anulado",
    "Verificação manual",
]


class ApiError(RuntimeError):
    """Erro de consulta a uma fonte publica."""


class ApiTimeout(ApiError):
    """Timeout de leitura que pode ser mitigado com uma consulta menor."""


@dataclass
class Processo:
    numero: str
    modalidade: str
    ano: int
    objeto: str
    situacao_gerencial: str
    data_relevante: datetime | None
    rotulo_data: str
    situacao_fonte: str
    numero_controle_pncp: str

    @property
    def numero_exibicao(self) -> str:
        return f"{self.modalidade} {self.numero}/{self.ano}"


@dataclass
class ResultadoColeta:
    processos: list[Processo] = field(default_factory=list)
    avisos: list[str] = field(default_factory=list)
    nao_classificados: int = 0
    total_consultado: int = 0
    dados_completos: bool = True
    falhas_essenciais: list[str] = field(default_factory=list)
    cache_pncp_utilizado: bool = False
    cache_pncp_fontes: list[str] = field(default_factory=list)
    cache_pncp_timestamps: list[datetime] = field(default_factory=list)
    cache_base_utilizado: bool = False
    cache_base_fontes: list[str] = field(default_factory=list)
    cache_base_timestamps: list[datetime] = field(default_factory=list)


def executar_com_retentativas(
    descricao: str,
    funcao: Any,
    tentativas: int = 3,
    espera_base_segundos: float = 4.0,
) -> Any:
    """Executa uma consulta publica com retentativas controladas.

    A rotina e usada nos complementos essenciais do PNCP. Em caso de falha,
    faz novas tentativas com espera progressiva para absorver indisponibilidades
    transitórias sem transformar um timeout isolado em relatorio incompleto.
    """
    total = max(1, int(tentativas))
    ultima_excecao: ApiError | None = None
    for numero_tentativa in range(1, total + 1):
        try:
            return funcao()
        except ApiError as exc:
            ultima_excecao = exc
            if numero_tentativa >= total:
                break
            espera = max(0.0, float(espera_base_segundos)) * numero_tentativa
            detalhe = str(exc)
            if "HTTP 429" in detalhe:
                espera = max(espera, 30.0 * numero_tentativa)
            elif "HTTP 503" in detalhe:
                espera = max(espera, 10.0 * numero_tentativa)
            logging.warning(
                "%s falhou (tentativa %d/%d): %s. Nova tentativa em %.0f s.",
                descricao,
                numero_tentativa,
                total,
                exc,
                espera,
            )
            if espera:
                time.sleep(espera)
    assert ultima_excecao is not None
    raise ultima_excecao


def texto_pdf_seguro(valor: Any) -> str:
    """Escapa texto externo antes de passa-lo ao parser XML-like do ReportLab."""
    return html.escape(str(valor or ""), quote=False)


def resumo_erro_para_relatorio(exc: Exception) -> str:
    """Converte erros HTTP extensos em uma descricao curta e segura para o PDF."""
    texto = re.sub(r"\s+", " ", str(exc)).strip()
    if "HTTP 429" in texto:
        return "HTTP 429 - limite de requisicoes do PNCP excedido"
    if "HTTP 503" in texto:
        return "HTTP 503 - servico PNCP indisponivel"
    if "Timeout" in texto or "timed out" in texto:
        return "timeout na consulta ao PNCP"
    if len(texto) > 220:
        texto = texto[:217] + "..."
    return texto


def configurar_logs(diretorio: Path, verbose: bool = False) -> None:
    diretorio.mkdir(parents=True, exist_ok=True)
    nivel = logging.DEBUG if verbose else logging.INFO
    formato = "%(asctime)s | %(levelname)s | %(message)s"
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stdout)]
    try:
        handlers.append(logging.FileHandler(diretorio / "compras_765741.log", encoding="utf-8"))
    except OSError:
        pass
    logging.basicConfig(level=nivel, format=formato, handlers=handlers, force=True)


def criar_sessao(status_retries: int = 2) -> requests.Session:
    retry = Retry(
        total=max(0, int(status_retries)),
        connect=2,
        read=0,
        backoff_factor=0.8,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset({"GET"}),
        raise_on_status=False,
        respect_retry_after_header=True,
    )
    adapter = HTTPAdapter(max_retries=retry, pool_connections=10, pool_maxsize=10)
    sessao = requests.Session()
    sessao.headers.update(
        {
            "Accept": "application/json",
            "User-Agent": "Relatorio-Compras-UASG-765741/1.0",
        }
    )
    sessao.mount("https://", adapter)
    return sessao


class ClienteHttp:
    def __init__(
        self,
        timeout_conexao: float = 6,
        timeout_leitura: float = 35,
        status_retries: int = 2,
    ):
        self.timeout = (timeout_conexao, timeout_leitura)
        self.status_retries = max(0, int(status_retries))
        self._local = threading.local()

    def _sessao(self) -> requests.Session:
        if not hasattr(self._local, "sessao"):
            self._local.sessao = criar_sessao(self.status_retries)
        return self._local.sessao

    def obter_json(self, url: str, params: dict[str, Any] | None = None) -> Any:
        logging.debug("GET %s params=%s", url, params)
        try:
            resposta = self._sessao().get(url, params=params, timeout=self.timeout)
        except requests.RequestException as exc:
            detalhe = str(exc)
            if isinstance(exc, requests.ReadTimeout) or "Read timed out" in detalhe:
                raise ApiTimeout(f"Timeout de leitura em {url}: {exc}") from exc
            raise ApiError(f"Falha de rede em {url}: {exc}") from exc

        if resposta.status_code == 204:
            return None
        if not resposta.ok:
            detalhe = re.sub(r"\s+", " ", resposta.text).strip()[:350]
            detalhe_norm = detalhe.casefold()
            if resposta.status_code in (400, 408, 504) and (
                "timed out" in detalhe_norm
                or "timeout" in detalhe_norm
                or "connection is not available" in detalhe_norm
                or "hikaripool" in detalhe_norm
            ):
                raise ApiTimeout(
                    f"Timeout/indisponibilidade do servidor em {resposta.url}: "
                    f"{detalhe or 'resposta sem detalhes'}"
                )
            raise ApiError(
                f"HTTP {resposta.status_code} em {resposta.url}: "
                f"{detalhe or 'resposta sem detalhes'}"
            )
        try:
            return resposta.json()
        except ValueError as exc:
            raise ApiError(f"Resposta nao JSON em {resposta.url}") from exc


class ClienteCompras(ClienteHttp):
    def consultar_uasg(self) -> dict[str, Any]:
        url = f"{COMPRAS_BASE}/modulo-uasg/1_consultarUasg"
        dados = self.obter_json(
            url,
            params={"pagina": 1, "codigoUasg": UASG, "statusUasg": "true"},
        )
        registros = (dados or {}).get("resultado", [])
        for registro in registros:
            if str(registro.get("codigoUasg")) == UASG:
                return registro
        raise ApiError(f"UASG {UASG} nao localizada como ativa no Compras.gov.br")

    @staticmethod
    def _janelas_consulta(data_inicial: date, data_final: date) -> Iterable[tuple[date, date]]:
        """Divide a consulta do Compras.gov em blocos de ate 90 dias.

        A API aceita filtros por data de publicacao e UASG, mas pode sofrer
        indisponibilidades do pool de banco. Blocos menores reduzem o custo
        da consulta e permitem subdivisao automatica em caso de timeout.
        """
        if data_inicial > data_final:
            raise ValueError("data_inicial nao pode ser posterior a data_final")
        inicio = data_inicial
        while inicio <= data_final:
            fim = min(inicio + timedelta(days=89), data_final)
            yield inicio, fim
            inicio = fim + timedelta(days=1)

    def _consultar_janela(
        self,
        url: str,
        modalidade: int,
        inicio: date,
        fim: date,
    ) -> list[dict[str, Any]]:
        pagina = 1
        registros: list[dict[str, Any]] = []
        try:
            while True:
                params = {
                    "pagina": pagina,
                    "tamanhoPagina": 100,
                    "unidadeOrgaoCodigoUnidade": UASG,
                    "dataPublicacaoPncpInicial": inicio.isoformat(),
                    "dataPublicacaoPncpFinal": fim.isoformat(),
                    "codigoModalidade": modalidade,
                }
                dados = self.obter_json(url, params=params) or {}
                lote = dados.get("resultado", [])
                if not isinstance(lote, list):
                    raise ApiError("Compras.gov retornou o campo 'resultado' em formato inesperado")
                registros.extend(lote)
                total_paginas = int(dados.get("totalPaginas") or 0)
                paginas_restantes = int(dados.get("paginasRestantes") or 0)
                if (total_paginas and pagina >= total_paginas) or (not total_paginas and paginas_restantes <= 0):
                    break
                pagina += 1
            return registros
        except ApiTimeout:
            dias = (fim - inicio).days + 1
            if dias <= 3:
                raise
            meio = inicio + timedelta(days=(fim - inicio).days // 2)
            logging.warning(
                "Compras.gov demorou para responder no periodo %s a %s; "
                "dividindo automaticamente em periodos menores.",
                inicio, fim,
            )
            esquerda = self._consultar_janela(url, modalidade, inicio, meio)
            direita = self._consultar_janela(
                url, modalidade, meio + timedelta(days=1), fim
            )
            return esquerda + direita

    def listar_contratacoes(
        self,
        data_inicial: date,
        data_final: date,
        modalidade: int,
    ) -> list[dict[str, Any]]:
        url = f"{COMPRAS_BASE}/modulo-contratacoes/1_consultarContratacoes_PNCP_14133"
        registros: list[dict[str, Any]] = []
        for inicio_janela, fim_janela in self._janelas_consulta(data_inicial, data_final):
            logging.info(
                "Consultando Compras.gov modalidade=%s no periodo %s a %s",
                modalidade, inicio_janela, fim_janela,
            )
            registros.extend(
                self._consultar_janela(url, modalidade, inicio_janela, fim_janela)
            )
        return registros

    def _listar_paginado(self, url: str, params_base: dict[str, Any]) -> list[dict[str, Any]]:
        pagina = 1
        registros: list[dict[str, Any]] = []
        while True:
            params = {**params_base, "pagina": pagina, "tamanhoPagina": 50}
            dados = self.obter_json(url, params=params) or {}
            if isinstance(dados, list):
                lote = dados
                total_paginas = 1
            else:
                lote = dados.get("data", [])
                if not isinstance(lote, list):
                    raise ApiError("PNCP retornou o campo 'data' em formato inesperado")
                total_paginas = int(dados.get("totalPaginas") or 0)
            registros.extend(lote)
            if total_paginas <= 1 or pagina >= total_paginas:
                break
            pagina += 1
        return registros

    def listar_propostas_abertas(
        self,
        cnpj: str,
        data_final: date,
        modalidade: int,
    ) -> list[dict[str, Any]]:
        """Consulta o endpoint específico do PNCP para prazos de proposta em aberto.

        Esse endpoint é usado como complemento de atualidade porque o Dados Abertos
        do Compras.gov pode ter defasagem de atualização.
        """
        url = f"{PNCP_CONSULTA_BASE}/v1/contratacoes/proposta"
        return self._listar_paginado(
            url,
            {
                "dataFinal": data_final.strftime("%Y%m%d"),
                "codigoModalidadeContratacao": modalidade,
                "cnpj": cnpj,
                "codigoUnidadeAdministrativa": UASG,
            },
        )

    def listar_atualizadas_recentes(
        self,
        cnpj: str,
        data_inicial: date,
        data_final: date,
        modalidade: int,
    ) -> list[dict[str, Any]]:
        """Lista contratações com atualização global recente no PNCP.

        A consulta é deliberadamente curta e complementar. Se o PNCP estiver
        indisponível, a coleta principal do Compras.gov continua válida.
        """
        url = f"{PNCP_CONSULTA_BASE}/v1/contratacoes/atualizacao"
        return self._listar_paginado(
            url,
            {
                "dataInicial": data_inicial.strftime("%Y%m%d"),
                "dataFinal": data_final.strftime("%Y%m%d"),
                "codigoModalidadeContratacao": modalidade,
                "cnpj": cnpj,
                "codigoUnidadeAdministrativa": UASG,
            },
        )

    def listar_itens(self, compra: dict[str, Any]) -> list[dict[str, Any]]:
        id_compra = compra.get("idCompra")
        if not id_compra:
            raise ApiError("Contratacao sem idCompra para consultar itens no Compras.gov")
        url = f"{COMPRAS_BASE}/modulo-contratacoes/2.1_consultarItensContratacoes_PNCP_14133_Id"
        dados = self.obter_json(
            url,
            params={"tipo": "idCompra", "codigo": id_compra},
        ) or {}
        if isinstance(dados, list):
            lote = dados
        else:
            lote = dados.get("resultado", [])
        if not isinstance(lote, list):
            raise ApiError("Compras.gov retornou itens em formato inesperado")
        return [normalizar_item_compras(item) for item in lote]


class ClientePncp(ClienteHttp):
    def __init__(self, timeout_conexao: float = 6, timeout_leitura: float = 35):
        # Os complementos do PNCP ja possuem retentativas explicitas. Evitar
        # retries ocultos do urllib3 reduz rajadas de requisicoes e o risco de HTTP 429.
        super().__init__(timeout_conexao, timeout_leitura, status_retries=0)

    @staticmethod
    def _janelas_consulta(data_inicial: date, data_final: date) -> Iterable[tuple[date, date]]:
        """Divide a consulta em blocos curtos para reduzir timeouts do PNCP.

        O endpoint aceita no maximo 365 dias, mas consultas proximas desse limite
        podem ser lentas e, em alguns momentos, expirar. A V1 usa blocos de ate
        60 dias e ainda aplica subdivisao automatica caso um bloco sofra timeout.
        """
        if data_inicial > data_final:
            raise ValueError("data_inicial nao pode ser posterior a data_final")

        inicio = data_inicial
        while inicio <= data_final:
            fim = min(inicio + timedelta(days=59), data_final)
            yield inicio, fim
            inicio = fim + timedelta(days=1)

    def _consultar_janela(
        self,
        url: str,
        cnpj: str,
        modalidade: int,
        inicio: date,
        fim: date,
    ) -> list[dict[str, Any]]:
        """Consulta uma janela; em timeout, divide-a recursivamente."""
        pagina = 1
        registros: list[dict[str, Any]] = []
        try:
            while True:
                params = {
                    "dataInicial": inicio.strftime("%Y%m%d"),
                    "dataFinal": fim.strftime("%Y%m%d"),
                    "codigoModalidadeContratacao": modalidade,
                    "cnpj": cnpj,
                    "codigoUnidadeAdministrativa": UASG,
                    "pagina": pagina,
                    "tamanhoPagina": 50,
                }
                dados = self.obter_json(url, params=params) or {}
                lote = dados.get("data", [])
                if not isinstance(lote, list):
                    raise ApiError("PNCP retornou o campo 'data' em formato inesperado")
                registros.extend(lote)
                total_paginas = int(dados.get("totalPaginas") or 0)
                if total_paginas <= 1 or pagina >= total_paginas:
                    break
                pagina += 1
            return registros
        except ApiTimeout:
            dias = (fim - inicio).days + 1
            if dias <= 7:
                raise
            meio = inicio + timedelta(days=(fim - inicio).days // 2)
            logging.warning(
                "PNCP demorou para responder no periodo %s a %s; "
                "dividindo automaticamente em periodos menores.",
                inicio, fim,
            )
            esquerda = self._consultar_janela(url, cnpj, modalidade, inicio, meio)
            direita = self._consultar_janela(
                url, cnpj, modalidade, meio + timedelta(days=1), fim
            )
            return esquerda + direita

    def listar_contratacoes(
        self,
        cnpj: str,
        data_inicial: date,
        data_final: date,
        modalidade: int,
    ) -> list[dict[str, Any]]:
        url = f"{PNCP_CONSULTA_BASE}/v1/contratacoes/publicacao"
        registros: list[dict[str, Any]] = []

        for inicio_janela, fim_janela in self._janelas_consulta(data_inicial, data_final):
            logging.info(
                "Consultando PNCP modalidade=%s no periodo %s a %s",
                modalidade, inicio_janela, fim_janela,
            )
            registros.extend(
                self._consultar_janela(
                    url, cnpj, modalidade, inicio_janela, fim_janela
                )
            )

        return registros

    def _listar_paginado(self, url: str, params_base: dict[str, Any]) -> list[dict[str, Any]]:
        pagina = 1
        registros: list[dict[str, Any]] = []
        while True:
            params = {**params_base, "pagina": pagina, "tamanhoPagina": 50}
            dados = self.obter_json(url, params=params) or {}
            if isinstance(dados, list):
                lote = dados
                total_paginas = 1
            else:
                lote = dados.get("data", [])
                if not isinstance(lote, list):
                    raise ApiError("PNCP retornou o campo 'data' em formato inesperado")
                total_paginas = int(dados.get("totalPaginas") or 0)
            registros.extend(lote)
            if total_paginas <= 1 or pagina >= total_paginas:
                break
            pagina += 1
        return registros

    def listar_propostas_abertas(
        self,
        cnpj: str,
        data_final: date,
        modalidade: int,
    ) -> list[dict[str, Any]]:
        """Consulta o endpoint específico do PNCP para prazos de proposta em aberto.

        Esse endpoint é usado como complemento de atualidade porque o Dados Abertos
        do Compras.gov pode ter defasagem de atualização.
        """
        url = f"{PNCP_CONSULTA_BASE}/v1/contratacoes/proposta"
        return self._listar_paginado(
            url,
            {
                "dataFinal": data_final.strftime("%Y%m%d"),
                "codigoModalidadeContratacao": modalidade,
                "cnpj": cnpj,
                "codigoUnidadeAdministrativa": UASG,
            },
        )

    def listar_atualizadas_recentes(
        self,
        cnpj: str,
        data_inicial: date,
        data_final: date,
        modalidade: int,
    ) -> list[dict[str, Any]]:
        """Lista contratações com atualização global recente no PNCP.

        A consulta é deliberadamente curta e complementar. Se o PNCP estiver
        indisponível, a coleta principal do Compras.gov continua válida.
        """
        url = f"{PNCP_CONSULTA_BASE}/v1/contratacoes/atualizacao"
        return self._listar_paginado(
            url,
            {
                "dataInicial": data_inicial.strftime("%Y%m%d"),
                "dataFinal": data_final.strftime("%Y%m%d"),
                "codigoModalidadeContratacao": modalidade,
                "cnpj": cnpj,
                "codigoUnidadeAdministrativa": UASG,
            },
        )

    def listar_itens(self, compra: dict[str, Any]) -> list[dict[str, Any]]:
        cnpj = compra.get("orgaoEntidade", {}).get("cnpj")
        ano = compra.get("anoCompra")
        sequencial = compra.get("sequencialCompra")
        if not cnpj or ano is None or sequencial is None:
            raise ApiError("Contratacao sem identificadores suficientes para consultar itens")
        url = f"{PNCP_DADOS_BASE}/v1/orgaos/{cnpj}/compras/{ano}/{sequencial}/itens"
        dados = self.obter_json(url)
        if dados is None:
            return []
        if not isinstance(dados, list):
            raise ApiError("PNCP retornou itens em formato inesperado")
        return dados


def normalizar_compra_compras(compra: dict[str, Any]) -> dict[str, Any]:
    """Converte o formato do Dados Abertos do Compras.gov para o modelo interno."""
    modalidade_pncp = int(compra.get("modalidadeIdPncp") or 0)
    codigo_modalidade = int(compra.get("codigoModalidade") or 0)
    if not modalidade_pncp:
        modalidade_pncp = 6 if codigo_modalidade == 5 else 8 if codigo_modalidade == 6 else 0

    atualizacao = compra.get("dataAtualizacaoPncp") or compra.get("dataAualizacaoPncp")
    return {
        **compra,
        "modalidadeId": modalidade_pncp,
        "anoCompra": compra.get("anoCompraPncp") or compra.get("anoCompra"),
        "sequencialCompra": compra.get("sequencialCompraPncp") or compra.get("sequencialCompra"),
        "situacaoCompraNome": compra.get("situacaoCompraNomePncp") or compra.get("situacaoCompraNome"),
        "dataAtualizacaoGlobal": atualizacao or compra.get("dataAtualizacaoGlobal"),
        "dataAtualizacao": atualizacao or compra.get("dataAtualizacao"),
        "dataAberturaProposta": compra.get("dataAberturaPropostaPncp") or compra.get("dataAberturaProposta"),
        "dataEncerramentoProposta": compra.get("dataEncerramentoPropostaPncp") or compra.get("dataEncerramentoProposta"),
        "orgaoEntidade": {
            "cnpj": compra.get("orgaoEntidadeCnpj")
            or (compra.get("orgaoEntidade") or {}).get("cnpj")
        },
        "unidadeOrgao": {
            "codigoUnidade": str(compra.get("unidadeOrgaoCodigoUnidade") or (compra.get("unidadeOrgao") or {}).get("codigoUnidade") or "")
        },
    }


def normalizar_item_compras(item: dict[str, Any]) -> dict[str, Any]:
    """Normaliza nomes de datas dos itens retornados pelo Compras.gov."""
    return {
        **item,
        "dataAtualizacao": item.get("dataAtualizacaoPncp") or item.get("dataAtualizacao"),
    }


def sem_acentos(texto: Any) -> str:
    valor = str(texto or "").casefold()
    return "".join(
        caractere
        for caractere in unicodedata.normalize("NFKD", valor)
        if not unicodedata.combining(caractere)
    )


def interpretar_data(valor: Any) -> datetime | None:
    if not valor:
        return None
    texto = str(valor).strip().replace("Z", "+00:00")
    try:
        resultado = datetime.fromisoformat(texto)
    except ValueError:
        return None
    if resultado.tzinfo is None:
        # As datas de abertura/encerramento do PNCP sao informadas em Brasilia.
        return resultado.replace(tzinfo=FUSO_BRASILIA)
    return resultado.astimezone(FUSO_BRASILIA)


def data_mais_recente(valores: Iterable[Any]) -> datetime | None:
    datas = [data for data in (interpretar_data(v) for v in valores) if data is not None]
    return max(datas) if datas else None


def resumir_objeto(texto: Any, limite: int = 260) -> str:
    objeto = re.sub(r"\s+", " ", str(texto or "Objeto nao informado")).strip()
    if len(objeto) <= limite:
        return objeto
    corte = objeto[: limite - 1].rsplit(" ", 1)[0]
    return f"{corte}..."


def classificar(
    compra: dict[str, Any],
    itens: list[dict[str, Any]],
    agora: datetime,
) -> tuple[str | None, datetime | None, str, str]:
    """Retorna categoria, data relevante, rotulo da data e situacao da fonte."""
    situacao_compra = str(compra.get("situacaoCompraNome") or "")
    situacao_compra_norm = sem_acentos(situacao_compra)
    atualizacao_compra = data_mais_recente(
        [compra.get("dataAtualizacaoGlobal"), compra.get("dataAtualizacao")]
    )

    if "suspens" in situacao_compra_norm:
        return "Suspenso", atualizacao_compra, "Atualizacao", situacao_compra
    if "revog" in situacao_compra_norm or "anulad" in situacao_compra_norm:
        return "Revogado/Anulado", atualizacao_compra, "Atualizacao", situacao_compra

    abertura = interpretar_data(compra.get("dataAberturaProposta"))
    encerramento = interpretar_data(compra.get("dataEncerramentoProposta"))
    if abertura and agora < abertura:
        return (
            "Aguardando abertura de propostas",
            abertura,
            "Abertura",
            situacao_compra or "Datas do PNCP",
        )
    if abertura and encerramento and abertura <= agora <= encerramento:
        return (
            "Recebendo propostas",
            encerramento,
            "Encerramento",
            situacao_compra or "Datas do PNCP",
        )

    nomes_itens = [str(item.get("situacaoCompraItemNome") or "") for item in itens]
    nomes_norm = [sem_acentos(nome) for nome in nomes_itens]
    ultima_atualizacao_item = data_mais_recente(item.get("dataAtualizacao") for item in itens)
    situacao_fonte = ", ".join(sorted({nome for nome in nomes_itens if nome}))
    situacao_fonte = situacao_fonte or situacao_compra or "Nao informada"

    if encerramento and agora > encerramento and any("andamento" in nome for nome in nomes_norm):
        return (
            "Fase externa em andamento",
            encerramento,
            "Encerramento das propostas",
            situacao_fonte,
        )

    tem_homologado = any("homolog" in nome for nome in nomes_norm)
    tem_fracassado_deserto = any(
        "fracass" in nome or "desert" in nome for nome in nomes_norm
    )
    tem_cancelado = any(
        "cancel" in nome or "anulad" in nome or "revog" in nome
        for nome in nomes_norm
    )

    if tem_homologado:
        data_homologacao = ultima_atualizacao_item or atualizacao_compra
        return (
            "Homologado",
            data_homologacao,
            "Atualizacao dos itens",
            situacao_fonte,
        )

    if tem_fracassado_deserto:
        return (
            "Fracassado/Deserto",
            ultima_atualizacao_item or atualizacao_compra,
            "Atualizacao dos itens",
            situacao_fonte,
        )
    if tem_cancelado:
        return (
            "Revogado/Anulado",
            ultima_atualizacao_item or atualizacao_compra,
            "Atualizacao dos itens",
            situacao_fonte,
        )

    # Alguns registros permanecem com a situação global "Divulgada no PNCP"
    # mesmo quando as fontes públicas não disponibilizam itens suficientes para
    # concluir se houve homologação, fracasso, deserção ou encerramento. Nesses
    # casos, não inferimos fase operacional nem descartamos silenciosamente o
    # registro: ele é destacado para verificação manual.
    if "divulgada" in situacao_compra_norm and not itens:
        data_relevante = encerramento or atualizacao_compra
        rotulo = "Encerramento das propostas" if encerramento else "Última atualização"
        return (
            "Verificação manual",
            data_relevante,
            rotulo,
            "Divulgada no PNCP - sem itens disponíveis nas fontes públicas",
        )

    return None, atualizacao_compra, "Atualizacao", situacao_fonte


def construir_processo(
    compra: dict[str, Any],
    itens: list[dict[str, Any]],
    agora: datetime,
) -> Processo | None:
    categoria, data_relevante, rotulo_data, situacao_fonte = classificar(
        compra, itens, agora
    )
    if categoria is None:
        return None

    modalidade = "Pregão" if int(compra.get("modalidadeId") or 0) == 6 else "Dispensa"
    return Processo(
        numero=str(compra.get("numeroCompra") or "s/n"),
        modalidade=modalidade,
        ano=int(compra.get("anoCompra") or 0),
        objeto=resumir_objeto(compra.get("objetoCompra")),
        situacao_gerencial=categoria,
        data_relevante=data_relevante,
        rotulo_data=rotulo_data,
        situacao_fonte=situacao_fonte,
        numero_controle_pncp=str(compra.get("numeroControlePNCP") or ""),
    )


def coletar(
    inicio: date,
    fim: date,
    agora: datetime,
    max_workers: int,
    tentativas_pncp: int = 3,
    espera_base_pncp: float = 4.0,
    cache_pncp_path: Path | None = None,
    cache_pncp_max_age_hours: float = 24.0,
    cache_base_path: Path | None = None,
    cache_base_max_age_hours: float = 30.0,
) -> ResultadoColeta:
    resultado = ResultadoColeta()
    compras = ClienteCompras(timeout_leitura=30)
    pncp_busca = ClientePncp(timeout_leitura=25)
    pncp_itens = ClientePncp(timeout_leitura=18)

    try:
        cadastro = compras.consultar_uasg()
        cnpj = re.sub(r"\D", "", str(cadastro.get("cnpjCpfOrgao") or ""))
        if len(cnpj) != 14:
            raise ApiError("Cadastro da UASG retornou CNPJ do orgao invalido")
        logging.info("UASG %s confirmada: %s", UASG, cadastro.get("nomeUasg"))
    except ApiError as exc:
        cnpj = CNPJ_ORGAO_FALLBACK
        aviso = (
            "Nao foi possivel confirmar o cadastro da UASG no Compras.gov.br; "
            "foi usado o CNPJ previamente validado do Comando da Marinha. "
            f"Detalhe: {exc}"
        )
        logging.warning(aviso)
        resultado.avisos.append(aviso)

    contratacoes: list[dict[str, Any]] = []
    falhas_modalidade = 0

    # A consulta principal usa o Dados Abertos do Compras.gov. Em falha transitória,
    # a primeira contingência é um cache operacional recente. Isso evita varrer
    # todo o ano no PNCP quando o backend público está degradado. O PNCP completo
    # fica como última contingência apenas quando não existe cache recente.
    for nome, codigos in MODALIDADES.items():
        lote_normalizado: list[dict[str, Any]] = []
        chave_base = f"base:{fim.year}:{sem_acentos(nome)}"
        try:
            lote = compras.listar_contratacoes(inicio, fim, codigos["compras"])
            lote_normalizado = [normalizar_compra_compras(c) for c in lote]
            logging.info("Compras.gov: %s - %d registro(s)", nome, len(lote_normalizado))
            if cache_base_path is not None:
                salvar_entrada(cache_base_path, chave_base, lote_normalizado, agora)
        except ApiError as exc_compras:
            cache_recente = None
            if cache_base_path is not None:
                cache_recente = carregar_entrada_recente(
                    cache_base_path, chave_base, agora, cache_base_max_age_hours
                )
            if cache_recente is not None:
                lote_normalizado, salvo_em = cache_recente
                resultado.cache_base_utilizado = True
                resultado.cache_base_fontes.append(nome)
                resultado.cache_base_timestamps.append(salvo_em)
                aviso = (
                    f"Compras.gov indisponivel para {nome}; usando cache operacional de "
                    f"{salvo_em.astimezone(FUSO_BRASILIA).strftime('%d/%m/%Y %H:%M')}."
                )
                logging.warning("%s Detalhe da fonte: %s", aviso, exc_compras)
                resultado.avisos.append(aviso)
            else:
                logging.warning(
                    "Falha no Compras.gov para %s e nenhum cache recente foi encontrado; "
                    "tentando PNCP como ultima contingencia: %s",
                    nome, exc_compras,
                )
                try:
                    lote_normalizado = pncp_busca.listar_contratacoes(
                        cnpj, inicio, fim, codigos["pncp"]
                    )
                    logging.info("PNCP contingencia: %s - %d registro(s)", nome, len(lote_normalizado))
                    resultado.avisos.append(
                        f"A consulta de {nome} usou o PNCP como contingencia porque o Dados Abertos do Compras.gov falhou."
                    )
                    if cache_base_path is not None:
                        salvar_entrada(cache_base_path, chave_base, lote_normalizado, agora)
                except ApiError as exc_pncp:
                    falhas_modalidade += 1
                    aviso = (
                        f"Falha ao consultar {nome} nas duas fontes publicas e sem cache operacional recente. "
                        f"Compras.gov: {exc_compras} | PNCP: {exc_pncp}"
                    )
                    logging.error(aviso)
                    resultado.avisos.append(aviso)
                    resultado.dados_completos = False
                    resultado.falhas_essenciais.append(f"consulta principal de {nome}")

        contratacoes.extend(lote_normalizado)

    if falhas_modalidade == len(MODALIDADES):
        raise ApiError("As consultas de Pregao e Dispensa falharam no Compras.gov e no PNCP")

    # Complemento de atualidade.
    #
    # Os dois recortes do PNCP abaixo sao ESSENCIAIS para a integridade do
    # relatorio diario, pois capturam certames com propostas abertas e
    # atualizacoes/reaberturas recentes que podem ainda nao ter chegado ao
    # Dados Abertos do Compras.gov. Se qualquer um deles continuar falhando
    # apos as retentativas, o PDF e gerado para diagnostico, mas o resultado
    # e marcado como incompleto para bloquear o envio automatico por e-mail.
    pncp_atual = ClientePncp(timeout_conexao=5, timeout_leitura=12)
    inicio_atualizacoes = max(inicio, fim - timedelta(days=29))
    data_limite_propostas = fim + timedelta(days=60)

    def registrar_falha_essencial(rotulo: str, detalhe: Exception) -> None:
        resultado.dados_completos = False
        resultado.falhas_essenciais.append(rotulo)
        aviso_log = f"{rotulo} indisponivel apos retentativas: {detalhe}"
        logging.error(aviso_log)
        resultado.avisos.append(
            f"{rotulo} indisponivel apos retentativas ({resumo_erro_para_relatorio(detalhe)})."
        )

    def chave_cache(tipo: str, nome: str) -> str:
        return f"{tipo}:{nome}"

    def usar_cache_ou_registrar_falha(
        rotulo: str,
        chave: str,
        detalhe: Exception,
    ) -> list[dict[str, Any]]:
        if cache_pncp_path is not None:
            cache = carregar_entrada_recente(
                cache_pncp_path,
                chave,
                agora,
                cache_pncp_max_age_hours,
            )
            if cache is not None:
                registros, salvo_em = cache
                resultado.cache_pncp_utilizado = True
                resultado.cache_pncp_fontes.append(rotulo)
                resultado.cache_pncp_timestamps.append(salvo_em)
                aviso = (
                    f"{rotulo} indisponivel no momento; usando snapshot PNCP de "
                    f"{salvo_em.astimezone(FUSO_BRASILIA).strftime('%d/%m/%Y %H:%M')}."
                )
                logging.warning(aviso)
                resultado.avisos.append(aviso)
                return registros
        registrar_falha_essencial(rotulo, detalhe)
        return []

    for nome, codigos in MODALIDADES.items():
        descricao_abertos = f"PNCP complemento de propostas abertas para {nome}"
        cache_abertos = chave_cache("propostas_abertas", nome)
        try:
            abertos = executar_com_retentativas(
                descricao_abertos,
                lambda cnpj=cnpj, codigo=codigos["pncp"]: pncp_atual.listar_propostas_abertas(
                    cnpj, data_limite_propostas, codigo
                ),
                tentativas=tentativas_pncp,
                espera_base_segundos=espera_base_pncp,
            )
            logging.info(
                "PNCP complemento: %s - %d com propostas abertas",
                nome,
                len(abertos),
            )
            contratacoes.extend(abertos)
            if cache_pncp_path is not None:
                salvar_entrada(cache_pncp_path, cache_abertos, abertos, agora)
        except ApiError as exc:
            contratacoes.extend(
                usar_cache_ou_registrar_falha(
                    descricao_abertos, cache_abertos, exc
                )
            )

        descricao_atualizados = f"PNCP complemento de atualizacoes recentes para {nome}"
        cache_atualizados = chave_cache("atualizacoes_recentes", nome)
        try:
            atualizados = executar_com_retentativas(
                descricao_atualizados,
                lambda cnpj=cnpj, codigo=codigos["pncp"]: pncp_atual.listar_atualizadas_recentes(
                    cnpj, inicio_atualizacoes, fim, codigo
                ),
                tentativas=tentativas_pncp,
                espera_base_segundos=espera_base_pncp,
            )
            logging.info(
                "PNCP complemento: %s - %d atualizado(s) nos ultimos 30 dias",
                nome,
                len(atualizados),
            )
            contratacoes.extend(atualizados)
            if cache_pncp_path is not None:
                salvar_entrada(cache_pncp_path, cache_atualizados, atualizados, agora)
        except ApiError as exc:
            contratacoes.extend(
                usar_cache_ou_registrar_falha(
                    descricao_atualizados, cache_atualizados, exc
                )
            )

    # Defesa contra duplicidades e respostas que ignorem filtros. Registros do
    # PNCP são anexados depois da base do Compras.gov e, em caso de mesma chave,
    # prevalece a versão mais recente do PNCP.
    unicos: dict[str, dict[str, Any]] = {}
    for compra in contratacoes:
        unidade = str((compra.get("unidadeOrgao") or {}).get("codigoUnidade") or "")
        modalidade = int(compra.get("modalidadeId") or 0)
        if unidade != UASG or modalidade not in MODALIDADES_PNCP:
            continue
        chave = str(compra.get("numeroControlePNCP") or compra.get("idCompra") or id(compra))
        unicos[chave] = compra
    contratacoes = list(unicos.values())
    resultado.total_consultado = len(contratacoes)

    itens_por_chave: dict[str, list[dict[str, Any]]] = {}
    falhas_itens: set[str] = set()
    precisam_itens: list[dict[str, Any]] = []
    for compra in contratacoes:
        situacao = sem_acentos(compra.get("situacaoCompraNome"))
        encerramento = interpretar_data(compra.get("dataEncerramentoProposta"))
        if not ("suspens" in situacao or "revog" in situacao or "anulad" in situacao):
            if encerramento and agora > encerramento:
                precisam_itens.append(compra)

    logging.info("Consultando itens de %d contratacao(oes)", len(precisam_itens))

    def buscar_itens_robusto(
        compra: dict[str, Any],
    ) -> tuple[list[dict[str, Any]], datetime | None]:
        # Para itens, prioriza o endpoint transacional do PNCP. Se PNCP e
        # Compras.gov falharem simultaneamente, usa cache operacional recente
        # daquela contratação. O timestamp retornado identifica uso de cache.
        identificador = str(
            compra.get("numeroControlePNCP") or compra.get("idCompra") or ""
        )
        chave_item = f"itens:{identificador}"
        erro_pncp: Exception | None = None
        try:
            itens = pncp_itens.listar_itens(compra)
            if itens:
                return itens, None
        except ApiError as exc:
            erro_pncp = exc
            logging.warning(
                "Falha ao consultar itens no PNCP de %s; tentando Compras.gov: %s",
                identificador, exc,
            )

        erro_compras: Exception | None = None
        if compra.get("idCompra"):
            try:
                itens = compras.listar_itens(compra)
                if itens:
                    return itens, None
            except ApiError as exc:
                erro_compras = exc

        houve_erro = erro_pncp is not None or erro_compras is not None
        if houve_erro and cache_base_path is not None and identificador:
            cache_item = carregar_entrada_recente(
                cache_base_path, chave_item, agora, cache_base_max_age_hours
            )
            if cache_item is not None:
                itens_cache, salvo_em = cache_item
                logging.warning(
                    "Itens de %s indisponiveis nas fontes atuais; usando cache de %s.",
                    identificador,
                    salvo_em.astimezone(FUSO_BRASILIA).strftime('%d/%m/%Y %H:%M'),
                )
                return itens_cache, salvo_em

        if erro_pncp is not None and erro_compras is not None:
            raise ApiError(
                f"Itens indisponiveis nas duas fontes. PNCP: {erro_pncp} | Compras.gov: {erro_compras}"
            ) from erro_compras
        if erro_compras is not None:
            raise ApiError(f"Itens indisponiveis no Compras.gov: {erro_compras}") from erro_compras
        if erro_pncp is not None:
            raise ApiError(f"Itens indisponiveis no PNCP: {erro_pncp}") from erro_pncp

        # As duas fontes responderam sem erro, mas sem itens.
        return [], None

    itens_cache_usados: list[datetime] = []
    itens_live_para_cache: list[tuple[str, list[dict[str, Any]]]] = []
    with ThreadPoolExecutor(max_workers=max(1, min(max_workers, 6))) as executor:
        futuros = {executor.submit(buscar_itens_robusto, compra): compra for compra in precisam_itens}
        for futuro in as_completed(futuros):
            compra = futuros[futuro]
            chave = str(compra.get("numeroControlePNCP") or compra.get("idCompra") or "")
            try:
                itens, cache_salvo_em = futuro.result()
                itens_por_chave[chave] = itens
                if cache_salvo_em is not None:
                    itens_cache_usados.append(cache_salvo_em)
                elif cache_base_path is not None and chave:
                    itens_live_para_cache.append((f"itens:{chave}", itens))
            except ApiError as exc:
                falhas_itens.add(chave)
                logging.error("Falha ao consultar itens de %s: %s", chave, exc)

    # Escrita sequencial para evitar concorrência no arquivo JSON do cache.
    if cache_base_path is not None:
        for chave_item, itens in itens_live_para_cache:
            salvar_entrada(cache_base_path, chave_item, itens, agora)

    if itens_cache_usados:
        resultado.cache_base_utilizado = True
        resultado.cache_base_fontes.append(f"itens de {len(itens_cache_usados)} contratacao(oes)")
        resultado.cache_base_timestamps.extend(itens_cache_usados)
        mais_antigo = min(itens_cache_usados)
        resultado.avisos.append(
            f"Itens de {len(itens_cache_usados)} contratacao(oes) foram obtidos de cache operacional recente "
            f"({mais_antigo.astimezone(FUSO_BRASILIA).strftime('%d/%m/%Y %H:%M')})."
        )

    if falhas_itens:
        resultado.dados_completos = False
        resultado.falhas_essenciais.append(
            f"itens indisponiveis para {len(falhas_itens)} contratacao(oes)"
        )
        resultado.avisos.append(
            f"{len(falhas_itens)} contratacao(oes) nao puderam ser classificadas "
            "porque a consulta publica de itens falhou nas fontes disponiveis."
        )

    for compra in contratacoes:
        chave = str(compra.get("numeroControlePNCP") or compra.get("idCompra") or "")
        if chave in falhas_itens:
            resultado.nao_classificados += 1
            continue
        processo = construir_processo(
            compra,
            itens_por_chave.get(chave, []),
            agora,
        )
        if processo:
            resultado.processos.append(processo)
        else:
            resultado.nao_classificados += 1
            logging.warning(
                "Contratacao nao classificada: %s | numero=%s/%s | situacao=%s | itens=%s",
                compra.get("numeroControlePNCP") or compra.get("idCompra"),
                compra.get("numeroCompra"),
                compra.get("anoCompra"),
                compra.get("situacaoCompraNome") or "Nao informada",
                ", ".join(
                    sorted(
                        {
                            str(item.get("situacaoCompraItemNome") or "Nao informada")
                            for item in itens_por_chave.get(chave, [])
                        }
                    )
                ) or "sem itens",
            )

    resultado.processos.sort(
        key=lambda p: (
            ORDEM_SITUACOES.index(p.situacao_gerencial),
            -(p.data_relevante.timestamp() if p.data_relevante else 0),
            p.numero_exibicao,
        )
    )
    return resultado



def atualizar_snapshot_pncp(
    inicio: date,
    fim: date,
    agora: datetime,
    cache_pncp_path: Path,
    tentativas_pncp: int = 3,
    espera_base_pncp: float = 4.0,
    timeout_leitura: float = 20.0,
    cache_fresh_hours: float = 6.0,
) -> tuple[int, list[str]]:
    """Pré-coleta os quatro complementos essenciais do PNCP e os salva no cache.

    É destinada à execução de madrugada. Entradas anteriores são preservadas quando
    uma consulta específica falha; a rotina diária das 08:00 valida a idade antes de
    aceitá-las como contingência.
    """
    compras = ClienteCompras(timeout_leitura=30)
    try:
        cadastro = compras.consultar_uasg()
        cnpj = re.sub(r"\D", "", str(cadastro.get("cnpjCpfOrgao") or ""))
        if len(cnpj) != 14:
            raise ApiError("Cadastro da UASG retornou CNPJ do orgao invalido")
        logging.info("UASG %s confirmada para snapshot PNCP: %s", UASG, cadastro.get("nomeUasg"))
    except ApiError as exc:
        cnpj = CNPJ_ORGAO_FALLBACK
        logging.warning(
            "Snapshot PNCP: nao foi possivel confirmar a UASG; usando CNPJ validado. Detalhe: %s",
            exc,
        )

    pncp = ClientePncp(timeout_conexao=5, timeout_leitura=timeout_leitura)
    inicio_atualizacoes = max(inicio, fim - timedelta(days=29))
    data_limite_propostas = fim + timedelta(days=60)
    atualizadas = 0
    falhas: list[str] = []

    consultas = []
    for nome, codigos in MODALIDADES.items():
        consultas.extend(
            [
                (
                    f"propostas_abertas:{nome}",
                    f"Snapshot PNCP de propostas abertas para {nome}",
                    lambda codigo=codigos["pncp"]: pncp.listar_propostas_abertas(
                        cnpj, data_limite_propostas, codigo
                    ),
                ),
                (
                    f"atualizacoes_recentes:{nome}",
                    f"Snapshot PNCP de atualizacoes recentes para {nome}",
                    lambda codigo=codigos["pncp"]: pncp.listar_atualizadas_recentes(
                        cnpj, inicio_atualizacoes, fim, codigo
                    ),
                ),
            ]
        )

    for chave, descricao, consulta in consultas:
        # As execucoes noturnas sao cumulativas. Se este recorte ja foi obtido
        # com sucesso nas ultimas horas, nao o consulta novamente. Isso reduz
        # carga no PNCP e evita 429 quando uma rodada posterior tenta apenas
        # completar os recortes que faltaram na rodada anterior.
        existente = carregar_entrada_recente(
            cache_pncp_path,
            chave,
            agora,
            max_age_hours=max(0.0, float(cache_fresh_hours)),
        )
        if existente is not None:
            registros_existentes, salvo_em = existente
            atualizadas += 1
            logging.info(
                "%s ja possui snapshot recente de %s (%d registro(s)); mantendo cache e pulando nova consulta.",
                descricao,
                salvo_em.astimezone(FUSO_BRASILIA).strftime("%d/%m/%Y %H:%M"),
                len(registros_existentes),
            )
            continue

        try:
            registros = executar_com_retentativas(
                descricao,
                consulta,
                tentativas=tentativas_pncp,
                espera_base_segundos=espera_base_pncp,
            )
            salvar_entrada(cache_pncp_path, chave, registros, agora)
            atualizadas += 1
            logging.info(
                "%s atualizado com sucesso: %d registro(s) | cache=%s",
                descricao,
                len(registros),
                cache_pncp_path,
            )
        except ApiError as exc:
            falhas.append(descricao)
            logging.error("%s falhou: %s", descricao, exc)

    return atualizadas, falhas

def formatar_data(data_hora: datetime | None) -> str:
    if data_hora is None:
        return "Não informada"
    return data_hora.astimezone(FUSO_BRASILIA).strftime("%d/%m/%Y %H:%M")


def gerar_pdf(
    caminho: Path,
    resultado: ResultadoColeta,
    agora: datetime,
    inicio: date,
    fim: date,
) -> None:
    caminho.parent.mkdir(parents=True, exist_ok=True)
    estilos_base = getSampleStyleSheet()
    azul = colors.HexColor("#163A5F")
    azul_claro = colors.HexColor("#EAF1F7")
    cinza = colors.HexColor("#4B5563")
    borda = colors.HexColor("#CBD5E1")

    estilos = {
        "titulo": ParagraphStyle(
            "TituloRelatorio",
            parent=estilos_base["Title"],
            fontName="Helvetica-Bold",
            fontSize=16,
            leading=19,
            textColor=azul,
            alignment=TA_LEFT,
            spaceAfter=4,
        ),
        "subtitulo": ParagraphStyle(
            "SubtituloRelatorio",
            parent=estilos_base["Normal"],
            fontSize=8.5,
            leading=11,
            textColor=cinza,
            spaceAfter=10,
        ),
        "secao": ParagraphStyle(
            "SecaoRelatorio",
            parent=estilos_base["Heading2"],
            fontName="Helvetica-Bold",
            fontSize=11,
            leading=14,
            textColor=azul,
            spaceBefore=9,
            spaceAfter=5,
        ),
        "celula": ParagraphStyle(
            "CelulaRelatorio",
            parent=estilos_base["BodyText"],
            fontSize=7.6,
            leading=9.5,
            textColor=colors.HexColor("#111827"),
        ),
        "cabecalho": ParagraphStyle(
            "CabecalhoTabela",
            parent=estilos_base["Normal"],
            fontName="Helvetica-Bold",
            fontSize=7.4,
            leading=9,
            textColor=colors.white,
            alignment=TA_CENTER,
        ),
        "nota": ParagraphStyle(
            "NotaRelatorio",
            parent=estilos_base["BodyText"],
            fontSize=7.5,
            leading=9.5,
            textColor=cinza,
        ),
        "alerta": ParagraphStyle(
            "AlertaIntegridade",
            parent=estilos_base["BodyText"],
            fontName="Helvetica-Bold",
            fontSize=8.3,
            leading=10.5,
            textColor=colors.HexColor("#8A1C1C"),
            backColor=colors.HexColor("#FDECEC"),
            borderColor=colors.HexColor("#E3A6A6"),
            borderWidth=0.6,
            borderPadding=6,
            spaceAfter=8,
        ),
        "contingencia": ParagraphStyle(
            "ContingenciaPncp",
            parent=estilos_base["BodyText"],
            fontSize=8.1,
            leading=10.3,
            textColor=colors.HexColor("#6B4F00"),
            backColor=colors.HexColor("#FFF7D6"),
            borderColor=colors.HexColor("#E2C55A"),
            borderWidth=0.6,
            borderPadding=6,
            spaceAfter=8,
        ),
    }

    documento = SimpleDocTemplate(
        str(caminho),
        pagesize=A4,
        rightMargin=1.35 * cm,
        leftMargin=1.35 * cm,
        topMargin=1.4 * cm,
        bottomMargin=1.4 * cm,
        title=f"Relatório gerencial de contratações - UASG {UASG}",
        author="Relatório Compras UASG 765741",
    )

    historia: list[Any] = [
        Paragraph("Relatório gerencial de contratações", estilos["titulo"]),
        Paragraph(
            f"UASG {UASG} - Laboratório Farmacêutico da Marinha/RJ<br/>"
            f"Gerado em {formatar_data(agora)} (horário de Brasília) | "
            f"Publicações consultadas: {inicio.strftime('%d/%m/%Y')} a {fim.strftime('%d/%m/%Y')}",
            estilos["subtitulo"],
        ),
    ]

    if not resultado.dados_completos:
        falhas = "; ".join(resultado.falhas_essenciais) or "fonte publica essencial indisponivel"
        historia.append(
            Paragraph(
                "ATENÇÃO: relatório potencialmente incompleto. "
                f"Falha(s) de integridade: {texto_pdf_seguro(falhas)}. O envio automático por e-mail deve permanecer bloqueado.",
                estilos["alerta"],
            )
        )

    if resultado.cache_pncp_utilizado:
        timestamps = resultado.cache_pncp_timestamps or []
        mais_antigo = min(timestamps) if timestamps else None
        data_snapshot = formatar_data(mais_antigo) if mais_antigo else "horário não informado"
        fontes = "; ".join(dict.fromkeys(resultado.cache_pncp_fontes))
        historia.append(
            Paragraph(
                "CONTINGÊNCIA PNCP: a API não respondeu em uma ou mais consultas atuais. "
                f"Foram usados dados do snapshot local de {data_snapshot}. "
                f"Recortes em contingência: {texto_pdf_seguro(fontes)}.",
                estilos["contingencia"],
            )
        )

    contagens = {
        situacao: sum(1 for p in resultado.processos if p.situacao_gerencial == situacao)
        for situacao in ORDEM_SITUACOES
    }
    resumo_linhas = []
    for situacao in ORDEM_SITUACOES:
        if contagens[situacao]:
            resumo_linhas.append(
                [Paragraph(situacao, estilos["celula"]), str(contagens[situacao])]
            )
    if not resumo_linhas:
        resumo_linhas = [[Paragraph("Nenhum registro classificado", estilos["celula"]), "0"]]

    resumo = Table(
        [[Paragraph("Resumo", estilos["cabecalho"]), ""]] + resumo_linhas,
        colWidths=[15.8 * cm, 2.1 * cm],
    )
    resumo.setStyle(
        TableStyle(
            [
                ("SPAN", (0, 0), (1, 0)),
                ("BACKGROUND", (0, 0), (-1, 0), azul),
                ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
                ("ALIGN", (1, 1), (1, -1), "CENTER"),
                ("FONTNAME", (1, 1), (1, -1), "Helvetica-Bold"),
                ("FONTSIZE", (1, 1), (1, -1), 8),
                ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, azul_claro]),
                ("GRID", (0, 0), (-1, -1), 0.35, borda),
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                ("LEFTPADDING", (0, 0), (-1, -1), 6),
                ("RIGHTPADDING", (0, 0), (-1, -1), 6),
                ("TOPPADDING", (0, 0), (-1, -1), 4),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
            ]
        )
    )
    historia.extend([resumo, Spacer(1, 7)])

    notas = []
    if resultado.nao_classificados:
        notas.append(
            f"{resultado.nao_classificados} registro(s) ficaram fora das seções por dados "
            "públicos insuficientes para classificação segura."
        )
    notas.extend(resultado.avisos)
    if notas:
        historia.append(
            Paragraph("Observacoes: " + texto_pdf_seguro(" ".join(notas)), estilos["nota"])
        )

    for situacao in ORDEM_SITUACOES:
        processos = [p for p in resultado.processos if p.situacao_gerencial == situacao]
        if not processos:
            continue
        titulo_secao = (
            "Verificação manual - dados públicos insuficientes"
            if situacao == "Verificação manual"
            else situacao
        )
        historia.append(Paragraph(f"{texto_pdf_seguro(titulo_secao)} ({len(processos)})", estilos["secao"]))
        linhas = [
            [
                Paragraph("Número", estilos["cabecalho"]),
                Paragraph("Objeto resumido", estilos["cabecalho"]),
                Paragraph("Data relevante", estilos["cabecalho"]),
                Paragraph("Situação gerencial", estilos["cabecalho"]),
            ]
        ]
        for processo in processos:
            data_texto = f"{texto_pdf_seguro(processo.rotulo_data)}:<br/>{texto_pdf_seguro(formatar_data(processo.data_relevante))}"
            linhas.append(
                [
                    Paragraph(texto_pdf_seguro(processo.numero_exibicao), estilos["celula"]),
                    Paragraph(texto_pdf_seguro(processo.objeto), estilos["celula"]),
                    Paragraph(data_texto, estilos["celula"]),
                    Paragraph(texto_pdf_seguro(processo.situacao_gerencial), estilos["celula"]),
                ]
            )
        tabela = LongTable(
            linhas,
            colWidths=[2.8 * cm, 8.6 * cm, 3.1 * cm, 3.4 * cm],
            repeatRows=1,
            splitByRow=1,
        )
        tabela.setStyle(
            TableStyle(
                [
                    ("BACKGROUND", (0, 0), (-1, 0), azul),
                    ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
                    ("VALIGN", (0, 0), (-1, -1), "TOP"),
                    ("GRID", (0, 0), (-1, -1), 0.35, borda),
                    ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, azul_claro]),
                    ("LEFTPADDING", (0, 0), (-1, -1), 4),
                    ("RIGHTPADDING", (0, 0), (-1, -1), 4),
                    ("TOPPADDING", (0, 0), (-1, -1), 4),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
                ]
            )
        )
        historia.append(tabela)

    historia.extend(
        [
            Spacer(1, 9),
            Paragraph(
                "Critério conservador: o relatório não identifica lances, habilitação, "
                "recurso ou outra subfase operacional. 'Fase externa em andamento' "
                "significa apenas que o prazo de propostas terminou e ao menos um item "
                "consta publicamente como 'Em andamento'. 'Homologado' significa que "
                "há ao menos um item homologado; a contratação pode conter outros itens "
                "fracassados ou cancelados. 'Verificação manual' identifica registros "
                "cuja fonte pública informa apenas 'Divulgada no PNCP' e não fornece "
                "itens suficientes para uma classificação segura.",
                estilos["nota"],
            ),
        ]
    )

    def cabecalho_rodape(canvas: Any, doc: Any) -> None:
        canvas.saveState()
        canvas.setStrokeColor(borda)
        canvas.line(1.35 * cm, 1.08 * cm, A4[0] - 1.35 * cm, 1.08 * cm)
        canvas.setFont("Helvetica", 7)
        canvas.setFillColor(cinza)
        canvas.drawString(1.35 * cm, 0.72 * cm, f"UASG {UASG} | Fonte: Compras.gov.br e PNCP")
        canvas.drawRightString(A4[0] - 1.35 * cm, 0.72 * cm, f"Página {doc.page}")
        canvas.restoreState()

    documento.build(historia, onFirstPage=cabecalho_rodape, onLaterPages=cabecalho_rodape)


def argumentos() -> argparse.Namespace:
    hoje = datetime.now(FUSO_BRASILIA).date()
    parser = argparse.ArgumentParser(
        description="Gera PDF gerencial de Pregoes e Dispensas da UASG 765741."
    )
    parser.add_argument(
        "--saida",
        type=Path,
        default=Path("output") / f"relatorio_compras_765741_{hoje.isoformat()}.pdf",
        help="Caminho do PDF de saida.",
    )
    parser.add_argument(
        "--inicio",
        type=date.fromisoformat,
        default=hoje - timedelta(days=370),
        help="Data inicial de publicacao no formato AAAA-MM-DD (padrao: 370 dias atras).",
    )
    parser.add_argument(
        "--fim",
        type=date.fromisoformat,
        default=hoje,
        help="Data final de publicacao no formato AAAA-MM-DD (padrao: hoje).",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=5,
        help="Consultas simultaneas de itens do PNCP (1 a 8; padrao: 5).",
    )
    parser.add_argument("--verbose", action="store_true", help="Ativa logs detalhados.")
    return parser.parse_args()


def main() -> int:
    args = argumentos()
    configurar_logs(Path("logs"), args.verbose)
    if args.inicio > args.fim:
        logging.error("A data inicial nao pode ser posterior a data final.")
        return 2
    agora = datetime.now(FUSO_BRASILIA)
    try:
        resultado = coletar(
            args.inicio,
            args.fim,
            agora,
            args.workers,
        )
        gerar_pdf(
            args.saida,
            resultado,
            agora,
            args.inicio,
            args.fim,
        )
    except (ApiError, OSError) as exc:
        logging.error("Relatorio nao gerado: %s", exc)
        return 1

    logging.info(
        "PDF gerado: %s | %d classificado(s) de %d consultado(s)",
        args.saida.resolve(),
        len(resultado.processos),
        resultado.total_consultado,
    )
    if not resultado.dados_completos:
        logging.warning(
            "Relatorio potencialmente incompleto: %s",
            "; ".join(resultado.falhas_essenciais) or "falha de fonte essencial",
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
