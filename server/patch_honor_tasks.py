#!/usr/bin/env python3
"""Patch production credits server: make the honor card's task count self-heal.

nodes.total_tasks is only incremented by earn_credits() (the gateway billing
path). Nodes served through the router never touch it, so their public honor
card shows "0 tasks" forever even though the ledger holds the real history.
Count distinct task_ids from joule_ledger and take the larger number.
"""
import io
import sys
import ast

PATH = "/www/wwwroot/meshmoe.com/meshmoe/credits/server.py"

OLD = '''        owner_joules = 0
        if row[4]:
            cur.execute("SELECT COALESCE(SUM(joules),0) FROM joule_ledger WHERE owner_user_id = %s", (row[4],))
            owner_joules = cur.fetchone()[0]
    return {"node_id": row[0], "joules": row[1], "tasks": row[2], "tokens": row[3],'''

NEW = '''        owner_joules = 0
        if row[4]:
            cur.execute("SELECT COALESCE(SUM(joules),0) FROM joule_ledger WHERE owner_user_id = %s", (row[4],))
            owner_joules = cur.fetchone()[0]
        # nodes.total_tasks only moves on the earn_credits() path; router-served
        # nodes stay 0. Self-heal from the append-only ledger.
        ledger_tasks = 0
        try:
            cur.execute("SELECT COUNT(DISTINCT task_id) FROM joule_ledger "
                        "WHERE node_id = %s AND task_id IS NOT NULL", (node_id,))
            ledger_tasks = cur.fetchone()[0] or 0
        except Exception:
            pass
        tasks = max(row[2] or 0, ledger_tasks)
    return {"node_id": row[0], "joules": row[1], "tasks": tasks, "tokens": row[3],'''

src = io.open(PATH, encoding="utf-8").read()

if "ledger_tasks" in src:
    print("ALREADY_PATCHED")
    sys.exit(0)

if src.count(OLD) != 1:
    print("PATTERN_MISMATCH count=%d" % src.count(OLD))
    sys.exit(2)

io.open(PATH + ".bak-honortasks", "w", encoding="utf-8").write(src)
new = src.replace(OLD, NEW)
ast.parse(new)
io.open(PATH, "w", encoding="utf-8").write(new)
print("PATCHED_OK")
