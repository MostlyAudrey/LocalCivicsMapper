"""Condensed affiliations (lib/affiliations.py). No database needed:
    /root/.venv/bin/python tests/test_affiliations.py"""
import sys
from datetime import date

sys.path.insert(0, ".")
from lib.affiliations import condense, condense_members, role_family  # noqa: E402

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


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
