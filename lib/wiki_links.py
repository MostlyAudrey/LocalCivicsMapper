"""[[Wiki Link]] rendering (SPEC_WEB_UI §4 "Wiki-Link Auto-Resolver"), registered as Jinja filters.

render_wiki_links(text, entity_lookup=None) -> Markup
    Escapes `text`, then turns [[Entity Name]] into links to /wiki/entity/{id} when the name matches a
    TB_Entity canonical_name or alias (case-insensitive); otherwise plain text. If no lookup dict is
    passed, the names in the text are resolved with one query.
render_bio(text) -> Markup
    Same, plus blank-line separated paragraphs -> <p>.
"""
from __future__ import annotations

import re
from typing import Dict, Iterable, Optional

from markupsafe import Markup, escape

from lib.db_conn import Db_conn

db = Db_conn()
WIKI_LINK = re.compile(r"\[\[([^\[\]|]{1,120})(?:\|([^\[\]]{1,120}))?\]\]")


def lookup_entities(names: Iterable[str]) -> Dict[str, int]:
    names = sorted({n.strip().lower() for n in names if n.strip()})
    if not names:
        return {}
    rows = db.fetch_all(
        """
        SELECT LOWER(canonical_name) AS k, id FROM TB_Entity WHERE LOWER(canonical_name) = ANY(%s)
        UNION ALL
        SELECT LOWER(a) AS k, id FROM TB_Entity, unnest(aliases) a WHERE LOWER(a) = ANY(%s)
        """,
        (names, names),
    )
    out: Dict[str, int] = {}
    for r in rows:
        out.setdefault(r["k"], r["id"])
    return out


def render_wiki_links(text: Optional[str], entity_lookup: Optional[Dict[str, int]] = None) -> Markup:
    if not text:
        return Markup("")
    if entity_lookup is None:
        entity_lookup = lookup_entities(m.group(1) for m in WIKI_LINK.finditer(text))
    out, pos = [], 0
    for m in WIKI_LINK.finditer(text):
        out.append(escape(text[pos:m.start()]))
        name, label = m.group(1).strip(), (m.group(2) or m.group(1)).strip()
        eid = entity_lookup.get(name.lower())
        if eid:
            out.append(Markup('<a href="/wiki/entity/{}" class="text-blue-600 hover:underline font-medium">{}</a>')
                       .format(eid, label))
        else:
            out.append(escape(label))
        pos = m.end()
    out.append(escape(text[pos:]))
    return Markup("").join(out)


def render_bio(text: Optional[str]) -> Markup:
    if not text:
        return Markup("")
    lookup = lookup_entities(m.group(1) for m in WIKI_LINK.finditer(text))
    paras = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    return Markup("").join(
        Markup('<p class="mb-3 leading-relaxed">{}</p>').format(render_wiki_links(" ".join(p.split()), lookup))
        for p in paras
    )
