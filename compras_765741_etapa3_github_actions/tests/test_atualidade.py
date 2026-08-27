import unittest
from datetime import date, datetime
from unittest.mock import patch

from main import ClientePncp, FUSO_BRASILIA
from run_daily import determinar_periodo


class TestPeriodoRelatorio(unittest.TestCase):
    def test_padrao_e_ano_corrente(self):
        agora = datetime(2026, 8, 25, 8, 0, tzinfo=FUSO_BRASILIA)
        self.assertEqual(determinar_periodo({}, agora), (date(2026, 1, 1), date(2026, 8, 25)))

    def test_janela_movel_so_quando_explicitamente_solicitada(self):
        agora = datetime(2026, 8, 25, 8, 0, tzinfo=FUSO_BRASILIA)
        cfg = {"periodo_relatorio": "janela_dias", "janela_dias": 30}
        self.assertEqual(determinar_periodo(cfg, agora), (date(2026, 7, 26), date(2026, 8, 25)))


class TestComplementoPncp(unittest.TestCase):
    def test_propostas_abertas_usa_endpoint_especifico(self):
        cliente = ClientePncp()
        resposta = {"data": [{"numeroControlePNCP": "x"}], "totalPaginas": 1}
        with patch.object(cliente, "obter_json", return_value=resposta) as obter:
            registros = cliente.listar_propostas_abertas("00394502000144", date(2026, 8, 25), 6)
        self.assertEqual(len(registros), 1)
        url = obter.call_args.args[0]
        params = obter.call_args.kwargs["params"]
        self.assertTrue(url.endswith("/v1/contratacoes/proposta"))
        self.assertEqual(params["dataFinal"], "20260825")
        self.assertEqual(params["codigoUnidadeAdministrativa"], "765741")
        self.assertEqual(params["codigoModalidadeContratacao"], 6)

    def test_atualizacoes_recentes_usa_endpoint_de_atualizacao(self):
        cliente = ClientePncp()
        resposta = {"data": [], "totalPaginas": 1}
        with patch.object(cliente, "obter_json", return_value=resposta) as obter:
            cliente.listar_atualizadas_recentes(
                "00394502000144", date(2026, 7, 27), date(2026, 8, 25), 8
            )
        url = obter.call_args.args[0]
        params = obter.call_args.kwargs["params"]
        self.assertTrue(url.endswith("/v1/contratacoes/atualizacao"))
        self.assertEqual(params["dataInicial"], "20260727")
        self.assertEqual(params["dataFinal"], "20260825")
        self.assertEqual(params["codigoModalidadeContratacao"], 8)


if __name__ == "__main__":
    unittest.main()
