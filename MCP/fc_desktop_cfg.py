"""Add or update the 'filmcraft' MCP server in Claude Desktop's config.

Usage: python fc_desktop_cfg.py <path-to-filmcraft-cli.exe> <port> <token> [--direct]
Run by FILMCRAFT.bat when REG_CLAUDE_DESKTOP=1. Keep this file, and
fc_bridge_keepalive.py, next to the .bat.

By default Desktop is pointed at fc_bridge_keepalive.py, which restarts the
FilmCraft bridge whenever it exits (so closing/restarting FilmCraft no longer
needs a Claude Desktop restart). Use --direct to register the bridge exe
directly, as before.

Handles both a normal install (%APPDATA%\\Claude) and a Microsoft Store / MSIX
install (%LOCALAPPDATA%\\Packages\\Claude_*\\LocalCache\\Roaming\\Claude).

Exit codes:  0 = config already up to date (no Desktop restart needed)
            10 = config changed (fully quit and reopen Claude Desktop)
             1 = error / no config found
"""
import glob
import json
import os
import shutil
import sys

args = [a for a in sys.argv[1:] if not a.startswith("--")]
direct = "--direct" in sys.argv
if len(args) != 3:
    sys.exit("usage: fc_desktop_cfg.py <filmcraft-cli.exe> <port> <token> [--direct]")

cli, port, token = args
name = "claude_desktop_config.json"
bridge_args = ["mcp", "--bridge", f"127.0.0.1:{port}"]

proxy = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                     "fc_bridge_keepalive.py")
if direct or not os.path.exists(proxy):
    if not direct:
        print("fc_bridge_keepalive.py not found next to this script - "
              "registering the bridge directly.")
    command, cmd_args = cli, bridge_args
else:
    command, cmd_args = sys.executable, [proxy, cli] + bridge_args

entry = {"command": command, "args": cmd_args,
         "env": {"FILMCRAFT_CONTROL_TOKEN": token}}

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

changed = False
for cfg_path in existing:
    cfg = {}
    with open(cfg_path, encoding="utf-8-sig") as fh:      # tolerate a BOM
        text = fh.read().strip()
        if text:
            try:
                cfg = json.loads(text)
            except ValueError as e:
                print(f"{cfg_path} is not valid JSON ({e}); left untouched.")
                sys.exit(1)

    servers = cfg.setdefault("mcpServers", {})
    if servers.get("filmcraft") == entry:
        print(f"Already up to date: {cfg_path}")
        continue

    shutil.copy2(cfg_path, cfg_path + ".filmcraft.bak")    # backup first
    servers["filmcraft"] = entry
    with open(cfg_path, "w", encoding="utf-8") as fh:
        json.dump(cfg, fh, indent=2)
    print(f"Updated {cfg_path} (backup: {name}.filmcraft.bak)")
    changed = True

sys.exit(10 if changed else 0)
