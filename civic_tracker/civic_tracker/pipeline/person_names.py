"""Person-name rules shared by the resolver, jurisdiction.alias_ok and the cleanup tools.

Two kinds of person names:
  * a FULL name ("Pat Ferrell", "Albert Okonkwo") identifies a person well enough to match by name;
  * a WEAK name - a bare surname or first name ("Ferrell", "Pat") or a title + one name ("DA Ferrell",
    "Mayor Hallworth", "Honorable Judge Brandt") - does NOT. Weak names are resolved only from context (role,
    organisations in the article, city, date: see resolver._contextual_person), never by pure name/alias
    matching, so a different "Pat" elected years later does not inherit Pat Ferrell's record.
"""
from __future__ import annotations

import re
import unicodedata
from difflib import SequenceMatcher
from typing import List, Optional, Set, Tuple

# ---------------------------------------------------------------------------- titles
# longest forms first (the regex alternation takes the first that matches)
TITLES = (
    r"district\s+attorney|d\.?a\.?|assistant\s+city\s+manager|city\s+manager|city\s+clerk|city\s+attorney|"
    r"county\s+counsel|public\s+defender|mayor\s+pro\s+tem(?:pore)?|pro\s+tem(?:pore)?|vice\s+mayor|mayor|"
    r"supervisor|council\s*member|councilman|councilwoman|"
    r"vice\s+chair(?:man|woman|person)?|chair(?:man|woman|person)?|president|director|principal|"
    r"sheriff|police\s+chief|chief|judge|justice|lt\.?\s+governor|governor|gov|senator|sen|"
    r"assemblymember|assemblyman|assemblywoman|congressman|congresswoman|rep|representative|"
    r"superintendent|trustee|commissioner|chancellor|officer|detective|sergeant|sgt|lieutenant|lt|"
    r"captain|capt|deputy|the\s+honorable|honorable|hon|dr|mr|mrs|ms|mx"
)
_TITLE_RX = re.compile(rf"(?:{TITLES})\.?(?=\s)", re.I)
_TITLE_FORM = re.compile(
    rf"^(?:(?:former|acting|interim|retired|incumbent)\s+)?(?P<titles>(?:(?:{TITLES})\.?\s+)+)(?P<rest>.+)$", re.I)
# titles that say nothing about the office
HONORIFICS = {"the honorable", "honorable", "hon", "dr", "mr", "mrs", "ms", "mx"}

# title -> word expected in the person's affiliation role_title
TITLE_ROLE = {
    "da": "district attorney", "d.a": "district attorney", "district attorney": "district attorney",
    "mayor": "mayor", "vice mayor": "mayor", "mayor pro tem": "mayor", "mayor pro tempore": "mayor",
    "pro tem": "mayor", "pro tempore": "mayor", "supervisor": "supervisor",
    "council member": "council", "councilmember": "council", "councilman": "council", "councilwoman": "council",
    "city manager": "manager", "assistant city manager": "manager", "city clerk": "clerk",
    "city attorney": "attorney", "county counsel": "counsel", "public defender": "public defender",
    "chair": "chair", "chairman": "chair", "chairwoman": "chair", "chairperson": "chair",
    "vice chair": "chair", "vice chairman": "chair", "vice chairwoman": "chair", "vice chairperson": "chair",
    "president": "president", "director": "director", "principal": "principal",
    "sheriff": "sheriff", "chief": "chief", "police chief": "chief", "judge": "judge", "justice": "justice",
    "governor": "governor", "gov": "governor", "lt governor": "lieutenant governor",
    "senator": "senator", "sen": "senator", "assemblymember": "assembly", "assemblyman": "assembly",
    "assemblywoman": "assembly", "congressman": "representative", "congresswoman": "representative",
    "rep": "representative", "representative": "representative",
    "superintendent": "superintendent", "trustee": "trustee", "commissioner": "commissioner",
    "chancellor": "chancellor",
}
# how the title can appear in an article next to the full name ("the Honorable Daniel M. Brandt" = a judge)
TITLE_TEXT = {"judge": r"judge|honorable|hon", "justice": r"justice|honorable|hon"}


def _norm_title(t: str) -> str:
    return re.sub(r"\s+", " ", t.lower().replace(".", " ")).strip()


def surname_form(name: str) -> Optional[Tuple[Optional[str], str]]:
    """A weak name split into (title, single name token):
    'District Attorney Ferrell' -> ('district attorney', 'ferrell'); 'Honorable Judge Brandt' -> ('judge', 'brandt');
    'Ferrell' -> (None, 'ferrell'); 'Pat Ferrell' (a full name) -> None. Of several titles the most specific
    (last non-honorific) one is returned."""
    n = " ".join((name or "").split()).strip(" .,;:")
    m = _TITLE_FORM.match(n)
    title = None
    rest = n
    if m:
        rest = m.group("rest")
        found = [_norm_title(t) for t in _TITLE_RX.findall(m.group("titles"))]
        specific = [t for t in found if t not in HONORIFICS]
        title = (specific or found or [None])[-1]
    toks = rest.split()
    return (title, toks[0].lower().strip(".,")) if len(toks) == 1 and toks[0][:1].isalpha() else None


def is_weak_name(name: str) -> bool:
    return surname_form(name) is not None


# ---------------------------------------------------------------------------- full names
_SUFFIX = {"jr", "sr", "ii", "iii", "iv"}
NICKNAMES = {
    "al": {"albert", "alan", "allen", "alfred", "alexander"}, "alex": {"alexander", "alexandra", "alejandro"},
    "andy": {"andrew"}, "drew": {"andrew"}, "ben": {"benjamin"}, "bill": {"william"}, "billy": {"william"},
    "will": {"william"}, "bob": {"robert"}, "bobby": {"robert"}, "rob": {"robert"}, "robbie": {"robert"},
    "chris": {"christopher", "christine", "christina"}, "dan": {"daniel"}, "danny": {"daniel"},
    "dave": {"david"}, "don": {"donald"}, "ed": {"edward", "edwin"}, "eddie": {"edward"},
    "geoff": {"geoffrey"}, "jeff": {"jeffrey", "jeffery"}, "greg": {"gregory"}, "jim": {"james"},
    "jimmy": {"james"}, "jamie": {"james"}, "joe": {"joseph"}, "joey": {"joseph"}, "jon": {"jonathan"},
    "josh": {"joshua"}, "ken": {"kenneth"}, "kenny": {"kenneth"}, "larry": {"lawrence"}, "matt": {"matthew"},
    "mike": {"michael"}, "mick": {"michael"}, "nick": {"nicholas"}, "pat": {"patrick", "patricia"},
    "pete": {"peter"}, "phil": {"phillip", "philip"}, "rich": {"richard"}, "rick": {"richard"},
    "dick": {"richard"}, "ron": {"ronald"}, "sam": {"samuel", "samantha"}, "steve": {"steven", "stephen"},
    "ted": {"edward", "theodore"}, "tim": {"timothy"}, "tom": {"thomas"}, "tommy": {"thomas"},
    "tony": {"anthony"}, "vince": {"vincent"}, "zach": {"zachary"}, "abby": {"abigail"},
    "beth": {"elizabeth"}, "liz": {"elizabeth"}, "betsy": {"elizabeth"}, "cathy": {"catherine", "cathleen"},
    "kathy": {"katherine", "kathleen"}, "kate": {"katherine", "catherine", "kathleen"},
    "katie": {"katherine", "catherine", "kathleen"}, "jen": {"jennifer"}, "jenny": {"jennifer"},
    "jess": {"jessica"}, "meg": {"margaret", "megan"}, "peggy": {"margaret"}, "maggie": {"margaret"},
    "sue": {"susan", "suzanne"}, "suzy": {"susan"}, "val": {"valerie"}, "vicky": {"victoria"},
    "gar": {"garth"}, "ray": {"raymond"}, "randy": {"randall", "randolph"}, "fred": {"frederick", "alfred"},
    "frank": {"francis", "franklin"}, "gabe": {"gabriel"}, "manny": {"manuel"}, "nate": {"nathan", "nathaniel"},
    "becky": {"rebecca"}, "debbie": {"deborah"}, "terry": {"terrence", "theresa"},
}
_PARTICLES = {"de", "del", "della", "la", "las", "los", "van", "von", "der", "den", "da", "di", "du", "le", "st"}


def _ascii(s: str) -> str:
    return unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()


def name_parts(name: str) -> Optional[Tuple[str, List[str], str]]:
    """'Daniel M. Brandt' -> ('daniel', ['m'], 'brandt'); 'Varadan, Jay' -> ('jay', [], 'varadan');
    a weak or one-word name -> None."""
    n = _ascii(" ".join((name or "").split()))
    if "," in n:
        last, _, first = n.partition(",")
        if first.strip().lower().strip(".") not in _SUFFIX:
            n = f"{first.strip()} {last.strip()}"
    toks = [t.strip(".,").lower() for t in n.split()]
    toks = [t for t in toks if t and t not in _SUFFIX]
    if len(toks) < 2 or surname_form(name):
        return None
    # "Martin de los Angeles": the particles belong to the surname ("delosangeles")
    i = len(toks) - 1
    while i > 1 and toks[i - 1] in _PARTICLES:
        i -= 1
    return toks[0], toks[1:i], "".join(toks[i:])


def first_name_variants(first: str) -> Set[str]:
    f = first.lower()
    out = {f} | NICKNAMES.get(f, set())
    out |= {nick for nick, full in NICKNAMES.items() if f in full}
    return out


def _close(a: str, b: str, ratio: float = 0.85) -> bool:
    a, b = a.replace("-", "").replace("'", ""), b.replace("-", "").replace("'", "")
    return a == b or (min(len(a), len(b)) >= 4 and SequenceMatcher(None, a, b).ratio() >= ratio)


def persons_compatible(a: str, b: str) -> bool:
    """Could two FULL names be the same person? Same surname (spelling slips allowed), same first name or a
    nickname of it ('Al' ~ 'Albert') or an initial, and no conflicting middle initials ('David W. Reed' is not
    'David M. Reed'). Weak names are never 'compatible' - they need context."""
    pa, pb = name_parts(a), name_parts(b)
    if not pa or not pb:
        return False
    (fa, ma, la), (fb, mb, lb) = pa, pb
    if not _close(la, lb) and not (set(la.split("-")) & set(lb.split("-"))):
        return False
    first_ok = (fa == fb or fb in first_name_variants(fa) or (len(fa) == 1 and fb.startswith(fa))
                or (len(fb) == 1 and fa.startswith(fb)) or _close(fa, fb))
    if not first_ok:
        return False
    ia, ib = [m[0] for m in ma], [m[0] for m in mb]
    return not (ia and ib and ia[0] != ib[0])


_STRAY_LETTER = re.compile(r"^(?P<name>\S+(?:\s+\S+)+?)\s+[A-Z]$")


def clean_person_name(name: str) -> str:
    """Drop a stray trailing capital left by a signature block: 'Carol Pellegrin F' -> 'Carol Pellegrin'
    (a middle initial keeps its place and its period: 'Daniel M. Brandt')."""
    m = _STRAY_LETTER.match(name.strip())
    if m and len(m.group("name").split()) >= 2 and not surname_form(m.group("name")):
        return m.group("name")
    return name


_PLACEHOLDER = re.compile(
    r"^(?:the\s+|a\s+|an\s+)?(?:victim|victims|suspect|suspects|defendant|defendants|plaintiff|witness|"
    r"resident|residents|student|students|official|officials|speaker|caller|driver|man|woman|boy|girl|"
    r"(?:jane|john)\s+doe(?:\s*#?\s*\d+)?|unnamed\s+\w+|unidentified\s+\w+|anonymous(?:\s+\w+)?)$", re.I)


def is_placeholder_person(name: str) -> bool:
    """'the victim', 'Jane Doe #2', 'a resident': not a person the wiki can track."""
    return bool(_PLACEHOLDER.match(" ".join((name or "").split()).strip(" .,;:")))
