"""Add or update the 'filmcraft' MCP server in Claude Desktop's config.

Usage: python fc_desktop_cfg.py <path-to-filmcraft-cli.exe> <port> <token>
Run by FILMCRAFT.bat when REG_CLAUDE_DESKTOP=1. Keep this file next to the .bat.

Handles both a normal install (%APPDATA%\\Claude) and a Microsoft Store / MSIX
install (%LOCALAPPDATA%\\Packages\\Claude_*\\LocalCache\\Roaming\\Claude).
"""
import glob
import json
import os
import sys

if len(sys.argv) != 4:
    sys.exit("usage: fc_desktop_cfg.py <filmcraft-cli.exe> <port> <token>")

cli, port, token = sys.argv[1:4]
name = "claude_desktop_config.json"

candidates = []
local = os.environ.get("LOCALAPPDATA", "")
# Store / MSIX installs first, then the regular location
candidates += glob.glob(os.path.join(local, "Packages", "Claude_*", "LocalCache", "Roaming", "Claude", name))
candidates += glob.glob(os.path.join(local, "Packages", "*Claude*", "LocalCache", "Roaming", "Claude", name))
candidates.append(os.path.join(os.environ.get("APPDATA", ""), "Claude", name))

existing = [p for p in dict.fromkeys(candidates) if os.path.exists(p)]
if not existing:
    print("No claude_desktop_config.json found. In Claude Desktop open")
    print("Settings > Developer > Edit Config once to create it, then rerun this.")
    sys.exit(1)

for cfg_path in existing:
    cfg = {}
    with open(cfg_path, encoding="utf-8") as fh:
        text = fh.read().strip()
        if text:
            cfg = json.loads(text)

    cfg.setdefault("mcpServers", {})["filmcraft"] = {
        "command": cli,
        "args": ["mcp", "--bridge", f"127.0.0.1:{port}"],
        "env": {"FILMCRAFT_CONTROL_TOKEN": token},
    }

    with open(cfg_path, "w", encoding="utf-8") as fh:
        json.dump(cfg, fh, indent=2)
    print(f"Updated {cfg_path}")
