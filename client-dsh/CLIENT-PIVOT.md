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
MeshMoe-Client-Setup.exe
├── dsh/                     # DeepSeek Harness (desktop profile)
├── runtime/
│   ├── llama-bin/           # llama.cpp server (CUDA build, CPU fallback)
│   └── MeshMoe-4B-Q4_K_M.gguf
├── dsh-plugin-meshmoe/      # our plugin (config merge + honor polling)
└── scripts/
    ├── start-client.cmd     # boot llama-server (:8081) → DSH desktop
    └── install-node.cmd     # register anonymous node, persist ~/.meshmoe/node.json
```

## User experience

1. Download & run installer (no admin, no account).
2. First launch: llama-server loads MeshMoe-4B (32K ctx, tool-calling on, no-thinking mode).
3. DSH opens with `meshmoe-4b` as a selectable model — agent works offline, free.
4. In background: edge node self-registers to `api.meshmoe.com` (token in `~/.meshmoe/node.json`), joins mesh, earns **Joules**.
5. Any time later: claim the node on meshmoe.com to attach a public identity (honor card, tiers).

## Migration

- Existing 0.3.x installs keep working; downloads page stays until the new installer ships.
- No data migration needed — Joules are keyed by node identity, not by client version.
- Flutter repos stay archived for reference; no further investment.

## Next steps (P0)

1. NSIS/Inno Setup script that stitches the bundle above (runtime + gguf + DSH + plugin).
2. `start-client.cmd` smoke test on a clean VM (no GPU → CPU fallback path).
3. Replace meshmoe.com/downloads entry point with the new installer.
