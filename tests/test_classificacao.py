import sys
import unittest
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from main import ApiTimeout, ClientePncp, classificar  # noqa: E402


TZ = ZoneInfo("America/Sao_Paulo")
AGORA = datetime(2026, 8, 25, 10, 0, tzinfo=TZ)


def compra(abertura, encerramento, situacao="Divulgada no PNCP"):
    return {
        "dataAberturaProposta": abertura,
        "dataEncerramentoProposta": encerramento,
        "situacaoCompraNome": situacao,
        "dataAtualizacaoGlobal": "2026-08-24T09:00:00",
    }


class TestClassificacao(unittest.TestCase):
    def test_aguardando_abertura(self):
        c = compra("2026-08-26T09:00:00", "2026-09-01T09:00:00")
        self.assertEqual(classificar(c, [], AGORA)[0], "Aguardando abertura de propostas")

    def test_recebendo_propostas(self):
        c = compra("2026-08-20T09:00:00", "2026-08-27T09:00:00")
        self.assertEqual(classificar(c, [], AGORA)[0], "Recebendo propostas")

    def test_fase_externa_so_com_item_em_andamento(self):
        c = compra("2026-08-01T09:00:00", "2026-08-10T09:00:00")
        itens = [{"situacaoCompraItemNome": "Em andamento", "dataAtualizacao": "2026-08-24T08:00:00"}]
        self.assertEqual(classificar(c, itens, AGORA)[0], "Fase externa em andamento")

    def test_nao_inventa_fase_sem_itens(self):
        c = compra("2026-08-01T09:00:00", "2026-08-10T09:00:00")
        self.assertEqual(classificar(c, [], AGORA)[0], "Verificação manual")

    def test_suspenso_tem_prioridade(self):
        c = compra("2026-08-01T09:00:00", "2026-08-30T09:00:00", "Suspensa")
        self.assertEqual(classificar(c, [], AGORA)[0], "Suspenso")

    def test_revogado(self):
        c = compra("2026-08-01T09:00:00", "2026-08-10T09:00:00", "Revogada")
        self.assertEqual(classificar(c, [], AGORA)[0], "Revogado/Anulado")

    def test_homologado_recente(self):
        c = compra("2026-07-01T09:00:00", "2026-07-10T09:00:00")
        itens = [{"situacaoCompraItemNome": "Homologado", "dataAtualizacao": "2026-08-20T08:00:00"}]
        self.assertEqual(classificar(c, itens, AGORA)[0], "Homologado")

    def test_homologado_antigo_tambem_e_incluido(self):
        c = compra("2026-01-01T09:00:00", "2026-01-10T09:00:00")
        itens = [{"situacaoCompraItemNome": "Homologado", "dataAtualizacao": "2026-02-01T08:00:00"}]
        self.assertEqual(classificar(c, itens, AGORA)[0], "Homologado")

    def test_cancelado_usa_grupo_terminal(self):
        c = compra("2026-07-01T09:00:00", "2026-07-10T09:00:00")
        itens = [{"situacaoCompraItemNome": "Cancelado", "dataAtualizacao": "2026-08-20T08:00:00"}]
        self.assertEqual(classificar(c, itens, AGORA)[0], "Revogado/Anulado")

    def test_deserto_somente_quando_informado(self):
        c = compra("2026-07-01T09:00:00", "2026-07-10T09:00:00")
        itens = [{"situacaoCompraItemNome": "Deserto", "dataAtualizacao": "2026-08-20T08:00:00"}]
        self.assertEqual(classificar(c, itens, AGORA)[0], "Fracassado/Deserto")


class TestJanelasPncp(unittest.TestCase):

    def test_timeout_subdivide_janela_automaticamente(self):
        cliente = ClientePncp()
        chamadas = []

        def fake_obter_json(url, params=None):
            ini = datetime.strptime(params["dataInicial"], "%Y%m%d").date()
            fim = datetime.strptime(params["dataFinal"], "%Y%m%d").date()
            chamadas.append((ini, fim))
            if (fim - ini).days + 1 > 10:
                raise ApiTimeout("timeout simulado")
            return {"data": [], "totalPaginas": 1}

        cliente.obter_json = fake_obter_json
        resultado = cliente._consultar_janela(
            "https://exemplo.invalid", "00394502000144", 6,
            date(2026, 1, 1), date(2026, 1, 30)
        )
        self.assertEqual(resultado, [])
        self.assertTrue(any(((fim - ini).days + 1) <= 10 for ini, fim in chamadas))

    def test_janela_de_370_dias_e_dividida(self):
        janelas = list(ClientePncp._janelas_consulta(date(2025, 8, 20), date(2026, 8, 25)))
        self.assertGreaterEqual(len(janelas), 7)
        self.assertLessEqual((janelas[0][1] - janelas[0][0]).days, 59)
        self.assertEqual(janelas[0][1] + __import__("datetime").timedelta(days=1), janelas[1][0])
        self.assertEqual(janelas[-1][1], date(2026, 8, 25))


if __name__ == "__main__":
    unittest.main()
