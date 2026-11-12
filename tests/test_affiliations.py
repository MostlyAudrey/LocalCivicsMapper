"""Condensed affiliations (lib/affiliations.py). No database needed:
    /root/.venv/bin/python tests/test_affiliations.py"""
import sys
from datetime import date

sys.path.insert(0, ".")
from lib.affiliations import condense, condense_members, group_members, role_family  # noqa: E402

CAC, CTE = "Winters Climate Action Commission", "Winters Climate Action Committee"


def row(role, org=None, oid=None, first=(2024, 1, 1), last=None):
    return {"role_title": role, "organization": org, "organization_id": oid,
            "first_seen_date": date(*first), "last_seen_date": date(*(last or first))}


def test_role_families():
    assert role_family("Commission Member", CAC) == ("member", "Commissioner")
    assert role_family("Commissioner, Winters Climate Action Commission", None)[0] == "member"
    assert role_family("Mayor Pro Tempore", "Woodland City Council") == ("mayor-pro-tem", "Mayor Pro Tem")
    assert role_family("City Council Member", "Winters City Council")[0] == "councilmember"
    assert role_family("Vice Chair", CAC)[0] == "vice-chair" and role_family("Chair", CAC)[0] == "chair"
    assert role_family("City Clerk of Woodland", None)[1] == "City Clerk"


def test_synonyms_merge_into_one_line_with_full_range():
    rows = [row("Commission Member", CAC, 1, (2022, 5, 9), (2026, 4, 13)),
            row("Commissioner", CAC, 1, (2024, 11, 11), (2026, 5, 11)),
            row("Climate Action Commission Member", None, None, (2025, 11, 12)),  # names the body
            row("Commissioner", CTE, 2, (2024, 12, 9), (2026, 9, 8)),
            row("Commissioner", None, None, (2022, 7, 11), (2026, 9, 8)),           # bare: held most at CAC
            row("Vice Chair", CAC, 1, (2022, 10, 10), (2025, 7, 14))]
    out = condense(rows)
    cac = next(o for o in out if o["organization"] == CAC)
    member = next(r for r in cac["roles"] if r["label"] == "Commissioner")
    assert (member["first_seen"], member["last_seen"]) == (date(2022, 5, 9), date(2026, 9, 8))
    assert "Climate Action Commission Member" in member["variants"]
    assert [r["label"] for r in cac["roles"]].count("Commissioner") == 1
    assert all(o["organization"] for o in out)  # no org-less leftovers


def test_city_rows_fold_into_the_city_body():
    out = condense([row("Councilmember", "Winters City Council", 5, (2023, 3, 13), (2026, 8, 17)),
                    row("City Councilmember", "City of Winters", 6, (2026, 4, 20))])
    assert [o["organization"] for o in out] == ["Winters City Council"]


def test_members_one_line_per_person():
    people = condense_members([
        {"id": 1, "canonical_name": "Ken Halvorsen", "role_title": "Commissioner", "organization": CAC,
         "last_seen_date": date(2026, 1, 1)},
        {"id": 1, "canonical_name": "Ken Halvorsen", "role_title": "Commission Member", "organization": CAC,
         "last_seen_date": date(2025, 1, 1)},
        {"id": 1, "canonical_name": "Ken Halvorsen", "role_title": "Chair", "organization": CAC,
         "last_seen_date": date(2025, 6, 1)}])
    assert len(people) == 1 and people[0]["role_title"] == "Commissioner, Chair"


COUNCIL = "Woodland City Council"


def mem(pid, name, role, last):
    return {"id": pid, "canonical_name": name, "role_title": role, "organization": COUNCIL,
            "last_seen_date": date(*last)}


def test_group_members_uses_roll_call_roster():
    rows = [mem(1, "Tom Penhallow", "Mayor", (2026, 10, 21)), mem(2, "Victoria Ibarrola", "Councilmember", (2024, 8, 20)),
            mem(3, "Marissa Quill", "City Clerk", (2026, 11, 6)), mem(4, "Jesse Ondrako", "Candidate", (2026, 10, 21)),
            mem(5, "Castell", "Councilmember", (2026, 9, 15)), mem(6, "David Castell", "Councilmember", (2026, 9, 15)),
            mem(7, "Mayra Ruelas", "Mayor Pro Tem", (2026, 11, 6))]
    roster = {1: {"canonical_name": "Tom Penhallow"}, 6: {"canonical_name": "David Castell"}}
    g = group_members(rows, roster, date(2026, 9, 15))
    assert [p["canonical_name"] for p in g["current"]] == ["David Castell", "Mayra Ruelas", "Tom Penhallow"]  # Ruelas: seen in a seat since
    assert [p["canonical_name"] for p in g["former"]] == ["Victoria Ibarrola"]   # surname-only "Castell" hidden
    assert [p["canonical_name"] for p in g["staff"]] == ["Marissa Quill"]        # candidate left out
    assert g["from_minutes"] and g["as_of"] == date(2026, 11, 6)


def test_group_members_without_minutes_uses_recency():
    rows = [mem(1, "Ann Recent", "Commissioner", (2026, 8, 1)), mem(2, "Bob Old", "Commissioner", (2024, 1, 1)),
            mem(3, "Cy Former", "Former Commissioner", (2026, 7, 1)), mem(4, "Liz Liaison", "Liaison", (2026, 8, 1))]
    g = group_members(rows)
    assert [p["canonical_name"] for p in g["current"]] == ["Ann Recent"]
    assert {p["canonical_name"] for p in g["former"]} == {"Bob Old", "Cy Former"}
    assert [p["canonical_name"] for p in g["staff"]] == ["Liz Liaison"] and not g["from_minutes"]


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
