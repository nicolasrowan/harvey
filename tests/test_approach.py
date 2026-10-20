"""APPROACH: drafts how to open each company, stores it, exports it. Sends nothing."""

import csv
import json

import pytest
import pytest_asyncio

from harvey import approach as ap
from harvey.models.company import Company
from harvey.state import StateManager


class FakeConfig:
    class persona:
        name, company, role, tone = "Nico", "Rowan", "Consultor", "sobrio"

    class product:
        name, description, pricing = "Rowan Growth", "SEO + GEO", "99 €/mes"
        key_benefits = ["Sale en ChatGPT"]

    class usage:
        max_daily_claude_percent = 60


class FakeBrain:
    provider_label = "Claude"

    def __init__(self, fit="alta", within_budget=True):
        self.fit = fit
        self.within_budget = within_budget
        self.prompts = []

    def load_prompt(self, name, **kw):
        return f"RULES for {kw.get('product_name')}"

    def load_skill(self, name):
        return f"[{name}]"

    async def is_within_budget(self, *a, **kw):
        return self.within_budget

    async def think_json(self, prompt, **kw):
        self.prompts.append(prompt)
        ids = [line.split("id=")[1] for line in prompt.splitlines() if line.startswith("### id=")]
        return {"approaches": [
            {"id": i, "encaje": self.fit.upper(), "por_que": "encaja", "gancho": "su web",
             "preguntar_por": "Gerente", "llamada": "Hola", "asunto": "una duda",
             "mensaje": "¿Os preguntan por ChatGPT?", "siguiente_paso": "auditoría"}
            for i in ids
        ] + [{"id": "not-asked", "mensaje": "x"}]}


@pytest_asyncio.fixture
async def state(tmp_path):
    s = StateManager(db_path=str(tmp_path / "harvey.db"))
    await s.init_db()
    yield s


async def _add(state, name, industry, domain="", notes=""):
    return await state.add_company(Company(
        name=name, domain=domain, website=f"https://{domain}" if domain else "",
        industry=industry, location="Murcia", notes=notes))


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    async def fake_read_site(collector, domain):
        return {"title": f"{domain} web", "text": "Somos un despacho", "tools": []} if domain else {}
    monkeypatch.setattr(ap, "read_site", fake_read_site)


@pytest.mark.asyncio
async def test_spread_takes_one_per_industry_first(state):
    for i in range(3):
        await _add(state, f"Abogados {i}", "Abogados", f"abog{i}.es")
    await _add(state, "Yoga 0", "Yoga", "yoga0.es")
    await _add(state, "Dental 0", "Dentistas", "dent0.es")
    picked = await ap.companies_needing_approach(state, 3, spread=True)
    assert sorted(c["industry"] for c in picked) == ["Abogados", "Dentistas", "Yoga"]


@pytest.mark.asyncio
async def test_draft_saves_parsed_approaches_and_skips_done(state):
    await _add(state, "Despacho Sol", "Abogados", "despachosol.es",
               notes="Notas: ya tienen a alguien que les lleva la web")
    await _add(state, "Peluquería Luna", "Peluquerías", notes="Web en la lista: instagram.com/luna")
    brain = FakeBrain()
    report = await ap.draft_approaches(state, brain, FakeConfig, limit=10, batch=5)
    assert (report.candidates, report.drafted, report.calls, report.sites_read) == (2, 2, 1, 1)
    prompt = brain.prompts[0]
    assert "ya tienen a alguien" in prompt          # CRM notes reach the model
    assert "no tiene web" in prompt                 # a company without a site says so
    assert "Nadie va a enviar" in prompt

    rows = await ap.approach_rows(state)
    assert {r["empresa"] for r in rows} == {"Despacho Sol", "Peluquería Luna"}
    assert all(r["encaje"] == "alta" for r in rows)  # normalised from "ALTA"

    again = await ap.draft_approaches(state, FakeBrain(), FakeConfig, limit=10)
    assert again.candidates == 0                    # already drafted: not redone


@pytest.mark.asyncio
async def test_stops_when_over_budget(state):
    await _add(state, "Despacho Sol", "Abogados", "despachosol.es")
    report = await ap.draft_approaches(state, FakeBrain(within_budget=False), FakeConfig)
    assert report.drafted == 0 and report.calls == 0
    assert "usage limit" in report.stopped


def test_parse_rejects_unknown_ids_and_bad_fits():
    out = ap._parse({"approaches": [
        {"id": "a", "encaje": "regular", "mensaje": "hola"},
        {"id": "b", "encaje": "alta"},                      # no message, no opener
        {"id": "zzz", "encaje": "alta", "mensaje": "x"},     # not asked for
    ]}, {"a", "b"})
    assert list(out) == ["a"]
    assert out["a"]["encaje"] == "media"


@pytest.mark.asyncio
async def test_export_csv_and_xlsx(state, tmp_path):
    await _add(state, "Despacho Sol", "Abogados", "despachosol.es")
    await ap.draft_approaches(state, FakeBrain(fit="baja"), FakeConfig)
    rows = await ap.approach_rows(state)

    path = ap.write_export(rows, tmp_path / "enfoques.csv")
    raw = path.read_bytes()
    assert raw.startswith(b"\xef\xbb\xbf")  # BOM: Excel in Spanish opens it as UTF-8
    lines = list(csv.reader(raw.decode("utf-8-sig").splitlines(), delimiter=";"))
    assert lines[0][0] == "Empresa" and lines[1][0] == "Despacho Sol"

    openpyxl = pytest.importorskip("openpyxl")
    xlsx = ap.write_export(rows, tmp_path / "enfoques.xlsx")
    ws = openpyxl.load_workbook(xlsx).active
    assert ws["A1"].value == "Empresa" and ws["A2"].value == "Despacho Sol"

    assert await ap.approach_rows(state, fit="alta") == []
    assert len(await ap.approach_rows(state, fit="alta,baja")) == 1
    assert json.loads(json.dumps(rows))  # plain data, nothing exotic


def test_merge_vars_become_the_real_name():
    payload = {"mensaje": "{{company}} trabaja en el Eixample. Hola {{first_name}}, ¿sabéis?",
               "asunto": "inmobiliarias"}
    out = ap.fill_merge_vars(payload, "Dinmo | Inmobiliaria Barcelona")
    assert out["mensaje"] == "Dinmo trabaja en el Eixample. Hola ¿sabéis?"
    assert ap.short_name("CAPIA clínica de Psicología - Psicólogos en Barcelona") == "CAPIA clínica de Psicología"
    assert ap.short_name("Talleres GA-ES") == "Talleres GA-ES"  # no spaces around the dash
