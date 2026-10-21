"""APPROACH — how to open a conversation with each company, for teams that
don't send cold email.

For every company in the pipeline Harvey reads its homepage, adds what you
already know about it (notes imported from your CRM) and drafts the approach
a salesperson would take: whether it fits, the hook, who to ask for, an
opener for a call and the first message as Harvey would write it. Nothing is
queued, scheduled or sent; the output is a sheet a person works from.

Approaches live in their own table, created on first use, so this feature
never takes a schema version number upstream might want.
"""

import csv
import json
import logging
import re
from dataclasses import dataclass, field
from html import unescape
from itertools import zip_longest
from pathlib import Path

import aiosqlite

from harvey.collectors.profile import ProfileCollector, _text

logger = logging.getLogger("harvey.approach")

SITE_TEXT_CHARS = 2500
FITS = ("alta", "media", "baja", "no")
FIELDS = ("encaje", "por_que", "gancho", "preguntar_por", "llamada",
          "asunto", "mensaje", "siguiente_paso")

SCHEMA = """
CREATE TABLE IF NOT EXISTS approaches (
    company_id TEXT PRIMARY KEY,
    product TEXT DEFAULT '',
    fit TEXT DEFAULT '',
    payload_json TEXT DEFAULT '{}',
    site_read INTEGER DEFAULT 0,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
"""

EXPORT_COLUMNS = (
    ("empresa", "Empresa"), ("web", "Web"), ("sector", "Sector"), ("zona", "Zona"),
    ("telefono", "Teléfono"), ("encaje", "Encaje"), ("por_que", "Por qué"),
    ("gancho", "Gancho"), ("preguntar_por", "Preguntar por"),
    ("llamada", "Para abrir la llamada"), ("asunto", "Asunto"),
    ("mensaje", "Mensaje (no se envía)"), ("siguiente_paso", "Siguiente paso"),
    ("lo_que_sabiamos", "Lo que ya sabíamos"), ("fecha", "Fecha"),
)


@dataclass
class ApproachReport:
    candidates: int = 0
    drafted: int = 0
    sites_read: int = 0
    calls: int = 0
    stopped: str = ""
    errors: list[str] = field(default_factory=list)


async def ensure_table(state):
    async with state._connect() as db:
        await db.executescript(SCHEMA)
        await db.commit()


async def companies_needing_approach(state, limit: int, industry: str = "",
                                     redo: bool = False, spread: bool = False,
                                     order: list[str] | None = None) -> list[dict]:
    """Companies without an approach yet, oldest first.

    ``spread`` takes them round-robin across industries, so a small trial
    run shows every kind of company instead of the first 10 of one sector.
    """
    await ensure_table(state)
    where = ["1 = 1"]
    params: list = []
    if not redo:
        where.append("c.id NOT IN (SELECT company_id FROM approaches)")
    if industry:
        where.append("LOWER(c.industry) = LOWER(?)")
        params.append(industry)
    async with state._connect() as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            f"""SELECT c.id, c.name, c.domain, c.website, c.industry, c.location,
                       c.phone, c.notes, c.description, c.tech_stack_json
                FROM companies c WHERE {' AND '.join(where)}
                ORDER BY c.created_at, c.id""",
            params,
        ) as cursor:
            rows = [dict(r) for r in await cursor.fetchall()]
    if order:
        # Best first, as `harvey rank` scored them; anything unranked goes last.
        position = {cid: i for i, cid in enumerate(order)}
        rows = [r for r in rows if r["id"] in position]
        rows.sort(key=lambda r: position[r["id"]])
    elif spread:
        by_industry: dict[str, list[dict]] = {}
        for row in rows:
            by_industry.setdefault(row["industry"] or "", []).append(row)
        rows = [r for group in zip_longest(*by_industry.values()) for r in group if r]
    return rows[:limit] if limit else rows


async def read_site(collector: ProfileCollector, domain: str) -> dict:
    """Title, meta description, visible text and tools of a homepage."""
    if not domain:
        return {}
    base, html = await collector._resolve_site(domain)
    if not html:
        return {}
    title = re.search(r"(?is)<title[^>]*>(.*?)</title>", html)
    meta = re.search(
        r'(?is)<meta[^>]+name=["\']description["\'][^>]*content=["\']([^"\']*)', html)
    return {
        "url": base,
        "title": unescape(re.sub(r"\s+", " ", title.group(1))).strip() if title else "",
        "description": unescape(meta.group(1)).strip() if meta else "",
        "text": unescape(_text(html)).strip()[:SITE_TEXT_CHARS],
        "tools": collector.detect_tech(html),
    }


def company_facts(company: dict, site: dict, signals: list[str]) -> str:
    lines = [
        f"### id={company['id']}",
        f"- Empresa: {company['name']}",
        f"- Sector: {company['industry'] or 'desconocido'}",
        f"- Zona: {company['location'] or 'desconocida'}",
        f"- Web: {company['website'] or company['domain'] or 'no tiene web'}",
    ]
    if site:
        if site.get("title"):
            lines.append(f"- Título de su web: {site['title']}")
        if site.get("description"):
            lines.append(f"- Cómo se describe: {site['description']}")
        if site.get("text"):
            lines.append(f"- Texto de su portada (leído hoy): {site['text']}")
        if site.get("tools"):
            lines.append(f"- Herramientas detectadas en su web: {', '.join(site['tools'])}")
    elif company["domain"]:
        lines.append("- No hemos podido leer su web (suele ser un bloqueo a nuestro lector, "
                     "no que esté caída): no lo menciones ni lo uses como gancho.")
    if signals:
        lines.append(f"- Señales recogidas: {'; '.join(signals)}")
    if company.get("notes"):
        lines.append("- Lo que ya sabemos (CRM):\n  " + company["notes"].replace("\n", "\n  "))
    return "\n".join(lines)


def build_prompt(brain, config, facts: list[str]) -> str:
    persona, product = config.persona, config.product
    writer_rules = brain.load_prompt(
        "writer",
        product_name=product.name,
        product_description=product.description,
        product_benefits="\n".join(f"- {b}" for b in product.key_benefits),
        product_pricing=product.pricing,
        persona_name=persona.name,
        persona_company=persona.company,
        persona_role=persona.role,
        persona_tone=persona.tone,
    ) or ""
    knowledge = "\n\n---\n\n".join(
        k for k in (brain.load_skill("house_rules"), brain.load_skill("product_knowledge")) if k
    )
    return f"""{writer_rules}

---
## FOUNDATIONAL KNOWLEDGE

{knowledge}

---
## TAREA: ENFOQUE DE CADA EMPRESA (no se envía nada)

Nadie va a enviar estos mensajes. Un comercial de {persona.company} los usará
para preparar la llamada o la visita a cada empresa y decidir si merece la
pena. Para cada empresa de abajo, prepara el enfoque para {product.name}.

Reglas:
- Usa solo los DATOS de cada empresa: su web leída hoy y lo que ya sabemos del
  CRM. Si un dato no está, no lo sabes. No inventes nombres, cifras ni hechos.
- Su web se ha leído sin ejecutar JavaScript: un contador a cero, una sección
  vacía o algo que no aparece puede ser efecto de eso. No uses como gancho lo
  que falta o sale vacío; apóyate en lo que la web sí dice.
- Nunca digas que su web está caída o no carga.
- encaje: "alta", "media", "baja" o "no", según la ficha del producto (señales
  de compra y cuándo no conviene) y las notas del CRM. Si ya dijeron que no les
  interesa o que lo tienen cubierto, dilo y baja el encaje.
- por_que: una o dos frases con el motivo del encaje.
- gancho: la observación concreta y comprobable que abre la conversación, y
  de dónde sale (su web o el CRM).
- preguntar_por: a quién pedir al llamar: el nombre si el CRM lo da, si no el cargo.
- llamada: una o dos frases para abrir la llamada, con el gancho.
- asunto y mensaje: el primer correo tal como lo escribirías siguiendo TODAS
  las reglas de arriba (menos de 75 palabras, una sola pregunta, sin nombrar
  el producto). Es el enfoque por escrito; no se va a enviar.
- siguiente_paso: qué haría el comercial después (la llamada a la acción de
  la ficha: diagnóstico, demo, auditoría gratis...).
- Todo en castellano de España, con trato de tú, con tildes y eñes.
- Sin firma ni despedida: lo usará el comercial que lleve la cuenta.
- Nada de variables tipo {{{{company}}}} o {{{{first_name}}}}: escribe el nombre real.

Devuelve SOLO JSON con esta forma:
{{"approaches": [{{"id": "...", "encaje": "...", "por_que": "...", "gancho": "...",
"preguntar_por": "...", "llamada": "...", "asunto": "...", "mensaje": "...",
"siguiente_paso": "..."}}]}}
Una entrada por empresa, con su id tal cual.

EMPRESAS:

{chr(10).join(facts)}
"""


def short_name(name: str) -> str:
    """'Dinmo | Inmobiliaria Barcelona' -> 'Dinmo': listing names carry SEO tails."""
    return re.split(r"\s+[|·–-]\s+", name or "", maxsplit=1)[0].strip() or name


def fill_merge_vars(payload: dict, company_name: str) -> dict:
    """The writer rules mention merge variables; a draft that kept one gets
    the real name instead of going out with '{{company}}' in it."""
    out = {}
    for key, value in payload.items():
        value = re.sub(r"\{\{\s*company\s*\}\}", short_name(company_name), value)
        value = re.sub(r"\{\{\s*\w+\s*\}\},?\s*", "", value)
        out[key] = value
    return out


def _parse(result, ids: set[str]) -> dict[str, dict]:
    items = result.get("approaches") if isinstance(result, dict) else result
    out = {}
    for item in items or []:
        if not isinstance(item, dict) or item.get("id") not in ids:
            continue
        clean = {k: str(item.get(k) or "").strip() for k in FIELDS}
        fit = clean["encaje"].lower()
        clean["encaje"] = fit if fit in FITS else ("media" if fit else "")
        if clean["mensaje"] or clean["llamada"]:
            out[item["id"]] = clean
    return out


async def save_approach(state, company_id: str, product: str, payload: dict, site_read: bool):
    async with state._connect() as db:
        await db.execute(
            """INSERT INTO approaches (company_id, product, fit, payload_json, site_read, created_at)
               VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
               ON CONFLICT(company_id) DO UPDATE SET product = excluded.product,
                   fit = excluded.fit, payload_json = excluded.payload_json,
                   site_read = excluded.site_read, created_at = CURRENT_TIMESTAMP""",
            (company_id, product, payload.get("encaje", ""),
             json.dumps(payload, ensure_ascii=False), int(site_read)),
        )
        await db.commit()


async def _signals_for(state, company_id: str) -> list[str]:
    try:
        rows = await state.get_observations(company_id=company_id, latest_only=True, limit=20)
    except Exception:
        return []
    out = []
    for r in rows:
        value = r.get("value_text") or (r.get("value_num") if r.get("value_num") is not None else "")
        out.append(f"{r.get('signal_code')}{': ' + str(value) if value != '' else ''}")
    return out


async def draft_approaches(state, brain, config, limit: int = 10, batch: int = 5,
                           industry: str = "", redo: bool = False,
                           spread: bool = False,
                           order: list[str] | None = None) -> ApproachReport:
    report = ApproachReport()
    companies = await companies_needing_approach(state, limit, industry, redo, spread, order)
    report.candidates = len(companies)
    if not companies:
        return report

    max_calls = max(int(200 * (config.usage.max_daily_claude_percent / 100)), 1)
    async with ProfileCollector(state, confirmed=set()) as collector:
        for start in range(0, len(companies), max(batch, 1)):
            chunk = companies[start:start + max(batch, 1)]
            if not await brain.is_within_budget(
                    max_calls, max_percent=config.usage.max_daily_claude_percent):
                report.stopped = (f"{brain.provider_label} usage limit reached "
                                  f"({config.usage.max_daily_claude_percent}%)")
                break
            sites, facts = {}, []
            for company in chunk:
                site = await read_site(collector, company["domain"])
                sites[company["id"]] = site
                report.sites_read += bool(site)
                facts.append(company_facts(company, site, await _signals_for(state, company["id"])))
            prompt = build_prompt(brain, config, facts)
            report.calls += 1
            try:
                result = await brain.think_json(
                    prompt, session_id="harvey-approach", agent="writer", task="approach")
            except Exception as e:
                report.errors.append(str(e))
                continue
            drafted = _parse(result, {c["id"] for c in chunk})
            missing = [c["name"] for c in chunk if c["id"] not in drafted]
            if missing:
                report.errors.append(f"no approach returned for: {', '.join(missing)}")
            names = {c["id"]: c["name"] for c in chunk}
            for company_id, payload in drafted.items():
                payload = fill_merge_vars(payload, names[company_id])
                await save_approach(state, company_id, config.product.name, payload,
                                    bool(sites.get(company_id)))
                report.drafted += 1
    return report


async def approach_rows(state, fit: str = "") -> list[dict]:
    """Every saved approach joined to its company, best fit first."""
    await ensure_table(state)
    order = "CASE a.fit WHEN 'alta' THEN 0 WHEN 'media' THEN 1 WHEN 'baja' THEN 2 ELSE 3 END"
    where, params = "", []
    if fit:
        where = "WHERE a.fit IN (" + ",".join("?" * len(fit.split(","))) + ")"
        params = [f.strip() for f in fit.split(",")]
    async with state._connect() as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            f"""SELECT c.name, c.website, c.domain, c.industry, c.location, c.phone,
                       c.notes, a.payload_json, a.created_at
                FROM approaches a JOIN companies c ON c.id = a.company_id
                {where} ORDER BY {order}, c.industry, c.name""",
            params,
        ) as cursor:
            raw = [dict(r) for r in await cursor.fetchall()]
    rows = []
    for r in raw:
        payload = json.loads(r["payload_json"] or "{}")
        rows.append({
            "empresa": r["name"], "web": r["website"] or r["domain"],
            "sector": r["industry"], "zona": r["location"], "telefono": r["phone"],
            **{k: payload.get(k, "") for k in FIELDS},
            "lo_que_sabiamos": r["notes"] or "",
            "fecha": str(r["created_at"] or "")[:10],
        })
    return rows


def write_export(rows: list[dict], path: str | Path) -> Path:
    """.xlsx when openpyxl is there (and asked for), else CSV that Excel in
    Spanish opens directly: UTF-8 with BOM, semicolon-separated."""
    path = Path(path)
    headers = [label for _, label in EXPORT_COLUMNS]
    keys = [key for key, _ in EXPORT_COLUMNS]
    if path.suffix.lower() == ".xlsx":
        from openpyxl import Workbook
        from openpyxl.styles import Alignment, Font

        wb = Workbook()
        ws = wb.active
        ws.title = "Enfoques"
        ws.append(headers)
        for cell in ws[1]:
            cell.font = Font(bold=True)
        for row in rows:
            ws.append([row.get(k, "") for k in keys])
        widths = {"Mensaje (no se envía)": 70, "Lo que ya sabíamos": 50, "Por qué": 40,
                  "Gancho": 45, "Para abrir la llamada": 45, "Siguiente paso": 30}
        for i, header in enumerate(headers, start=1):
            letter = ws.cell(row=1, column=i).column_letter
            ws.column_dimensions[letter].width = widths.get(header, 18)
        for row in ws.iter_rows(min_row=2):
            for cell in row:
                cell.alignment = Alignment(wrap_text=True, vertical="top")
        ws.freeze_panes = "B2"
        ws.auto_filter.ref = ws.dimensions
        wb.save(path)
        return path
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        writer = csv.writer(f, delimiter=";")
        writer.writerow(headers)
        for row in rows:
            writer.writerow([row.get(k, "") for k in keys])
    return path
