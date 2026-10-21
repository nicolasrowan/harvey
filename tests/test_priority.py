"""PRIORITY: plain rules over signals and notes, every point with its reason."""

import pytest
import pytest_asyncio

from harvey import priority as pr
from harvey.models.company import Company
from harvey.signals import seed_signal_catalog
from harvey.state import StateManager

RULES = {
    "base": 40,
    "default_product": "Rowan Growth",
    "rules": [
        {"if": {"no_website": True}, "product": "Web a medida", "points": 15,
         "reason": "Sin web propia"},
        {"if": {"notes": "env[ií][eé].{0,20}info"}, "points": 15, "reason": "Pidió info"},
        {"if": {"notes": "no (le )?interesa"}, "points": -20, "reason": "No le interesa"},
        {"if": {"signal": "DECISION_MAKER_TITLE"}, "points": 15,
         "reason": "Preguntar por {text}"},
        {"if": {"signal": "RUNNING_GOOGLE_ADS", "value": 1}, "points": 10, "reason": "Paga Google Ads"},
        {"if": {"signal": "BLOG_STALE", "min": 365}, "points": 10, "reason": "Parada hace {value} días"},
        {"if": {"signal": "TECH_STACK", "text": "WordPress"}, "points": 5, "reason": "WordPress"},
        {"if": {"signal": "NO_ONLINE_BOOKING", "value": 1, "industry": "dent"},
         "points": 5, "reason": "Sin reserva online"},
        {"if": {"unreadable": True}, "points": -5, "reason": "Web ilegible"},
    ],
}


@pytest_asyncio.fixture
async def state(tmp_path):
    s = StateManager(db_path=str(tmp_path / "harvey.db"))
    await s.init_db()
    await seed_signal_catalog(s)
    yield s


async def _company(state, name, domain="", industry="Dentistas", notes="", profiled=True):
    cid = await state.add_company(Company(name=name, domain=domain, industry=industry, notes=notes))
    if profiled:
        await state.mark_company_profiled(cid)
    return cid


async def _obs(state, cid, code, num=None, text=""):
    await state.add_observations([{"company_id": cid, "signal_code": code, "collector": "profile",
                                   "value_num": num, "value_text": text}])


@pytest.mark.asyncio
async def test_rules_add_points_and_reasons(state):
    good = await _company(state, "Clínica Sol", "clinicasol.es",
                          notes="Notas: Responde la dueña, dice que le envíe la info")
    await _obs(state, good, "DECISION_MAKER_TITLE", text="Ana Ruiz — Gerente")
    await _obs(state, good, "RUNNING_GOOGLE_ADS", num=1)
    await _obs(state, good, "BLOG_STALE", num=700, text="newest content 2024-11-01")
    await _obs(state, good, "TECH_STACK", text="WordPress, Elementor")
    await _obs(state, good, "NO_ONLINE_BOOKING", num=1)

    cold = await _company(state, "Dental Norte", "dentalnorte.es", notes="Notas: no le interesa")
    await _obs(state, cold, "RUNNING_GOOGLE_ADS", num=0)

    ranked = await pr.rank_companies(state, RULES)
    by_name = {r.company["name"]: r for r in ranked}
    sol = by_name["Clínica Sol"]
    assert sol.score == 40 + 15 + 15 + 10 + 10 + 5 + 5 == 100
    assert "+15 Preguntar por Ana Ruiz — Gerente" in sol.reasons
    assert "+10 Parada hace 700 días" in sol.reasons
    assert sol.ask_for == "Ana Ruiz — Gerente"
    assert by_name["Dental Norte"].score == 20
    assert ranked[0].company["name"] == "Clínica Sol"


@pytest.mark.asyncio
async def test_product_and_no_website_and_unreadable(state):
    await _company(state, "Peluquería Luna", "", notes="Contacto: Marta\nWeb en la lista: instagram.com/luna")
    unread = await _company(state, "Dental Mudo", "dentalmudo.es")   # profiled, nothing observed
    await _company(state, "Dental Nuevo", "dentalnuevo.es", profiled=False)
    by_name = {r.company["name"]: r for r in await pr.rank_companies(state, RULES)}

    luna = by_name["Peluquería Luna"]
    assert luna.product == "Web a medida" and luna.score == 55
    assert luna.ask_for == "Marta (del CRM)"
    assert by_name["Dental Mudo"].score == 35 and "-5 Web ilegible" in by_name["Dental Mudo"].reasons
    assert by_name["Dental Nuevo"].score == 40           # not read yet: no penalty
    assert by_name["Dental Mudo"].product == "Rowan Growth"
    assert unread


@pytest.mark.asyncio
async def test_newest_value_wins_after_reprofiling(state):
    cid = await _company(state, "Clínica Sol", "clinicasol.es")
    await _obs(state, cid, "RUNNING_GOOGLE_ADS", num=1)
    await _obs(state, cid, "RUNNING_GOOGLE_ADS", num=0)   # stopped paying since
    ranked = await pr.rank_companies(state, RULES)
    assert ranked[0].score == 40


def test_score_is_clamped_and_reasons_unsigned_when_zero():
    rules = {"base": 95, "default_product": "", "rules": [
        {"if": {"industry": "dent"}, "points": 20, "reason": "Sector"},
        {"if": {"industry": "dent"}, "points": 0, "reason": "Solo informativo"}]}
    r = pr.score_company({"industry": "Dentistas"}, {}, rules)
    assert r.score == 100
    assert r.reasons == ["+20 Sector", "Solo informativo"]


@pytest.mark.asyncio
async def test_export_xlsx(state, tmp_path):
    openpyxl = pytest.importorskip("openpyxl")
    await _company(state, "Clínica Sol", "clinicasol.es")
    path = pr.write_ranking(await pr.rank_companies(state, RULES), tmp_path / "p.xlsx")
    ws = openpyxl.load_workbook(path).active
    assert ws["A1"].value == "Puesto" and ws["C2"].value == "Clínica Sol"


def test_growth_rules_file_is_valid():
    """The rules shipped with the growth profile load and every rule has a reason."""
    from harvey.paths import PROJECT_ROOT
    path = PROJECT_ROOT / "profiles" / "growth" / "priority.yaml"
    if not path.exists():
        pytest.skip("profiles repo not present")
    rules = pr.load_rules(path)
    assert rules["rules"] and all(r.get("reason") for r in rules["rules"])
    import re
    for r in rules["rules"]:
        for key in ("notes", "industry", "text"):
            if key in r["if"]:
                re.compile(r["if"][key])
