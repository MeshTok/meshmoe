#!/usr/bin/env python3
"""Patch production credits server: create the user account only AFTER
node existence + ownership + token checks pass.

Before: any POST /claim_node with a bogus node_id created a user row
(with 500 starter credits). That is an unauthenticated account-creation
hole. After: account creation happens last, inside the verified branch.
"""
import io
import sys

PATH = "/www/wwwroot/meshmoe.com/meshmoe/credits/server.py"

A_OLD = '''                user = get_user(conn, username)
                with conn.cursor() as cur:
                    cur.execute("SELECT node_token_hash, owner_user_id FROM nodes WHERE peer_id = %s FOR UPDATE", (node_id,))'''
A_NEW = '''                # 账号必须在 node/token 校验通过后才创建，否则任意邮箱 POST 一个
                # 不存在的 node_id 就能凭空建号并拿到 500 初始 credits。
                with conn.cursor() as cur:
                    cur.execute("SELECT node_token_hash, owner_user_id FROM nodes WHERE peer_id = %s FOR UPDATE", (node_id,))'''

B_OLD = '''                        self._send({"error": "invalid node token"}, 403)
                        return
                    cur.execute("UPDATE nodes SET owner_user_id = %s WHERE peer_id = %s", (user["id"], node_id))'''
B_NEW = '''                        self._send({"error": "invalid node token"}, 403)
                        return
                    # 校验通过，这才建号（已存在则复用）
                    user = get_user(conn, username)
                    cur.execute("UPDATE nodes SET owner_user_id = %s WHERE peer_id = %s", (user["id"], node_id))'''

src = io.open(PATH, encoding="utf-8").read()

if "校验通过，这才建号" in src:
    print("ALREADY_PATCHED")
    sys.exit(0)

if src.count(A_OLD) != 1 or src.count(B_OLD) != 1:
    print("PATTERN_MISMATCH a=%d b=%d" % (src.count(A_OLD), src.count(B_OLD)))
    sys.exit(2)

src = src.replace(A_OLD, A_NEW).replace(B_OLD, B_NEW)

io.open(PATH + ".bak-claimorder", "w", encoding="utf-8").write(
    io.open(PATH, encoding="utf-8").read()
)
io.open(PATH, "w", encoding="utf-8").write(src)

import ast
ast.parse(src)
print("PATCHED_OK")
