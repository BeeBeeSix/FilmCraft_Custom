# FilmCraft + Claude Desktop Setup

Connects Claude Desktop to FilmCraft through the FilmCraft MCP bridge, using the two files required:

| File | Role |
|---|---|
| `FILMCRAFT.bat` | Launches FilmCraft with a fresh control token, then registers the MCP bridge with Claude. |
| `fc_desktop_cfg.py` | Called by the `.bat`. Adds or updates the `filmcraft` entry in `claude_desktop_config.json`. |

Windows only (the launcher is a batch file).

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

1. Put `FILMCRAFT.bat` and `fc_desktop_cfg.py` **in the same folder**, anywhere you like (for example `C:\Tools\MCP\`). The `.bat` finds the script via `%~dp0`, so they must stay together.

## 2. Check the settings at the top of `FILMCRAFT.bat`

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

1. Close Claude Desktop completely (see step 6 for how).
2. Double-click `FILMCRAFT.bat`.
3. The script will:
   1. Check that FilmCraft and Anaconda exist.
   2. Generate a random 64-character token.
   3. Start FilmCraft with `--control 9876`.
   4. Run `fc_desktop_cfg.py`, which writes this entry into every Claude Desktop config it finds:

      ```json
      "mcpServers": {
        "filmcraft": {
          "command": "C:\\Program Files\\ArtCraft\\FilmCraft\\filmcraft-cli.exe",
          "args": ["mcp", "--bridge", "127.0.0.1:9876"],
          "env": { "FILMCRAFT_CONTROL_TOKEN": "<token>" }
        }
      }
      ```
   5. Print the token and leave a command prompt open. It has the token set and Anaconda (base) active.

Expect to see `Updated C:\Users\...\claude_desktop_config.json`.

## 6. Fully restart Claude Desktop

Claude Desktop reads its config only at launch, and closing the window is not enough.

1. Right-click the Claude icon in the system tray and choose **Quit**. (Or end `Claude.exe` in Task Manager.)
2. Reopen Claude Desktop.

## 7. Verify

- In Claude Desktop, open **Settings → Developer**. `filmcraft` should be listed as **running**.
- Start a new chat. The tools (hammer/connector) menu should show FilmCraft tools.
- If it shows as failed, open the MCP logs at `%APPDATA%\Claude\logs\mcp-server-filmcraft.log`. Microsoft Store installs keep them under `%LOCALAPPDATA%\Packages\Claude_*\LocalCache\Roaming\Claude\logs\`.

---

## Every-day workflow

The token changes **every time** `FILMCRAFT.bat` runs, and FilmCraft only accepts the token it was started with. So each session:

1. Quit Claude Desktop.
2. Run `FILMCRAFT.bat`.
3. Reopen Claude Desktop.

Use the prompt the `.bat` leaves open to run FilmCraft scripts, for example `python filmcraft_slideshow_gui.py`. It already has the token and Anaconda active.

## Optional: Claude Code

To register FilmCraft with Claude Code as well, set `REG_CLAUDE_CODE=1` (and optionally `LAUNCH_CLAUDE_CODE=1`). The script runs `claude mcp add --scope user ...` with the same token. Inside Claude Code, type `/mcp` to confirm `filmcraft` is connected.

## Troubleshooting

| Symptom | Fix |
|---|---|
| `Cannot find ...filmcraft.exe` | Fix `FC_DIR` / `FC_EXE` in the `.bat`. |
| `Cannot find ...filmcraft-cli.exe - skipping MCP registration` | Build or copy `filmcraft-cli.exe` into `FC_DIR`, or fix `FC_CLI`. FilmCraft still starts, but nothing is registered. |
| `Cannot find ...activate.bat` | Fix `CONDA_ACTIVATE`, e.g. `miniconda3` or a different install drive. |
| `Could not make a token` | `python` is not on PATH after Anaconda activation. Check the Anaconda install. |
| `No claude_desktop_config.json found` | Do step 4, then rerun the `.bat`. |
| FilmCraft tools missing or auth errors in Claude | Claude Desktop is holding an old token. Quit it fully from the tray, then reopen. If you restarted FilmCraft by hand, rerun `FILMCRAFT.bat` and restart Claude again. |
| Port already in use | Change `FILMCRAFT_CONTROL_PORT` in the `.bat` and rerun. |
| Config JSON error | `fc_desktop_cfg.py` fails if the file contains invalid JSON. Fix or reset the file, then rerun. |

## Security notes

- The token is written in plain text to `claude_desktop_config.json` and printed in the console. Treat that file as private.
- The bridge only talks to `127.0.0.1` (this machine).
- The script rewrites the config with `json.dump`, so formatting and comments in the file may change. Other servers and settings are kept.
- To remove the integration, delete the `filmcraft` block under `mcpServers` in the config and restart Claude Desktop.
