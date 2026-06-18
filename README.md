# MAP — Minecraft Admin Panel

![Python](https://img.shields.io/badge/python-3.12-blue?logo=python&logoColor=white)
![Flask](https://img.shields.io/badge/flask-3.0%2B-black?logo=flask&logoColor=white)
![Docker](https://img.shields.io/badge/docker-required-2496ED?logo=docker&logoColor=white)
![License](https://img.shields.io/badge/license-GPLv3-blue)

A self-hosted web panel for managing Minecraft servers in Docker.
Built with Flask + vanilla JS.

---

## Requirements

- Docker (running and accessible to the current user)
- Python 3.9+
- `pip install flask`

---

## Quick Start

```bash
# 1. Install dependencies
pip install -r requirements.txt

# 2. Start the panel
python app.py

# 3. Open in browser
open http://localhost:5000
```

---

## Features

| Feature | Details |
|---|---|
| **Launch servers** | Spins up `itzg/minecraft-server` containers with customisable type, version, memory, port, difficulty, MOTD |
| **Console** | Live-tailing logs (4 s poll) + free-text command input + quick-command shortcuts |
| **Players & OPs** | OP/De-OP, Kick/Ban/Pardon, Give items, switch Game Mode — all via RCON |
| **Resource stats** | Per-container CPU %, memory, network I/O, disk I/O (5 s poll) |
| **Backups** | Create labelled `.tar.gz` snapshots of the world volume, restore, download, delete |
| **Multi-server** | Manage unlimited server instances simultaneously |

---

## Architecture

```
browser  ──HTTP──▶  Flask (app.py)  ──subprocess──▶  docker CLI
                         │
                    /backups/          (tar.gz archives)
```

The panel talks to Docker via the `docker` CLI (no Docker socket bind-mount
needed). Every Minecraft container is tagged with `mc-panel=true` so the panel
only sees its own servers.

---

## Server types supported

Via `itzg/minecraft-server` image:

- Paper (default, best performance)
- Vanilla
- Fabric
- Forge
- Spigot
- Purpur

---

## Ports

Each server uses two ports:

| Port | Purpose |
|---|---|
| `<N>` | Minecraft clients |
| `<N+1>` | RCON (used for in-panel commands) |

Default first server: `25565` (game) / `25566` (RCON).

---

## Backup storage

Backups are stored in `./backups/` as `<server-name>_<label>.tar.gz`.
They contain the full `/data` volume (world, configs, plugins, etc.).

---

## Security note

This panel is intended for trusted local networks or personal use.
It does not include authentication. Do **not** expose it to the public internet
without adding a reverse proxy with auth (e.g. nginx + basic auth, or Authelia).

---

## License

This project is licensed under the [GNU General Public License v3.0](LICENSE).
