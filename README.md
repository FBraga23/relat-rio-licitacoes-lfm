# Relatório Diário de Contratações — UASG 765741

Aplicação Python para macOS que consulta Compras.gov/PNCP, classifica Pregões e Dispensas por situação gerencial, gera PDF e envia por SMTP do Gmail. Esta versão adiciona **pré-coleta do PNCP às 01:00 + relatório às 08:00 com cache de contingência**.

## Fluxo diário

1. **01:00 — snapshot PNCP**
   - consulta propostas abertas e atualizações recentes para Pregão e Dispensa;
   - salva somente dados públicos em `cache/pncp_complementos.json`;
   - não gera PDF e não envia e-mail.

2. **08:00 — relatório**
   - consulta Compras.gov normalmente;
   - tenta consultar novamente os complementos atuais do PNCP;
   - se o PNCP responder, usa os dados atuais e atualiza o cache;
   - se o PNCP falhar, aceita o snapshot local **somente se tiver no máximo 24 horas**;
   - quando usar cache, o PDF e o corpo do e-mail informam a data/hora do snapshot;
   - se não houver cache recente, o relatório é marcado como potencialmente incompleto e o envio automático é bloqueado.

A regra conservadora permanece: o relatório não inventa subfases como lances, habilitação ou recurso.

## Arquivos principais

- `main.py` — fontes, classificação, cache de contingência e PDF.
- `run_daily.py` — relatório das 08:00 e envio SMTP.
- `snapshot_pncp.py` — pré-coleta do PNCP da madrugada.
- `pncp_cache.py` — persistência/validação do snapshot.
- `smtp_sender.py` — envio pelo Gmail SMTP usando Senha de App no Keychain.
- `smtp_setup.py` — configura a Senha de App no Keychain.
- `scripts/install_launchd.py` — instala os dois agendamentos.

## Configuração do cache e horários

Os campos abaixo são opcionais. Se não existirem no seu `config.json`, os valores abaixo serão usados automaticamente:

```json
{
  "pncp_cache_path": "cache/pncp_complementos.json",
  "pncp_cache_max_age_hours": 24,
  "pncp_snapshot_tentativas": 3,
  "pncp_snapshot_espera_base_segundos": 4,
  "pncp_snapshot_timeout_leitura_segundos": 20,
  "hora_snapshot_pncp": 1,
  "minuto_snapshot_pncp": 0,
  "hora_relatorio": 8,
  "minuto_relatorio": 0
}
```

## Primeiro teste manual

Ative o ambiente virtual:

```bash
cd /caminho/para/compras_765741_etapa2_smtp
source .venv/bin/activate
```

Atualize o snapshot manualmente:

```bash
python snapshot_pncp.py
```

Se tudo funcionar, o final do log será semelhante a:

```text
Snapshot PNCP concluido com sucesso: 4/4 recortes atualizados.
```

O arquivo será criado em:

```text
cache/pncp_complementos.json
```

Depois teste o relatório sem e-mail:

```bash
python run_daily.py --sem-email
```

Para testar o envio real:

```bash
python run_daily.py
```

## Testar a contingência

Se o PNCP estiver indisponível às 08:00 e houver snapshot com menos de 24 horas, o log mostrará algo semelhante a:

```text
PNCP complemento ... indisponivel no momento; usando snapshot PNCP de 27/08/2026 01:03.
```

O PDF exibirá um quadro amarelo informando que foi usado o snapshot local. O e-mail continuará permitido porque a contingência está dentro do limite configurado.

Se o snapshot estiver ausente ou tiver mais de 24 horas:

```text
Relatorio potencialmente incompleto: ...
Envio de e-mail cancelado por seguranca.
```

## Instalar os dois agendamentos no macOS

Se já havia um LaunchAgent da versão anterior, reinstale:

```bash
python scripts/install_launchd.py --desinstalar
python scripts/install_launchd.py
```

A instalação cria:

- `br.mil.marinha.compras765741.snapshot` — 01:00;
- `br.mil.marinha.compras765741` — 08:00.

Teste o snapshot via `launchd`:

```bash
python scripts/install_launchd.py --executar-snapshot-agora
```

Teste o relatório via `launchd`:

```bash
python scripts/install_launchd.py --executar-agora
```

Logs:

```text
logs/compras_765741.log
logs/launchd_snapshot_stdout.log
logs/launchd_snapshot_stderr.log
logs/launchd_stdout.log
logs/launchd_stderr.log
```

## SMTP / Gmail

A versão continua usando Gmail SMTP com Senha de App armazenada no Keychain do macOS. Se ainda não configurou:

```bash
python smtp_setup.py
python smtp_setup.py --verificar
```

## Segurança

- O cache contém apenas respostas públicas do PNCP; não contém senha, token OAuth ou credencial do Gmail.
- `cache/`, `output/`, `logs/` e `config.json` estão ignorados no `.gitignore`.
- Falha do PNCP + cache vencido = bloqueio de envio.
- Cache recente utilizado = envio permitido com indicação explícita no PDF/e-mail.

## Testes

```bash
python -m unittest discover -s tests -v
```

Esta versão possui testes para cache recente, expiração do cache, pré-coleta dos quatro recortes PNCP, classificação, integridade e SMTP.

## Execucao sem depender do Mac (GitHub Actions)

A versao desta pasta inclui dois workflows em `.github/workflows/` para executar o snapshot do PNCP as 01:00 e o relatorio/e-mail as 08:00 no fuso `America/Sao_Paulo`.

Veja `README_GITHUB_ACTIONS.md` para a configuracao do repositorio privado, Secrets, teste manual e operacao automatica.
