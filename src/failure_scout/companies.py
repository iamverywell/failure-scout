"""Company-name normalisation, shared by event merging and the company list.

"F. Hoffmann-La Roche Ltd", "Hoffmann-La Roche" and "Roche" -> "roche";
"Aldeyra Therapeutics, Inc." and "Aldeyra" -> "aldeyra"; "Bright Minds'" -> "bright minds"."""

from __future__ import annotations

import re
import unicodedata

# Legal-form and generic industry words dropped from the end (and anywhere for legal forms).
LEGAL = {"inc", "incorporated", "ltd", "limited", "llc", "plc", "corp", "corporation", "co",
         "company", "ag", "sa", "se", "nv", "bv", "gmbh", "kg", "spa", "srl", "as", "ab", "oy",
         "kk", "pty", "lp", "holdings", "holding", "group"}
GENERIC = {"therapeutics", "pharmaceuticals", "pharmaceutical", "pharma", "biosciences",
           "bioscience", "biotherapeutics", "biotechnology", "biotech", "biopharma",
           "biopharmaceuticals", "bio", "medicines", "sciences", "labs", "laboratories",
           "oncology", "international", "global", "usa", "us"}

# Names that normalisation alone can't unify. Extend as duplicates show up.
ALIASES = {
    "hoffmann la roche": "roche",
    "f hoffmann la roche": "roche",
    "genentech": "roche",  # Roche subsidiary; registry records often name Genentech
    "kite a gilead": "gilead",
    "kite": "gilead",
    "glaxosmithkline": "gsk",
    "bristol myers squibb": "bms",
    "bristol myers": "bms",
    "merck sharp dohme": "merck",
    "msd": "merck",
    "janssen": "johnson johnson",
    "j j": "johnson johnson",
    "sanofi aventis": "sanofi",
    "eli lilly": "lilly",
    "novo": "novo nordisk",
}


def company_key(name: str | None) -> str | None:
    if not name:
        return None
    s = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode().lower()
    s = re.sub(r"['’]s?\b", "", s)  # possessives: "Bright Minds'" / "Rhythm's"
    # Trailing appositive: "Kanyos Bio, Inc., a wholly owned subsidiary of ...", "Kite, A Gilead Company"
    s = re.sub(r",\s*(a|an)\s.*$", "", s)
    words = re.findall(r"[a-z0-9]+", s)
    words = [w for w in words if w not in LEGAL]
    while len(words) > 1 and words[-1] in GENERIC:
        words.pop()
    key = " ".join(words)
    if key in ALIASES:
        return ALIASES[key]
    for alias, target in ALIASES.items():  # alias as a prefix: "kite a gilead" + more words
        if key.startswith(alias + " "):
            return target
    return key or None


def drug_key(name: str | None) -> str | None:
    if not name:
        return None
    k = re.sub(r"[^a-z0-9]+", "", name.lower())
    return k or None
