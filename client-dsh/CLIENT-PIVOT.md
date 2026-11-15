# MeshMoe Client — Pivot to DSH (2026-11-14)

## Decision

**Deprecated**: the Flutter Desktop client (`meshmoe-app` / `meshmoe-flutter` / `meshmoe-client`, shipped as `MeshMoE_0.3.x_x64-setup.exe`).

**New route**: MeshMoe Client = **DSH + MeshMoe-4B runtime + dsh-plugin-meshmoe**, packaged as one installer.

## Why

| | Flutter 0.3.x | DSH-based |
|---|---|---|
| What user gets | A custom chat app, chat only | A full **agent** (tools, files, code, MCP) |
| Reuse | All custom code to maintain | DSH maintained upstream; we ship a plugin + config patch |
| Install → useful | Needs onboarding, account | **Install → agent works immediately**, no registration |
| Mesh membership | Custom implementation | Edge node + Joule metering already live (router :4001 / credits :4005 in production) |
| Acquisition | Standalone app | Also distributes as a **DSH plugin** (`MeshTok/dsh-plugin-meshmoe`, listed in awesome-dsh-plugin) |

The Flutter client duplicates what DSH does better, with none of DSH's ecosystem pull. Every install via the plugin or the client is one more node in the mesh — same outcome, 10x less code.

## What ships

```
MeshMoe-Node-Setup.exe  (user-level, no admin)
├── runtime/
│   ├── llama-bin/           # llama.cpp server (CUDA build, CPU fallback backends)
│   └── models/MeshMoe-4B-Q4_K_M.gguf  (2.7 GB)
├── node/edge_node.py        # mesh membership, Joule metering, connector mode
├── scripts/
│   ├── start-node.cmd       # llama-server (:8081) -> edge node (:4002)
│   ├── start-client.cmd     # same + DSH desktop for existing DSH users
│   ├── enable-autostart.cmd # optional schtasks run-at-logon (reversible)
│   └── uninstall-node.cmd
└── uninstall.exe
```

Existing DSH users do **not** need this package: `dsh plugin install dsh-plugin-meshmoe` adds the local `meshmoe-local` provider and auto-starts the runtime + edge node.

## User experience

1. Download & run installer (no admin, no account).
2. First launch: llama-server loads MeshMoe-4B (32K ctx, tool-calling on, no-thinking mode). GPU when available, CPU otherwise (`MESHMOE_NO_GPU=1`).
3. DSH opens with `meshmoe-4b` as a selectable model — agent works offline, free (for users who installed DSH + plugin).
4. In background: edge node self-registers to `api.meshmoe.com` (token in `~/.meshmoe/node.json`), joins mesh, earns **Joules**.
5. Any time later: claim the node on meshmoe.com to attach a public identity (honor card, tiers).

## Migration

- Existing 0.3.x installs keep working; downloads page stays until the new installer ships.
- No data migration needed — Joules are keyed by node identity, not by client version.
- Flutter repos stay archived for reference; no further investment.

## Status (2026-11-15) — all P0 items shipped

1. **NSIS installer**: shipped. `installer.nsi` + `build-installer.cmd` produce `dist\MeshMoe-Node-<ver>-x64-setup.exe` containing the runtime, the 2.7 GB model, `edge_node.py`, launchers, uninstaller and optional autostart. User-level install, no admin, no DSH desktop bundled.
2. **CPU fallback**: `MESHMOE_NO_GPU=1` uses `-ngl 0`; llama.cpp ships CPU backends for every x86 feature level.
3. **Download entry**: `meshmoe.com/downloads/` now links to the GitHub release and the plugin repo instead of the bare repository.
4. **Plugin auto-join**: `dsh-meshmoe` now starts the edge node on load, so a DSH user becomes a mesh node without touching the terminal.
5. **Not done (deliberate)**: bundling a full DSH desktop build into the installer; the standalone installer targets users without DSH, and DSH users get the plugin. No clean-VM smoke test on a second physical machine yet (verified on the dev machine).
