"""IMPORT: companies from a CSV or Excel file, through the discovery door."""

import pytest
import pytest_asyncio

from harvey.collectors.importer import import_companies, map_columns, read_rows
from harvey.state import StateManager


@pytest_asyncio.fixture
async def state(tmp_path):
    s = StateManager(db_path=str(tmp_path / "harvey.db"))
    await s.init_db()
    yield s


def test_map_columns_spanish_and_english():
    mapping = map_columns(["Razón Social", "Página Web", "Teléfono", "Localidad", "SECTOR"])
    assert mapping == {
        "name": "Razón Social", "website": "Página Web", "phone": "Teléfono",
        "location": "Localidad", "category": "SECTOR",
    }
    assert map_columns(["Company", "Website"]) == {"name": "Company", "website": "Website"}


def test_read_rows_semicolon_csv_with_bom(tmp_path):
    f = tmp_path / "leads.csv"
    f.write_bytes("﻿Empresa;Web;Ciudad\nTransportes Miño;www.mino.es;Ourense\n".encode("utf-8"))
    rows = read_rows(f)
    assert rows == [{"Empresa": "Transportes Miño", "Web": "www.mino.es", "Ciudad": "Ourense"}]


@pytest.mark.asyncio
async def test_import_dedups_and_keeps_companies_without_web(state, tmp_path):
    f = tmp_path / "leads.csv"
    f.write_text(
        "empresa,web,ciudad,sector\n"
        "Transportes Miño,https://www.mino.es/,Ourense,Transporte\n"
        "Transportes Miño SL,mino.es,Ourense,Transporte\n"        # same domain
        "Bebidas Rías,,Vigo,Distribución de bebidas\n"            # no website
        "Páginas Amarillas,https://www.paginasamarillas.es/x,,\n"   # directory
        ",,,\n",                                                    # empty
        encoding="utf-8",
    )
    report = await import_companies(state, f, industry="Logística")
    assert report.rows == 4  # the all-empty line is dropped by the reader
    assert report.new_companies == 2
    assert report.known_companies == 1
    assert report.junk == 1

    mino = await state.get_company_by_domain("mino.es")
    assert mino.name == "Transportes Miño"
    assert mino.industry == "Transporte"
    assert mino.location == "Ourense"
    assert mino.source == "discover:import"

    again = await import_companies(state, f)
    assert again.new_companies == 0  # re-importing never duplicates, web or not


@pytest.mark.asyncio
async def test_import_without_name_or_web_column_explains(state, tmp_path):
    f = tmp_path / "x.csv"
    f.write_text("foo,bar\n1,2\n", encoding="utf-8")
    report = await import_companies(state, f)
    assert report.new_companies == 0
    assert "no name or website column" in report.errors[0]


@pytest.mark.asyncio
async def test_import_xlsx(state, tmp_path):
    openpyxl = pytest.importorskip("openpyxl")
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(["Nombre", "Web"])
    ws.append(["Frío Levante", "friolevante.es"])
    f = tmp_path / "leads.xlsx"
    wb.save(f)
    report = await import_companies(state, f)
    assert report.new_companies == 1
    assert (await state.get_company_by_domain("friolevante.es")).name == "Frío Levante"
