import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from smtp_sender import SMTPError, construir_mensagem, obter_senha_app, obter_senha_keychain  # noqa: E402


class TestSMTPSender(unittest.TestCase):
    def test_mensagem_com_pdf_e_bcc_oculto(self):
        with tempfile.TemporaryDirectory() as tmp:
            pdf = Path(tmp) / "relatorio.pdf"
            pdf.write_bytes(b"%PDF-1.4\n%teste")
            msg, destinos = construir_mensagem(
                remetente="remetente@example.com",
                nome_remetente="Relatorio",
                destinatarios=["a@example.com", "b@example.com"],
                cc=["c@example.com"],
                bcc=["oculto@example.com"],
                assunto="Relatorio",
                corpo="Segue anexo.",
                anexos=[pdf],
            )
            self.assertIn("a@example.com", msg["To"])
            self.assertEqual(msg["Subject"], "Relatorio")
            self.assertIsNone(msg.get("Bcc"))
            self.assertIn("oculto@example.com", destinos)
            anexos = list(msg.iter_attachments())
            self.assertEqual(len(anexos), 1)
            self.assertEqual(anexos[0].get_filename(), "relatorio.pdf")
            self.assertEqual(anexos[0].get_content_type(), "application/pdf")

    def test_sem_destinatario_e_erro(self):
        with self.assertRaises(SMTPError):
            construir_mensagem(
                remetente="remetente@example.com",
                nome_remetente=None,
                destinatarios=[],
                assunto="x",
                corpo="y",
                anexos=[],
            )

    @patch("smtp_sender.subprocess.run")
    def test_le_senha_do_keychain(self, run):
        run.return_value = subprocess.CompletedProcess([], 0, stdout="abcd efgh ijkl mnop\n", stderr="")
        senha = obter_senha_keychain(usuario="x@gmail.com", servico="teste.smtp")
        self.assertEqual(senha, "abcdefghijklmnop")
        args = run.call_args.args[0]
        self.assertIn("security", args[0])
        self.assertIn("-w", args)

    @patch("smtp_sender.subprocess.run")
    def test_keychain_ausente_e_erro(self, run):
        run.return_value = subprocess.CompletedProcess([], 44, stdout="", stderr="not found")
        with self.assertRaises(SMTPError):
            obter_senha_keychain(usuario="x@gmail.com", servico="teste.smtp")

    @patch.dict("os.environ", {"SMTP_APP_PASSWORD": "abcd efgh ijkl mnop"}, clear=False)
    @patch("smtp_sender.subprocess.run")
    def test_env_tem_precedencia_sobre_keychain(self, run):
        senha = obter_senha_app(usuario="x@gmail.com", servico="teste.smtp")
        self.assertEqual(senha, "abcdefghijklmnop")
        run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
