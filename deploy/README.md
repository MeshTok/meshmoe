# Deployment notes (siliconvalley / nanjing)

## Site (meshmoe.com -> siliconvalley)
Static root: `/var/www/meshmoe`, served by `/etc/nginx/sites-enabled/meshmoe`.

Deploy the site:

```bash
# from a checkout
tar -cf site.tar -C site-v2 .
scp -J nanjing site.tar siliconvalley:/tmp/
ssh -J nanjing siliconvalley 'mkdir -p /tmp/mmup && tar -xf /tmp/meshmoe-site.tar -C /tmp/mmup'
```

### nginx quirks that bit us

* `location = /privacy/` and `location = /terms/` were hard-coded to
  `try_files /privacy.html`, so the v2 directory pages were shadowed by the
  legacy Era-2 HTML. `deploy/fix-nginx-legal-pages.py` fixes this (idempotent,
  tests `nginx -t` and auto-rolls back on failure).
* **Never write backups into `/etc/nginx/sites-enabled/`** - nginx loads every
  file in that directory and a `.bak` copy causes
  `duplicate listen options` and a failed reload. Backups go to
  `/etc/nginx/backups/`.

## API (api.meshmoe.com -> nanjing)

* pm2 services: router(4001), credits(4005), gateway(4006), edge(4002),
  bootstrap(4003/4004), litellm(4000).
* Credits server source: `/www/wwwroot/meshmoe.com/meshmoe/credits/server.py`.
* Apply schema/patch scripts from `server/` with `python3 /tmp/<patch>.py`;
  they are idempotent and keep a `.bak-*` next to the file.
* DB credentials live in `/www/wwwroot/meshmoe.com/meshtok/.env` (`PG_PASSWORD`).

## Push path

Direct `git push` from the Windows dev box fails (proxy 502 / TLS). Working
route, used by this repo:

```bash
git bundle create /tmp/p.bundle main
scp -J nanjing /tmp/p.bundle siliconvalley:/tmp/
ssh -J nanjing siliconvalley 'cd /tmp && rm -rf pr && mkdir pr && cd pr \
  && git init -q && git remote add origin <repo-url> \
  && git fetch -q /tmp/p.bundle main:refs/heads/main \
  && git push origin refs/heads/main:refs/heads/main'
```

Siliconvalley has no stored GitHub credential: pass the remote URL (with its
token) in the remote command, never commit it.
