import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from main import ApiTimeout, ResultadoColeta, executar_com_retentativas  # noqa: E402
from run_daily import motivo_bloqueio_envio  # noqa: E402


class TestRetentativasPncp(unittest.TestCase):
    def test_recupera_apos_timeout_transitorio(self):
        estado = {"n": 0}

        def consulta():
            estado["n"] += 1
            if estado["n"] < 3:
                raise ApiTimeout("timeout simulado")
            return ["ok"]

        with patch("main.time.sleep") as dormir:
            resultado = executar_com_retentativas(
                "consulta de teste", consulta, tentativas=3, espera_base_segundos=1
            )

        self.assertEqual(resultado, ["ok"])
        self.assertEqual(estado["n"], 3)
        self.assertEqual(dormir.call_count, 2)

    def test_propaga_erro_apos_esgotar_tentativas(self):
        def consulta():
            raise ApiTimeout("timeout persistente")

        with patch("main.time.sleep"):
            with self.assertRaises(ApiTimeout):
                executar_com_retentativas(
                    "consulta de teste", consulta, tentativas=3, espera_base_segundos=0
                )


class TestBloqueioDeEnvio(unittest.TestCase):
    def test_relatorio_integro_pode_ser_enviado(self):
        resultado = ResultadoColeta(dados_completos=True)
        self.assertIsNone(motivo_bloqueio_envio(resultado))

    def test_relatorio_incompleto_bloqueia_envio(self):
        resultado = ResultadoColeta(
            dados_completos=False,
            falhas_essenciais=["PNCP propostas abertas para Pregao"],
        )
        motivo = motivo_bloqueio_envio(resultado)
        self.assertIsNotNone(motivo)
        self.assertIn("PNCP propostas abertas", motivo)


if __name__ == "__main__":
    unittest.main()
