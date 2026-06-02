#!/usr/bin/env python3
"""
Datadog Observability Maturity Scanner — GitHub API (sem clone)
Usa Code Search + Contents API do GitHub para varrer N repos
sem precisar clonar nada localmente.

Pré-requisito:
    pip install requests

Uso:
    export GITHUB_TOKEN=ghp_...
    python dd_github_scan.py --org MinhaOrg
    python dd_github_scan.py --org MinhaOrg --csv --json
    python dd_github_scan.py --org MinhaOrg --workers 8
    python dd_github_scan.py --org MinhaOrg --resume   # retoma varredura interrompida

Como gerar o token:
    github.com → Settings → Developer settings → Personal access tokens → Fine-grained
    Permissões necessárias: Contents (read), Metadata (read)
"""

import os
import re
import json
import time
import csv
import sys
import argparse
import threading
from pathlib import Path
from datetime import datetime
from dataclasses import dataclass, field, asdict
from typing import Optional
from concurrent.futures import ThreadPoolExecutor, as_completed

try:
    import requests
except ImportError:
    print("Instale o requests: pip install requests")
    sys.exit(1)


# ──────────────────────────────────────────────
# Cliente GitHub com retry + rate limit
# ──────────────────────────────────────────────

class GitHubClient:
    BASE = "https://api.github.com"

    def __init__(self, token: str):
        self.session = requests.Session()
        self.session.headers.update({
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        })
        self._lock = threading.Lock()

    def _request(self, method: str, url: str, **kwargs) -> requests.Response:
        last_exc = None
        for attempt in range(5):
            try:
                r = self.session.request(method, url, timeout=30, **kwargs)
            except (requests.exceptions.ConnectionError,
                    requests.exceptions.ChunkedEncodingError,
                    requests.exceptions.Timeout) as e:
                last_exc = e
                wait = 5 * (attempt + 1)
                print(f"\n  ⚠️  Erro de conexão ({type(e).__name__}) — tentativa {attempt+1}/5, aguardando {wait}s...", flush=True)
                time.sleep(wait)
                continue

            # Rate limit primário
            if r.status_code == 403 and "rate limit" in r.text.lower():
                reset_ts = int(r.headers.get("X-RateLimit-Reset", time.time() + 60))
                wait = max(reset_ts - time.time() + 2, 5)
                print(f"\n  ⏳ Rate limit atingido — aguardando {wait:.0f}s...", flush=True)
                time.sleep(wait)
                continue

            # Secondary rate limit (abuso)
            if r.status_code == 429:
                retry_after = int(r.headers.get("Retry-After", 30))
                print(f"\n  ⏳ Secondary rate limit — aguardando {retry_after}s...", flush=True)
                time.sleep(retry_after)
                continue

            # Erros transientes
            if r.status_code in (500, 502, 503, 504):
                time.sleep(2 ** attempt)
                continue

            return r

        # esgotou tentativas — retorna None-like via response vazia ou relança
        if last_exc:
            raise last_exc
        return r

    def get(self, path: str, **kwargs):
        url = path if path.startswith("http") else f"{self.BASE}{path}"
        r = self._request("GET", url, **kwargs)
        if r.status_code == 200:
            return r.json()
        return None

    def paginate(self, path: str, params: dict = None, max_pages: int = 999):
        """Itera todas as páginas de um endpoint paginado."""
        params = {**(params or {}), "per_page": 100, "page": 1}
        for _ in range(max_pages):
            data = self.get(path, params=params)
            if not data:
                break
            yield from (data if isinstance(data, list) else data.get("items", []))
            if len(data if isinstance(data, list) else data.get("items", [])) < 100:
                break
            params["page"] += 1

    def search_code(self, query: str, max_results: int = 1000) -> set[str]:
        """
        Usa a Code Search API para encontrar repos que contêm um padrão.
        Retorna set de nomes de repositórios ('org/repo').
        Respeita o rate limit de 30 req/min da Search API.
        Trata RemoteDisconnected e outros erros de conexão com retry automático.
        """
        import requests as _requests

        repos  = set()
        params = {"q": query, "per_page": 100, "page": 1}
        consecutive_errors = 0
        MAX_ERRORS = 5

        while len(repos) < max_results:
            r = None
            for attempt in range(4):
                try:
                    r = self.session.get(
                        f"{self.BASE}/search/code",
                        params=params,
                        timeout=30,
                    )
                    break
                except (_requests.exceptions.ConnectionError,
                        _requests.exceptions.ChunkedEncodingError,
                        _requests.exceptions.Timeout) as e:
                    wait = 10 * (attempt + 1)
                    print(f"\n  ⚠️  Erro de conexão ({type(e).__name__}) — tentativa {attempt+1}/4, aguardando {wait}s...", flush=True)
                    time.sleep(wait)

            if r is None:
                consecutive_errors += 1
                if consecutive_errors >= MAX_ERRORS:
                    print(f"\n  ❌ Muitos erros consecutivos — encerrando Code Search.", flush=True)
                    break
                continue

            consecutive_errors = 0

            if r.status_code == 403 and "rate limit" in r.text.lower():
                reset_ts = int(r.headers.get("X-RateLimit-Reset", time.time() + 62))
                wait = max(reset_ts - time.time() + 2, 30)
                print(f"\n  ⏳ Search rate limit — aguardando {wait:.0f}s...", flush=True)
                time.sleep(wait)
                continue

            if r.status_code == 429:
                retry_after = int(r.headers.get("Retry-After", 60))
                print(f"\n  ⏳ Secondary rate limit — aguardando {retry_after}s...", flush=True)
                time.sleep(retry_after)
                continue

            if r.status_code == 422:
                break

            if r.status_code != 200:
                time.sleep(5)
                continue

            data  = r.json()
            items = data.get("items", [])
            for item in items:
                repos.add(item["repository"]["full_name"])

            if len(items) < 100:
                break

            params["page"] += 1
            time.sleep(2.1)

        return repos

    def file_exists(self, repo: str, path: str) -> bool:
        r = self.session.get(
            f"{self.BASE}/repos/{repo}/contents/{path}",
            timeout=15,
        )
        return r.status_code == 200

    def get_file(self, repo: str, path: str) -> Optional[str]:
        """Retorna conteúdo decodificado de um arquivo (máx ~1MB)."""
        import base64
        data = self.get(f"/repos/{repo}/contents/{path}")
        if not data or data.get("encoding") != "base64":
            return None
        try:
            return base64.b64decode(data["content"]).decode("utf-8", errors="ignore")
        except Exception:
            return None


# ──────────────────────────────────────────────
# Varredura por linguagem via Contents API
# ──────────────────────────────────────────────

def detect_language_api(client: GitHubClient, repo: str, api_lang: str) -> str:
    """Usa a linguagem reportada pela API do GitHub como base."""
    mapping = {
        "Ruby": "ruby",
        "JavaScript": "nodejs",
        "TypeScript": "nodejs",
        "Go": "go",
        "C#": "dotnet",
        "F#": "dotnet",
        "Visual Basic .NET": "dotnet",
    }
    return mapping.get(api_lang or "", "unknown")


# Padrões ddtrace buscados em qualquer arquivo Ruby
DDTRACE_PATTERNS = [
    re.compile(r"""require\s+['"]ddtrace['"]""",                 re.IGNORECASE),
    re.compile(r"""require\s+['"]datadog['"]""",                 re.IGNORECASE),
    re.compile(r"""require\s+['"]ddtrace/contrib/""",            re.IGNORECASE),
    re.compile(r"Datadog\.configure",                             re.IGNORECASE),
    re.compile(r"DDTrace",                                        re.IGNORECASE),
]
LOG_INJECTION_PATTERN   = re.compile(r"c\.tracing\.log_injection\s*=\s*true")
LOGRAGE_ENABLED_PATTERN = re.compile(r"config\.lograge\.enabled\s*=\s*true")
LOGRAGE_JSON_PATTERN    = re.compile(r"config\.lograge\.formatter\s*=\s*Lograge::Formatters::Json\.new")


def _list_config_initializers(client: GitHubClient, repo: str) -> list[str]:
    """Retorna lista de paths em config/initializers/ via tree API."""
    tree = client.get(f"/repos/{repo}/git/trees/HEAD?recursive=1")
    if not tree:
        return []
    return [
        item["path"] for item in tree.get("tree", [])
        if item["path"].startswith("config/initializers/") and item["path"].endswith(".rb")
    ]


def _list_nodejs_source_files(client: GitHubClient, repo: str) -> list[str]:
    """
    Lista arquivos .js e .ts do repo via tree API aplicando filtros:
      - Exclui pastas irrelevantes (node_modules, dist, build, etc.)
      - Limita a 3 níveis de profundidade
      - Exclui arquivos de teste
      - Prioriza arquivos menores (entrypoints e configs tendem a ser pequenos)
    """
    EXCLUDE_DIRS = {
        "node_modules", "dist", "build", ".next", ".nuxt", "coverage",
        "__tests__", "test", "tests", "spec", "specs", ".cache", "vendor",
        "public", "static", "assets", "migrations", "fixtures", "mocks",
        "storybook", ".storybook", "e2e", "cypress",
    }
    EXCLUDE_SUFFIXES = (
        ".test.js", ".test.ts", ".spec.js", ".spec.ts",
        ".d.ts", ".min.js", ".bundle.js",
    )

    tree = client.get(f"/repos/{repo}/git/trees/HEAD?recursive=1")
    if not tree:
        return []

    candidates = []
    for item in tree.get("tree", []):
        path = item["path"]

        # limita profundidade a 3 níveis (ex: src/tracing/dd-trace.js = 3)
        if path.count("/") > 3:
            continue

        # só .js e .ts
        if not (path.endswith(".js") or path.endswith(".ts")):
            continue

        # exclui arquivos de teste e declarações
        if any(path.endswith(s) for s in EXCLUDE_SUFFIXES):
            continue

        # exclui pastas irrelevantes (verifica cada parte do path)
        parts = path.split("/")
        if any(part in EXCLUDE_DIRS for part in parts[:-1]):
            continue

        candidates.append(path)

    # Ordena por prioridade:
    # 1. arquivos com "trac", "datadog", "dd-trace", "monitor", "instrument", "apm" no nome
    # 2. entrypoints conhecidos (main, server, app, index) em src/bin ou raiz
    # 3. demais arquivos por profundidade (mais rasos primeiro)
    def priority(path: str) -> tuple:
        name = path.split("/")[-1].lower()
        depth = path.count("/")
        is_dd_file    = any(k in name for k in ["trac", "datadog", "dd-trace", "dd_trace", "monitor", "instrument", "apm", "observ"])
        is_entrypoint = any(k in name for k in ["main", "server", "app", "index", "bootstrap"])
        return (not is_dd_file, not is_entrypoint, depth)

    return sorted(candidates, key=priority)


def check_ruby_api(client: GitHubClient, repo: str) -> dict:
    signals = {}

    # ── Gemfile ──
    gemfile = client.get_file(repo, "Gemfile")
    if gemfile:
        has = bool(re.search(r"gem ['\"]ddtrace['\"]|gem ['\"]datadog['\"]", gemfile))
        ver = None
        m = re.search(r"gem ['\"]ddtrace['\"].*?([0-9]+\.[0-9.]+)", gemfile)
        if m:
            ver = m.group(1)
        signals["gem_ddtrace"] = {"found": has, "version": ver}
    else:
        signals["gem_ddtrace"] = {"found": False}

    # ── Varre config/initializers/ + config/application.rb ──
    signals["rails_dd_initializer"] = check_rails_ddtrace_files(client, repo)

    # ── config/initializers/lograge.rb (ou qualquer initializer com lograge) ──
    signals["rails_lograge"] = check_rails_lograge_files(client, repo)

    return signals


def check_rails_ddtrace_files(client: GitHubClient, repo: str) -> dict:
    """
    Varre config/initializers/*.rb e config/application.rb buscando
    configurações ddtrace. Registra em qual arquivo cada sinal foi encontrado.
    """
    # Arquivos candidatos: todos os initializers + application.rb
    initializers = _list_config_initializers(client, repo)
    candidates   = initializers + ["config/application.rb"]

    found_in_file    = None   # arquivo onde achou ddtrace
    log_injection    = False
    log_injection_file = None

    for path in candidates:
        file_content = client.get_file(repo, path)
        if not file_content:
            continue

        has_ddtrace = any(p.search(file_content) for p in DDTRACE_PATTERNS)
        has_inj     = bool(LOG_INJECTION_PATTERN.search(file_content))

        if has_ddtrace and not found_in_file:
            found_in_file = path

        if has_inj and not log_injection_file:
            log_injection      = True
            log_injection_file = path

    if not found_in_file:
        return {
            "found":             False,
            "path":              None,
            "has_ddtrace":       False,
            "log_injection":     False,
            "log_injection_file": None,
        }

    return {
        "found":              True,
        "path":               found_in_file,       # arquivo onde achou ddtrace
        "has_ddtrace":        True,
        "log_injection":      log_injection,
        "log_injection_file": log_injection_file,  # pode ser um arquivo diferente
    }


def check_rails_lograge_files(client: GitHubClient, repo: str) -> dict:
    """
    Varre config/initializers/*.rb e config/application.rb buscando
    configurações lograge. Registra em qual arquivo encontrou.
    """
    initializers = _list_config_initializers(client, repo)
    candidates   = initializers + ["config/application.rb"]

    lograge_enabled         = False
    lograge_enabled_file    = None
    lograge_json_formatter  = False
    lograge_json_file       = None

    for path in candidates:
        file_content = client.get_file(repo, path)
        if not file_content:
            continue

        if LOGRAGE_ENABLED_PATTERN.search(file_content) and not lograge_enabled_file:
            lograge_enabled      = True
            lograge_enabled_file = path

        if LOGRAGE_JSON_PATTERN.search(file_content) and not lograge_json_file:
            lograge_json_formatter = True
            lograge_json_file      = path

    found = lograge_enabled or lograge_json_formatter

    return {
        "found":                  found,
        "path":                   lograge_enabled_file or lograge_json_file,
        "lograge_enabled":        lograge_enabled,
        "lograge_enabled_file":   lograge_enabled_file,
        "lograge_json_formatter": lograge_json_formatter,
        "lograge_json_file":      lograge_json_file,
    }


# mantido por compatibilidade com calculate_score (não é mais chamado diretamente)
def check_rails_datadog_initializer(client, repo):
    return check_rails_ddtrace_files(client, repo)

def check_rails_lograge_initializer(client, repo):
    return check_rails_lograge_files(client, repo)


# Padrões de inicialização do tracer Datadog em arquivos Node.js
NODEJS_LOG_INJECTION_PATTERNS = [
    re.compile(r"logInjection\s*:\s*true",        re.IGNORECASE),
    re.compile(r"log_injection\s*:\s*true",       re.IGNORECASE),
    re.compile(r"""['"]logInjection['"]\s*:\s*true""", re.IGNORECASE),
]

NODEJS_MONITORING_TOGGLE_PATTERNS = [
    re.compile(r"DD_MONITORING_ENABLED",          re.IGNORECASE),
    re.compile(r"DD_TRACE_ENABLED",               re.IGNORECASE),
    re.compile(r"DD_APM_ENABLED",                 re.IGNORECASE),
]

NODEJS_TRACER_PATTERNS = [
    re.compile(r"""require\s*\(\s*['"]dd-trace['"]\s*\)""",           re.IGNORECASE),
    re.compile(r"""require\s*\(\s*['"]dd-trace/init['"]\s*\)""",      re.IGNORECASE),
    re.compile(r"""from\s+['"]dd-trace['"]""",                        re.IGNORECASE),
    re.compile(r"""import\s+['"]dd-trace/init['"]""",                 re.IGNORECASE),
    re.compile(r"tracer\.init\s*\(",                                   re.IGNORECASE),
    re.compile(r"dd-trace.*\.init\s*\(",                               re.IGNORECASE),
]

# Arquivos candidatos a conter a inicialização do tracer (ordem de prioridade)
NODEJS_TRACER_CANDIDATES = [
    # arquivos dedicados ao tracer (padrão recomendado pela documentação oficial)
    "tracer.js", "tracer.ts",
    "datadog.js", "datadog.ts",
    "instrument.js", "instrument.ts",
    "observability.js", "observability.ts",
    "apm.js", "apm.ts",
    "dd-tracer.js", "dd-tracer.ts",
    "dd-trace.js", "dd-trace.ts",
    # entrypoints comuns da aplicação
    "server.js", "server.ts",
    "app.js", "app.ts",
    "index.js", "index.ts",
    "main.js", "main.ts",
    # subpastas comuns
    "src/tracer.js", "src/tracer.ts",
    "src/datadog.js", "src/datadog.ts",
    "src/instrument.js", "src/instrument.ts",
    "src/observability.js", "src/observability.ts",
    "src/apm.js", "src/apm.ts",
    "src/server.js", "src/server.ts",
    "src/app.js", "src/app.ts",
    "src/index.js", "src/index.ts",
    "src/main.js", "src/main.ts",
    "src/lib/tracer.js", "src/lib/tracer.ts",
    "src/config/datadog.js", "src/config/datadog.ts",
    "src/config/tracer.js", "src/config/tracer.ts",
    "lib/tracer.js", "lib/tracer.ts",
    "config/datadog.js", "config/datadog.ts",
    "config/tracer.js", "config/tracer.ts",
    # pasta src/tracing/ (padrão encontrado em projetos NestJS/Express)
    "src/tracing/tracer.js", "src/tracing/tracer.ts",
    "src/tracing/datadog.js", "src/tracing/datadog.ts",
    "src/tracing/dd-trace.js", "src/tracing/dd-trace.ts",
    "src/tracing/dd-tracing.js", "src/tracing/dd-tracing.ts",
    "src/tracing/instrument.js", "src/tracing/instrument.ts",
    "src/tracing/monitoring.js", "src/tracing/monitoring.ts",
    "src/tracing/observability.js", "src/tracing/observability.ts",
    "tracing/tracer.js", "tracing/tracer.ts",
    "tracing/datadog.js", "tracing/datadog.ts",
    "tracing/dd-trace.js", "tracing/dd-trace.ts",
    "tracing/dd-tracing.js", "tracing/dd-tracing.ts",
]


def check_nodejs_api(client: GitHubClient, repo: str) -> dict:
    signals = {}

    # ── package.json — dependência declarada ──
    pkg = client.get_file(repo, "package.json")
    if pkg:
        try:
            data = json.loads(pkg)
            deps = {**data.get("dependencies", {}), **data.get("devDependencies", {})}
            has = any(k in deps for k in ["dd-trace", "datadog-lambda-js", "@datadog/browser-rum"])
            ver = next((deps[k] for k in ["dd-trace", "datadog-lambda-js"] if k in deps), None)
            signals["package_dd_trace"] = {"found": has, "version": ver}

            # --require dd-trace/init via scripts.start
            start_script = data.get("scripts", {}).get("start", "")
            has_require_flag = bool(re.search(r"--require\s+dd-trace", start_script))
            signals["package_start_require"] = {
                "found": has_require_flag,
                "script": start_script if has_require_flag else None,
            }
        except Exception:
            signals["package_dd_trace"] = {"found": False}
            signals["package_start_require"] = {"found": False}
    else:
        signals["package_dd_trace"] = {"found": False}
        signals["package_start_require"] = {"found": False}

    # ── Busca inicialização do tracer nos arquivos candidatos ──
    signals["nodejs_tracer_init"] = check_nodejs_tracer_init(client, repo)

    return signals


def check_nodejs_tracer_init(client: GitHubClient, repo: str) -> dict:
    """
    Varre arquivos .js/.ts do repo em busca da inicialização do dd-trace.
    Estratégia em duas fases:
      Fase 1 — lista candidatos da tree API com filtros e ordenação por prioridade
      Fase 2 — lê cada arquivo e verifica padrões de init, logInjection e monitoring toggle
    Registra em qual arquivo encontrou cada sinal.
    """
    found_path         = None
    log_injection      = False
    log_injection_file = None
    monitoring_toggle  = False
    monitoring_file    = None
    files_checked      = 0
    MAX_FILES          = 60  # limite de segurança para não explodir requisições

    source_files = _list_nodejs_source_files(client, repo)

    for path in source_files:
        if files_checked >= MAX_FILES:
            break

        file_content = client.get_file(repo, path)
        files_checked += 1
        if not file_content:
            continue

        has_init = any(p.search(file_content) for p in NODEJS_TRACER_PATTERNS)
        has_inj  = any(p.search(file_content) for p in NODEJS_LOG_INJECTION_PATTERNS)
        has_tog  = any(p.search(file_content) for p in NODEJS_MONITORING_TOGGLE_PATTERNS)

        if has_init and not found_path:
            found_path = path
        if has_inj and not log_injection_file:
            log_injection      = True
            log_injection_file = path
        if has_tog and not monitoring_file:
            monitoring_toggle  = True
            monitoring_file    = path

        # se já encontrou todos os sinais, para de varrer
        if found_path and log_injection_file and monitoring_file:
            break

    return {
        "found":              bool(found_path),
        "path":               found_path,
        "log_injection":      log_injection,
        "log_injection_file": log_injection_file,
        "monitoring_toggle":  monitoring_toggle,
        "monitoring_file":    monitoring_file,
        "files_checked":      files_checked,
    }


def check_go_api(client: GitHubClient, repo: str) -> dict:
    signals = {}
    gomod = client.get_file(repo, "go.mod")
    if gomod:
        has = bool(re.search(r'DataDog/dd-trace-go', gomod, re.IGNORECASE))
        m = re.search(r'dd-trace-go[./v0-9]*\s+([v0-9.]+)', gomod)
        signals["go_mod_ddtrace"] = {"found": has, "version": m.group(1) if m else None}
    else:
        signals["go_mod_ddtrace"] = {"found": False}
    return signals


def check_dotnet_api(client: GitHubClient, repo: str) -> dict:
    signals = {}
    # Tenta achar .csproj na raiz (nome variável)
    tree = client.get(f"/repos/{repo}/git/trees/HEAD?recursive=0")
    csproj = None
    if tree:
        for item in tree.get("tree", []):
            if item["path"].endswith(".csproj"):
                csproj = item["path"]
                break
    if csproj:
        content = client.get_file(repo, csproj)
        if content:
            has = bool(re.search(r'Datadog\.Trace', content, re.IGNORECASE))
            m = re.search(r'Datadog\.Trace.*?([0-9]+\.[0-9.]+)', content)
            signals["nuget_dd_trace"] = {"found": has, "version": m.group(1) if m else None}
        else:
            signals["nuget_dd_trace"] = {"found": False}
    else:
        signals["nuget_dd_trace"] = {"found": False}
    return signals


def check_docker_k8s_api(client: GitHubClient, repo: str) -> dict:
    dd_pattern = re.compile(
        r'DD_AGENT_HOST|DD_SERVICE|DD_ENV|DD_VERSION|datadog[/-]agent|datadoghq\.com',
        re.IGNORECASE
    )
    for path in ["docker-compose.yml", "docker-compose.yaml", ".env", "Dockerfile",
                 "k8s/deployment.yaml", "deploy/deployment.yaml", "helm/values.yaml"]:
        content = client.get_file(repo, path)
        if content and dd_pattern.search(content):
            return {"found": True, "file": path}
    return {"found": False}


def check_unified_tagging_api(client: GitHubClient, repo: str) -> dict:
    content_all = ""
    for path in [".env", "docker-compose.yml", "docker-compose.yaml"]:
        c = client.get_file(repo, path)
        if c:
            content_all += c
    return {
        "found": bool(re.search(r'DD_SERVICE', content_all) and re.search(r'DD_ENV', content_all)),
        "DD_SERVICE": bool(re.search(r'DD_SERVICE', content_all)),
        "DD_ENV":     bool(re.search(r'DD_ENV', content_all)),
        "DD_VERSION": bool(re.search(r'DD_VERSION', content_all)),
    }


# ──────────────────────────────────────────────
# Code Search: mapa repo → sinais encontrados
# ──────────────────────────────────────────────

def build_search_map(client: GitHubClient, org: str) -> dict[str, dict]:
    """
    Usa a Code Search API para pré-popular sinais em lote.
    Muito mais eficiente que inspecionar arquivo por arquivo em 1300 repos.
    """
    print("\n  🔍 Fase 1/2: Code Search API (varredura em lote)...")
    signal_map: dict[str, dict] = {}

    queries = [
        # Ruby / Rails — Gemfile e initializer padrão Microservice::Toolkit
        (f"ddtrace org:{org} in:file filename:Gemfile",                          "gem_ddtrace"),
        (f"Datadog.configure org:{org} in:file extension:rb",                    "tracer_init_ruby"),
        (f"Datadog.configure org:{org} in:file filename:datadog.rb",             "rails_dd_initializer"),
        (f"c.tracing.log_injection org:{org} in:file filename:datadog.rb",       "rails_log_injection"),
        (f"lograge.enabled org:{org} in:file filename:lograge.rb",                  "rails_lograge"),
        (f"Lograge::Formatters::Json org:{org} in:file filename:lograge.rb",          "rails_lograge_json"),
        # Node
        (f"dd-trace org:{org} in:file filename:package.json",                    "package_dd_trace"),
        (f"require dd-trace org:{org} in:file",                                  "tracer_init_node"),
        (f"tracer.init org:{org} in:file extension:js",                          "tracer_init_node"),
        (f"tracer.init org:{org} in:file extension:ts",                          "tracer_init_node"),
        (f"dd-trace/init org:{org} in:file",                                     "nodejs_tracer_init"),
        # Go
        (f"dd-trace-go org:{org} in:file filename:go.mod",                      "go_mod_ddtrace"),
        # .NET
        (f"Datadog.Trace org:{org} in:file extension:csproj",                    "nuget_dd_trace"),
        (f"Tracer.Instance org:{org} in:file extension:cs",                      "tracer_init_dotnet"),
        # Infra
        (f"DD_SERVICE org:{org} in:file",                                        "env_dd_service"),
        (f"DD_AGENT_HOST org:{org} in:file",                                     "env_dd_agent"),
        (f"datadoghq.com org:{org} in:file",                                     "docker_k8s"),
        # APM avançado
        (f"StartSpan org:{org} in:file",                                         "custom_spans"),
        (f"dd.trace_id org:{org} in:file",                                       "log_injection"),
    ]

    total = len(queries)
    for i, (query, signal) in enumerate(queries, 1):
        print(f"  [{i:2}/{total}] {signal}...", end="\r", flush=True)
        found_repos = client.search_code(query)
        for repo_full in found_repos:
            if repo_full not in signal_map:
                signal_map[repo_full] = {}
            signal_map[repo_full][signal] = True
        # Search API: 30 req/min
        time.sleep(2.2)

    print(f"\n  ✓ Code Search concluída — {len(signal_map)} repos com sinais DD encontrados.\n")
    return signal_map


# ──────────────────────────────────────────────
# Maturidade
# ──────────────────────────────────────────────

MATURITY_LEVELS = [
    (0,  "🔴 Não instrumentado"),
    (20, "🟡 Instalação básica"),
    (50, "🟠 Parcialmente instrumentado"),
    (75, "🟢 Bem instrumentado"),
    (90, "🚀 Maturidade avançada"),
]

SIGNAL_WEIGHTS = {
    # Dependência declarada
    "gem_ddtrace":            20,
    "package_dd_trace":       20,
    "go_mod_ddtrace":         20,
    "nuget_dd_trace":         20,
    # Initializer Rails (config/initializers/datadog.rb) — sinal principal para apps RD
    "rails_dd_initializer":   30,  # arquivo presente com ddtrace configurado
    "rails_log_injection":    15,  # c.tracing.log_injection = true
    # Lograge
    "rails_lograge":          10,  # config/initializers/lograge.rb presente e enabled
    "rails_lograge_json":      5,  # formatter = Lograge::Formatters::Json.new
    # Init genérico do tracer
    "tracer_init":            25,
    "tracer_init_ruby":       25,
    "tracer_init_node":       25,
    "nodejs_tracer_init":     25,  # tracer init encontrado em arquivo dedicado ou entrypoint
    "nodejs_log_injection":   15,  # logInjection: true no tracer init
    "nodejs_monitoring_toggle": 5, # DD_MONITORING_ENABLED / DD_TRACE_ENABLED presente
    "package_start_require":   15,  # --require dd-trace/init no scripts.start
    "tracer_init_dotnet":     25,
    # Infra / env
    "env_vars":               15,
    "env_dd_service":         10,
    "env_dd_agent":            5,
    "docker_k8s":             10,
    "unified_tagging":        15,
    # APM avançado
    "custom_spans":           10,
    "log_injection":           5,
}

def calculate_score(signals: dict) -> tuple[int, str]:
    score = 0
    for key, w in SIGNAL_WEIGHTS.items():
        sig = signals.get(key)
        if sig is None:
            continue
        if sig is True:
            score += w
        elif isinstance(sig, dict):
            # rails_dd_initializer: só conta se o arquivo existe E tem ddtrace configurado
            if key == "rails_dd_initializer":
                if sig.get("found") and sig.get("has_ddtrace"):
                    score += w
            # nodejs_tracer_init: arquivo dedicado ou entrypoint com init encontrado
            elif key == "nodejs_tracer_init":
                if isinstance(sig, dict) and sig.get("found"):
                    score += w
            # nodejs_log_injection: logInjection: true no tracer
            elif key == "nodejs_log_injection":
                init = signals.get("nodejs_tracer_init", {})
                if isinstance(init, dict) and init.get("log_injection"):
                    score += w
            # nodejs_monitoring_toggle: DD_MONITORING_ENABLED presente
            elif key == "nodejs_monitoring_toggle":
                init = signals.get("nodejs_tracer_init", {})
                if isinstance(init, dict) and init.get("monitoring_toggle"):
                    score += w
            # package_start_require: --require dd-trace/init no scripts.start
            elif key == "package_start_require":
                if isinstance(sig, dict) and sig.get("found"):
                    score += w
            # rails_log_injection: conta se o initializer existe e log_injection está true
            elif key == "rails_log_injection":
                init = signals.get("rails_dd_initializer", {})
                if isinstance(init, dict) and init.get("found") and init.get("log_injection"):
                    score += w
            # rails_lograge: arquivo presente com enabled=true
            elif key == "rails_lograge":
                lg = signals.get("rails_lograge", {})
                if isinstance(lg, dict) and lg.get("found") and lg.get("lograge_enabled"):
                    score += w
            # rails_lograge_json: formatter Json configurado
            elif key == "rails_lograge_json":
                lg = signals.get("rails_lograge", {})
                if isinstance(lg, dict) and lg.get("found") and lg.get("lograge_json_formatter"):
                    score += w
            elif sig.get("found"):
                score += w
    score = min(score, 100)
    label = MATURITY_LEVELS[0][1]
    for threshold, lvl in MATURITY_LEVELS:
        if score >= threshold:
            label = lvl
    return score, label


# ──────────────────────────────────────────────
# Resultado
# ──────────────────────────────────────────────

@dataclass
class RepoResult:
    name: str
    full_name: str
    language: str
    archived: bool
    signals: dict
    score: int
    maturity: str
    url: str

    def to_dict(self):
        return asdict(self)


# ──────────────────────────────────────────────
# Scan principal
# ──────────────────────────────────────────────

def scan_repo(client: GitHubClient, repo_meta: dict, search_signals: dict) -> RepoResult:
    full_name = repo_meta["full_name"]
    api_lang  = repo_meta.get("language") or ""
    language  = detect_language_api(client, full_name, api_lang)

    # Começa com sinais do Code Search (rápido)
    signals = dict(search_signals)

    # Enriquece com Contents API só para repos que já têm algum sinal
    # ou para os que a linguagem é conhecida (evita requisições em repos irrelevantes)
    if signals or language != "unknown":
        if language == "ruby":
            signals.update(check_ruby_api(client, full_name))
        elif language == "nodejs":
            signals.update(check_nodejs_api(client, full_name))
        elif language == "go":
            signals.update(check_go_api(client, full_name))
        elif language == "dotnet":
            signals.update(check_dotnet_api(client, full_name))

        if signals:  # só busca infra se já há algum sinal
            signals["docker_k8s"]      = check_docker_k8s_api(client, full_name)
            signals["unified_tagging"] = check_unified_tagging_api(client, full_name)

    score, maturity = calculate_score(signals)
    return RepoResult(
        name=repo_meta["name"],
        full_name=full_name,
        language=language,
        archived=repo_meta.get("archived", False),
        signals=signals,
        score=score,
        maturity=maturity,
        url=repo_meta.get("html_url", ""),
    )


# ──────────────────────────────────────────────
# Output
# ──────────────────────────────────────────────

def print_table(results: list[RepoResult], top: int = 0):
    results = sorted(results, key=lambda r: r.score, reverse=True)
    if top:
        results = results[:top]

    if not results:
        print("\n  Nenhum repositório encontrado. Verifique o token e a autorização SSO da organização.")
        return

    name_w = min(max(len(r.name) for r in results), 45)
    lang_w = max(len(r.language) for r in results)
    mat_w  = max(len(r.maturity) for r in results)

    header = f"  {'Repositório':<{name_w}}  {'Lang':<{lang_w}}  {'Score':>5}  {'Maturidade':<{mat_w}}"
    sep    = "─" * len(header)
    print()
    print("=" * len(header))
    print("  DATADOG OBSERVABILITY MATURITY REPORT")
    print("=" * len(header))
    print(header)
    print(sep)
    for r in results:
        arch = " [archived]" if r.archived else ""
        print(
            f"  {(r.name + arch):<{name_w}}  "
            f"{r.language:<{lang_w}}  "
            f"{r.score:>5}%  "
            f"{r.maturity:<{mat_w}}"
        )
    print(sep)

    total       = len(results)
    any_inst    = sum(1 for r in results if r.score >= 20)
    well_inst   = sum(1 for r in results if r.score >= 75)
    avg         = sum(r.score for r in results) // total if total else 0

    print(f"\n  Repos analisados       : {total}")
    print(f"  Com alguma instrumentação : {any_inst}/{total} ({any_inst*100//total if total else 0}%)")
    print(f"  Bem instrumentados (≥75%) : {well_inst}/{total} ({well_inst*100//total if total else 0}%)")
    print(f"  Score médio               : {avg}%")

    # Breakdown por linguagem
    langs = {}
    for r in results:
        langs.setdefault(r.language, []).append(r.score)
    print("\n  Por linguagem:")
    for lang, scores in sorted(langs.items()):
        avg_l = sum(scores) // len(scores)
        inst  = sum(1 for s in scores if s >= 20)
        print(f"    {lang:<10} {len(scores):>4} repos  avg {avg_l:>3}%  instrumentados: {inst}")
    print()


def print_detail(results: list[RepoResult]):
    """Detalha sinais por repo, com destaque para o initializer Rails."""
    results = sorted(results, key=lambda r: r.score, reverse=True)
    for r in results:
        print(f"\n  ── {r.name} ({r.language})  {r.maturity}")

        init = r.signals.get("rails_dd_initializer")
        if isinstance(init, dict):
            if init.get("found"):
                dd_file = init.get("path", "?")
                inj     = init.get("log_injection", False)
                inj_file = init.get("log_injection_file")
                inj_str = f"✓ log_injection=true [{inj_file}]" if inj else "✗ log_injection ausente"
                print(f"    ✓ ddtrace  [{dd_file}]  |  {inj_str}")
            else:
                print(f"    ✗ ddtrace  → não encontrado em config/initializers/ nem config/application.rb")

        # Lograge
        lograge = r.signals.get("rails_lograge")
        if isinstance(lograge, dict):
            if lograge.get("found"):
                en_file  = lograge.get("lograge_enabled_file", "?")
                fmt_file = lograge.get("lograge_json_file")
                enabled  = f"✓ lograge.enabled=true [{en_file}]" if lograge.get("lograge_enabled") else "✗ lograge.enabled ausente"
                fmt      = f"✓ Json formatter [{fmt_file}]" if lograge.get("lograge_json_formatter") else "✗ Json formatter ausente"
                print(f"    ✓ lograge  {enabled}  |  {fmt}")
            else:
                print(f"    ✗ lograge  → não encontrado em config/initializers/ nem config/application.rb")

        # Node.js — tracer init
        node_init = r.signals.get("nodejs_tracer_init")
        if isinstance(node_init, dict):
            if node_init.get("found"):
                inj_str = f"✓ logInjection=true [{node_init['log_injection_file']}]" if node_init.get("log_injection") else "✗ logInjection ausente"
                tog_str = f"  |  ✓ monitoring toggle [{node_init['monitoring_file']}]" if node_init.get("monitoring_toggle") else ""
                print(f"    ✓ tracer init  [{node_init['path']}]  |  {inj_str}{tog_str}")
            else:
                print(f"    ✗ tracer init  → não encontrado em arquivos candidatos")

        # Node.js — --require no scripts.start
        start_req = r.signals.get("package_start_require")
        if isinstance(start_req, dict) and start_req.get("found"):
            print(f"    ✓ --require dd-trace/init  [package.json scripts.start]")

        skip = {"rails_dd_initializer", "rails_log_injection", "rails_lograge", "rails_lograge_json",
                "nodejs_tracer_init", "nodejs_log_injection", "nodejs_monitoring_toggle", "package_start_require"}
        for key, val in r.signals.items():
            if key in skip:
                continue
            if isinstance(val, dict):
                status = "✓" if val.get("found") else "✗"
                extra = f" [{val['version']}]" if val.get("version") else ""
                extra += f" → {val['file']}" if val.get("file") else ""
                print(f"    {status} {key}{extra}")
            elif val is True:
                print(f"    ✓ {key}")


def export_csv(results: list[RepoResult], path: str):
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow([
            "repositorio", "full_name", "linguagem", "score", "maturidade",
            "dd_initializer", "log_injection",
            "lograge_initializer", "lograge_enabled", "lograge_json_formatter",
            "archived", "url",
        ])
        for r in sorted(results, key=lambda x: x.score, reverse=True):
            init   = r.signals.get("rails_dd_initializer", {})
            lograge = r.signals.get("rails_lograge", {})
            has_init  = init.get("found", False)          if isinstance(init, dict)    else False
            has_inj   = init.get("log_injection", False)  if isinstance(init, dict)    else False
            has_lg    = lograge.get("found", False)        if isinstance(lograge, dict) else False
            lg_enabled = lograge.get("lograge_enabled", False)        if isinstance(lograge, dict) else False
            lg_json    = lograge.get("lograge_json_formatter", False) if isinstance(lograge, dict) else False
            w.writerow([
                r.name, r.full_name, r.language, r.score, r.maturity,
                has_init, has_inj,
                has_lg, lg_enabled, lg_json,
                r.archived, r.url,
            ])
    print(f"  📄 CSV exportado: {path}")


def export_json(results: list[RepoResult], path: str):
    with open(path, "w") as f:
        json.dump([r.to_dict() for r in results], f, indent=2, ensure_ascii=False)
    print(f"  📄 JSON exportado: {path}")


# ──────────────────────────────────────────────
# Entrypoint
# ──────────────────────────────────────────────

def prompt_interactive() -> dict:
    """Modo interativo: coleta org, token e opções via input() quando nenhum argumento é passado."""
    print()
    print("╔══════════════════════════════════════════════════╗")
    print("║   Datadog Observability Maturity Scanner         ║")
    print("╚══════════════════════════════════════════════════╝")
    print()

    # ── Organização ──
    while True:
        org = input("  Nome da organização no GitHub (ex: MinhaOrg): ").strip()
        if org:
            break
        print("  ⚠️  O nome da organização não pode ser vazio.")

    # ── Token ──
    token_env = os.environ.get("GITHUB_TOKEN", "")
    if token_env:
        print(f"  Token detectado na variável GITHUB_TOKEN — pressione Enter para usar ou cole outro: ", end="")
        token_input = input().strip()
        token = token_input or token_env
    else:
        print("  Cole seu GitHub Personal Access Token (ghp_...): ", end="")
        token = input().strip()
        while not token:
            print("  ⚠️  Token obrigatório. Cole o token: ", end="")
            token = input().strip()

    # ── Opções ──
    print()
    print("  Opções (pressione Enter para aceitar o padrão):")

    skip_archived_in = input("  Ignorar repositórios arquivados? [S/n]: ").strip().lower()
    skip_archived = skip_archived_in not in ("n", "nao", "não", "no")

    skip_unknown_in = input("  Ignorar repositórios com linguagem desconhecida? [S/n]: ").strip().lower()
    skip_unknown = skip_unknown_in not in ("n", "nao", "não", "no")

    lang_in = input("  Filtrar por linguagem? [ruby/nodejs/go/dotnet] ou Enter para todas: ").strip().lower()
    lang = lang_in if lang_in in ("ruby", "nodejs", "go", "dotnet") else None

    csv_in = input("  Exportar CSV? [S/n]: ").strip().lower()
    export_csv_flag = csv_in not in ("n", "nao", "não", "no")

    detail_in = input("  Mostrar sinais detalhados no terminal? [s/N]: ").strip().lower()
    detail = detail_in in ("s", "sim", "y", "yes")

    print()
    return {
        "org":           org,
        "token":         token,
        "skip_archived": skip_archived,
        "skip_unknown":  skip_unknown,
        "lang":          lang,
        "csv":           export_csv_flag,
        "detail":        detail,
        "workers":       5,
        "top":           0,
        "resume":        None,
        "search_only":   False,
        "json":          True,
    }


def main():
    parser = argparse.ArgumentParser(
        description="Datadog Observability Scanner via GitHub API",
        epilog="Se executado sem argumentos, entra no modo interativo e solicita as informações no terminal.",
    )
    parser.add_argument("--org",     help="Organização GitHub (ex: MinhaOrg) — se omitido, será solicitado interativamente")
    parser.add_argument("--token",   help="GitHub token (ou use env GITHUB_TOKEN)")
    parser.add_argument("--workers", type=int, default=5, help="Threads paralelas para Contents API (default: 5)")
    parser.add_argument("--skip-archived", action="store_true", help="Ignora repositórios arquivados")
    parser.add_argument("--skip-unknown",  action="store_true", help="Ignora repositórios com linguagem desconhecida")
    parser.add_argument("--lang",    help="Filtra por linguagem (ruby/nodejs/go/dotnet)")
    parser.add_argument("--top",     type=int, default=0, help="Mostra só os N primeiros na tabela")
    parser.add_argument("--json",    action="store_true", help="Exporta JSON")
    parser.add_argument("--csv",     action="store_true", help="Exporta CSV")
    parser.add_argument("--resume",  help="Retoma de um JSON anterior (passa o caminho)")
    parser.add_argument("--detail",  action="store_true", help="Mostra sinais detalhados por repo no terminal")
    parser.add_argument("--search-only", action="store_true",
                        help="Usa só Code Search (mais rápido, menos detalhe)")
    args = parser.parse_args()

    # ── Modo interativo: nenhum argumento passado ──
    if not args.org:
        opts = prompt_interactive()
        org          = opts["org"]
        token        = opts["token"]
        skip_archived = opts["skip_archived"]
        skip_unknown  = opts["skip_unknown"]
        lang         = opts["lang"]
        export_csv_f = opts["csv"]
        export_json_f = opts["json"]
        detail       = opts["detail"]
        workers      = opts["workers"]
        top          = opts["top"]
        resume       = opts["resume"]
        search_only  = opts["search_only"]
    else:
        org          = args.org
        token        = args.token or os.environ.get("GITHUB_TOKEN")
        skip_archived = args.skip_archived
        skip_unknown  = args.skip_unknown
        lang         = args.lang
        export_csv_f = args.csv
        export_json_f = args.json
        detail       = args.detail
        workers      = args.workers
        top          = args.top
        resume       = args.resume
        search_only  = args.search_only

    if not token:
        print("❌ Token necessário: --token ghp_... ou export GITHUB_TOKEN=ghp_...")
        print("\nComo gerar:")
        print("  github.com → Settings → Developer settings → Personal access tokens")
        print("  Permissões: repo (classic token) ou Contents read-only (fine-grained)")
        sys.exit(1)

    client = GitHubClient(token)

    # ── 1. Lista todos os repos da org ──
    if resume:
        print(f"  📂 Retomando de: {resume}")
        with open(resume) as f:
            prev = json.load(f)
        done_names = {r["full_name"] for r in prev}
        results = [RepoResult(**r) for r in prev]
    else:
        done_names = set()
        results = []

    # ── Diagnóstico do token antes de começar ──
    print(f"\n  🔑 Verificando token e acesso à organização {org}...")
    rate = client.get("/rate_limit")
    if rate:
        core_limit = rate.get("resources", {}).get("core", {}).get("limit", 0)
        if core_limit <= 60:
            print(f"\n  ❌ PROBLEMA DETECTADO: token sem autenticação válida.")
            print(f"     Limite core: {core_limit}/hora (esperado: 5.000/hora com token válido)")
            print(f"\n  Causas mais comuns:")
            print(f"     1. Token expirado — verifique em github.com → Settings → Developer settings")
            print(f"     2. SSO não autorizado — na listagem de tokens, clique em 'Configure SSO'")
            print(f"        e autorize a organização {org}")
            print(f"     3. Token sem escopo 'repo' — gere um novo marcando o escopo repo")
            print(f"\n  Encerrando.")
            sys.exit(1)
        print(f"  ✓ Token válido — {core_limit} req/hora disponíveis")

    org_info = client.get(f"/orgs/{org}")
    if not org_info:
        print(f"\n  ❌ Organização '{org}' não encontrada ou token sem acesso.")
        print(f"     Verifique se o nome está correto e se o SSO está autorizado.")
        sys.exit(1)
    total_repos = org_info.get("public_repos", 0) + org_info.get("total_private_repos", 0)
    print(f"  ✓ Organização encontrada — {total_repos} repositórios no total\n")

    print(f"  🔍 Buscando repositórios de {org}...")
    all_repos = list(client.paginate(f"/orgs/{org}/repos", {"type": "all"}))
    print(f"  ✓ {len(all_repos)} repositórios encontrados.")

    if len(all_repos) == 0 and total_repos > 0:
        print(f"\n  ⚠️  A org tem {total_repos} repos mas a API retornou 0.")
        print(f"     Isso indica que o token não tem acesso aos repositórios privados.")
        print(f"     Solução: autorize o SSO em github.com → Settings → Developer settings")
        print(f"     → Personal access tokens → Configure SSO → {org}")
        sys.exit(1)

    # Filtros
    repos = [r for r in all_repos if r["full_name"] not in done_names]
    if skip_archived:
        repos = [r for r in repos if not r.get("archived")]
    if skip_unknown:
        known_langs = {"Ruby", "JavaScript", "TypeScript", "Go", "C#", "F#", "Visual Basic .NET"}
        before = len(repos)
        repos = [r for r in repos if r.get("language") in known_langs]
        print(f"  ✓ {before - len(repos)} repos com linguagem desconhecida ignorados.")
    if lang:
        lang_map = {"nodejs": ["JavaScript", "TypeScript"], "ruby": ["Ruby"],
                    "go": ["Go"], "dotnet": ["C#", "F#", "Visual Basic .NET"]}
        allowed = lang_map.get(lang, [])
        repos = [r for r in repos if r.get("language") in allowed]

    print(f"  ✓ {len(repos)} repos para analisar.\n")

    # ── 2. Code Search em lote ──
    search_map = build_search_map(client, org)

    if search_only:
        # Modo rápido: só Code Search, sem Contents API
        for repo_meta in repos:
            fn = repo_meta["full_name"]
            lang = detect_language_api(client, fn, repo_meta.get("language") or "")
            signals = search_map.get(fn, {})
            score, maturity = calculate_score(signals)
            results.append(RepoResult(
                name=repo_meta["name"], full_name=fn, language=lang,
                archived=repo_meta.get("archived", False),
                signals=signals, score=score, maturity=maturity,
                url=repo_meta.get("html_url", ""),
            ))
    else:
        # ── 3. Contents API em paralelo para repos com sinais ──
        # Prioriza repos que já têm sinais no Code Search; o resto é "não instrumentado"
        repos_with_signals = [r for r in repos if r["full_name"] in search_map]
        repos_without      = [r for r in repos if r["full_name"] not in search_map]

        print(f"  🔎 Fase 2/2: Contents API para {len(repos_with_signals)} repos com sinais...")
        print(f"  (demais {len(repos_without)} classificados como não instrumentados)\n")

        done = 0
        total = len(repos_with_signals)
        lock = threading.Lock()

        def process(repo_meta):
            nonlocal done
            signals = search_map.get(repo_meta["full_name"], {})
            result = scan_repo(client, repo_meta, signals)
            with lock:
                done += 1
                print(f"  [{done:>4}/{total}] {repo_meta['name']:<50} {result.maturity}", flush=True)
            return result

        with ThreadPoolExecutor(max_workers=workers) as ex:
            futures = {ex.submit(process, r): r for r in repos_with_signals}
            for fut in as_completed(futures):
                try:
                    results.append(fut.result())
                except Exception as e:
                    r = futures[fut]
                    results.append(RepoResult(
                        name=r["name"], full_name=r["full_name"],
                        language="error", archived=False,
                        signals={"error": str(e)}, score=0,
                        maturity="⚠️  Erro", url=r.get("html_url", ""),
                    ))

        # Adiciona repos sem sinais como não instrumentados
        for repo_meta in repos_without:
            lang = detect_language_api(client, repo_meta["full_name"], repo_meta.get("language") or "")
            if skip_unknown and lang == "unknown":
                continue
            results.append(RepoResult(
                name=repo_meta["name"], full_name=repo_meta["full_name"],
                language=lang, archived=repo_meta.get("archived", False),
                signals={}, score=0, maturity="🔴 Não instrumentado",
                url=repo_meta.get("html_url", ""),
            ))

    # ── Output ──
    print_table(results, top=top)

    if detail:
        print_detail(results)

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    if export_json_f or True:  # sempre salva JSON para permitir --resume
        json_path = f"dd_maturity_{org}_{ts}.json"
        export_json(results, json_path)
    if export_csv_f:
        csv_path = f"dd_maturity_{org}_{ts}.csv"
        export_csv(results, csv_path)


if __name__ == "__main__":
    main()
