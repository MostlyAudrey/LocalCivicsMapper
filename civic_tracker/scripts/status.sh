#!/usr/bin/env bash
# Pipeline status: article counts per source and status, entity/event totals, recent failures.
cd "$(dirname "$0")/.."
.venv/bin/python - <<'EOF'
import psycopg
from civic_tracker.config import settings

with psycopg.connect(settings.DATABASE_URL) as c:
    print(f"{'source':32} {'PENDING':>8} {'STAGE1':>7} {'DONE':>6} {'SKIP':>6} {'FAIL':>6}")
    for r in c.execute("""
        SELECT s.name,
               COUNT(*) FILTER (WHERE a.processing_status = 'PENDING'),
               COUNT(*) FILTER (WHERE a.processing_status = 'STAGE1_DONE'),
               COUNT(*) FILTER (WHERE a.processing_status = 'PROCESSED'),
               COUNT(*) FILTER (WHERE a.processing_status = 'SKIPPED'),
               COUNT(*) FILTER (WHERE a.processing_status = 'FAILED')
        FROM TB_Sources s LEFT JOIN TB_Article a ON a.source_id = s.id
        GROUP BY s.id, s.name ORDER BY s.id"""):
        print(f"{r[0][:32]:32} {r[1]:>8} {r[2]:>7} {r[3]:>6} {r[4]:>6} {r[5]:>6}")
    ent, ev, ee = c.execute("SELECT (SELECT COUNT(*) FROM TB_Entity), (SELECT COUNT(*) FROM TB_Event), (SELECT COUNT(*) FROM TB_Entity_Event)").fetchone()
    print(f"\nentities={ent} events={ev} entity-event links={ee}")
    fails = c.execute("SELECT id, url, LEFT(SPLIT_PART(error_log, E'\\n', -2), 160) FROM TB_Article WHERE processing_status='FAILED' ORDER BY id DESC LIMIT 5").fetchall()
    if fails:
        print("\nrecent failures:")
        for f in fails:
            print(f"  #{f[0]} {f[1]}\n     {f[2]}")
EOF
