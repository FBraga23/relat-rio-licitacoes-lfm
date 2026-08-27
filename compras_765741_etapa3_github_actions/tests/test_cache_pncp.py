import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from main import ApiTimeout, FUSO_BRASILIA, atualizar_snapshot_pncp, coletar  # noqa: E402
from pncp_cache import carregar_entrada_recente, salvar_entrada, status_cache  # noqa: E402

TZ = ZoneInfo("America/Sao_Paulo")


class TestCachePncp(unittest.TestCase):
    def test_salva_e_recupera_entrada_recente(self):
        with tempfile.TemporaryDirectory() as tmp:
            caminho = Path(tmp) / "cache.json"
            agora = datetime(2026, 8, 27, 1, 0, tzinfo=TZ)
            dados = [{"numeroControlePNCP": "x"}]
            salvar_entrada(caminho, "propostas_abertas:Pregao", dados, agora)
            recuperado = carregar_entrada_recente(
                caminho,
                "propostas_abertas:Pregao",
                agora + timedelta(hours=7),
                24,
            )
            self.assertIsNotNone(recuperado)
            registros, salvo_em = recuperado
            self.assertEqual(registros, dados)
            self.assertEqual(salvo_em, agora)

    def test_rejeita_cache_expirado(self):
        with tempfile.TemporaryDirectory() as tmp:
            caminho = Path(tmp) / "cache.json"
            agora = datetime(2026, 8, 26, 1, 0, tzinfo=TZ)
            salvar_entrada(caminho, "atualizacoes_recentes:Dispensa", [], agora)
            recuperado = carregar_entrada_recente(
                caminho,
                "atualizacoes_recentes:Dispensa",
                agora + timedelta(hours=25),
                24,
            )
            self.assertIsNone(recuperado)

    def test_snapshot_atualiza_quatro_recortes(self):
        with tempfile.TemporaryDirectory() as tmp:
            caminho = Path(tmp) / "cache.json"
            agora = datetime(2026, 8, 27, 1, 0, tzinfo=TZ)
            cadastro = {
                "cnpjCpfOrgao": "00394502000144",
                "nomeUasg": "LABORATORIO FARMACEUTICO DA MARINHA/RJ",
            }
            with patch("main.ClienteCompras.consultar_uasg", return_value=cadastro), patch(
                "main.ClientePncp.listar_propostas_abertas", return_value=[{"tipo": "aberta"}]
            ), patch(
                "main.ClientePncp.listar_atualizadas_recentes", return_value=[{"tipo": "atualizada"}]
            ):
                atualizadas, falhas = atualizar_snapshot_pncp(
                    inicio=agora.date().replace(month=1, day=1),
                    fim=agora.date(),
                    agora=agora,
                    cache_pncp_path=caminho,
                    tentativas_pncp=1,
                    espera_base_pncp=0,
                )
            self.assertEqual(atualizadas, 4)
            self.assertEqual(falhas, [])
            self.assertEqual(len(status_cache(caminho)), 4)

    def test_coleta_usa_snapshot_recente_sem_marcar_incompleto(self):
        with tempfile.TemporaryDirectory() as tmp:
            caminho = Path(tmp) / "cache.json"
            agora = datetime(2026, 8, 27, 8, 0, tzinfo=TZ)
            snapshot = agora - timedelta(hours=7)
            for chave in [
                "propostas_abertas:Pregao",
                "atualizacoes_recentes:Pregao",
                "propostas_abertas:Dispensa",
                "atualizacoes_recentes:Dispensa",
            ]:
                salvar_entrada(caminho, chave, [], snapshot)
            cadastro = {
                "cnpjCpfOrgao": "00394502000144",
                "nomeUasg": "LABORATORIO FARMACEUTICO DA MARINHA/RJ",
            }
            with patch("main.ClienteCompras.consultar_uasg", return_value=cadastro), patch(
                "main.ClienteCompras.listar_contratacoes", return_value=[]
            ), patch(
                "main.ClientePncp.listar_propostas_abertas", side_effect=ApiTimeout("timeout")
            ), patch(
                "main.ClientePncp.listar_atualizadas_recentes", side_effect=ApiTimeout("timeout")
            ), patch("main.time.sleep"):
                resultado = coletar(
                    inicio=agora.date().replace(month=1, day=1),
                    fim=agora.date(),
                    agora=agora,
                    max_workers=1,
                    tentativas_pncp=1,
                    espera_base_pncp=0,
                    cache_pncp_path=caminho,
                    cache_pncp_max_age_hours=24,
                )
            self.assertTrue(resultado.dados_completos)
            self.assertTrue(resultado.cache_pncp_utilizado)
            self.assertEqual(len(resultado.cache_pncp_fontes), 4)
            self.assertEqual(resultado.falhas_essenciais, [])


if __name__ == "__main__":
    unittest.main()
