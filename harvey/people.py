"""PEOPLE — who decides at a company, from what search results already say.

Most small businesses don't publish a team page, so the profile collector
can't name anyone. Search results often can: a professional profile titled
"Ana Ruiz - Gerente - Clínica Sol", a press note, a directory entry. This
stage runs one or two web searches per company (DuckDuckGo, then Bing, with
pauses) and keeps a name only when the result is about THAT company and the
role reads like a decision maker. It reads search results only; it never
logs in anywhere or visits a social network.

Findings are observations (CONTACT_FOUND / DECISION_MAKER_TITLE) from the
"search" collector, with the result as evidence, so ranking and approaches
use them like anything else.
"""

import asyncio
import logging
import random
import re
import unicodedata
from dataclasses import dataclass, field
from datetime import date
from html import unescape
from urllib.parse import parse_qs, quote_plus, urlparse

import httpx

from harvey.collectors.profile import (
    NON_NAME_WORDS, USER_AGENTS, is_decision_maker_title, plausible_name,
)

logger = logging.getLogger("harvey.people")

SEARCH_DELAY = (3.0, 6.0)
TIMEOUT = httpx.Timeout(connect=8.0, read=15.0, write=8.0, pool=8.0)
SEARCHED_KEY = "people_searched:"

# Words that say what a business does, not which one it is: "Abogados de
# Familia" has no distinctive token, so nothing can be matched to it safely.
GENERIC_TOKENS = frozenset("""
abogados abogado abogada asesoria asesores asesor gestoria clinica clinicas dental
dentista dentistas centro centros estudio estetica medicina medica fisioterapia
fisioterapeutas psicologia psicologos inmobiliaria inmobiliarias hotel hoteles
restaurante restaurantes gimnasio gimnasios yoga academia academias idiomas colegio
colegios taller talleres peluqueria peluquerias fotografo fotografos fotografia
arquitectos arquitectura agencia agencias marketing consultora consultoria grupo
servicios reformas hogar barcelona madrid malaga marbella murcia valencia vigo
coruna santiago ourense spain espana official oficial
familia extranjeria laboral fiscal penal civil mercantil divorcio herencias
salud bienestar belleza dental odontologia ortodoncia implantes cirugia
plastica nutricion terapia pilates fitness crossfit spain english school
language languages escuela formacion inmobiliario propiedades viviendas casas
gastronomico gastronomia cocina bodega bodegas vinos hostal apartamentos
abogacia juridico juridicos legal legales consulting partners group
""".split()) | NON_NAME_WORDS

_ROLE_FIRST = re.compile(
    r"(?i)^(CEO|COO|CFO|Owner|Founder|Co-?founder|Managing (?:Director|Partner)|Gerente|"
    r"Fundador[a]?|Cofundador[a]?|Propietari[oa]|Titular|Soci[oa](?: [Dd]irector[a]?)?|"
    r"Director[a]?(?: [Gg]eneral| [Cc]omercial| de [Mm]arketing)?)\b[\s,:·|]*(?:de|en|at|@)?\s*(.*)$")


def _plain(text: str) -> str:
    text = unicodedata.normalize("NFKD", text or "")
    return "".join(c for c in text if not unicodedata.combining(c)).lower()


def distinctive_tokens(company_name: str, domain: str = "") -> set[str]:
    """Tokens that identify this business rather than its trade or town."""
    head = re.split(r"\s+[|·–-]\s+", company_name or "", maxsplit=1)[0]
    tokens = {t for t in re.findall(r"[a-z0-9]+", _plain(head))
              if len(t) >= 4 and t not in GENERIC_TOKENS}
    stem = _plain(domain).split(".")[0]
    if len(stem) >= 5 and stem not in GENERIC_TOKENS:
        tokens.add(stem)
    return tokens


def mentions_company(text: str, tokens: set[str]) -> bool:
    plain = _plain(text)
    squashed = re.sub(r"[^a-z0-9]", "", plain)
    return any(t in plain or t in squashed for t in tokens)


@dataclass
class Finding:
    name: str
    title: str
    url: str
    source: str
    decision_maker: bool


@dataclass
class PeopleReport:
    searched: int = 0
    queries: int = 0
    found_people: int = 0
    found_decision_makers: int = 0
    skipped_generic: int = 0
    errors: list[str] = field(default_factory=list)


def parse_profile_title(title: str) -> tuple[str, str, str]:
    """A professional profile's search title -> (name, role, company).

    'Ana Ruiz - Gerente - Clínica Sol | LinkedIn' and
    'Ana Ruiz - Gerente en Clínica Sol ... - LinkedIn' both give
    ('Ana Ruiz', 'Gerente', 'Clínica Sol').
    """
    clean = re.sub(r"\s*[|·\-–—]\s*LinkedIn.*$", "", title or "", flags=re.I)
    clean = re.sub(r"\s*(\.\.\.|…)\s*$", "", clean)
    parts = [p.strip() for p in re.split(r"\s+[-–—]\s+", clean) if p.strip()]
    if len(parts) < 2:
        return "", "", ""
    name = parts[0]
    if len(parts) >= 3:
        return name, parts[1], parts[-1]
    m = re.match(r"(?i)(.+?)\s+(?:en|de|at|@)\s+(.+)$", parts[1])
    if m:
        return name, m.group(1).strip(), m.group(2).strip()
    return name, "", parts[1]


def findings_from_results(results: list[dict], company: dict) -> list[Finding]:
    tokens = distinctive_tokens(company.get("name", ""), company.get("domain", ""))
    out: dict[str, Finding] = {}
    for r in results:
        title, url, snippet = r.get("title", ""), r.get("url", ""), r.get("snippet", "")
        host = urlparse(url).netloc.lower()
        about_us = mentions_company(f"{title} {snippet}", tokens)
        if "linkedin.com" in host and "/in/" in url:
            name, role, where = parse_profile_title(title)
            if not role:
                # "Eva López - CEO Fincas Eva": the role leads the company part.
                m = _ROLE_FIRST.match(where)
                if m:
                    role, where = m.group(1), m.group(2) or where
            if not plausible_name(name) or not mentions_company(f"{where} {role} {snippet}", tokens):
                continue
            if not role:
                m = re.search(r"\b((?:CEO|[Gg]erente|[Ff]undador[a]?|[Ss]oci[oa]|[Dd]irector[a]?|"
                              r"[Pp]ropietari[oa]|[Tt]itular|[Oo]wner|[Ff]ounder|[Mm]anaging [Pp]artner)"
                              r"[^.·|]{0,40})", snippet)
                role = m.group(1).strip() if m else ""
            if not role:
                continue
            out.setdefault(name.lower(), Finding(name, role[:70], url, "LinkedIn",
                                                 is_decision_maker_title(role)))
            continue
        if not about_us:
            continue
        # Press notes, directories, the company's own pages: "Ana Ruiz, gerente de…"
        from harvey.collectors.profile import ProfileCollector
        for p in ProfileCollector(state=None).detect_owner_mentions(snippet, company.get("domain", "")):
            name = f"{p['first_name']} {p['last_name']}"
            out.setdefault(name.lower(), Finding(name, p["title"], url, host.replace("www.", ""),
                                                 p["is_decision_maker"]))
    return list(out.values())


async def search(client: httpx.AsyncClient, query: str) -> list[dict]:
    """[{title, url, snippet}] from DuckDuckGo's HTML page, else Bing's."""
    for backend in (_duckduckgo, _bing):
        try:
            results = await backend(client, query)
        except httpx.HTTPError as e:
            logger.debug(f"people: {backend.__name__} failed: {e}")
            results = []
        if results:
            return results
    return []


def _text(html: str) -> str:
    return unescape(re.sub(r"\s+", " ", re.sub(r"(?s)<[^>]+>", " ", html or ""))).strip()


async def _duckduckgo(client, query) -> list[dict]:
    r = await client.get(f"https://html.duckduckgo.com/html/?q={quote_plus(query)}")
    if r.status_code != 200:
        return []
    out = []
    # One chunk per result: everything up to the next result's opening tag.
    for block in re.split(r'<div class="result results_links', r.text)[1:]:
        a = re.search(r'(?s)class="result__a"[^>]*href="([^"]+)"[^>]*>(.*?)</a>', block)
        if not a:
            continue
        href = unescape(a.group(1))
        if "uddg=" in href:
            href = parse_qs(urlparse(href).query).get("uddg", [href])[0]
        if "duckduckgo.com/y.js" in href or "ad_domain=" in href:
            continue            # an ad, not a result
        snip = re.search(r'(?s)class="result__snippet"[^>]*>(.*?)</a>', block)
        out.append({"title": _text(a.group(2)), "url": href,
                    "snippet": _text(snip.group(1)) if snip else ""})
    return out


async def _bing(client, query) -> list[dict]:
    r = await client.get(f"https://www.bing.com/search?q={quote_plus(query)}&setlang=es&cc=ES")
    if r.status_code != 200:
        return []
    out = []
    for block in re.findall(r'(?s)<li class="b_algo".*?</li>', r.text):
        a = re.search(r'(?s)<h2[^>]*>\s*<a[^>]*href="([^"]+)"[^>]*>(.*?)</a>', block)
        if not a:
            continue
        snip = re.search(r'(?s)<p[^>]*>(.*?)</p>', block)
        out.append({"title": _text(a.group(2)), "url": unescape(a.group(1)),
                    "snippet": _text(snip.group(1)) if snip else ""})
    return out


def queries_for(company: dict) -> list[str]:
    head = re.split(r"\s+[|·–-]\s+", company.get("name") or "", maxsplit=1)[0].strip()
    town = (company.get("location") or "").split(",")[0].strip()
    # Professional profiles first: their titles carry name, role and company.
    # DuckDuckGo's HTML page returns nothing for long OR chains, so the
    # second query stays short.
    return [
        f'site:linkedin.com/in "{head}"',
        f'"{head}" {town} gerente'.strip(),
    ]


async def find_people(state, companies: list[dict], max_companies: int = 50,
                      delay: tuple[float, float] = SEARCH_DELAY) -> PeopleReport:
    """Search for the decision maker of each company that has none yet."""
    report = PeopleReport()
    async with httpx.AsyncClient(timeout=TIMEOUT, follow_redirects=True, headers={
        "User-Agent": USER_AGENTS[-1], "Accept-Language": "es-ES,es;q=0.9"}) as client:
        for company in companies:
            if report.searched >= max_companies:
                break
            if not distinctive_tokens(company.get("name", ""), company.get("domain", "")):
                report.skipped_generic += 1
                continue
            report.searched += 1
            findings: dict[str, Finding] = {}
            for i, query in enumerate(queries_for(company)):
                if report.queries:
                    await asyncio.sleep(random.uniform(*delay))
                report.queries += 1
                try:
                    results = await search(client, query)
                except Exception as e:
                    report.errors.append(f"{company.get('name')}: {e}")
                    results = []
                for f in findings_from_results(results, company):
                    findings.setdefault(f.name.lower(), f)
                if any(f.decision_maker for f in findings.values()):
                    break           # the first search already answered it
            rows = []
            for f in findings.values():
                label = f"{f.name} — {f.title} (según {f.source})"
                rows.append({"company_id": company["id"], "signal_code": "CONTACT_FOUND",
                             "collector": "search", "value_text": label,
                             "confidence": 0.7, "evidence_url": f.url})
                if f.decision_maker:
                    rows.append({"company_id": company["id"], "signal_code": "DECISION_MAKER_TITLE",
                                 "collector": "search", "value_text": label,
                                 "confidence": 0.7, "evidence_url": f.url})
                    report.found_decision_makers += 1
                report.found_people += 1
            if rows:
                await state.add_observations(rows)
            await state.set_setting(SEARCHED_KEY + company["id"], date.today().isoformat())
    return report


async def companies_to_search(state, ranked, min_score: int = 50) -> list[dict]:
    """Best-ranked first: no decision maker yet, not searched before."""
    out = []
    for r in ranked:
        if r.score < min_score:
            break
        if r.decision_maker:
            continue
        if await state.get_setting(SEARCHED_KEY + r.company["id"]):
            continue
        out.append(r.company)
    return out
