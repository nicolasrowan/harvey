"""The profile collector on Spanish small-business sites: team pages under
Spanish names, two surnames, Spanish titles, owners named in running text,
Spanish agency credits and booking buttons, WordPress sitemap indexes."""

import httpx
import pytest

from harvey.collectors.profile import (
    ProfileCollector, is_decision_maker_title, name_in_domain, plausible_name,
)


@pytest.fixture
def col():
    return ProfileCollector(state=None, confirmed=set())


def test_team_page_two_surnames_and_spanish_titles(col):
    html = """
    <h3>María de la Fuente</h3><p>Socia fundadora</p>
    <h3>José Luis Pérez García</h3><p>Abogado asociado</p>
    <h3>Nuestros Servicios</h3><p>Derecho civil</p>
    """
    people = {f"{p['first_name']} {p['last_name']}": p for p in col.detect_people(html)}
    assert set(people) == {"María de la Fuente", "José Luis Pérez García"}
    assert people["María de la Fuente"]["is_decision_maker"] is True
    # "asociado" contains "socia" but an associate is not a partner
    assert people["José Luis Pérez García"]["is_decision_maker"] is False


def test_honorific_heading_is_a_role_of_its_own(col):
    people = col.detect_people("<h2>Dra. Ana Ruiz</h2><p>Especialista en ortodoncia infantil</p>")
    assert [(p["first_name"], p["last_name"], p["title"]) for p in people] == [
        ("Ana", "Ruiz", "Doctora")]
    assert people[0]["is_decision_maker"] is False  # a doctor, not necessarily the owner


def test_owner_named_in_running_text(col):
    home = """
    <section><p>Soy Lucía Martín, fisioterapeuta colegiada en Vigo.</p></section>
    <footer><p>Fundadora: Carmen Vidal</p><p>Soy Fisioterapeuta Colegiada</p></footer>
    """
    people = {f"{p['first_name']} {p['last_name']}": p for p in col.detect_owner_mentions(home)}
    assert set(people) == {"Lucía Martín", "Carmen Vidal"}
    assert all(p["is_decision_maker"] for p in people.values())


def test_the_person_in_the_domain_is_the_business(col):
    assert name_in_domain("Daniel López", "daniellopezart.com")
    assert not name_in_domain("Daniel Ruiz", "daniellopezart.com")
    people = col.detect_owner_mentions("<h1>Dr. Daniel López</h1>", "daniellopezart.com")
    assert people[0]["is_decision_maker"] is True


def test_names_are_not_roles_or_buttons():
    assert plausible_name("Krystel Cano")
    assert not plausible_name("Pide Cita")
    assert not plausible_name("Fisioterapeuta Colegiada")
    assert not plausible_name("Krystel")


def test_spanish_decision_titles():
    for title in ("Gerente", "Socio director", "Fundadora", "Directora general", "Titular"):
        assert is_decision_maker_title(title), title
    for title in ("Abogado asociado", "Recepcionista", "Higienista dental"):
        assert not is_decision_maker_title(title), title


def test_spanish_agency_credit(col):
    html = ('<footer><p>© 2026 Clínica Sol · Diseño web por '
            '<a href="https://agenciax.es">Agencia X</a></p></footer>')
    name, conf, _ = col.detect_agency(html, "clinicasol.es")
    assert name == "Agencia X" and conf >= 0.75


def test_nav_link_follows_the_sites_own_menu(col):
    home = ('<nav><a href="/">Inicio</a><a href="/la-doctora/">La doctora</a>'
            '<a href="https://www.clinicasol.es/contacto">Contacto</a>'
            '<a href="https://facebook.com/x">Facebook</a></nav>')
    site = "https://clinicasol.es"
    assert col.nav_link(site, home, "team") == "https://clinicasol.es/la-doctora/"
    assert col.nav_link(site, home, "contact") == "https://www.clinicasol.es/contacto"
    assert col.nav_link(site, home, "careers") == ""
    assert col.page_language('<html lang="es-ES">') == "es"


@pytest.mark.asyncio
async def test_profile_company_on_a_spanish_wordpress_site():
    pages = {
        "/": '<html lang="es"><body><a href="/quienes-somos/">Quiénes somos</a>'
             '<a class="btn" href="/reservas">Pide cita</a>'
             '<link href="/wp-content/themes/x.css"></body></html>' + " " * 600,
        "/quienes-somos/": "<h2>Marta Prieto</h2><p>Gerente</p>" + " " * 600,
        "/sitemap_index.xml": "<sitemapindex><sitemap><loc>https://ejemplo.es/page-sitemap.xml</loc>"
                              "</sitemap></sitemapindex>",
        "/page-sitemap.xml": "<urlset>" + "".join(
            f"<url><loc>https://ejemplo.es/p{i}</loc><lastmod>2024-01-0{i}</lastmod></url>"
            for i in range(1, 6)) + "</urlset>",
    }

    def handler(request):
        body = pages.get(request.url.path)
        if body is None:
            return httpx.Response(404, headers={"content-type": "text/html"}, text="no")
        ctype = "application/xml" if request.url.path.endswith(".xml") else "text/html"
        return httpx.Response(200, headers={"content-type": ctype}, text=body)

    confirmed = {"CONTACT_FOUND", "DECISION_MAKER_TITLE", "NO_ONLINE_BOOKING",
                 "TECH_STACK", "SITE_PAGE_COUNT", "BLOG_STALE"}
    collector = ProfileCollector(state=None, confirmed=confirmed)
    collector._client = httpx.AsyncClient(transport=httpx.MockTransport(handler),
                                          follow_redirects=True)
    try:
        obs = await collector.profile_company("c1", "ejemplo.es")
    finally:
        await collector._client.aclose()
    got = {(o["signal_code"], o["value_text"] or o["value_num"]) for o in obs}
    assert ("DECISION_MAKER_TITLE", "Marta Prieto — Gerente") in got
    assert ("NO_ONLINE_BOOKING", 0) in got        # "Pide cita" is a booking button
    assert ("TECH_STACK", "WordPress") in got
    assert ("SITE_PAGE_COUNT", 5) in got          # pages, not the one child sitemap
    stale = next(o for o in obs if o["signal_code"] == "BLOG_STALE")
    assert "2024-01-05" in stale["value_text"]


def test_theme_demo_team_is_not_people(col):
    html = """<h3>Luke Jacobs</h3><p>Marketing Manager</p><p>Lorem ipsum dolor sit amet,
    consectetur adipisicing elit. hello@company.com</p>
    <h3>Ana Ruiz</h3><p>Gerente</p><p>Lleva el restaurante desde 2010.</p>"""
    assert [p["first_name"] for p in col.detect_people(html)] == ["Ana"]


@pytest.mark.asyncio
async def test_placeholder_content_and_owner_in_domain_are_signals():
    pages = {
        "/": '<html lang="es"><a href="/equipo/">Equipo</a>' + " " * 600 + "</html>",
        "/equipo/": "<h3>Inma Costa</h3><p>Equipo</p><h3>Luke Jacobs</h3><p>Marketing Manager</p>"
                    "<p>Lorem ipsum dolor sit amet</p>" + " " * 600,
    }

    def handler(request):
        body = pages.get(request.url.path)
        return (httpx.Response(200, headers={"content-type": "text/html"}, text=body) if body
                else httpx.Response(404, headers={"content-type": "text/html"}, text="no"))

    collector = ProfileCollector(state=None, confirmed={
        "PLACEHOLDER_CONTENT", "CONTACT_FOUND", "DECISION_MAKER_TITLE"})
    collector._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        obs = await collector.profile_company("c1", "inmacosta.com")
    finally:
        await collector._client.aclose()
    got = {(o["signal_code"], o["value_text"]) for o in obs}
    assert ("PLACEHOLDER_CONTENT", "«Lorem ipsum»") in got
    assert ("DECISION_MAKER_TITLE", "Inma Costa — Titular") in got
    assert not any("Luke" in (o["value_text"] or "") for o in obs)
