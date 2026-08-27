import sys
import unittest
from datetime import date, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from main import (  # noqa: E402
    ApiTimeout,
    ClienteCompras,
    normalizar_compra_compras,
    normalizar_item_compras,
)


class TestFonteComprasGov(unittest.TestCase):
    def test_normaliza_campos_da_contratacao(self):
        bruto = {
            "idCompra": "76574105900012026",
            "numeroControlePNCP": "00394502000144-1-000001/2026",
            "anoCompraPncp": 2026,
            "sequencialCompraPncp": 1,
            "unidadeOrgaoCodigoUnidade": "765741",
            "orgaoEntidadeCnpj": "00394502000144",
            "numeroCompra": "90001",
            "modalidadeIdPncp": 6,
            "codigoModalidade": 5,
            "modalidadeNome": "Pregão",
            "situacaoCompraNomePncp": "Divulgada no PNCP",
            "dataAtualizacaoPncp": "2026-08-24T10:00:00Z",
            "dataAberturaPropostaPncp": "2026-08-25T09:00:00-03:00",
            "dataEncerramentoPropostaPncp": "2026-08-30T09:00:00-03:00",
        }
        n = normalizar_compra_compras(bruto)
        self.assertEqual(n["modalidadeId"], 6)
        self.assertEqual(n["anoCompra"], 2026)
        self.assertEqual(n["unidadeOrgao"]["codigoUnidade"], "765741")
        self.assertEqual(n["situacaoCompraNome"], "Divulgada no PNCP")
        self.assertEqual(n["dataEncerramentoProposta"], "2026-08-30T09:00:00-03:00")

    def test_normaliza_data_do_item(self):
        item = normalizar_item_compras(
            {"situacaoCompraItemNome": "Homologado", "dataAtualizacaoPncp": "2026-08-24T10:00:00Z"}
        )
        self.assertEqual(item["dataAtualizacao"], "2026-08-24T10:00:00Z")


    def test_consulta_itens_por_id_usa_tipo_e_codigo(self):
        cliente = ClienteCompras()
        chamada = {}

        def fake_obter_json(url, params=None):
            chamada["url"] = url
            chamada["params"] = params
            return {"resultado": [{"situacaoCompraItemNome": "Em Andamento"}]}

        cliente.obter_json = fake_obter_json
        itens = cliente.listar_itens({"idCompra": "76574105900062025"})
        self.assertEqual(len(itens), 1)
        self.assertEqual(chamada["params"], {"tipo": "idCompra", "codigo": "76574105900062025"})

    def test_timeout_compras_subdivide_janela(self):
        cliente = ClienteCompras()
        chamadas = []

        def fake_obter_json(url, params=None):
            ini = datetime.strptime(params["dataPublicacaoPncpInicial"], "%Y-%m-%d").date()
            fim = datetime.strptime(params["dataPublicacaoPncpFinal"], "%Y-%m-%d").date()
            chamadas.append((ini, fim))
            if (fim - ini).days + 1 > 8:
                raise ApiTimeout("timeout simulado")
            return {"resultado": [], "totalPaginas": 1, "paginasRestantes": 0}

        cliente.obter_json = fake_obter_json
        resultado = cliente._consultar_janela(
            "https://exemplo.invalid", 5, date(2026, 1, 1), date(2026, 1, 25)
        )
        self.assertEqual(resultado, [])
        self.assertTrue(any(((fim - ini).days + 1) <= 8 for ini, fim in chamadas))


if __name__ == "__main__":
    unittest.main()
