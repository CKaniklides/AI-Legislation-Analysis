"""Verb lexicon: families of Dutch action verbs and their antonym axes (user-maintained)."""

import re

# ---------------------------------------------------------------------------
# Lexicon
# ---------------------------------------------------------------------------

_V_SUFFIX = r"(?:en|t|e|d|de|te)?"            # verb forms only (meld, melden, meldt, gemeld)
_N_SUFFIX = r"(?:en|t|e|d|de|te|ing|ingen)?"  # verb forms + the action noun (melding)


def _v(*stems: str) -> str:
    """Inflected verb forms of one or more stems, with an optional ge- participle prefix.
    Nouns are excluded on purpose where the noun is usually the OBJECT of another verb
    ("de vergunning intrekken" must not count as authorising)."""
    alt = "|".join(re.escape(s) for s in stems)
    return rf"\b(?:ge)?(?:{alt}){_V_SUFFIX}\b"


def _n(*stems: str) -> str:
    """Like _v, but also matches the -ing action noun (melding, bewaring, vernietiging)."""
    alt = "|".join(re.escape(s) for s in stems)
    return rf"\b(?:ge)?(?:{alt}){_N_SUFFIX}\b"


# family -> list of regexes (raw strings are for separable verbs and fixed phrases).
FAMILIES: dict[str, list[str]] = {
    # --- information flow ---------------------------------------------------------------
    "notify": [
        _n("meld"), _n("notificeer", "notificer"), _n("rapporteer", "rapporter"),
        _v("informeer", "informer"), r"\bge[iï]nformeerd\b", _n("waarschuw"),
        r"\bkennis(?:geving|geven|gegeven|geeft)\b",
        r"\bin\s+kennis\s+(?:te\s+)?(?:stel(?:len|t)|gesteld)\b",
        r"\bmede(?:delen|deelt|gedeeld|deling(?:en)?)\b",
    ],
    "provide": [
        _n("verstrekk", "verstrek"), _n("verschaff", "verschaf"),
        r"\bindienen\b", r"\bingediend\b", r"\bin\s+te\s+dienen\b", r"\bindiening\b",
        r"\btoe(?:zenden|zendt|gezonden|zending)\b", r"\b(?:doen|doet)\s+toekomen\b",
        _v("aanlever"), r"\baangeleverd\b",
        r"\b(?:ter\s+beschikking|beschikbaar)\s+(?:te\s+)?(?:stel(?:len|t)|gesteld)\b",
        r"\bvoorleg(?:gen|t)\b", r"\bvoorgelegd\b",
    ],
    "publish": [
        r"\bopenbaar\s+(?:te\s+)?(?:maken|maakt|gemaakt)\b",
        r"\bbekend\s*(?:te\s+)?(?:maken|maakt|gemaakt)\b", r"\bbekendmak(?:en|t|ing)\b",
        r"\bbekendgemaakt\b", _n("publiceer", "publicer"),
    ],
    "transfer": [
        _n("overdrag"), r"\bovergedragen\b", r"\bdoorgeven\b", r"\bdoorgegeven\b",
        r"\bdoorgifte\b", _n("verzend"), r"\bverzonden\b",
        r"\bdoorzend(?:en|t)\b", r"\bdoorgezonden\b",
    ],
    "access": [
        _v("raadpleeg", "raadpleg"), r"\binzien\b", r"\bingezien\b", r"\binzage\b", r"\binziet\b",
        r"\btoegang\s+(?:te\s+)?(?:hebben|heeft|krijgen|krijgt|verkrijgen|verkrijgt|verkregen)\b",
    ],
    "withhold": [   # NEGATIVE of notify / provide / publish / transfer
        _v("achterhoud"), r"\bachtergehouden\b", _v("verzwijg"), r"\bverzwegen\b",
        r"\bgeheimhoud\w*", r"\bgeheimgehouden\b", _v("onthoud"), r"\bonthouden\b",
        r"\bvertrouwelijk\s+(?:te\s+)?(?:houden|houdt|gehouden)\b",
        r"\bvertrouwelijk\s+(?:te\s+)?(?:behandel\w*|behandeld)\b",
    ],
    "deny_access": [   # NEGATIVE of access
        r"\btoegang\s+(?:te\s+)?(?:weiger(?:en|t)|geweigerd|ontzeg(?:gen|t)|ontzegd|beperk(?:en|t)|"
        r"beperkt|blokkeer(?:t)?|blokkeren|geblokkeerd)\b",
    ],
    # --- data lifecycle -----------------------------------------------------------------
    "collect": [
        _n("verzamel"), r"\binwinn?(?:en|t)\b", r"\bingewonnen\b", r"\binwinning\b",
        _v("vergaar", "vergar"), r"\bverkrijg(?:en|t|ing)\b", r"\bverkregen\b",
    ],
    "retain": [
        _n("bewaar", "bewar"), r"\bopslaan\b", r"\bopgeslagen\b", r"\bopslag\b",
        _n("archiveer", "archiver"), _n("behoud"), _v("conserveer", "conserver"),
    ],
    "erase": [   # NEGATIVE of retain
        _n("wiss", "wis"), _n("verwijder"), _n("vernietig"),
        _v("anonimiseer", "anonimiser"),   # "schrappen" moved to amend
    ],
    "use": [
        _v("gebruik"), _n("verwerk"), _v("benut"), _v("inzett", "inzet"), r"\bingezet\b",
        _v("exploiteer", "exploiter"),
    ],
    # --- permission lifecycle -----------------------------------------------------------
    "authorise": [
        _v("goedkeur"), _v("machtig"), _v("vergunn"), _v("autoriseer", "autoriser"),
        _v("accepteer", "accepter"),
        r"\btoe(?:staan|staat|gestaan)\b", r"\btoegelaten\b", r"\btoelaten\b", r"\btoelaat\b",
        r"\btoelating\b", r"\btoeken(?:nen|t|ning)\b", r"\btoegekend\b",
        r"\b(?:vergunning|toestemming|toegang|machtiging|goedkeuring|licentie)\s+(?:te\s+)?"
        r"(?:verlen(?:en|ing)|verleent|verleend)\b",
        r"\btoestemming\s+(?:te\s+)?(?:geven|geeft|gegeven)\b",
    ],
    "deny": [   # NEGATIVE of authorise
        _v("weiger"), r"\bweigering\b", _v("afwijz"), r"\bafgewezen\b", r"\bintrek(?:ken|t|king)\b",
        r"\bingetrokken\b", _v("herroep"), _v("annuleer", "annuler"), _v("ontneem", "ontnem"),
        r"\bontnomen\b", _v("ontzegg", "ontzeg"), _v("blokkeer", "blokker"), _v("verhinder"),
        _v("uitsluit"), r"\buitgesloten\b",
    ],
    "require": [
        r"\bvereis(?:en|t)\b", r"\bvoorschrijv(?:en|t)\b", r"\bvoorgeschreven\b",
        r"\bopleg(?:gen|t)\b", r"\bopgelegd\b", r"\bverplicht(?:en|t)\b",
    ],
    "exempt": [   # NEGATIVE of require
        r"\bvrijstel(?:len|t|ling)\b", r"\bvrijgesteld\b", r"\bontheff(?:en|t|ing)\b",
        r"\bontheven\b", r"\bontslag(?:en|t)\s+van\s+(?:de\s+)?(?:verplichting|plicht)\b",
    ],
    # --- operation and supply -----------------------------------------------------------
    "operate": [
        _v("voortzett", "voortzet"), r"\bvoortgezet\b", _v("continueer", "continuer"),
        _v("activeer", "activer"), _v("start"),
        r"\bin\s+werking\s+(?:te\s+)?(?:stel(?:len|t)|gesteld)\b",
        r"\bin\s+bedrijf\s+(?:te\s+)?(?:hou(?:den|dt)|gehouden|stel(?:len|t)|gesteld)\b",
    ],
    "halt": [   # NEGATIVE of operate
        _n("opschort"), r"\bopgeschort\b", r"\bstopzet(?:ten|t|ting)\b", r"\bstopgezet\b",
        r"\bbe[eë]indig(?:en|t|d|de|ing)?\b", r"\bstil(?:legg?en|legt|legging|gelegd)\b",
        _v("stopp", "stop"), _n("staak", "stak"), r"\bonderbrek(?:en|t|ing)\b", r"\bonderbroken\b",
        _v("deactiveer", "deactiver"), r"\buitschakel(?:en|t|ing)\b", r"\buitgeschakeld\b",
    ],
    "market": [
        r"\bin\s+de\s+handel\s+(?:te\s+)?(?:brengen|brengt|gebracht)\b",
        r"\bop\s+de\s+markt\s+(?:te\s+)?(?:brengen|brengt|gebracht|aanbieden|aanbiedt|aangeboden)\b",
        r"\bin\s+gebruik\s+(?:te\s+)?(?:stel(?:len|t)|gesteld|nemen|neemt|genomen)\b",
        _n("lever"), r"\baanbied(?:en|t)\b", r"\baangeboden\b",
    ],
    "withdraw": [   # NEGATIVE of market
        r"\buit\s+de\s+handel\s+(?:te\s+)?(?:nemen|neemt|genomen)\b",
        r"\bvan\s+de\s+markt\s+(?:te\s+)?(?:halen|haalt|gehaald|nemen|neemt|genomen)\b",
        r"\buit\s+(?:de\s+)?(?:gebruik|omloop)\s+(?:te\s+)?(?:nemen|neemt|genomen|halen|haalt|gehaald)\b",
        r"\bbuiten\s+gebruik\s+(?:te\s+)?(?:stel(?:len|t)|gesteld)\b",
        r"\bterugroep\w*", r"\bteruggeroepen\b", r"\bterugnem(?:en|t)\b", r"\bteruggenomen\b",
    ],
    # --- institutions -------------------------------------------------------------------
    "appoint": [
        _n("aanwijz", "aanwijs"), r"\baangewezen\b", _n("benoem"), _v("aanstel"), r"\baangesteld\b",
        r"\binstellen\b", r"\bingesteld\b", r"\boprichten\b", r"\bopgericht\b", r"\boprichting\b",
    ],
    "dismiss": [   # NEGATIVE of appoint
        r"\bontsla(?:an|at)\b", r"\bontslagen\b", r"\bafzet(?:ten|t|ting)\b", r"\bafgezet\b",
        r"\broyeer\w*", r"\broyer\w*", _n("opheff"), r"\bopgeheven\b", _v("ontbind"),
        r"\bontbonden\b", r"\bafschaff?(?:en|t|ing)\b", r"\bafgeschaft\b",
    ],
    # --- magnitude ----------------------------------------------------------------------
    "increase": [
        _n("verhoog", "verhog"), _n("vergroot", "vergrot"), r"\buitbreid(?:en|t|ing)\b",
        _v("versterk"), _v("intensiveer", "intensiver"),
    ],
    "decrease": [   # NEGATIVE of increase
        _n("verlaag", "verlag"), _n("verminder"), _v("verklein"), _n("beperk"), _v("inperk"),
        _v("reduceer", "reducer"), _v("verzwakk", "verzwak"),
    ],
    "extend": [_n("verleng")],
    "shorten": [_n("verkort", "bekort")],   # NEGATIVE of extend
    # --- weak / generic duty verbs (duplicate signal only; no antonym) -------------------
    "ensure": [
        _v("waarborg"), _v("garandeer", "garander"), r"\bzorg(?:en|t|dragen|draagt)\b",
        r"\bzorg\s+(?:te\s+)?dragen\b", r"\bzorgdrag\w*", r"\bziet\s+erop\s+toe\b", r"\btoezien\b",
    ],
    "measure": [
        r"\btreff(?:en|t)\b", r"\bgetroffen\b", _n("implementeer", "implementer"),
        r"\bimplementatie\b", r"\binvoeren\b", r"\bingevoerd\b", r"\btoepassen\b", r"\btoegepast\b",
    ],
    "assess": [
        _n("controleer", "controler"), _n("toets"), _n("beoordeel", "beoordel"),
        _n("evalueer", "evaluer"), _v("inspecteer", "inspecter"), _v("monitor"),
        r"\btoezicht\s+(?:te\s+)?(?:houden|houdt|gehouden)\b", r"\bverifi[eë]r(?:en|t)\b",
        r"\bgeverifieerd\b", _v("test"), r"\bonderzoek(?:en|t)\b", r"\bonderzocht\b",
    ],
    "record": [
        _v("documenteer", "documenter"), _n("registreer", "registrer"), r"\bregistratie\b",
        r"\bvastleg(?:gen|t|ging)\b", r"\bvastgelegd\b", r"\bbijhoud(?:en|t)\b", r"\bbijgehouden\b",
        _v("noteer", "noter"), r"\bdocumentatie\b",
    ],
    # --- legislative acts -----------------------------------------------------------------
    "adopt": [
        # "vaststellen dat ..." means establishing a fact, not adopting an act.
        r"\bvast(?:stellen|stelt)\b(?!\s+(?:dat|of|welke)\b)", r"\bvastgesteld\b(?!\s+(?:dat|of)\b)",
        r"\bvaststelling\b", r"\bvast\s+te\s+stellen\b(?!\s+(?:dat|of|welke)\b)",
        r"\buitvaardig(?:en|t|ing)\b", r"\buitgevaardigd\b",
    ],
    "amend": [
        _n("wijzig"), r"\baanvul(?:len|t|ling|lingen)\b", r"\baangevuld\b",
        r"\btoevoeg(?:en|t|ing)\b", r"\btoegevoegd\b",
        r"\baanpass(?:en|ing|ingen)\b", r"\baanpast\b", r"\baangepast\b",
        _v("actualiseer", "actualiser"), r"\bbijwerk(?:en|t)\b", r"\bbijgewerkt\b",
        r"\bherzi(?:en|et|ening)\b", _v("schrapp", "schrap"),
    ],
    # --- compliance and consideration (generic) ------------------------------------------
    "comply": [
        # \b after the form keeps "voldoende" (sufficient) out.
        r"\bvol(?:doen|doet|daan|deed|deden)\b",
        r"\bnalev(?:en|ing)\b", r"\bnaleeft\b", r"\bnageleefd\b",
        r"\bnakom(?:en|t|ing)\b", r"\bnagekomen\b",
        r"\bin\s+overeenstemming\s+(?:te\s+)?(?:brengen|brengt|gebracht)\b",
    ],
    "consider": [
        r"\brekening\s+(?:te\s+)?(?:houden|houdt|gehouden)\s+met\b",
        r"\bin\s+(?:aanmerking|acht|overweging)\s+(?:te\s+)?(?:nemen|neemt|genomen)\b",
        _v("overweeg", "overweg"), r"\boverwogen\b",
    ],
    # --- producing and shaping ----------------------------------------------------------------
    "produce": [
        r"\bopstel(?:len|t)\b", r"\bopgesteld\b", _v("ontwikkel"),
        r"\bontwerp(?:en|t)\b", r"\bontworpen\b", r"\buitwerk(?:en|t)\b", r"\buitgewerkt\b",
        r"\bopmak(?:en)\b", r"\bopmaakt\b", r"\bopgemaakt\b", r"\bopzet(?:ten)?\b", r"\bopgezet\b",
    ],
    "subject": [
        # the plural noun "onderwerpen" (topics) is excluded by requiring a following "aan".
        r"\bonderwerp(?:en|t)\b(?=(?:\s+\S+){0,8}?\s+aan\b)", r"\bonderworpen\b",
    ],
    "condition": [
        r"\bafhankelijk\s+(?:te\s+)?(?:stel(?:len|t)|gesteld)\s+van\b",
        r"\bvoorwaarden\s+(?:te\s+)?(?:verbinden|verbindt|verbonden)\b",
    ],
    "contain": [
        _v("bevatt", "bevat"), _v("omvatt", "omvat"),
        r"\bvergezeld\s+van\b", r"\bvoorzien\s+van\b",
        # "opgenomen" only as a verb ("worden opgenomen in"); "de in bijlage III opgenomen
        # systemen" is a reference. "contact opnemen" is getting in touch.
        r"(?<!contact\s)\bop(?:nemen|neemt)\b", r"\bop\s+te\s+nemen\b",
        r"\bopgenomen\b(?=\s*(?:$|[,.;:]|in\b|worden\b|wordt\b))",
        r"\binbouw(?:en|t)\b", r"\bingebouwd\b",
        _v("integreer", "integrer"), r"\bge[iï]ntegreerd\b",
    ],
}

# Every family is a literal on some axis. Positive families are (own name, +1); the rest are
# the NEGATIVE literal of one or more positive axes. One negative family may negate several
# axes (withholding information negates notifying, providing, publishing and transferring).
NEGATIVE_LITERALS: dict[str, list[tuple[str, int]]] = {
    "withhold": [("notify", -1), ("provide", -1), ("publish", -1), ("transfer", -1)],
    "erase": [("retain", -1)],
    "deny": [("authorise", -1)],
    "deny_access": [("access", -1)],
    "halt": [("operate", -1)],
    "withdraw": [("market", -1)],
    "exempt": [("require", -1)],
    "dismiss": [("appoint", -1)],
    "decrease": [("increase", -1)],
    "shorten": [("extend", -1)],
}
LITERALS: dict[str, list[tuple[str, int]]] = {
    fam: NEGATIVE_LITERALS.get(fam, [(fam, +1)]) for fam in FAMILIES
}

# Axes where a bare same-sign verb match proves very little ("ensure", "assess" appear in
# half of all duties). Duplicates on these need real object overlap; contradictions still need it too.
GENERIC_AXES = {"ensure", "measure", "assess", "record", "use",
                "adopt", "amend", "comply", "consider", "produce", "subject", "contain"}

# Different axes that describe neighbouring acts. Same-sign hits across them are a WEAK
# duplicate signal ("related_verbs"); they never produce a contradiction.
RELATED_AXES = {
    frozenset(p) for p in [
        ("notify", "provide"), ("provide", "publish"), ("provide", "transfer"),
        ("transfer", "publish"), ("retain", "record"), ("collect", "record"),
        ("access", "assess"), ("authorise", "require"), ("operate", "market"),
        ("adopt", "amend"), ("produce", "record"), ("contain", "record"),
        ("comply", "require"), ("condition", "require"), ("condition", "authorise"),
    ]
}