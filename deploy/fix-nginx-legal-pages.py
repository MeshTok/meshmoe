#!/usr/bin/env python3
"""Point /privacy/ and /terms/ at the new v2 pages (dir index.html),
keeping the legacy .html files as a fallback. Idempotent, backs up first."""
import io, sys, subprocess, datetime, os

PATH = "/etc/nginx/sites-enabled/meshmoe"
OLD = """    location = /terms/ { try_files /terms.html =404; }
    location = /privacy/ { try_files /privacy.html =404; }"""
NEW = """    # 2026-11-15: serve the v2 privacy/terms pages (dir index.html) first;
    # fall back to the legacy .html files if the directory is missing.
    location = /terms/ { try_files /terms/index.html /terms.html =404; }
    location = /privacy/ { try_files /privacy/index.html /privacy.html =404; }"""

src = io.open(PATH, encoding="utf-8").read()
if "/terms/index.html" in src:
    print("ALREADY_PATCHED")
    sys.exit(0)

if src.count(OLD) != 1:
    print("PATTERN_MISMATCH count=%d" % src.count(OLD))
    sys.exit(2)

stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
# IMPORTANT: never back up into sites-enabled/ - nginx loads every file there.
bakdir = "/etc/nginx/backups"
os.makedirs(bakdir, exist_ok=True)
bak = os.path.join(bakdir, "meshmoe-legal-" + stamp)
io.open(bak, "w", encoding="utf-8").write(src)
print("BACKUP", bak)

io.open(PATH, "w", encoding="utf-8").write(src.replace(OLD, NEW))
print("PATCHED_OK")

rc = subprocess.call(["nginx", "-t"])
if rc != 0:
    io.open(PATH, "w", encoding="utf-8").write(src)
    print("NGINX_TEST_FAILED_ROLLED_BACK")
    sys.exit(3)
print("NGINX_TEST_OK")
subprocess.call(["systemctl", "reload", "nginx"])
print("RELOADED")
