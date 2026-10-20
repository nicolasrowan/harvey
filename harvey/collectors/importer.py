"""IMPORT — companies you already have, from a CSV or Excel file.

Lead lists bought, exported from a CRM or built by hand go through the same
door as discovery: normalised to a Business, junk-filtered, deduplicated on
domain, recorded as a company. From there profiling and the Scout's contact
backfill pick them up like any discovered company. Free: no provider, no
model call.

Headers are matched loosely (case, accents and spacing ignored), in English
or Spanish: name/empresa, website/web/dominio, phone/teléfono,
address/dirección, city/localidad/provincia, industry/sector.
"""

import csv
import hashlib
import logging
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path

from harvey.collectors.discover import (
    Business, DiscoveryQuery, _upsert, is_junk, normalize_domain,
)

logger = logging.getLogger("harvey.import")

COLUMNS = {
    "name": ("name", "company", "company name", "business", "empresa", "nombre",
             "nombre empresa", "razon social", "cliente", "title"),
    "website": ("website", "web", "url", "domain", "dominio", "sitio web",
                "pagina web", "company website", "company domain"),
    "phone": ("phone", "telephone", "telefono", "tel", "movil", "company phone"),
    "address": ("address", "direccion", "domicilio"),
    "location": ("city", "location", "ciudad", "localidad", "municipio",
                 "poblacion", "provincia", "zona", "region"),
    "category": ("industry", "category", "sector", "categoria", "actividad",
                 "subsector", "tipo"),
}


def _norm(header: str) -> str:
    text = unicodedata.normalize("NFKD", str(header or ""))
    text = "".join(c for c in text if not unicodedata.combining(c))
    return " ".join(text.lower().replace("_", " ").replace("-", " ").split())


def map_columns(headers: list[str]) -> dict[str, str]:
    """{field: original header} for the headers we recognise (first match wins)."""
    normalised = {_norm(h): h for h in headers if h is not None}
    mapping = {}
    for fld, aliases in COLUMNS.items():
        for alias in aliases:
            if alias in normalised:
                mapping[fld] = normalised[alias]
                break
    return mapping


def read_rows(path: Path, sheet: str = "") -> list[dict]:
    """Rows of a .csv (any common delimiter) or .xlsx as header → value dicts."""
    suffix = path.suffix.lower()
    if suffix in (".xlsx", ".xlsm"):
        try:
            import openpyxl
        except ImportError:
            raise RuntimeError(
                "Reading Excel needs openpyxl: pip install openpyxl "
                "(or save the sheet as CSV).")
        wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
        ws = wb[sheet] if sheet else wb.worksheets[0]
        rows = ws.iter_rows(values_only=True)
        headers = None
        out = []
        for row in rows:
            cells = ["" if v is None else str(v).strip() for v in row]
            if headers is None:
                if any(cells):
                    headers = cells
                continue
            if any(cells):
                out.append(dict(zip(headers, cells)))
        wb.close()
        return out
    if suffix in (".csv", ".tsv", ".txt"):
        # utf-8-sig drops the BOM Excel adds; the sniffer handles ; and \t,
        # which is what a Spanish-locale Excel writes.
        text = path.read_text(encoding="utf-8-sig", errors="replace")
        try:
            dialect = csv.Sniffer().sniff(text[:4096], delimiters=",;\t|")
        except csv.Error:
            dialect = csv.excel
        reader = csv.DictReader(text.splitlines(), dialect=dialect)
        rows = (
            {k: (v or "").strip() for k, v in row.items() if k is not None}
            for row in reader
        )
        return [row for row in rows if any(row.values())]
    raise RuntimeError(f"Unsupported file type {suffix!r}: use .csv or .xlsx.")


def row_to_business(row: dict, mapping: dict[str, str], source: str) -> Business | None:
    get = lambda fld: (row.get(mapping[fld]) or "").strip() if fld in mapping else ""  # noqa: E731
    name, website = get("name"), get("website")
    domain = normalize_domain(website) if website else ""
    if not name and not domain:
        return None
    location = get("location")
    external_id = ""
    if not domain:
        # No site to key on: the name and place identify it, so importing
        # the same file twice still doesn't duplicate it.
        key = f"{_norm(name)}|{_norm(location)}"
        external_id = "import:" + hashlib.sha1(key.encode()).hexdigest()[:16]
    return Business(
        name=name or domain,
        domain=domain,
        website=website if website.startswith("http") else (f"https://{domain}" if domain else ""),
        phone=get("phone"),
        address=get("address"),
        location=location,
        category=get("category"),
        has_website=bool(domain),
        external_id=external_id,
        source_url=source,
        provider="import",
    )


@dataclass
class ImportReport:
    rows: int = 0
    new_companies: int = 0
    known_companies: int = 0
    junk: int = 0
    skipped: int = 0              # rows with neither a name nor a website
    columns: dict = field(default_factory=dict)
    run_id: str = ""
    errors: list[str] = field(default_factory=list)


async def import_companies(state, path: str | Path, sheet: str = "",
                           industry: str = "", limit: int = 0) -> ImportReport:
    """Record every company in the file. ``industry`` fills rows without one."""
    path = Path(path)
    report = ImportReport()
    rows = read_rows(path, sheet)
    if limit:
        rows = rows[:limit]
    report.rows = len(rows)
    if not rows:
        return report
    report.columns = map_columns(list(rows[0].keys()))
    if "name" not in report.columns and "website" not in report.columns:
        report.errors.append(
            "no name or website column found; headers were: "
            + ", ".join(str(h) for h in rows[0].keys()))
        return report

    report.run_id = await state.start_run(
        "import", provider="import", params={"file": path.name, "rows": len(rows)})
    query = DiscoveryQuery(term=industry)
    seen: set[str] = set()
    try:
        for row in rows:
            biz = row_to_business(row, report.columns, f"file:{path.name}")
            if biz is None:
                report.skipped += 1
                continue
            if biz.domain and is_junk(biz.domain, biz.name):
                report.junk += 1
                continue
            ident = biz.identity()
            if ident in seen:
                report.known_companies += 1
                continue
            seen.add(ident)
            _, is_new = await _upsert(state, biz, query)
            if is_new:
                report.new_companies += 1
            else:
                report.known_companies += 1
        await state.finish_run(report.run_id, status="completed",
                               records=report.new_companies + report.known_companies)
    except Exception as e:
        logger.exception("import failed")
        report.errors.append(str(e))
        await state.finish_run(report.run_id, status="failed",
                               records=report.new_companies, error=str(e))
    return report
