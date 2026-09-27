"""Match free-text job locations against the configured countries/cities.

Job sites write locations in many ways: "Berlin, Berlin, Germany", "DEU - Munich",
"München, BY, DE", "Lisboa, PT", "Remote - Germany".
City aliases come from GeoNames (via geonamescache) plus a multilingual table of
country names, plus whatever aliases the user adds in the config.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from functools import lru_cache

from ..config.schema import Coverage, LocationSpec
from ..models import normalize_text

# Country names in several languages, on top of the English names from GeoNames.
_COUNTRY_EXTRA: dict[str, list[str]] = {
    "ES": ["Spain", "España", "Espana", "Espagne", "Spanien", "Spagna", "Espanha"],
    "CH": ["Switzerland", "Suiza", "Suisse", "Schweiz", "Svizzera", "Suíça"],
    "MX": ["Mexico", "México", "Mexique", "Mexiko", "Messico"],
    "FR": ["France", "Francia", "Frankreich", "Francia"],
    "DE": ["Germany", "Alemania", "Allemagne", "Deutschland", "Germania"],
    "IT": ["Italy", "Italia", "Italie", "Italien"],
    "PT": ["Portugal", "Portogallo"],
    "GB": ["United Kingdom", "UK", "Great Britain", "England", "Reino Unido", "Royaume-Uni", "Inglaterra"],
    "US": ["United States", "USA", "U.S.", "Estados Unidos", "États-Unis", "Etats-Unis"],
    "NL": ["Netherlands", "Países Bajos", "Holanda", "Pays-Bas", "Niederlande"],
    "BE": ["Belgium", "Bélgica", "Belgique", "Belgien"],
    "LU": ["Luxembourg", "Luxemburgo", "Luxemburg"],
    "IE": ["Ireland", "Irlanda", "Irlande"],
    "AT": ["Austria", "Autriche", "Österreich"],
    "AE": ["United Arab Emirates", "UAE", "Emiratos Árabes Unidos", "Emirats Arabes Unis", "Dubai"],
    "MC": ["Monaco", "Mónaco"],
    "AD": ["Andorra", "Andorre"],
    "SG": ["Singapore", "Singapur", "Singapour"],
    "HK": ["Hong Kong"],
    "CO": ["Colombia", "Colombie"],
    "CL": ["Chile", "Chili"],
    "AR": ["Argentina", "Argentine"],
    "PE": ["Peru", "Perú", "Pérou"],
    "BR": ["Brazil", "Brasil", "Brésil"],
    "UY": ["Uruguay"],
    "PA": ["Panama", "Panamá"],
}
_REMOTE = re.compile(r"\b(remote|remoto|teletrabajo|t[ée]l[ée]travail|home ?office|work from home)\b", re.I)


@lru_cache(maxsize=1)
def _geonames() -> tuple[dict, dict]:
    import geonamescache

    gc = geonamescache.GeonamesCache()
    return gc.get_countries(), gc.get_cities()


@lru_cache(maxsize=1)
def country_aliases() -> dict[str, set[str]]:
    """ISO2 -> normalized aliases (names in several languages)."""
    countries, _ = _geonames()
    out: dict[str, set[str]] = {}
    for iso2, c in countries.items():
        out.setdefault(iso2, set()).add(normalize_text(c["name"]))
    for iso2, names in _COUNTRY_EXTRA.items():
        out.setdefault(iso2, set()).update(normalize_text(n) for n in names)
    return out


@lru_cache(maxsize=1)
def _iso3() -> dict[str, str]:
    countries, _ = _geonames()
    return {iso2: c["iso3"] for iso2, c in countries.items()}


def country_name(iso2: str) -> str:
    """English country name (e.g. DE -> Germany)."""
    countries, _ = _geonames()
    return countries.get(iso2, {}).get("name", iso2)


def resolve_country(value: str | None) -> str | None:
    if not value:
        return None
    v = value.strip()
    if len(v) == 2 and v.upper() in country_aliases():
        return v.upper()
    if len(v) == 3:
        for iso2, iso3 in _iso3().items():
            if iso3 == v.upper():
                return iso2
    nv = normalize_text(v)
    for iso2, names in country_aliases().items():
        if nv in names:
            return iso2
    return None


def _latin(text: str) -> bool:
    return bool(text) and all(ord(ch) < 0x250 for ch in text)


@lru_cache(maxsize=512)
def city_aliases(city: str, country: str | None) -> frozenset[str]:
    """Normalized aliases for a city from GeoNames (Latin script, >= 4 chars)."""
    _, cities = _geonames()
    target = normalize_text(city)
    all_countries = set().union(*country_aliases().values())
    aliases = {target}
    for c in cities.values():
        if country and c["countrycode"] != country:
            continue
        names = [c["name"], *(c.get("alternatenames") or [])]
        if target not in {normalize_text(n) for n in names}:
            continue
        for n in names:
            nn = normalize_text(n)
            if _latin(n) and len(nn) >= 4 and nn not in all_countries:
                aliases.add(nn)
    return frozenset(aliases)


@lru_cache(maxsize=64)
def major_cities(country: str, min_population: int = 50000) -> frozenset[str]:
    """Names of the main cities of a country, so 'Hamburg' alone matches Germany."""
    _, cities = _geonames()
    out = set()
    for c in cities.values():
        if c["countrycode"] == country and c["population"] >= min_population:
            n = normalize_text(c["name"])
            if len(n) >= 4:
                out.add(n)
    return frozenset(out)


def _contains(haystack: str, needle: str) -> bool:
    return bool(needle) and re.search(rf"(?<!\w){re.escape(needle)}(?!\w)", haystack) is not None


def _codes_in(raw: str) -> set[str]:
    """ISO country codes written as codes: 'DEU-Berlin', 'Zurich, CH', 'BARCELONA, B, ES, 08028'.

    ISO3 codes count anywhere. Two-letter codes are ambiguous with region codes
    ('Geneva, GE, CH', 'Toronto, ON, CA'), so they only count as the first or last
    non-numeric part of the location.
    """
    iso3_to_2 = {v: k for k, v in _iso3().items()}
    out = {iso3_to_2[t] for t in re.findall(r"(?<![A-Za-z])[A-Z]{3}(?![A-Za-z])", raw) if t in iso3_to_2}
    parts = [p.strip() for p in re.split(r"[,;|/\-]", raw) if p.strip() and not p.strip().isdigit()]
    for tok in {parts[0], parts[-1]} if parts else set():
        if re.fullmatch(r"[A-Z]{2}", tok) and tok in country_aliases():
            out.add(tok)
    return out


def countries_in(raw: str) -> set[str]:
    norm = normalize_text(raw)
    found = {iso2 for iso2, names in country_aliases().items() if any(_contains(norm, n) for n in names)}
    return found or _codes_in(raw)


@dataclass
class _Target:
    country: str | None
    city_names: frozenset[str] = field(default_factory=frozenset)
    include_remote: bool = True
    label: str | None = None


class LocationMatcher:
    def __init__(self, specs: list[LocationSpec], coverage: Coverage | None = None):
        self.targets: list[_Target] = []
        for spec in specs:
            iso2 = resolve_country(spec.country) if spec.country else None
            names: frozenset[str] = frozenset()
            if spec.city:
                names = city_aliases(spec.city, iso2) | {normalize_text(a) for a in spec.aliases}
            elif spec.aliases and iso2:
                country_aliases()[iso2].update(normalize_text(a) for a in spec.aliases)
            self.targets.append(_Target(iso2, names, spec.include_remote, spec.city))
        self.coverage = coverage if coverage and coverage.enabled else None
        self._cov_terms = [normalize_text(t) for t in (self.coverage.text_any if self.coverage else [])]
        self._cov_cities = [normalize_text(c) for c in (self.coverage.only_cities if self.coverage else [])]

    @property
    def empty(self) -> bool:
        return not self.targets

    def hint_terms(self) -> list[str]:
        """Human-readable names used to pick server-side location facets."""
        terms: list[str] = []
        for t in self.targets:
            if t.city_names:
                terms.extend(sorted(t.city_names))
            elif t.country:
                terms.extend(sorted(country_aliases()[t.country]))
        return terms

    def countries(self) -> list[str]:
        """ISO2 codes of all configured targets (cities contribute their country)."""
        return list(dict.fromkeys(t.country for t in self.targets if t.country))

    def cities(self) -> list[str]:
        return [t.label for t in self.targets if t.label]

    def match_location(self, loc: str) -> bool:
        norm = normalize_text(loc)
        if not norm:
            return False
        mentioned = countries_in(loc)
        for t in self.targets:
            if t.city_names:
                if any(_contains(norm, n) for n in t.city_names) and (not mentioned or t.country in mentioned):
                    return True
                continue
            if t.country is None:
                continue
            if t.country in mentioned:
                if _REMOTE.search(loc) and not t.include_remote:
                    continue
                return True
            if not mentioned and any(_contains(norm, n) for n in major_cities(t.country)):
                return True
        return False

    def match_coverage(self, locations: list[str], text: str) -> bool:
        if not self._cov_terms:
            return False
        norm = normalize_text(text)
        if not any(_contains(norm, term) for term in self._cov_terms):
            return False
        if not self._cov_cities:
            return True
        locs = normalize_text(" ".join(locations))
        return any(_contains(locs, c) for c in self._cov_cities)

    def matches(self, locations: list[str], text: str = "") -> tuple[bool, bool]:
        """Returns (matches, via_coverage)."""
        if self.empty:
            return True, False
        if any(self.match_location(loc) for loc in locations):
            return True, False
        if self.match_coverage(locations, text):
            return True, True
        return False, False
