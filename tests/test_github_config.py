import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class TestGithubPrepareConfig(unittest.TestCase):
    def test_gera_config_sem_gravar_senha(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmpdir = Path(tmp)
            (tmpdir / "config.example.json").write_text(
                json.dumps(
                    {
                        "destinatarios": [],
                        "cc": [],
                        "bcc": [],
                        "smtp_usuario": "x",
                        "smtp_remetente": "x",
                        "smtp_nome_remetente": "Relatorio",
                        "pncp_cache_path": "cache/pncp_complementos.json",
                        "diretorio_pdf": "output",
                    }
                ),
                encoding="utf-8",
            )
            script = (ROOT / "github_prepare_config.py").read_text(encoding="utf-8")
            (tmpdir / "github_prepare_config.py").write_text(script, encoding="utf-8")
            env = os.environ.copy()
            env.update(
                {
                    "SMTP_USUARIO": "remetente@example.com",
                    "SMTP_APP_PASSWORD": "segredo-que-nao-pode-ser-gravado",
                    "DESTINATARIOS": "a@example.com,b@example.com",
                    "CC": "c@example.com",
                }
            )
            proc = subprocess.run(
                [sys.executable, "github_prepare_config.py"],
                cwd=tmpdir,
                env=env,
                text=True,
                capture_output=True,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            config = json.loads((tmpdir / "config.json").read_text(encoding="utf-8"))
            self.assertEqual(config["smtp_usuario"], "remetente@example.com")
            self.assertEqual(config["destinatarios"], ["a@example.com", "b@example.com"])
            self.assertEqual(config["cc"], ["c@example.com"])
            self.assertNotIn("SMTP_APP_PASSWORD", config)
            self.assertNotIn("segredo-que-nao-pode-ser-gravado", json.dumps(config))


if __name__ == "__main__":
    unittest.main()
