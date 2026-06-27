"""General country / demonym gazetteer — a language-agnostic literal normalizer.

Maps a country mentioned in a question (its English name, demonym, common
abbreviation, or a Russian / declined form) to its canonical English name(s), so
value-routing can match the literal against the DB's stored country values even
when the extraction model misreads or mistranslates it (e.g. a weak model that
reads Russian «Италии» as the literal "Iran").

This is GENERAL world knowledge, not per-DB data — countries are universal, so
this is a reusable normalizer, never a database-specific hardcode. Matching is
prefix-based for Cyrillic stems so noun declensions (Италия / Италии / Италию /
Италией) all resolve. The canonical list carries several surface forms per
country (USA / United States / US) so at least one matches the DB's chosen form.
"""
from __future__ import annotations

import re

# (canonical_forms_to_emit, detection_terms_lowercased)
# A detection term is matched as a whole token, OR — when it is a stem of length
# >= 4 — as a token prefix (so Cyrillic declensions resolve). Multiword terms are
# matched as a substring of the full text.
_COUNTRIES: list[tuple[list[str], list[str]]] = [
    (["Italy"], ["italy", "italian", "итали"]),
    (["Germany"], ["germany", "german", "герман"]),
    (["France"], ["france", "french", "франц"]),
    (["Spain"], ["spain", "spanish", "испан"]),
    (["UK", "United Kingdom", "Britain", "England", "Great Britain"],
     ["uk", "britain", "british", "england", "english", "британ", "англи"]),
    (["USA", "United States", "US", "America"],
     ["usa", "united states", "america", "american", "сша", "америк"]),
    (["Russia"], ["russia", "russian", "росси"]),
    (["Japan"], ["japan", "japanese", "япони"]),
    (["Brazil"], ["brazil", "brazilian", "бразил"]),
    (["Canada"], ["canada", "canadian", "канад"]),
    (["Australia"], ["australia", "australian", "австрал"]),
    (["Monaco"], ["monaco", "monegasque", "монак"]),
    (["Belgium"], ["belgium", "belgian", "бельги"]),
    (["Netherlands", "Holland"], ["netherlands", "dutch", "holland", "нидерланд", "голланд"]),
    (["Austria"], ["austria", "austrian", "австри"]),
    (["Mexico"], ["mexico", "mexican", "мексик"]),
    (["China"], ["china", "chinese", "кита"]),
    (["India"], ["india", "indian", "инди"]),
    (["Singapore"], ["singapore", "singaporean", "сингапур"]),
    (["Portugal"], ["portugal", "portuguese", "португал"]),
    (["Turkey"], ["turkey", "turkish", "турци"]),
    (["Bahrain"], ["bahrain", "bahraini", "бахрейн"]),
    (["Hungary"], ["hungary", "hungarian", "венгри"]),
    (["Azerbaijan"], ["azerbaijan", "azerbaijani", "азербайджан"]),
    (["Saudi Arabia"], ["saudi arabia", "saudi", "саудовск", "саудит"]),
    (["UAE", "United Arab Emirates", "Abu Dhabi"],
     ["uae", "united arab emirates", "emirates", "abu dhabi", "оаэ", "эмират"]),
    (["Qatar"], ["qatar", "qatari", "катар"]),
    (["South Africa"], ["south africa", "south african", "юар"]),
    (["Argentina"], ["argentina", "argentine", "argentinian", "аргентин"]),
    (["South Korea", "Korea"], ["south korea", "korea", "korean", "коре"]),
    (["Sweden"], ["sweden", "swedish", "швеци", "швед"]),
    (["Switzerland"], ["switzerland", "swiss", "швейцар"]),
    (["Malaysia"], ["malaysia", "malaysian", "малайзи"]),
    (["Finland"], ["finland", "finnish", "финлянд", "финск"]),
    (["Poland"], ["poland", "polish", "польш"]),
    (["Czech Republic", "Czechia"], ["czech", "чехи", "чешск"]),
    (["Denmark"], ["denmark", "danish", "дани", "датск"]),
    (["Norway"], ["norway", "norwegian", "норвеги", "норвежск"]),
    (["Ireland"], ["ireland", "irish", "ирланди"]),
    (["New Zealand"], ["new zealand", "новая зеланди", "новой зеланди", "зеланди"]),
    (["Indonesia"], ["indonesia", "indonesian", "индонези"]),
    (["Thailand"], ["thailand", "thai", "таиланд", "тайск"]),
    (["Vietnam"], ["vietnam", "vietnamese", "вьетнам"]),
    (["Egypt"], ["egypt", "egyptian", "египет", "египт"]),
    (["Greece"], ["greece", "greek", "греци", "греческ"]),
    (["Israel"], ["israel", "israeli", "израил"]),
]

_TOKEN = re.compile(r"[A-Za-zА-Яа-яЁё]+")


def extract_country_literals(text: str) -> set[str]:
    """Return canonical English country forms mentioned in ``text`` (any language
    / declension). Empty set if none. Never raises."""
    try:
        low = (text or "").lower()
        if not low:
            return set()
        tokens = _TOKEN.findall(low)
        token_set = set(tokens)
        found: set[str] = set()
        for canon, terms in _COUNTRIES:
            hit = False
            for term in terms:
                if " " in term:
                    if term in low:
                        hit = True
                        break
                    continue
                if term in token_set:
                    hit = True
                    break
                if len(term) >= 4:
                    if any(tok.startswith(term) for tok in tokens):
                        hit = True
                        break
            if hit:
                found.update(canon)
        return found
    except Exception:  # pylint: disable=broad-exception-caught
        return set()
