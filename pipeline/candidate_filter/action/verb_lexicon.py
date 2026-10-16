"""Verb lexicon: families of Dutch action verbs and their axes (user-maintained).

The pipeline matches these patterns against the `action_lemma` field (usually an
infinitive or short verb phrase), but most patterns also cover inflected forms so the
lexicon still works on running text.

Version 2 (recall-oriented):
  * new families for frequent norm verbs: cooperate, request, decide, perform, support,
    describe, possess, liable
  * bare-lemma forms added where the old patterns required a context word
    ("rekening houden", "onderwerpen", "verlenen", "vergezeld gaan")
  * joined lemma spellings ("aanstellen", "meedelen", "beschikbaarstellen", "openbaarmaken")
  * missing participles ("goedgekeurd", "ingeperkt", "uitgebreid")
  * a few false positives removed ("stak", bare noun "opzet")
Polarity is kept in LITERALS for reference, but the candidate scorer ignores it.
"""

import re

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_V_SUFFIX = r"(?:en|t|e|d|de|te)?"            # verb forms only (meld, melden, meldt, gemeld)
_N_SUFFIX = r"(?:en|t|e|d|de|te|ing|ingen)?"  # verb forms + the action noun (melding)


def _v(*stems: str) -> str:
    """Inflected verb forms of one or more stems, with an optional ge- participle prefix."""
    alt = "|".join(re.escape(s) for s in stems)
    return rf"\b(?:ge)?(?:{alt}){_V_SUFFIX}\b"


def _n(*stems: str) -> str:
    """Like _v, but also matches the -ing action noun (melding, bewaring, vernietiging)."""
    alt = "|".join(re.escape(s) for s in stems)
    return rf"\b(?:ge)?(?:{alt}){_N_SUFFIX}\b"


def _p(*phrases: str) -> str:
    """Fixed phrases; spaces match any whitespace, an optional 'te' may precede the last word."""
    out = []
    for ph in phrases:
        words = [re.escape(w) for w in ph.split()]
        if len(words) > 1:
            out.append(r"\s+".join(words[:-1]) + r"\s+(?:te\s+)?" + words[-1])
        else:
            out.append(words[0])
    return r"\b(?:" + "|".join(out) + r")\b"


# family -> list of regexes
FAMILIES: dict[str, list[str]] = {
    # --- information flow ---------------------------------------------------------------
    "notify": [
        _n("meld"), _n("notificeer", "notificer"), _n("rapporteer", "rapporter"),
        _v("informeer", "informer"), r"\bge[iï]nformeerd\b", _n("waarschuw"),
        r"\bkennis(?:geving|geven|gegeven|geeft)\b",
        r"\bin\s+kennis\s+(?:te\s+)?(?:stel(?:len|t)|gesteld)\b",
        r"\bmede(?:delen|deelt|gedeeld|deling(?:en)?)\b",
        # v2
        r"\bmee(?:delen|deelt)\b", r"\bmeegedeeld\b",
        r"\bop\s+de\s+hoogte\s+(?:te\s+)?(?:brengen|brengt|gebracht|stellen|stelt|gesteld|houden|houdt|gehouden)\b",
        _v("inlicht"), _v("aanmeld"), r"\baangemeld\b",
        r"\bverslag\s+(?:te\s+)?(?:uitbrengen|uitbrengt|uitgebracht|doen|doet|gedaan)\b",
        r"\bwijz(?:en|t)\s+op\b", r"\bwijst\s+op\b",
        r"\b(?:kennisgeving|melding|mededeling)\s+(?:te\s+)?(?:doen|doet|gedaan)\b",
    ],
    "provide": [
        _n("verstrekk", "verstrek"), _n("verschaff", "verschaf"),
        r"\bindienen\b", r"\bingediend\b", r"\bin\s+te\s+dienen\b", r"\bindiening\b",
        r"\btoe(?:zenden|zendt|gezonden|zending)\b", r"\b(?:doen|doet)\s+toekomen\b",
        _v("aanlever"), r"\baangeleverd\b",
        r"\b(?:ter\s+beschikking|beschikbaar)\s*(?:te\s+)?(?:stel(?:len|t)|gesteld)\b",
        r"\bvoorleg(?:gen|t)\b", r"\bvoorgelegd\b",
        # v2
        r"\bgeven\b", r"\bgeeft\b", r"\bgegeven\b", _v("bezorg"),
        _v("aflever"), r"\bafgeleverd\b", r"\bbieden\b", r"\bbiedt\b", r"\bgeboden\b",
        r"\boverleggen\b(?!\s+met\b)", r"\boverlegt\b(?!\s+met\b)", r"\boverlegd\b",
    ],
    "publish": [
        r"\bopenbaar\s*(?:te\s+)?(?:maken|maakt|gemaakt)\b",
        r"\bbekend\s*(?:te\s+)?(?:maken|maakt|gemaakt)\b", r"\bbekendmak(?:en|t|ing)\b",
        r"\bbekendgemaakt\b", _n("publiceer", "publicer"),
    ],
    "transfer": [
        _n("overdrag"), r"\bovergedragen\b", r"\bdoorgeven\b", r"\bdoorgegeven\b",
        r"\bdoorgifte\b", _n("verzend"), r"\bverzonden\b",
        r"\bdoorzend(?:en|t)\b", r"\bdoorgezonden\b",
        # v2
        r"\bdel(?:en|t)\b", r"\bgedeeld\b", _n("uitwissel"),
    ],
    "access": [
        _v("raadpleeg", "raadpleg"), r"\binzien\b", r"\bingezien\b", r"\binzage\b", r"\binziet\b",
        r"\btoegang\s+(?:te\s+)?(?:hebben|heeft|krijgen|krijgt|verkrijgen|verkrijgt|verkregen)\b",
    ],
    "withhold": [
        _v("achterhoud"), r"\bachtergehouden\b", _v("verzwijg"), r"\bverzwegen\b",
        r"\bgeheimhoud\w*", r"\bgeheimgehouden\b", _v("onthoud"), r"\bonthouden\b",
        r"\bvertrouwelijk\s+(?:te\s+)?(?:houden|houdt|gehouden)\b",
        r"\bvertrouwelijk\s+(?:te\s+)?(?:behandel\w*|behandeld)\b",
    ],
    "deny_access": [
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
        # v2
        r"\bter\s+beschikking\s+(?:te\s+)?(?:houden|houdt|gehouden)\b",
        r"\bin\s+stand\s+(?:te\s+)?(?:houden|houdt|gehouden)\b",
        r"\bhouden\b", r"\bhoudt\b",
    ],
    "erase": [
        _n("wiss", "wis"), _n("verwijder"), _n("vernietig"),
        _v("anonimiseer", "anonimiser"),
    ],
    "use": [
        _v("gebruik"), _n("verwerk"), _v("benut"), _v("inzett", "inzet"), r"\bingezet\b",
        _v("exploiteer", "exploiter"),
        # v2
        r"\bgebruik\s*(?:te\s+)?(?:maken|maakt|gemaakt)\b",
    ],
    # --- permission lifecycle -----------------------------------------------------------
    "authorise": [
        _v("goedkeur"), r"\bgoedgekeurd\b", _v("machtig"), _v("vergunn"),
        _v("autoriseer", "autoriser"), _v("accepteer", "accepter"),
        r"\btoe(?:staan|staat|gestaan)\b", r"\btoegelaten\b", r"\btoelaten\b", r"\btoelaat\b",
        r"\btoelating\b", r"\btoeken(?:nen|t|ning)\b", r"\btoegekend\b",
        r"\b(?:vergunning|toestemming|toegang|machtiging|goedkeuring|licentie)\s+(?:te\s+)?"
        r"(?:verlen(?:en|ing)|verleent|verleend)\b",
        r"\btoestemming\s+(?:te\s+)?(?:geven|geeft|gegeven)\b",
        # v2
        r"\bverlen(?:en|ing)\b", r"\bverleent\b", r"\bverleend\b", _v("aanvaard"),
    ],
    "deny": [
        _v("weiger"), r"\bweigering\b", _v("afwijz"), r"\bafgewezen\b", r"\bintrek(?:ken|t|king)\b",
        r"\bingetrokken\b", _v("herroep"), _v("annuleer", "annuler"), _v("ontneem", "ontnem"),
        r"\bontnomen\b", _v("ontzegg", "ontzeg"), _v("blokkeer", "blokker"), _v("verhinder"),
        _v("uitsluit"), r"\buitgesloten\b",
        # v2
        _v("verwerp"), r"\bverworpen\b",
    ],
    "require": [
        r"\bvereis(?:en|t)\b", r"\bvoorschrijv(?:en|t)\b", r"\bvoorgeschreven\b",
        r"\bopleg(?:gen|t)\b", r"\bopgelegd\b", r"\bverplicht(?:en|t)\b",
        # v2
        _v("afdwing"), r"\bafgedwongen\b", r"\bopdrag(?:en)\b", r"\bopdraagt\b", r"\bopgedragen\b",
        r"\beisen\s+(?:te\s+)?(?:stellen|stelt|gesteld)\b",
    ],
    "exempt": [
        r"\bvrijstel(?:len|t|ling)\b", r"\bvrijgesteld\b", r"\bontheff(?:en|t|ing)\b",
        r"\bontheven\b", r"\bontslag(?:en|t)\s+van\s+(?:de\s+)?(?:verplichting|plicht)\b",
    ],
    # --- operation and supply -----------------------------------------------------------
    "operate": [
        _v("voortzett", "voortzet"), r"\bvoortgezet\b", _v("continueer", "continuer"),
        _v("activeer", "activer"), _v("start"),
        r"\bin\s+werking\s+(?:te\s+)?(?:stel(?:len|t)|gesteld|zetten|zet|gezet)\b",
        r"\bin\s+bedrijf\s+(?:te\s+)?(?:hou(?:den|dt)|gehouden|stel(?:len|t)|gesteld)\b",
        # v2
        r"\bbeginn?en\b", r"\bbegint\b", r"\bbegonnen\b", _v("functioneer", "functioner"),
    ],
    "halt": [
        _n("opschort"), r"\bopgeschort\b", r"\bstopzet(?:ten|t|ting)\b", r"\bstopgezet\b",
        r"\bbe[eë]indig(?:en|t|d|de|ing)?\b", r"\bstil(?:legg?en|legt|legging|gelegd)\b",
        _v("stopp", "stop"),
        r"\bsta(?:ken|akt|aking)\b", r"\bgestaakt\b",            # v2: no longer matches "stak"
        r"\bonderbrek(?:en|t|ing)\b", r"\bonderbroken\b",
        _v("deactiveer", "deactiver"), r"\buitschakel(?:en|t|ing)\b", r"\buitgeschakeld\b",
        # v2
        _n("schors"), r"\been\s+einde\s+(?:te\s+)?(?:maken|maakt|gemaakt)\b",
    ],
    "market": [
        r"\bin\s+de\s+handel\s+(?:te\s+)?(?:brengen|brengt|gebracht)\b",
        r"\bop\s+de\s+markt\s+(?:te\s+)?(?:brengen|brengt|gebracht|aanbieden|aanbiedt|aangeboden)\b",
        r"\bin\s+gebruik\s+(?:te\s+)?(?:stel(?:len|t)|gesteld|nemen|neemt|genomen)\b",
        _n("lever"), r"\baanbied(?:en|t)\b", r"\baangeboden\b",
    ],
    "withdraw": [
        r"\buit\s+de\s+handel\s+(?:te\s+)?(?:nemen|neemt|genomen)\b",
        r"\bvan\s+de\s+markt\s+(?:te\s+)?(?:halen|haalt|gehaald|nemen|neemt|genomen)\b",
        r"\buit\s+(?:de\s+)?(?:gebruik|omloop)\s+(?:te\s+)?(?:nemen|neemt|genomen|halen|haalt|gehaald)\b",
        r"\bbuiten\s+gebruik\s+(?:te\s+)?(?:stel(?:len|t)|gesteld)\b",
        r"\bterugroep\w*", r"\bteruggeroepen\b", r"\bterugnem(?:en|t)\b", r"\bteruggenomen\b",
    ],
    # --- institutions -------------------------------------------------------------------
    "appoint": [
        _n("aanwijz", "aanwijs"), r"\baangewezen\b", _n("benoem"),
        _v("aanstell", "aanstel"), r"\baanstelling\b", r"\baangesteld\b",   # v2: "aanstellen"
        r"\binstellen\b", r"\bingesteld\b", r"\boprichten\b", r"\bopgericht\b", r"\boprichting\b",
        # v2
        r"\bbelast(?:en)?\s+met\b", r"\bbelasten\b",
    ],
    "dismiss": [
        r"\bontsla(?:an|at)\b", r"\bontslagen\b", r"\bafzet(?:ten|t|ting)\b", r"\bafgezet\b",
        r"\broyeer\w*", r"\broyer\w*", _n("opheff"), r"\bopgeheven\b", _v("ontbind"),
        r"\bontbonden\b", r"\bafschaff?(?:en|t|ing)\b", r"\bafgeschaft\b",
    ],
    # --- magnitude ----------------------------------------------------------------------
    "increase": [
        _n("verhoog", "verhog"), _n("vergroot", "vergrot"), r"\buitbreid(?:en|t|ing)\b",
        r"\buitgebreid\b", _v("versterk"), _v("intensiveer", "intensiver"),
    ],
    "decrease": [
        _n("verlaag", "verlag"), _n("verminder"), _v("verklein"), _n("beperk"),
        _v("inperk"), r"\bingeperkt\b",
        _v("reduceer", "reducer"), _v("verzwakk", "verzwak"),
    ],
    "extend": [_n("verleng")],
    "shorten": [_n("verkort", "bekort")],
    # --- weak / generic duty verbs --------------------------------------------------------
    "ensure": [
        _v("waarborg"), _v("garandeer", "garander"), r"\bzorg(?:en|t|dragen|draagt)\b",
        r"\bzorg\s+(?:te\s+)?dragen\b", r"\bzorgdrag\w*", r"\bziet\s+erop\s+toe\b", r"\btoezien\b",
        # v2
        r"\bzorg\s+(?:er\s*)?voor\b", _v("bescherm"), _v("vermijd"),
    ],
    "measure": [
        r"\btreff(?:en|t)\b", r"\bgetroffen\b", _n("implementeer", "implementer"),
        r"\bimplementatie\b", r"\binvoeren\b", r"\bingevoerd\b", r"\btoepassen\b", r"\btoegepast\b",
        # v2: "(maatregelen) nemen"; longer phrases with "nemen" win on overlap
        r"\bnemen\b", r"\bneemt\b", r"\bgenomen\b",
    ],
    "assess": [
        _n("controleer", "controler"), _n("toets"), _n("beoordeel", "beoordel"),
        _n("evalueer", "evaluer"), _v("inspecteer", "inspecter"), _v("monitor"),
        r"\btoezicht\s+(?:te\s+)?(?:houden|houdt|gehouden|uitoefenen|uitoefent|uitgeoefend)\b",
        r"\bverifi[eë]r(?:en|t)\b", r"\bgeverifieerd\b", _v("test"),
        r"\bonderzoek(?:en|t)\b", r"\bonderzocht\b",
    ],
    "record": [
        _v("documenteer", "documenter"), _n("registreer", "registrer"), r"\bregistratie\b",
        r"\bvastleg(?:gen|t|ging)\b", r"\bvastgelegd\b", r"\bbijhoud(?:en|t)\b", r"\bbijgehouden\b",
        _v("noteer", "noter"), r"\bdocumentatie\b",
    ],
    # --- legislative acts -----------------------------------------------------------------
    "adopt": [
        r"\bvast(?:stellen|stelt)\b(?!\s+(?:dat|of|welke)\b)", r"\bvastgesteld\b(?!\s+(?:dat|of)\b)",
        r"\bvaststelling\b", r"\bvast\s+te\s+stellen\b(?!\s+(?:dat|of|welke)\b)",
        r"\buitvaardig(?:en|t|ing)\b", r"\buitgevaardigd\b",
        # v2
        r"\bbindend\s+(?:te\s+)?(?:maken|maakt|gemaakt)\b",
        r"\balgemene\s+geldigheid\s+(?:te\s+)?(?:verlenen|verleent|verleend)\b",
    ],
    "amend": [
        _n("wijzig"), r"\baanvul(?:len|t|ling|lingen)\b", r"\baangevuld\b",
        r"\btoevoeg(?:en|t|ing)\b", r"\btoegevoegd\b",
        r"\baanpass(?:en|ing|ingen)\b", r"\baanpast\b", r"\baangepast\b",
        _v("actualiseer", "actualiser"), r"\bbijwerk(?:en|t)\b", r"\bbijgewerkt\b",
        r"\bherzi(?:en|et|ening)\b", _v("schrapp", "schrap"),
        # v2
        r"\b(?:actueel|up-to-date|bij)\s+(?:te\s+)?(?:houden|houdt|gehouden)\b",
    ],
    # --- compliance and consideration (generic) ------------------------------------------
    "comply": [
        r"\bvol(?:doen|doet|daan|deed|deden)\b",
        r"\bnalev(?:en|ing)\b", r"\bnaleeft\b", r"\bnageleefd\b",
        r"\bnakom(?:en|t|ing)\b", r"\bnagekomen\b",
        r"\bin\s+overeenstemming\s+(?:te\s+)?(?:brengen|brengt|gebracht|zijn|is)\b",
        # v2
        _v("eerbiedig"), _v("respecteer", "respecter"), _v("volg"),
        r"\bzich\s+(?:te\s+)?(?:houden|houdt)\s+aan\b",
    ],
    "consider": [
        r"\brekening\s+(?:te\s+)?(?:houden|houdt|gehouden)\b",          # v2: "met" no longer required
        r"\bin\s+(?:aanmerking|acht|overweging)\s+(?:te\s+)?(?:nemen|neemt|genomen)\b",
        _v("overweeg", "overweg"), r"\boverwogen\b",
        # v2
        r"\baandacht\s+(?:te\s+)?(?:besteden|besteedt|besteed|schenken|schenkt|geschonken)\b",
    ],
    # --- producing and shaping ----------------------------------------------------------------
    "produce": [
        r"\bopstel(?:len|t)\b", r"\bopgesteld\b", _v("ontwikkel"),
        r"\bontwerp(?:en|t)\b", r"\bontworpen\b", r"\buitwerk(?:en|t)\b", r"\buitgewerkt\b",
        r"\bopmak(?:en)\b", r"\bopmaakt\b", r"\bopgemaakt\b",
        r"\bopzetten\b", r"\bopgezet\b",                         # v2: bare noun "opzet" removed
        # v2
        _v("vertaal", "vertal"), r"\bvoorstell?en\b", r"\bvoorgesteld\b", _v("vervaardig"),
        r"\bcre[eë](?:ren|ert)\b", r"\bgecre[eë]erd\b",
    ],
    "subject": [
        r"\bonderwerp(?:en|t)\b(?=(?:\s+\S+){0,8}?\s+aan\b)", r"\bonderworpen\b",
        # v2: bare lemma ("onderwerpen" as the whole action) and "ondergaan"
        r"^\s*onderwerpen\s*$", r"\bonderga(?:an|at)\b", r"\bondergaan\b",
    ],
    "condition": [
        r"\bafhankelijk\s+(?:te\s+)?(?:stel(?:len|t)|gesteld)\s+van\b",
        r"\bvoorwaarden\s+(?:te\s+)?(?:verbinden|verbindt|verbonden)\b",
    ],
    "contain": [
        _v("bevatt", "bevat"), _v("omvatt", "omvat"),
        r"\bvergezeld\s+(?:te\s+)?(?:gaan|gaat|van)\b", r"\bvoorzien\s+van\b",
        r"(?<!contact\s)\bop(?:nemen|neemt)\b", r"\bop\s+te\s+nemen\b",
        r"\bopgenomen\b(?=\s*(?:$|[,.;:]|in\b|worden\b|wordt\b))",
        r"\binbouw(?:en|t)\b", r"\bingebouwd\b",
        _v("integreer", "integrer"), r"\bge[iï]ntegreerd\b",
        # v2
        r"\bvoorzien\b", r"\bvoorziet\b", _v("bijvoeg"), r"\bbijgevoegd\b",
        _v("aanbreng"), r"\baangebracht\b",
    ],
    # --- v2 families (no antonym) -------------------------------------------------------------
    "cooperate": [
        _v("samenwerk"), r"\bsamengewerkt\b", r"\bsamenwerking\b",
        r"\bin\s+overleg\s+(?:te\s+)?(?:treden|treedt|getreden)\b",
        r"\boverleg(?:gen|t)\s+met\b", r"\bco[oö]rdin(?:eren|eert|atie)\b", r"\bgeco[oö]rdineerd\b",
        r"\bdeel(?:nemen|neemt)\b", r"\bdeelgenomen\b", r"\baan(?:gaan|gaat)\b", r"\baangegaan\b", r"\bovereen(?:komen|komt)\b",
        r"\bovereengekomen\b", _v("betrekk", "betrek"),
        r"\bbetrokken\b", _v("beleg"), r"\bbijeen(?:roepen|roept)\b", r"\bbijeengeroepen\b",
    ],
    "request": [
        _v("verzoek"), r"\bverzocht\b", _v("aanvraag", "aanvrag"), r"\baangevraagd\b",
        r"\bvrag(?:en)\b", r"\bvraagt\b", r"\bgevraagd\b", _v("uitnodig"),
        r"\been\s+beroep\s+(?:te\s+)?(?:doen|doet|gedaan)\b",
    ],
    "decide": [
        r"\bbeslui(?:ten|t)\b", r"\bbesloten\b", r"\bbeslis(?:sen|t)\b", r"\bbeslist\b",
        r"\bbeslissing(?:en)?\s+(?:te\s+)?(?:nemen|neemt|genomen)\b",
        r"\bbesluit(?:en)?\s+(?:te\s+)?(?:nemen|neemt|genomen)\b",
        r"\bkiez(?:en)\b", r"\bkiest\b", r"\bgekozen\b", _v("selecteer", "selecter"),
    ],
    "perform": [
        _v("uitvoer"), r"\buitgevoerd\b", _v("verricht"), _v("onderneem", "ondernem"),
        r"\bondernomen\b", _v("uitoefen"), r"\buitgeoefend\b", _v("organiseer", "organiser"),
        r"\bdoen\b", r"\bdoet\b", _v("verwezenlijk"), _v("verzorg"), _v("behandel"),
        _v("afhandel"), r"\boptre(?:den|edt)\b", r"\bopgetreden\b",
        _v("fungeer", "funger"), _v("vertegenwoordig"),
    ],
    "support": [
        _v("faciliteer", "faciliter"), _v("stimuleer", "stimuler"), _v("ondersteun"),
        _v("aanmoedig"), _v("bevorder"), r"\bhelp(?:en|t)\b", r"\bgeholpen\b",
        _v("vergemakkelijk"), _v("begeleid"),
        r"\bmogelijk\s+(?:te\s+)?(?:maken|maakt|gemaakt)\b",
        r"\bin\s+(?:staat|de\s+gelegenheid)\s+(?:te\s+)?(?:stellen|stelt|gesteld)\b",
        r"\b(?:bijstand|steun|ondersteuning)\s+(?:te\s+)?(?:verlenen|verleent|verleend|bieden|biedt|geboden)\b",
        r"\binzicht\s+(?:te\s+)?(?:geven|geeft|gegeven|bieden|biedt)\b",
    ],
    "describe": [
        _v("vermeld"), _v("beschrijv", "beschrijf"), r"\bbeschreven\b",
        _v("specificeer", "specificer"), _v("preciseer", "preciser"),
        r"\baan(?:geven|geeft)\b", r"\baangegeven\b", r"\buiteen(?:zetten|zet)\b",
        r"\buiteengezet\b", _v("toelicht"), _v("motiveer", "motiver"), _v("rechtvaardig"),
        r"\bgerechtvaardigd\s+achten\b", r"\baan(?:tonen|toont)\b", r"\baangetoond\b",
        _v("bevestig"), _v("verklaar", "verklar"), r"\baan(?:voeren|voert)\b", r"\baangevoerd\b",
    ],
    "possess": [
        _v("beschikk", "beschik"), _v("bezitt", "bezit"),
    ],
    "liable": [
        r"\b(?:aansprakelijk|verantwoordelijk)\s+(?:te\s+)?(?:zijn|is|blijven|blijft|gesteld|stellen)\b",
        r"\bverantwoordelijkheid\s+(?:op\s+zich\s+)?(?:te\s+)?(?:nemen|neemt|genomen|dragen|draagt)\b",
    ],
}

# Kept for reference (and for a later deontic/negation stage); the candidate scorer
# compares axes only and ignores the sign.
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

# Axes where a match proves little on its own. The scorer reports a `generic` flag for
# matches on these axes; the downstream ranking (with object/actor overlap) can discount them.
GENERIC_AXES = {"ensure", "measure", "assess", "record", "use",
                "adopt", "amend", "comply", "consider", "produce", "subject", "contain",
                "perform", "support", "describe", "possess"}

# Different axes that describe neighbouring acts (score 0.5).
RELATED_AXES = {
    frozenset(p) for p in [
        ("notify", "provide"), ("provide", "publish"), ("provide", "transfer"),
        ("transfer", "publish"), ("retain", "record"), ("collect", "record"),
        ("access", "assess"), ("authorise", "require"), ("operate", "market"),
        ("adopt", "amend"), ("produce", "record"), ("contain", "record"),
        ("comply", "require"), ("condition", "require"), ("condition", "authorise"),
        # v2
        ("describe", "contain"), ("describe", "record"), ("describe", "notify"),
        ("describe", "provide"), ("request", "provide"), ("request", "notify"),
        ("request", "access"), ("cooperate", "support"), ("cooperate", "request"),
        ("perform", "measure"), ("perform", "comply"), ("support", "ensure"),
        ("decide", "adopt"), ("decide", "authorise"), ("possess", "retain"),
        ("liable", "ensure"), ("liable", "comply"), ("halt", "withdraw"),
        ("deny", "halt"), ("erase", "withdraw"), ("authorise", "appoint"),
    ]
}

# Lemmas that are copulas or states rather than acts. Two identical lemmas from this
# set are NOT treated as the same action by the identical-lemma fallback.
STATIVE_LEMMAS = {
    "zijn", "hebben", "worden", "gelden", "blijven", "bestaan", "bedragen", "bevinden",
    "van toepassing zijn", "van kracht worden", "geldig zijn", "geldig blijven",
    "betrekking hebben", "deel uitmaken", "gebaseerd zijn", "plaatsvinden", "neerkomen",
    "zien", "maken", "zetten", "leiden", "richten", "dateren", "beschouwen", "genieten",
}