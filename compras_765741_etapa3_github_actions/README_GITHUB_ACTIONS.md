# Execucao na nuvem com GitHub Actions

Esta versao executa o projeto sem depender do Mac:

- **01:00 (America/Sao_Paulo):** pre-coleta os 4 complementos essenciais do PNCP e salva o snapshot no cache do GitHub Actions.
- **08:00 (America/Sao_Paulo):** consulta Compras.gov, tenta PNCP atual, usa o snapshot recente como contingencia quando necessario, gera o PDF e envia por SMTP do Gmail.
- Se os dados essenciais ficarem incompletos e nao houver snapshot valido de ate 24 horas, o PDF e gerado para diagnostico, mas o e-mail e bloqueado.

## 1. Crie um repositorio privado

No GitHub, crie um repositorio **Private**. Nao publique este projeto em repositorio publico.

Envie para o repositorio os arquivos desta pasta, inclusive `.github/workflows/`.

O arquivo `config.json` nao deve ser enviado; ele continua no `.gitignore`. No GitHub Actions ele e criado em tempo de execucao a partir de `config.example.json` e dos Secrets.

## 2. Configure os GitHub Actions Secrets

No repositorio:

`Settings` -> `Secrets and variables` -> `Actions` -> `New repository secret`

Crie:

| Secret | Conteudo |
|---|---|
| `SMTP_USUARIO` | conta Gmail que enviara o relatorio |
| `SMTP_APP_PASSWORD` | Senha de App do Google, sem necessidade de remover os espacos |
| `DESTINATARIOS` | e-mails separados por virgula |

Opcionais:

| Secret | Conteudo |
|---|---|
| `SMTP_REMETENTE` | normalmente o mesmo valor de `SMTP_USUARIO` |
| `SMTP_NOME_REMETENTE` | por exemplo `Relatorio Diario de Licitacoes - LFM` |
| `CC` | e-mails separados por virgula |
| `BCC` | e-mails separados por virgula |

**Nao use a senha normal da conta Google.** O secret `SMTP_APP_PASSWORD` deve conter uma Senha de App valida.

## 3. Habilite GitHub Actions

Abra a aba `Actions`. Se o GitHub solicitar confirmacao, habilite a execucao dos workflows.

Os dois workflows sao:

- `Snapshot PNCP - 01h`
- `Relatorio Diario - 08h`

Os agendamentos usam explicitamente `America/Sao_Paulo`.

## 4. Primeiro teste sem e-mail

Abra:

`Actions` -> `Relatorio Diario - 08h` -> `Run workflow`

Mantenha `Gerar o PDF sem enviar e-mail = true`.

Ao terminar, abra a execucao e baixe o artifact `relatorio-uasg-765741-...` para conferir o PDF e os logs.

## 5. Teste do snapshot

Abra:

`Actions` -> `Snapshot PNCP - 01h` -> `Run workflow`

Uma execucao bem-sucedida deve registrar `Snapshot PNCP concluido com sucesso: 4/4 recortes atualizados.`

O arquivo de snapshot e persistido pelo cache do GitHub Actions. O relatorio das 08h restaura automaticamente o cache mais recente com prefixo `pncp-snapshot-`.

## 6. Primeiro envio real

Depois de validar o PDF, execute manualmente `Relatorio Diario - 08h` novamente e desmarque `Gerar o PDF sem enviar e-mail`.

Se o Gmail aceitar a Senha de App, o log termina com algo semelhante a:

`E-mail enviado por SMTP do Gmail.`

Se a autenticacao for recusada, gere uma nova Senha de App e substitua somente o secret `SMTP_APP_PASSWORD`.

## 7. Funcionamento automatico

Depois dos testes, nenhuma acao adicional e necessaria. O GitHub executara os workflows no branch padrao:

- snapshot: 01:00;
- relatorio/e-mail: 08:00.

O Mac pode permanecer desligado.

## 8. Seguranca

- mantenha o repositorio privado;
- nunca grave `SMTP_APP_PASSWORD` em arquivo, commit, issue ou log;
- limite quem possui permissao de escrita no repositorio;
- `config.json`, `cache/`, `output/` e `logs/` permanecem ignorados pelo Git;
- os PDFs e logs de cada execucao ficam como artifacts por 7 dias para diagnostico.

## 9. Execucao local continua funcionando

No macOS, se `SMTP_APP_PASSWORD` nao estiver definida no ambiente, o programa continua tentando ler a Senha de App pelo Keychain, como antes.
