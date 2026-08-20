# Discord Flight Bot

Monitor de precos de voos **BEL -> NAT** (ida 27/12/2026, volta 05/01/2027),
com notificacao por webhook do Discord a cada checagem.

A busca e feita no **KAYAK** via [GeckoAPI](https://geckoapi.com.br)
(`POST /v1/extract`, target `kayak.com.br:plp`), **sem filtrar companhia**: a
metabusca cobre todas as companhias e agencias, e a mensagem informa de qual
companhia e a viagem mais barata encontrada e quem a vende.

Cada request custa **5 creditos** e o plano free da 100/mes, entao o bot roda
**a cada 2 dias, entre 05:00 e 09:00** de Belem, e tem um contador que impede o
estouro mesmo se o scheduler disparar fora de hora.

Na **vespera do reset de creditos** ele abre uma segunda janela, das 20:00 as
23:00, e gasta o saldo que sobrou antes de virar po.

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

Historico de precos e consumo pelo ledger local. **Nao toca na rede.**

```bash
python -m src.main credits
```

Consulta `GET /v1/me/credits` na GeckoAPI: saldo real, consumo em 24h/7d/30d, e
reconcilia o ledger local com esse numero. Endpoint de conta, nao despacha
extracao.

```bash
python -m src.main dry-run
```

Envia um embed de exemplo ao Discord para validar o webhook. **Nao gasta credito.**

```bash
python -m src.main once
```

Executa uma checagem. **Gasta 5 creditos.**

```bash
python -m src.main schedule
```

Deixa o processo rodando e checa a cada 2 dias via APScheduler. Roda uma vez
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

Aba **Actions > Checagem de precos > Run workflow**. Gasta 5 creditos e deve
resultar em um embed no Discord. A partir dai o cron assume: dias impares, as
9h de Belem.

### Janelas de execucao

| Janela | Quando | Cron (UTC) | O que faz |
| --- | --- | --- | --- |
| Manha | dias impares, 05:00-09:00 | `0 9 */2 * *` | checagem regular |
| Noite | so na vespera do reset, 20:00-23:00 | `0 23,0,1,2 * * *` | gasta o saldo restante |

O cron da noite dispara **todo dia**, mas o codigo so executa se for a vespera
do reset, se o horario local estiver na janela e se houver saldo. Nos outros
dias ele sai calado com codigo 0 - nao gasta credito, nao manda mensagem e nao
deixa o Actions vermelho.

A decisao final e tomada em Python, nao no cron: o GitHub dispara em UTC e pode
atrasar, entao o horario e reconvertido para Belem antes de decidir. Por isso o
cron da manha e as 09:00 UTC (06:00 em Belem), com 3h de folga ate o fim da
janela.

Rajada da noite: sao 4 disparos (23:00, 00:00, 01:00 e 02:00 UTC, ou seja
20:00 as 23:00 em Belem), e cada um so roda se ainda houver 5 creditos. No
maximo 20 creditos sao gastos assim. Se sobrar mais que isso, dispare
`Run workflow` com `mode: manual`.

### Quando os creditos resetam

**A GeckoAPI nao informa isso.** `GET /v1/me/credits` devolve saldo, plano e
consumo em 24h/7d/30d, e nada sobre renovacao - a doc tambem nao diz se a cota
e por mes-calendario ou por data de assinatura.

Entao assumimos `CREDIT_RESET_DAY=1` (configuravel) e, para nao ficar no
palpite, cada saldo observado e gravado em `credit_balance_history`. Um salto
para cima e um reset, e `python -m src.main status` mostra em que dia ele
aconteceu de verdade:

```
Reset de creditos: dia 1 (suposicao)
  Saldo subiu, na pratica, no(s) dia(s): [14]
  ATENCAO: ajuste CREDIT_RESET_DAY para 14
```

Ate observar o primeiro reset, a suposicao vale. Se ela estiver errada, o custo
e s perder a rajada da noite naquele mes - a checagem da manha nao e afetada.

### Detalhes do workflow

- **`concurrency`** impede duas execucoes escrevendo no banco ao mesmo tempo, e
  nao cancela a que ja esta rodando (ela pode ja ter gasto credito).
- **Commit de keepalive**: o GitHub desativa workflows agendados apos 60 dias
  sem atividade no repositorio. Com o banco remoto, nada mais commitaria. Por
  isso cada execucao grava `data/last-run.md` e commita — mantem o agendamento
  vivo e ainda deixa um rastro legivel. Roda com `if: always()`, para manter o
  agendamento vivo mesmo quando a checagem falha.
- **Cron do GitHub nao e pontual**: sob carga o disparo atrasa alguns minutos.
  Irrelevante para monitorar passagem a cada 2 dias.
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

**Windows (Agendador de Tarefas)** — a cada 2 dias, as 9h:

```powershell
$proj = "C:\caminho\para\discord-flight-bot"; $a = New-ScheduledTaskAction -Execute "$proj\.venv\Scripts\python.exe" -Argument "-m src.main once" -WorkingDirectory "$proj"; $t = New-ScheduledTaskTrigger -Daily -DaysInterval 2 -At 9am; $s = New-ScheduledTaskSettingsSet -StartWhenAvailable -WakeToRun; Register-ScheduledTask -TaskName "MonitorVoos" -Action $a -Trigger $t -Settings $s
```

`-WorkingDirectory` importa: `python -m src.main` so funciona a partir da raiz
do projeto. `-StartWhenAvailable` recupera disparo perdido com o PC desligado e
`-WakeToRun` acorda a maquina suspensa — o contador de creditos protege contra
o acumulo de disparos atrasados.

**Linux/macOS (cron)** — dias impares as 9h:

```bash
0 9 */2 * * cd /caminho/discord-flight-bot && .venv/bin/python -m src.main once >> data/cron.log 2>&1
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
  |- credits_tracker.ensure_budget(5)       # aborta se nao couber no mes
  |- fetch_prices.fetch_kayak               # 1 request, todas as companhias
  |- compare.compare_with_history           # MIN(price) global, ANTES de salvar
  |- storage.save_offer                     # inclui o JSON bruto
  `- discord_bot.notify_offer               # 1 embed: a viagem mais barata
```

Decisoes que valem registro:

- **A comparacao acontece antes do insert.** Se salvasse primeiro, o preco
  atual entraria no proprio `MIN()` e toda checagem "empataria" com o minimo.
- **Nao existe coluna `menor_preco_historico`.** E sempre derivada via
  `SELECT MIN(price) ... WHERE airline = ?`, para nao ter estado duplicado.
- **Creditos sao debitados por tentativa HTTP, nao por sucesso.** Um retry
  apos HTTP 500 debita os 5 creditos de novo, porque a API provavelmente
  cobrou. Erro de conexao (request nao chegou a sair) nao debita.
- **O saldo da GeckoAPI e a verdade; o ledger local e a trilha.** Antes de cada
  checagem, `GET /v1/me/credits` informa o saldo real e a diferenca entra como
  lancamento de ajuste. O ledger conta por tentativa e erra para cima (retry,
  estorno perdido, execucao morta no meio); sem reconciliar, ele acabaria
  recusando checagens que ainda cabiam. Se o endpoint nao responder, seguimos
  com o ledger local, que erra para o lado seguro.
- **O ledger de creditos e chaveado por `YYYY-MM`.** O "reset todo dia 1"
  acontece sozinho na virada do mes, sem rotina de limpeza.
- **Metabusca em vez de consultar cada companhia.** Uma request ao KAYAK cobre
  todas as companhias e custa metade de LATAM+Azul separados. E o schema dele
  entrega estruturado o que antes eu adivinhava: `legs[]` ja separa ida e volta,
  `durationMinutes` e numero, `price.amount` e o total da viagem.
- **A busca nao filtra companhia.** Filtrar esconderia a oferta mais barata; a
  companhia vem na resposta (`segments[].airlineName`) e vai para o embed.
- **Ofertas de agencia entram, marcadas.** `bookingOptions[].isDirect` distingue
  venda direta de agencia (123Milhas, Decolar), e o embed diz qual e qual.
- **O minimo historico e global.** Como a companhia varia a cada checagem,
  comparar so contra o historico da companhia sorteada esconderia o preco real.
- **`seatsRemaining` no lugar da validade da tarifa.** Nenhum target da GeckoAPI
  expoe validade; assentos restantes e o dado de urgencia mais proximo.
- **Duracao tem fallback pelos horarios.** Quando `durationMinutes` vem null, a
  duracao e calculada subtraindo saida de chegada.
- **HTTP 5xx gera estorno no ledger.** A GeckoAPI devolve o credito de
  extracoes que ela nao concluiu, e sem lancar o estorno o contador subiria
  sozinho e barraria checagens que ainda cabiam.
- **Nao entregar a mensagem e falha, e o job fica vermelho.** O preco continua
  salvo no banco, mas num bot cujo unico produto e a notificacao, silencio
  indistinguivel de sucesso e o pior desfecho. Pular por estar fora da janela,
  ao contrario, sai com codigo 0: e o comportamento desejado.
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

---

## Licenca

[MIT](LICENSE). Use, modifique e distribua livremente, mantendo o aviso de copyright.
