# Datadog Observability Maturity Scanner

Varre todos os repositórios de uma organização GitHub e avalia o nível de instrumentação com Datadog - **sem clonar nenhum repositório localmente**.

Desenvolvido para responder a uma pergunta simples: *quais dos nossos serviços já estão instrumentados com Datadog, e como está a qualidade da instrumentação?*

---

## Como funciona

O script opera em duas fases para ser eficiente com 1000+ repositórios:

**Fase 1 — Code Search em lote**
Faz 16 buscas na API do GitHub (`/search/code`) cobrindo todos os repositórios da org de uma vez. Identifica quais repos têm sinais Datadog sem precisar abrir nenhum arquivo individualmente. Repos sem nenhum sinal já são marcados como não instrumentados aqui, sem gastar mais requisições.

**Fase 2 — Contents API nos repos relevantes**
Somente para os repos que apareceram na fase 1, vai buscar os arquivos individualmente para confirmar e detalhar cada sinal. Roda com threads paralelas para ser mais rápido.

No total, a varredura de 500–1500 repositórios usa cerca de 300–500 requisições — bem dentro do limite de 5.000/hora da API do GitHub.

---

## Pré-requisitos

Python 3.10 ou superior.

É recomendado usar um ambiente virtual (`venv`) para manter as dependências isoladas e não poluir o Python do sistema:

```bash
# Cria o ambiente virtual
python3 -m venv .venv

# Ativa o ambiente
source .venv/bin/activate        # Linux / macOS
.venv\Scripts\activate           # Windows

# Instala as dependências
pip install -r requirements.txt
```

Para desativar o ambiente virtual quando terminar:

```bash
deactivate
```

> **Dica:** adicione `.venv/` ao seu `.gitignore` para não subir o ambiente virtual ao repositório.

---

## Configurando o token do GitHub

O script precisa de um **Personal Access Token** para acessar repositórios privados da organização. Isso é diferente de uma chave SSH — a chave SSH serve para operações Git, o token é para a API REST.

**Passo a passo:**

1. Acesse **github.com → clique no seu avatar → Settings**
2. No menu lateral, vá até o final: **Developer settings**
3. **Personal access tokens → Tokens (classic)**
4. Clique em **Generate new token (classic)**
5. Dê um nome (ex: `dd-observability-scan`) e defina uma expiração (30 dias está bom)
6. Marque o escopo **`repo`** — necessário para acessar repositórios privados
7. Clique em **Generate token** e copie o valor (`ghp_...`) — ele aparece só uma vez

> **Importante:** se a organização usa SSO, após criar o token clique em **Configure SSO** ao lado dele e autorize a organização. Sem isso o token não enxerga os repositórios privados.

---

## Como usar

O script tem dois modos de uso: **interativo** e **por linha de comando**.

---

### Modo interativo

Execute sem nenhum argumento e o script vai guiando com perguntas:

```bash
python dd_github_scan.py
```

Exemplo de sessão:

```
╔══════════════════════════════════════════════════╗
║   Datadog Observability Maturity Scanner         ║
╚══════════════════════════════════════════════════╝

  Nome da organização no GitHub (ex: MinhaOrg): MinhaOrg
  Cole seu GitHub Personal Access Token (ghp_...): ghp_xxxxxxxxxxxx

  Opções (pressione Enter para aceitar o padrão):
  Ignorar repositórios arquivados? [S/n]:
  Ignorar repositórios com linguagem desconhecida? [S/n]:
  Filtrar por linguagem? [ruby/nodejs/go/dotnet] ou Enter para todas:
  Exportar CSV? [S/n]:
  Mostrar sinais detalhados no terminal? [s/N]:
```

Basta responder as perguntas — todas têm um padrão sugerido entre colchetes. Pressionar Enter aceita o padrão.

Se a variável `GITHUB_TOKEN` já estiver exportada no ambiente, o token é detectado automaticamente e você pode apenas pressionar Enter para usá-lo.

---

### Modo linha de comando

Para uso em scripts, automações ou CI, passe os argumentos diretamente:

```bash
# Varredura completa com exportação CSV e JSON
python dd_github_scan.py --org MinhaOrg --csv --json

# Ignorar repositórios arquivados e linguagem desconhecida (recomendado)
python dd_github_scan.py --org MinhaOrg --csv --json --skip-archived --skip-unknown

# Ignorar só repositórios arquivados
python dd_github_scan.py --org MinhaOrg --csv --json --skip-archived

# Ver detalhes de cada repositório no terminal
python dd_github_scan.py --org MinhaOrg --detail

# Filtrar só repositórios Ruby
python dd_github_scan.py --org MinhaOrg --lang ruby --csv

# Modo rápido (só Code Search, sem inspecionar arquivos)
python dd_github_scan.py --org MinhaOrg --search-only

# Mostrar só os 20 com maior score
python dd_github_scan.py --org MinhaOrg --top 20

# Retomar uma varredura interrompida
python dd_github_scan.py --org MinhaOrg --resume dd_maturity_MinhaOrg_20260529_182139.json

# Passar o token diretamente (sem variável de ambiente)
python dd_github_scan.py --org MinhaOrg --token ghp_xxxx --csv
```

**Todas as flags disponíveis:**

| Flag | Padrão | Descrição |
|---|---|---|
| `--org` | — | Nome da organização no GitHub. Se omitido, entra no modo interativo |
| `--token` | `$GITHUB_TOKEN` | Token de acesso. Se omitido, lê da variável de ambiente |
| `--skip-archived` | desativado | Ignora repositórios arquivados do relatório |
| `--skip-unknown` | desativado | Ignora repositórios cuja linguagem o GitHub não consegue detectar (infra, docs, configs) |
| `--lang` | todas | Filtra por linguagem: `ruby`, `nodejs`, `go`, `dotnet` |
| `--csv` | desativado | Exporta relatório em CSV |
| `--json` | desativado | Exporta relatório em JSON (sempre salvo para permitir `--resume`) |
| `--detail` | desativado | Mostra sinais detalhados por repo no terminal |
| `--search-only` | desativado | Usa só Code Search, sem inspecionar arquivos individualmente |
| `--top N` | 0 (todos) | Exibe só os N repositórios com maior score na tabela |
| `--resume arquivo.json` | — | Retoma varredura interrompida a partir de um JSON anterior |
| `--workers N` | 5 | Número de threads paralelas para a Contents API |

---

## O que é verificado em cada linguagem

### Ruby / Rails

| Arquivo | O que verifica |
|---|---|
| `Gemfile` | Presença da gem `ddtrace` ou `datadog` e versão |
| `config/initializers/*.rb` | Qualquer initializer com `require 'ddtrace'`, `require 'ddtrace/contrib/...'`, `Datadog.configure` |
| `config/application.rb` | Mesmos padrões acima |
| `config/initializers/datadog.rb` | `c.tracing.log_injection = true` |
| `config/initializers/lograge.rb` | `config.lograge.enabled = true` e `Lograge::Formatters::Json.new` |

> O script não busca apenas em `datadog.rb` — ele lista **todos os arquivos dentro de `config/initializers/`** e também o `config/application.rb`, identificando em qual arquivo cada configuração foi encontrada.

### Node.js

| Arquivo | O que verifica |
|---|---|
| `package.json` | Dependência `dd-trace`, `datadog-lambda-js` ou `@datadog/browser-rum` e versão |

### Go

| Arquivo | O que verifica |
|---|---|
| `go.mod` | Dependência `DataDog/dd-trace-go` e versão |

### .NET

| Arquivo | O que verifica |
|---|---|
| `*.csproj` | Pacote NuGet `Datadog.Trace` e versão |

### Todos (independente de linguagem)

| Arquivo | O que verifica |
|---|---|
| `.env`, `docker-compose.yml` | Variáveis `DD_SERVICE`, `DD_ENV`, `DD_VERSION`, `DD_AGENT_HOST` |
| `*.yaml`, `*.yml` | Config do agente Datadog em manifests K8s/Helm |

---

## Níveis de maturidade

O score vai de 0 a 100 pontos, somando os sinais encontrados:

| Sinal | Pontos |
|---|---|
| Initializer com ddtrace configurado (Rails) | 30 |
| Init do tracer no código | 25 |
| Dependência DD no manifesto de pacotes | 20 |
| Unified tagging (`DD_SERVICE` + `DD_ENV`) | 15 |
| `log_injection = true` | 15 |
| Docker/K8s com config Datadog | 10 |
| `lograge.enabled = true` | 10 |
| Custom spans instrumentados manualmente | 10 |
| Json formatter no lograge | 5 |
| `DD_AGENT_HOST` configurado | 5 |

O score final classifica o repositório em um dos cinco níveis:

| Score | Nível |
|---|---|
| 0% | ❌ Não instrumentado |
| 20–49% | 🟡 Instalação básica |
| 50–74% | 🟠 Parcialmente instrumentado |
| 75–89% | 🟢 Bem instrumentado |
| 90–100% | ⭐ Maturidade avançada |

---

## Saída no terminal

Ao final da varredura o script exibe uma tabela ordenada por score e um resumo por linguagem:

```
══════════════════════════════════════════════════════════
  DATADOG OBSERVABILITY MATURITY REPORT
══════════════════════════════════════════════════════════
  Repositório                          Lang    Score  Maturidade
  ──────────────────────────────────────────────────────────────
  pagamentos-service                   ruby      85%  🟢 Bem instrumentado
  email-worker                         ruby      65%  🟠 Parcialmente instrumentado
  legacy-importer                      ruby       0%  ❌ Não instrumentado
  ──────────────────────────────────────────────────────────────

  Repos analisados            : 300
  Com alguma instrumentação   : 45/300 (15%)
  Bem instrumentados (≥75%)   : 20/300 (6%)
  Score médio                 : 9%

  Por linguagem:
    ruby        120 repos  avg  23%  instrumentados: 32
    nodejs       90 repos  avg   3%  instrumentados: 10
    go           60 repos  avg  15%  instrumentados: 8
    dotnet       30 repos  avg  10%  instrumentados: 5
```

Com `--detail`, cada repositório exibe os sinais encontrados e em qual arquivo:

```
── pagamentos-service (ruby)  🟢 Bem instrumentado
    ✓ ddtrace  [config/initializers/datadog.rb]  |  ✓ log_injection=true [config/initializers/datadog.rb]
    ✓ lograge  ✓ lograge.enabled=true [config/initializers/lograge.rb]  |  ✓ Json formatter [config/initializers/lograge.rb]
    ✓ gem_ddtrace [1.23.0]
```

---

## Arquivos gerados

### JSON
Contém todos os detalhes de cada repositório, incluindo os sinais encontrados, versões das dependências e arquivos onde cada configuração foi localizada. Gerado automaticamente em toda execução — serve também como checkpoint para `--resume`.

### CSV
Uma linha por repositório, pronto para abrir no Excel ou Google Sheets. Colunas:

`repositorio` · `full_name` · `linguagem` · `score` · `maturidade` · `dd_initializer` · `log_injection` · `lograge_initializer` · `lograge_enabled` · `lograge_json_formatter` · `archived` · `url`

---

## Sobre rate limits

O script gerencia os limites da API do GitHub automaticamente:

| API | Limite | Como o script lida |
|---|---|---|
| REST (listagem, contents) | 5.000 req/hora | ~300–400 req por varredura completa — 6–8% do limite |
| Code Search | 30 req/minuto | `sleep(2.2s)` entre queries; detecta o header `X-RateLimit-Reset` e aguarda se necessário |

Em caso de rate limit o script pausa automaticamente pelo tempo exato necessário e retoma sem intervenção.
