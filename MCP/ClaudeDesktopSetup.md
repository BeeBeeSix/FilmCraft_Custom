# FilmCraft + Claude Desktop Setup

Connects Claude Desktop to FilmCraft through the FilmCraft MCP bridge, using the three files in `MCP.zip`:

| File | Role |
|---|---|
| `FILMCRAFT.bat` | Launches FilmCraft with a persistent control token, then registers the MCP bridge with Claude. |
| `fc_desktop_cfg.py` | Called by the `.bat`. Adds or updates the `filmcraft` entry in `claude_desktop_config.json`. |
| `fc_bridge_keepalive.py` | Runs between Claude Desktop and the FilmCraft bridge and restarts the bridge if it exits, so the connector survives FilmCraft restarts. |

Windows only (the launcher is a batch file).

---

## What changed in this version

- **Keep-alive bridge.** Claude Desktop starts an MCP server once and never restarts it if it exits. The FilmCraft bridge exits when FilmCraft closes or restarts, which left the connector dead until Desktop was restarted. `fc_bridge_keepalive.py` now restarts the bridge automatically and replays the MCP handshake.
- **Persistent token.** The token is created on the first run and saved next to the `.bat` in `.filmcraft_token`. It is reused on every later run (previously it changed every run).
- **Restart only when needed.** `fc_desktop_cfg.py` reports whether it actually changed the config, and the `.bat` only tells you to restart Claude Desktop in that case.
- **Safer config handling.** The script reads files that start with a hidden BOM, refuses to touch invalid JSON, and keeps a backup (`claude_desktop_config.json.filmcraft.bak`) before changing anything.

---

## 1. Prerequisites

- [ ] **Claude Desktop** installed and opened at least once. Both the normal installer and the Microsoft Store (MSIX) build are supported.
- [ ] **FilmCraft** installed at `C:\Program Files\ArtCraft\FilmCraft\`, containing:
  - `filmcraft.exe`
  - `filmcraft-cli.exe` (the MCP bridge; build or copy it here if it is missing)
- [ ] **Anaconda** installed at `%USERPROFILE%\anaconda3` (the `.bat` calls `anaconda3\Scripts\activate.bat`).
- [ ] `python` works after Anaconda activates. It is used to generate the token and run `fc_desktop_cfg.py`.

If any path differs on your machine, change it in step 3.

## 2. Install the files

1. Extract `MCP.zip`.
2. Put `FILMCRAFT.bat`, `fc_desktop_cfg.py` and `fc_bridge_keepalive.py` **in the same folder**, anywhere you like (for example `C:\Tools\MCP\`). The `.bat` finds the scripts via `%~dp0`, so they must stay together.

> **Don't move the folder later without rerunning the `.bat`.** Claude Desktop's config stores the full path to `fc_bridge_keepalive.py` and to the Anaconda `python.exe` that was active when you ran the `.bat`. If either moves, rerun `FILMCRAFT.bat` and restart Claude Desktop once.

## 3. Check the settings at the top of `FILMCRAFT.bat`

Open it in a text editor and confirm:

```bat
set "FC_DIR=C:\Program Files\ArtCraft\FilmCraft"   rem FilmCraft install folder
set "FC_EXE=filmcraft.exe"
set "FC_CLI=filmcraft-cli.exe"
set "FILMCRAFT_CONTROL_PORT=9876"                  rem change only if 9876 is in use

set "REG_CLAUDE_CODE=0"        rem 1 = also register with Claude Code
set "LAUNCH_CLAUDE_CODE=0"     rem 1 = start Claude Code in this window
set "REG_CLAUDE_DESKTOP=1"     rem must be 1 for Claude Desktop
```

For Claude Desktop only, leave `REG_CLAUDE_DESKTOP=1` and the two Claude Code options at `0`.

## 4. Create the Claude Desktop config (first time only)

`fc_desktop_cfg.py` updates an existing config file. It does not create one. If none exists it prints an error and exits.

1. Open Claude Desktop.
2. Go to **Settings → Developer → Edit Config**. This creates `claude_desktop_config.json`.
3. Close the file. No edits are needed.

## 5. Run the launcher

1. Double-click `FILMCRAFT.bat`.
2. The script will:
   1. Check that FilmCraft and Anaconda exist.
   2. Read the saved token from `.filmcraft_token`, or generate a random 64-character token and save it on the first run.
   3. Start FilmCraft with `--control 9876` and that token.
   4. Run `fc_desktop_cfg.py`, which writes this entry into every Claude Desktop config it finds (the paths are examples):

      ```json
      "mcpServers": {
        "filmcraft": {
          "command": "C:\\Users\\<you>\\anaconda3\\python.exe",
          "args": [
            "C:\\Tools\\MCP\\fc_bridge_keepalive.py",
            "C:\\Program Files\\ArtCraft\\FilmCraft\\filmcraft-cli.exe",
            "mcp", "--bridge", "127.0.0.1:9876"
          ],
          "env": { "FILMCRAFT_CONTROL_TOKEN": "<token>" }
        }
      }
      ```
   5. Tell you whether the config changed, print the token and leave a command prompt open. The prompt has the token set and Anaconda (base) active.

You will see one of these messages:

| Message | Meaning |
|---|---|
| `Updated ...claude_desktop_config.json` and *"Claude Desktop config changed. Fully quit Claude Desktop..."* | The config was changed. Do step 6. |
| `Already up to date` and *"no restart needed"* | Nothing changed. Skip step 6. |
| *"Claude Desktop config was NOT updated"* | An error. Read the message above it (see Troubleshooting). |

## 6. Fully restart Claude Desktop (only when told to)

Claude Desktop reads its config only at launch, and closing the window is not enough.

1. Right-click the Claude icon in the system tray and choose **Quit**. (Or end `Claude.exe` in Task Manager.)
2. Reopen Claude Desktop.

## 7. Verify

- In Claude Desktop, open **Settings → Developer**. `filmcraft` should be listed as **running**.
- Start a **new** chat. The tools (hammer/connector) menu should show FilmCraft tools.
- Open the MCP log at `%APPDATA%\Claude\logs\mcp-server-filmcraft.log` (Microsoft Store installs keep it under `%LOCALAPPDATA%\Packages\Claude_*\LocalCache\Roaming\Claude\logs\`). A healthy start shows:
  - `[keepalive] supervising: ...filmcraft-cli.exe mcp --bridge 127.0.0.1:9876`
  - `Server started and connected successfully`

---

## Every-day workflow

1. Run `FILMCRAFT.bat` (this starts FilmCraft with the saved token).
2. Open Claude Desktop (or leave it running).
3. Work.

You normally do **not** need to restart Claude Desktop. If FilmCraft is closed or restarted, the keep-alive layer restarts the bridge within a few seconds, and the tools come back by themselves. Always start FilmCraft through `FILMCRAFT.bat`, so it uses the same token Claude has.

Restart Claude Desktop only when the `.bat` says the config changed. That happens:

- the first time you set this up
- after you delete `.filmcraft_token` to force a new token
- after you move the folder, change the port, or change which Anaconda `python.exe` the `.bat` uses

Use the prompt the `.bat` leaves open to run FilmCraft scripts, for example `python filmcraft_slideshow_gui.py`. It already has the token and Anaconda active.

## What happens when FilmCraft isn't running

If FilmCraft is closed, the keep-alive layer retries starting the bridge (waiting up to 10 seconds between attempts). Any tool call made in the meantime returns an immediate message that FilmCraft is reconnecting, instead of hanging. Such a call did **not** run, so repeat it once FilmCraft is back. Claude's tools return automatically when FilmCraft starts again. If you opened the current chat before the drop, start a new chat if the tools don't reappear.

## Optional: Claude Code

To register FilmCraft with Claude Code as well, set `REG_CLAUDE_CODE=1` (and optionally `LAUNCH_CLAUDE_CODE=1`). The script runs `claude mcp add --scope user ...` with the same token. Claude Code registers the bridge directly, without the keep-alive layer. Inside Claude Code, type `/mcp` to confirm `filmcraft` is connected.

## Registering the bridge directly (no keep-alive)

To go back to the old behaviour, add `--direct` to the `python "%~dp0fc_desktop_cfg.py" ...` line in `FILMCRAFT.bat`, rerun it and restart Claude Desktop. Desktop then starts `filmcraft-cli.exe` itself and will not restart it if it exits.

## Troubleshooting

| Symptom | Fix |
|---|---|
| `Cannot find ...filmcraft.exe` | Fix `FC_DIR` / `FC_EXE` in the `.bat`. |
| `Cannot find ...filmcraft-cli.exe - skipping MCP registration` | Build or copy `filmcraft-cli.exe` into `FC_DIR`, or fix `FC_CLI`. FilmCraft still starts, but nothing is registered. |
| `Cannot find ...activate.bat` | Fix `CONDA_ACTIVATE`, e.g. `miniconda3` or a different install drive. |
| `Could not make a token` | `python` is not on PATH after Anaconda activation. Check the Anaconda install. |
| `No claude_desktop_config.json found` | Do step 4, then rerun the `.bat`. |
| `...is not valid JSON ... left untouched` | The config file has a syntax error. Fix it (or restore `claude_desktop_config.json.filmcraft.bak`) and rerun. The script never overwrites an invalid file. |
| `fc_bridge_keepalive.py not found next to this script` | The script keeps all three files together. Copy it next to the `.bat`, or the bridge is registered directly with no keep-alive. |
| Tools show *"FilmCraft bridge is not connected - reconnecting"* | FilmCraft isn't running, or is running with a different token. Start it with `FILMCRAFT.bat` and try again. |
| FilmCraft tools missing or auth errors in Claude | FilmCraft was started by hand without the saved token, or the token changed. Run `FILMCRAFT.bat`; restart Claude Desktop only if it says the config changed. |
| Desktop can't start the connector after moving files or reinstalling Anaconda | The config still points at the old paths. Rerun `FILMCRAFT.bat` and restart Claude Desktop. |
| Port already in use | Change `FILMCRAFT_CONTROL_PORT` in the `.bat` and rerun. |
| `filmcraft` shows as failed in Settings → Developer | Open the log (step 7). `[keepalive]` lines say whether the bridge started or kept exiting. Paste them when asking for help. |

## Known limits

- The keep-alive layer detects a bridge that **exits**. A bridge that stays running but has silently lost its connection to FilmCraft is not detected.
- It restarts the bridge, not FilmCraft itself.
- It has been tested against a stand-in bridge that was crashed on purpose, and confirmed working with the real FilmCraft bridge. If the connector still drops, send the `[keepalive]` lines from the log.
- It works only in Claude Desktop. Claude on the web or phone cannot reach local tools.

## Security notes

- The token is written in plain text to `claude_desktop_config.json`, to `.filmcraft_token` next to the `.bat`, and printed in the console. Treat these as private and don't share the folder.
- The bridge only talks to `127.0.0.1` (this machine).
- The script rewrites the config with `json.dump`, so formatting and comments in the file may change. Other servers and settings are kept, and a backup is saved first as `claude_desktop_config.json.filmcraft.bak`.
- To remove the integration, delete the `filmcraft` block under `mcpServers` in the config and restart Claude Desktop. You can also delete `.filmcraft_token` to retire the token.