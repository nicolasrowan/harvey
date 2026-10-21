"""PEOPLE: a decision maker from search results, only when the result is
about that very company."""

import pytest
import pytest_asyncio

from harvey import people
from harvey.models.company import Company
from harvey.priority import rank_companies
from harvey.signals import seed_signal_catalog
from harvey.state import StateManager

FINCAS = {"id": "c1", "name": "Fincas Eva | Inmobiliaria Barcelona",
          "domain": "fincaseva.com", "location": "Barcelona"}


def test_profile_titles_in_both_shapes():
    assert people.parse_profile_title(
        "Rubén Martín - Director comercial en Fincas Eva Servicios ... - LinkedIn"
    ) == ("Rubén Martín", "Director comercial", "Fincas Eva Servicios")
    assert people.parse_profile_title("Ana Ruiz - Gerente - Clínica Sol | LinkedIn") == (
        "Ana Ruiz", "Gerente", "Clínica Sol")
    assert people.parse_profile_title("Clínica Sol") == ("", "", "")


def test_distinctive_tokens_ignore_trade_and_town():
    assert people.distinctive_tokens("Fincas Eva | Inmobiliaria Barcelona", "fincaseva.com") == {
        "fincas", "fincaseva"}
    assert people.distinctive_tokens("Abogados de Familia", "") == set()


def test_findings_keep_only_this_company():
    results = [
        {"title": "Eva López - CEO Fincas Eva - LinkedIn", "url": "https://es.linkedin.com/in/eva",
         "snippet": ""},
        {"title": "Hector Martinez - Administrador Comercial de Fincas Eva Servicios ...",
         "url": "https://es.linkedin.com/in/hector", "snippet": ""},
        {"title": "Laura Gil - Gerente en Inmobiliaria Sol - LinkedIn",
         "url": "https://es.linkedin.com/in/laura", "snippet": ""},          # other company
        {"title": "Quiénes somos - Fincas Eva", "url": "https://www.fincaseva.com/quienes-somos/",
         "snippet": "Eva López, fundadora de Fincas Eva, empezó en el año 2000."},
        {"title": "Pide Cita - LinkedIn", "url": "https://es.linkedin.com/in/x", "snippet": "Fincas Eva"},
    ]
    found = {f.name: f for f in people.findings_from_results(results, FINCAS)}
    assert set(found) == {"Eva López", "Hector Martinez"}
    assert found["Eva López"].decision_maker is True
    assert found["Eva López"].source == "LinkedIn"
    assert found["Hector Martinez"].decision_maker is False   # "administrador comercial"


@pytest_asyncio.fixture
async def state(tmp_path):
    s = StateManager(db_path=str(tmp_path / "harvey.db"))
    await s.init_db()
    await seed_signal_catalog(s)
    yield s


@pytest.mark.asyncio
async def test_find_people_stores_observations_and_stops_early(state, monkeypatch):
    cid = await state.add_company(Company(name="Fincas Eva | Inmobiliaria Barcelona",
                                          domain="fincaseva.com", location="Barcelona"))
    queries = []

    async def fake_search(client, query):
        queries.append(query)
        return [{"title": "Eva López - CEO - Fincas Eva | LinkedIn",
                 "url": "https://es.linkedin.com/in/eva", "snippet": ""}]

    monkeypatch.setattr(people, "search", fake_search)
    company = {"id": cid, "name": "Fincas Eva | Inmobiliaria Barcelona",
               "domain": "fincaseva.com", "location": "Barcelona"}
    report = await people.find_people(state, [company, {"id": "g", "name": "Abogados de Familia"}],
                                      delay=(0, 0))
    assert (report.searched, report.found_decision_makers, report.skipped_generic) == (1, 1, 1)
    assert len(queries) == 1                    # the decision maker came in the first search

    rules = {"base": 40, "default_product": "", "rules": [
        {"if": {"signal": "DECISION_MAKER_TITLE"}, "points": 15, "reason": "Preguntar por {text}"}]}
    ranked = await rank_companies(state, rules)
    assert ranked[0].score == 55
    assert ranked[0].ask_for == "Eva López — CEO (según LinkedIn)"
    assert ranked[0].decision_maker

    # Searched once: not searched again.
    assert await people.companies_to_search(state, ranked, min_score=0) == []


def test_without_a_site_the_town_must_match_and_roles_must_be_roles():
    gym = {"id": "g", "name": "Opera Gym", "domain": "", "location": "Málaga"}
    results = [
        {"title": "Kitney Bisher - Gym Instructor - Opera Gym | LinkedIn",
         "url": "https://www.linkedin.com/in/k", "snippet": "Houston, Texas"},
        {"title": "Laura Gil - Gerente - Opera Gym | LinkedIn",
         "url": "https://es.linkedin.com/in/l", "snippet": "Málaga, Andalucía"},
    ]
    assert [f.name for f in people.findings_from_results(results, gym)] == ["Laura Gil"]
    piso = {"id": "p", "name": "Mi Piso en Madrid", "domain": "mipisoenmadrid.com"}
    odd = [{"title": "Luis Aguirre Rocca - Mi Piso - Mi Piso en Madrid | LinkedIn",
            "url": "https://es.linkedin.com/in/luis", "snippet": ""}]
    assert people.findings_from_results(odd, piso) == []
