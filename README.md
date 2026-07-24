# Discord Flight Bot

Monitor de precos de voos **BEL -> NAT** (ida 27/12/2026, volta 05/01/2027) na
LATAM e na Azul, com notificacao por webhook do Discord a cada checagem.

Os dados vem da [GeckoAPI](https://geckoapi.com.br) (`POST /v1/extract`), que
cobra **5 creditos por request**. O plano free da 100 creditos/mes, entao o bot
roda **a cada 4 dias** (no maximo 8 checagens x 10 creditos = 80, com 20 de
folga para retries) e tem um contador de creditos que impede o estouro mesmo se
o scheduler disparar fora de hora.

---

## Instalacao

```bash
python -m venv .venv
```

```bash
.venv\Scripts\activate
```

```bash
pip install -e ".[dev]"
```

Copie o `.env.example` para `.env` e preencha as duas variaveis obrigatorias:

```bash
copy .env.example .env
```

| Variavel | Onde conseguir |
| --- | --- |
| `GECKOAPI_KEY` | https://dashboard.geckoapi.com.br |
| `DISCORD_WEBHOOK_URL` | Discord > Editar canal > Integracoes > Webhooks > Novo webhook |

O `.env` esta no `.gitignore` e nao deve ser commitado.

---

## Uso

```bash
python -m src.main status
```

Mostra creditos do mes e historico de precos. **Nao chama a API, nao gasta credito.**

```bash
python -m src.main dry-run
```

Envia um embed de exemplo ao Discord para validar o webhook. **Nao gasta credito.**

```bash
python -m src.main once
```

Executa uma checagem completa (LATAM + Azul). **Gasta 10 creditos.**

```bash
python -m src.main schedule
```

Deixa o processo rodando e checa a cada 4 dias via APScheduler. Roda uma vez
imediatamente ao subir e depois respeita o intervalo.

---

## Deploy: GitHub Actions + Turso

Roda na nuvem, de graca, sem depender do seu computador estar ligado.

| Papel | Servico | Por que |
| --- | --- | --- |
| Execucao + agendamento | GitHub Actions | cron nativo, 6h de timeout, gratis em repo privado |
| Banco | Turso (libSQL) | disco do Actions e efemero; libSQL fala SQLite, o SQL nao muda |

### 1. Criar o banco no Turso

```bash
turso db create voos
```

```bash
turso db show voos --url
```

```bash
turso db tokens create voos
```

Guarde a URL (`libsql://...`) e o token. O schema e criado sozinho na primeira
execucao — `init_db` roda toda vez e e idempotente.

### 2. Subir o repo (privado)

```bash
git init && git add . && git commit -m "primeira versao"
```

Confira que o `.env` **nao** entrou no commit — ele esta no `.gitignore`, mas
vale conferir com `git status` antes de publicar.

### 3. Cadastrar os secrets

Em **Settings > Secrets and variables > Actions**, crie os quatro:

| Secret | Valor |
| --- | --- |
| `GECKOAPI_KEY` | chave da GeckoAPI |
| `DISCORD_WEBHOOK_URL` | URL do webhook |
| `TURSO_DATABASE_URL` | `libsql://...` do passo 1 |
| `TURSO_AUTH_TOKEN` | token do passo 1 |

### 4. Testar

Aba **Actions > Checagem de precos > Run workflow**. Gasta 10 creditos e deve
resultar em dois embeds no Discord. A partir dai o cron assume: dias 1, 5, 9,
13, 17, 21, 25 e 29, as 9h de Belem.

### Detalhes do workflow

- **`concurrency`** impede duas execucoes escrevendo no banco ao mesmo tempo, e
  nao cancela a que ja esta rodando (ela pode ja ter gasto credito).
- **Commit de keepalive**: o GitHub desativa workflows agendados apos 60 dias
  sem atividade no repositorio. Com o banco remoto, nada mais commitaria. Por
  isso cada execucao grava `data/last-run.md` e commita — mantem o agendamento
  vivo e ainda deixa um rastro legivel. Roda com `if: always()`, para manter o
  agendamento vivo mesmo quando a checagem falha.
- **Cron do GitHub nao e pontual**: sob carga o disparo atrasa alguns minutos.
  Irrelevante para monitorar passagem a cada 4 dias.
- **`tests.yml`** roda a suite a cada push. Codigo quebrado que so falhasse na
  proxima checagem agendada custaria creditos.

### Local x producao

O mesmo codigo roda nos dois: `storage.connect()` escolhe o destino pela
presenca de `TURSO_DATABASE_URL`. Vazio (ou ausente) = SQLite local.

Isso significa que voce continua rodando `once`, `status` e `dry-run` na sua
maquina normalmente, e que **os testes rodam em SQLite em memoria, sem rede**.

Para inspecionar o banco de producao da sua maquina, basta preencher as duas
variaveis do Turso no `.env` e rodar `status` — nao gasta credito da GeckoAPI.

### Agendar sem deixar processo aberto

Se preferir rodar na sua propria maquina em vez do GitHub Actions:

O modo `schedule` exige um processo vivo. Para agendar pelo sistema:

**Windows (Agendador de Tarefas)** — a cada 4 dias, as 9h:

```powershell
$a = New-ScheduledTaskAction -Execute "C:\Users\emidi\OneDrive\Documentos\voos-project\discord-flight-bot\.venv\Scripts\python.exe" -Argument "-m src.main once" -WorkingDirectory "C:\Users\emidi\OneDrive\Documentos\voos-project\discord-flight-bot"; $t = New-ScheduledTaskTrigger -Daily -DaysInterval 4 -At 9am; $s = New-ScheduledTaskSettingsSet -StartWhenAvailable -WakeToRun; Register-ScheduledTask -TaskName "MonitorVoos" -Action $a -Trigger $t -Settings $s
```

`-WorkingDirectory` importa: `python -m src.main` so funciona a partir da raiz
do projeto. `-StartWhenAvailable` recupera disparo perdido com o PC desligado e
`-WakeToRun` acorda a maquina suspensa — o contador de creditos protege contra
o acumulo de disparos atrasados.

**Linux/macOS (cron)** — dias 1, 5, 9, 13, 17, 21, 25 e 29 as 9h:

```bash
0 9 */4 * * cd /caminho/discord-flight-bot && .venv/bin/python -m src.main once >> data/cron.log 2>&1
```

Em qualquer um dos dois, o contador de creditos continua valendo: se o
agendador disparar demais, a checagem e recusada antes de chamar a API.

---

## Testes

```bash
pytest
```

```bash
pytest --cov=src --cov-report=term-missing
```

`compare.py` e `credits_tracker.py` sao testados diretamente (logica pura +
SQLite em memoria). `fetch_prices.py` e testado com a `requests.Session`
mockada e payloads que seguem o schema documentado da GeckoAPI.

---

## Como funciona

```
main.py
  |- credits_tracker.ensure_budget(10)      # aborta se nao couber no mes
  |- fetch_prices.fetch_latam/fetch_azul    # 5 creditos cada, com retry
  |- compare.compare_with_history           # MIN(price) ANTES de salvar
  |- storage.save_offer                     # inclui o JSON bruto
  `- discord_bot.notify_offer               # 1 embed por companhia
```

Decisoes que valem registro:

- **A comparacao acontece antes do insert.** Se salvasse primeiro, o preco
  atual entraria no proprio `MIN()` e toda checagem "empataria" com o minimo.
- **Nao existe coluna `menor_preco_historico`.** E sempre derivada via
  `SELECT MIN(price) ... WHERE airline = ?`, para nao ter estado duplicado.
- **Creditos sao debitados por tentativa HTTP, nao por sucesso.** Um retry
  apos HTTP 500 debita os 5 creditos de novo, porque a API provavelmente
  cobrou. Erro de conexao (request nao chegou a sair) nao debita.
- **O ledger de creditos e chaveado por `YYYY-MM`.** O "reset todo dia 1"
  acontece sozinho na virada do mes, sem rotina de limpeza.
- **Falha de uma companhia nao derruba a outra**, e vira um embed vermelho de
  erro no Discord para voce nao achar que esta tudo bem em silencio.

### Esquema do banco

`data/prices.db`, tabela `price_checks` — uma linha por companhia por checagem:

| Coluna | Conteudo |
| --- | --- |
| `checked_at` | Timestamp ISO 8601 (UTC) |
| `airline` | `LATAM` ou `AZUL` |
| `price`, `currency` | Preco total ida + volta |
| `outbound_*` / `inbound_*` | Saida, chegada, duracao em minutos e conexoes |
| `fare_valid_until` | Validade da tarifa (ver ressalva abaixo) |
| `raw_response` | JSON bruto da API, backup caso o schema mude |

Tabela `credit_usage`: ledger append-only de creditos (`year_month`,
`credits`, `reason`, `recorded_at`).

---

## Ressalvas sobre a GeckoAPI

Tres pontos onde a doc oficial diverge do que costuma se assumir:

**1. `target` e `type` sao campos separados.** Nao e `target: "latamairlines.com:plp"`
— o corpo correto e `{"target": "latamairlines.com", "type": "plp", ...}`.

**2. Passageiros sao tres campos numericos**, nao um objeto: `numAdults`,
`numChildren`, `numInfants`.

**3. Validade da tarifa nao existe no schema.** Nem o target da LATAM nem o da
Azul retornam data de expiracao de tarifa. A coluna `fare_valid_until` existe e
esta pronta, mas e gravada como `NULL`, e o embed diz explicitamente que a
informacao nao vem da API em vez de inventar um prazo. Se a GeckoAPI passar a
expor o campo, basta preenche-lo nos parsers — o resto do pipeline ja o carrega.

Os dois formatos de resposta tambem sao bem diferentes:

- **LATAM** devolve `data.items[]`, uma lista plana com os dois sentidos
  misturados. Separamos por `route.originIata` e somamos o mais barato de cada
  lado.
- **Azul** devolve `data.trips[].journeys[]`, ja separado por trecho. Achamos
  cada trip pela origem e pegamos a journey mais barata de cada.

A duracao tambem difere: LATAM manda `flight.durationMinutes` (numero) e Azul
manda `journeys[].duration` (string). `parse_duration_to_minutes` normaliza
ISO-8601 (`PT5H20M`), relogio (`05:20`) e numero puro.

A doc avisa que "alguns campos podem retornar `null` em producao", entao todo
parse e defensivo e a resposta bruta fica salva para reprocessamento.

---

## Custos

| Acao | Creditos |
| --- | --- |
| 1 request (uma companhia) | 5 |
| 1 checagem completa | 10 |
| Orcamento mensal (plano free) | 100 |
| Checagens por mes (intervalo de 4 dias) | 8 no maximo = 80 creditos |
| Folga para retries | 20 creditos (4 tentativas extras) |

O intervalo de 3 dias, que parecia caber certinho em 100 creditos, na verdade
estourava: num mes de 31 dias o cron `*/3` dispara nos dias 1, 4, 7, ..., 31 —
**11 checagens = 110 creditos**. O contador barraria a ultima, mas voce perderia
uma leitura em vez de escolher onde economizar. Com 4 dias sao no maximo 8
disparos (dias 1, 5, 9, ..., 29), e sobram 20 creditos para absorver retries de
erro 5xx.

`python -m src.main status` mostra a qualquer momento quantas checagens completas
ainda cabem no mes.
