"""One-off repair of local-body entities (see pipeline/jurisdiction.py for the rules).

1. Remove aliases that fail jurisdiction.alias_ok (bare places like "Davis", bare generic names, and names of a
   different body: "West Sacramento Police Department" on "Sacramento Police Department").
2. Split catch-all entities with a bare generic name ("Planning Commission", "City Council"): every stance row /
   affiliation moves to the city's own body, placed by the article it came from (infer_jurisdiction). Rows that
   can't be placed stay where they are and are listed for review. An emptied catch-all is deleted.

`plan()` only reads; `apply()` runs the plan in the caller's transaction. CLI: `civic-tracker entity-cleanup`
(dry run + markdown report) / `--apply`.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from psycopg import AsyncConnection

from civic_tracker.pipeline.jurisdiction import (alias_ok, compatible_names, infer_jurisdiction, is_generic_body,
                                                 places_in,
                                                 qualify_generic)


@dataclass
class AliasFix:
    entity_id: int
    entity_name: str
    entity_type: str
    alias: str


@dataclass
class Move:
    kind: str                  # "event" (TB_Entity_Event row) | "affiliation" (organization side)
    row_id: int
    from_id: int
    target_name: str
    article_id: Optional[int]
    article_title: str


@dataclass
class Unplaced:
    kind: str
    row_id: int
    from_id: int
    article_id: Optional[int]
    article_title: str


@dataclass
class Plan:
    alias_fixes: List[AliasFix] = field(default_factory=list)
    generic: Dict[int, Tuple[str, str]] = field(default_factory=dict)   # id -> (name, type)
    moves: List[Move] = field(default_factory=list)
    unplaced: List[Unplaced] = field(default_factory=list)
    targets: Dict[Tuple[str, str], Optional[int]] = field(default_factory=dict)  # (name, type) -> existing id
    deletes: List[int] = field(default_factory=list)


async def _find_target(conn: AsyncConnection, name: str, etype: str) -> Optional[int]:
    cur = await conn.execute(
        """SELECT id, canonical_name, aliases FROM TB_Entity
           WHERE entity_type = %(t)s AND (LOWER(canonical_name) = LOWER(%(n)s)
                 OR LOWER(%(n)s) = ANY(SELECT LOWER(unnest(aliases))))
           ORDER BY (LOWER(canonical_name) = LOWER(%(n)s)) DESC, id LIMIT 5""",
        {"n": name, "t": etype},
    )
    for r in await cur.fetchall():
        if r["canonical_name"].lower() == name.lower() or alias_ok(name, r["canonical_name"], etype):
            return r["id"]
    # near-identical name for the same body ("Yolo County Sheriff's Department" ~ "...Sheriff's Office")
    cur = await conn.execute(
        """SELECT id, canonical_name FROM TB_Entity
           WHERE entity_type = %(t)s AND similarity(canonical_name, %(n)s) >= 0.6
           ORDER BY similarity(canonical_name, %(n)s) DESC LIMIT 5""", {"n": name, "t": etype})
    for r in await cur.fetchall():
        # the fuzzy target must name the SAME place ("Davis Transportation Commission" must not land on the
        # California Transportation Commission)
        if places_in(name) == places_in(r["canonical_name"]) and compatible_names(name, r["canonical_name"]) \
                and not is_generic_body(r["canonical_name"]):
            return r["id"]
    return None


async def plan(conn: AsyncConnection) -> Plan:
    p = Plan()
    cur = await conn.execute(
        "SELECT id, canonical_name, entity_type, aliases FROM TB_Entity WHERE cardinality(aliases) > 0 ORDER BY id")
    for e in await cur.fetchall():
        for a in e["aliases"]:
            if not alias_ok(a, e["canonical_name"], e["entity_type"]):
                p.alias_fixes.append(AliasFix(e["id"], e["canonical_name"], e["entity_type"], a))

    cur = await conn.execute("SELECT id, canonical_name, entity_type FROM TB_Entity WHERE entity_type <> 'PERSON'")
    p.generic = {e["id"]: (e["canonical_name"], e["entity_type"])
                 for e in await cur.fetchall() if is_generic_body(e["canonical_name"])}
    if not p.generic:
        return p

    ids = list(p.generic)
    rows = []
    cur = await conn.execute(
        """SELECT 'event' AS kind, ee.id AS row_id, ee.entity_id AS from_id, a.id AS article_id,
                  a.title, a.full_text, s.scrape_config->>'jurisdiction' AS src_j
           FROM TB_Entity_Event ee JOIN TB_Article a ON a.id = ee.source_article_id
           JOIN TB_Sources s ON s.id = a.source_id
           WHERE ee.entity_id = ANY(%s)""", (ids,))
    rows += await cur.fetchall()
    cur = await conn.execute(
        """SELECT 'affiliation' AS kind, af.id AS row_id, af.organization_id AS from_id, a.id AS article_id,
                  a.title, a.full_text, s.scrape_config->>'jurisdiction' AS src_j
           FROM TB_Entity_Affiliation af LEFT JOIN TB_Article a ON a.id = af.source_article_id
           LEFT JOIN TB_Sources s ON s.id = a.source_id
           WHERE af.organization_id = ANY(%s)""", (ids,))
    rows += await cur.fetchall()

    remaining = defaultdict(int)
    for r in rows:
        name, etype = p.generic[r["from_id"]]
        j = infer_jurisdiction(r["title"] or "", r["full_text"] or "", r["src_j"]) if r["article_id"] else None
        target = qualify_generic(name, j)
        if not target:
            p.unplaced.append(Unplaced(r["kind"], r["row_id"], r["from_id"], r["article_id"], r["title"] or ""))
            remaining[r["from_id"]] += 1
            continue
        p.moves.append(Move(r["kind"], r["row_id"], r["from_id"], target, r["article_id"], r["title"] or ""))
        key = (target, etype)
        if key not in p.targets:
            p.targets[key] = await _find_target(conn, target, etype)

    # catch-alls left with nothing (affiliations where the body itself is the "person" side are kept)
    cur = await conn.execute(
        "SELECT DISTINCT entity_id FROM TB_Entity_Affiliation WHERE entity_id = ANY(%s)", (ids,))
    own_affs = {r["entity_id"] for r in await cur.fetchall()}
    p.deletes = [i for i in ids if remaining[i] == 0 and i not in own_affs]
    return p


async def apply(conn: AsyncConnection, p: Plan) -> Dict[str, int]:
    """Execute a plan inside the caller's transaction. Returns counts."""
    n = defaultdict(int)
    for f in p.alias_fixes:
        await conn.execute(
            "UPDATE TB_Entity SET aliases = array_remove(aliases, %s), bio_is_dirty = TRUE WHERE id = %s",
            (f.alias, f.entity_id))
        n["aliases_removed"] += 1

    ids: Dict[Tuple[str, str], int] = {}
    for (name, etype), existing in p.targets.items():
        if existing is None:
            src = next(i for i, (gn, gt) in p.generic.items() if gt == etype)  # borrow a context embedding
            cur = await conn.execute(
                """INSERT INTO TB_Entity (canonical_name, entity_type, context_embedding, bio_is_dirty)
                   SELECT %s, %s, context_embedding, TRUE FROM TB_Entity WHERE id = %s
                   ON CONFLICT (canonical_name, entity_type) DO UPDATE SET bio_is_dirty = TRUE
                   RETURNING id""", (name, etype, src))
            existing = (await cur.fetchone())["id"]
            n["entities_created"] += 1
        ids[(name, etype)] = existing

    for m in p.moves:
        tid = ids[(m.target_name, p.generic[m.from_id][1])]
        if m.kind == "event":
            cur = await conn.execute(
                """UPDATE TB_Entity_Event t SET direct_quotes = ARRAY(
                       SELECT DISTINCT q FROM unnest(t.direct_quotes || s.direct_quotes) q)
                   FROM TB_Entity_Event s
                   WHERE s.id = %s AND t.entity_id = %s AND t.event_id = s.event_id
                     AND t.source_article_id = s.source_article_id
                   RETURNING t.id""", (m.row_id, tid))
            if await cur.fetchone():
                await conn.execute("DELETE FROM TB_Entity_Event WHERE id = %s", (m.row_id,))
            else:
                await conn.execute("UPDATE TB_Entity_Event SET entity_id = %s WHERE id = %s", (tid, m.row_id))
        else:
            cur = await conn.execute(
                """SELECT 1 FROM TB_Entity_Affiliation t JOIN TB_Entity_Affiliation s ON s.id = %s
                   WHERE t.entity_id = s.entity_id AND t.organization_id = %s AND t.role_title = s.role_title""",
                (m.row_id, tid))
            if await cur.fetchone():
                await conn.execute("DELETE FROM TB_Entity_Affiliation WHERE id = %s", (m.row_id,))
            else:
                await conn.execute("UPDATE TB_Entity_Affiliation SET organization_id = %s WHERE id = %s",
                                   (tid, m.row_id))
        await conn.execute("UPDATE TB_Entity SET bio_is_dirty = TRUE WHERE id = %s", (tid,))
        n[f"{m.kind}_rows_moved"] += 1

    cur = await conn.execute("SELECT to_regclass('tb_vote_motion') IS NOT NULL AS ok")
    votes_ready = (await cur.fetchone())["ok"]
    for i in p.deletes:
        if votes_ready:  # v1.8: a catch-all still acting as a measure's body or a voter is kept for review
            cur = await conn.execute(
                """SELECT EXISTS (SELECT 1 FROM TB_Measure WHERE body_entity_id = %(i)s)
                       OR EXISTS (SELECT 1 FROM TB_Vote_Motion WHERE body_entity_id = %(i)s)
                       OR EXISTS (SELECT 1 FROM TB_Vote WHERE entity_id = %(i)s) AS used""", {"i": i})
            if (await cur.fetchone())["used"]:
                n["catch_alls_kept_in_use"] += 1
                continue
        await conn.execute("DELETE FROM TB_Entity WHERE id = %s", (i,))
        n["catch_alls_deleted"] += 1
    return dict(n)


def report(p: Plan) -> str:
    out = ["# Entity cleanup plan", "",
           f"- {len(p.alias_fixes)} alias(es) to remove",
           f"- {len(p.generic)} catch-all entit(y/ies) with a bare generic name",
           f"- {len(p.moves)} row(s) to move to the right city's body "
           f"({sum(1 for v in p.targets.values() if v is None)} new body entities)",
           f"- {len(p.unplaced)} row(s) that can't be placed (stay put, for review)",
           f"- {len(p.deletes)} emptied catch-all(s) to delete", ""]
    out += ["## 1. Aliases to remove", "", "| Entity | Alias removed |", "|---|---|"]
    out += [f"| #{f.entity_id} {f.entity_name} | {f.alias} |" for f in p.alias_fixes] or ["| (none) | |"]
    out += ["", "## 2. Catch-all entities split by city", ""]
    by_from = defaultdict(list)
    for m in p.moves:
        by_from[m.from_id].append(m)
    unpl = defaultdict(list)
    for u in p.unplaced:
        unpl[u.from_id].append(u)
    for gid, (name, etype) in sorted(p.generic.items(), key=lambda x: x[1][0]):
        ms, us = by_from.get(gid, []), unpl.get(gid, [])
        if not ms and not us:
            continue
        fate = "deleted afterwards" if gid in p.deletes else "kept (has unplaced rows or own affiliations)"
        out += [f"### #{gid} {name} ({etype}): {fate}", ""]
        counts = defaultdict(int)
        for m in ms:
            counts[m.target_name] += 1
        for t, c in sorted(counts.items()):
            ex = p.targets.get((t, etype))
            out.append(f"- {c} row(s) -> **{t}** ({'existing #' + str(ex) if ex else 'new entity'})")
        for u in us:
            out.append(f"- unplaced {u.kind} row #{u.row_id}: article #{u.article_id} “{u.article_title[:80]}”")
        out.append("")
    return "\n".join(out)
