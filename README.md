# vizard-mcp

An [MCP](https://modelcontextprotocol.io) server that connects AI assistants such as Claude to a **running [WorldViz Vizard](https://www.worldviz.com/vizard) application**.

The assistant can look at the scene graph by the names your script uses, change nodes, run Python inside the app, **see** the result (screenshots, labelled screenshots), click GUI buttons and press keys, count what makes a scene slow, and look up Vizard's own API, all while the app keeps running.

This repository is the **basic edition**. A **full edition** with more tools is available separately; see [Full edition](#full-edition).

> Unofficial community project. Not affiliated with or endorsed by WorldViz. "Vizard" and "WorldViz" are trademarks of WorldViz, Inc.

## Why

Vizard apps are Python, so an AI assistant can already write them. What it cannot normally do is **check the running result**: whether a model landed at the right scale, whether a shader turned black, or what the console printed at frame 300. vizard-mcp closes that loop. The assistant changes something, lets the app run, looks and checks, and corrects itself.

## How it works

```
 Claude / any MCP client
        |  MCP (stdio)
        v
 vizard-mcp server  (normal Python 3.10+, outside Vizard; makes images, API index)
        |  JSON lines over TCP 127.0.0.1:9877
        v
 vizmcp module  (inside Vizard's own Python, standard library only)
        |  vizact.onupdate -> every command runs on Vizard's main thread
        v
 your Vizard app
```

Vizard's scene graph is not thread-safe. The socket threads in `vizmcp` only queue requests, and every command runs on the main thread between frames. Commands that need time (screenshots, waits, clicks) are generators that advance one step per frame, so the app never stalls.

## Requirements

- Windows with WorldViz Vizard 8 (tested with Vizard 8.3.1, Python 3.13, desktop and vizconnect apps)
- Python 3.10+ outside Vizard for the MCP server

## Install

```bash
pip install vizard-mcp
vizard-mcp install
vizard-mcp doctor
```

`vizard-mcp install` registers the server in Claude Code (`~/.claude.json`) and Claude Desktop (`%APPDATA%\Claude\claude_desktop_config.json`), whichever are installed. It backs the file up first, leaves your other servers alone, and does nothing when run a second time. `--dry-run` shows the change without writing it; `--claude-code` / `--claude-desktop` pick a client explicitly, `--name` and `--command` override the server name and program. Restart Claude Desktop (or start a new Claude Code session) afterwards.

`vizard-mcp doctor` prints PASS / WARN / FAIL with a fix for each problem: Python, `mcp` and Pillow versions, `winviz.exe` and its Vizard folder, whether port 9877 is free or a vizmcp app answers, whether the server is registered in Claude Code and Claude Desktop, and whether the registered command still exists. It makes no network calls (only a connection to `127.0.0.1`) and exits with 1 if anything FAILs. The assistant can run the same checks with the `doctor` tool.

From a clone (to hack on it):

```bash
git clone https://github.com/Zarrar110/vizard-mcp
cd vizard-mcp
python -m venv .venv
.venv\Scripts\pip install -e ".[test]"
.venv\Scripts\vizard-mcp install
```

Without the installer, register it by hand:

```bash
claude mcp add --scope user vizard -- C:/path/to/vizard-mcp/.venv/Scripts/vizard-mcp.exe
```

```json
{
  "mcpServers": {
    "vizard": {
      "command": "C:/path/to/vizard-mcp/.venv/Scripts/vizard-mcp.exe"
    }
  }
}
```

## Connecting an app

**Option A: let the assistant launch it.** `launch_app` starts any Vizard script in `winviz.exe` with vizmcp injected. The script needs no changes:

> "Launch C:\MyLab\main.py, look at the crane and tell me if anything looks wrong."

**Option B: add two lines to your script** and run it from the Vizard IDE as usual:

```python
import vizmcp
vizmcp.start()
```

Copy `vizmcp.py` next to your script with `vizard-mcp copy-module C:\MyLab`, or add `src/vizard_mcp/vizard_side` to `sys.path`.

`winviz.exe` is found under `C:\Program Files\WorldViz\Vizard*`; set `VIZARD_EXE` to override.

## Tools

Screen coordinates are 0..1 from the top-left corner of a screenshot. Nodes are referenced by numeric id or any Python expression in the app (`"ground"`, `"LAB.site.crane.hook"`, `"cars[2]"`).

**App**

| Tool | What it does |
|---|---|
| `vizard_status` | Connected? Script, versions, frame, FPS, window, camera, node count, commands |
| `launch_app` | Start a script with vizmcp injected and wait until it answers (`env` for app switches) |
| `quit_app` | Close the app cleanly |
| `reload_module` | Reload one of the app's own modules after editing it |
| `doctor` | The same checks as `vizard-mcp doctor`: versions, Vizard, port, Claude registration, with a fix for each problem |

**Inspect**

| Tool | What it does |
|---|---|
| `scene_tree` | Scene graph with ids, classes, visibility, positions and the **names the script uses** |
| `find_nodes` | Search nodes by script name: `crane` finds `LAB.site.crane.hook` |
| `node_info` | Local/global transform, world bounding box, parents, children, sub-node names |
| `set_node` | Position, euler, scale, visibility, color, alpha |
| `run_code` | Python in the app's `__main__` on the main thread: stdout, stderr, value of the last expression, traceback |
| `get_log` | Console output since start; incremental, filter by stream or text |
| `scene_stats` | Why is a scene slow? Counts of nodes, visible nodes, node types, textures, approximate triangles, lights, and the `top` heaviest nodes with their script names |
| `vizconnect_info` | Trackers, transports, displays, inputs, avatars, tools and events with world positions |
| `vizard_api` | Search Vizard's installed Python API (6000+ functions, classes, methods, constants) for exact signatures and docstrings; works without a running app |

**See**

| Tool | What it does |
|---|---|
| `screenshot` | The window as rendered now; `annotate=true` tags each named node on screen with its script name |
| `pick` | What is at a screen point: node, script name or nearest named ancestor, world point, normal, distance |
| `project` | Where world points or node centers appear on screen |
| `camera` | Get/set `viz.MainView`; `hold` keeps the view even when vizconnect drives it |

**Drive**

| Tool | What it does |
|---|---|
| `send_key` | KEYDOWN/KEYUP events, and `viz.key.isDown()` held true for N frames |
| `click` | Real mouse click in the Vizard window. Coordinates are 0..1 of the window's client area; refuses if another window covers the point; cursor restored |
| `gui_list` | The app's 2D GUI widgets (buttons, labels, checkboxes, sliders, textboxes, ...): `id`, script names, class, kind, text, `visible`, `enabled` and `center`/`size` as 0..1 of the window from the top-left |
| `click_button` | Click a 2D widget by `label` (exact text, then ignoring case, then a case-insensitive part of the text) or by `id`. Hidden or disabled widgets are skipped; no match or several matches is an error that lists the candidates |
| `wait` | Let N frames and/or seconds pass |
| `wait_until` | Run until a Python condition is true in the app (`"truck.x > 30"`) |
| `call_command` | Call a command the app registered itself |

### Node names

vizmcp walks the app's globals, the attributes of the app's own objects, lists and dicts, and the globals of the modules in the app's folder (four levels deep). A node held as `self.site.crane.hook` on a global `LAB` is reported as `LAB.site.crane.hook`. Nodes the script never stores get only an id.

### App-specific commands

```python
import vizmcp

def reset_experiment(params):
    experiment.reset(seed=params.get('seed', 0))
    return {'trial': experiment.trial}

vizmcp.register('reset_experiment', reset_experiment)
```

The assistant calls it with `call_command("reset_experiment", {"seed": 3})`. A command can also be a generator that yields once per frame and returns its result.

## Full edition

The full edition of vizard-mcp is a separate package with the tools that go beyond this basic edition:

- **Asset audit** (`asset_check`): cost per node and for the scene, scale bugs (a model exported in the wrong unit), empty and hidden-but-costly nodes, duplicated heavy subtrees, texture memory hogs, and missing files.
- **GUI smoke test** (`crawl_gui`): clicks every safe button, reports what changed on screen, which exceptions it raised and what it logged, and puts the scene back after each click.
- **Snapshots and undo**: save and restore node transforms, visibility and app state, in memory or in a file.
- **Reports**: one self-contained HTML report (print it to PDF) with charts, tables, screenshots and the audit and test results.
- **Videos and tours**: MP4 recordings, and camera tours along keyframes or orbits, rendered frame by frame so they are smooth even when the scene draws slowly.
- **Measuring**: record any expression every frame, plot it, export it to CSV or JSON, and validate it against a formula with error statistics and a pass/fail verdict.
- **More views**: multi-angle orbit images, filmstrips, screenshot regression checks against a reference image, and a Python profiler for the frame.
- **VR without a headset**: drive vizconnect tracker poses and button presses to test VR menus on a desktop.
- **Remote apps**: connect to a Vizard app on another PC (an exhibition machine) over the LAN, with a token.

For the full edition, contact **zarrarabbas73@gmail.com**.

## Settings

| Variable | Default | Meaning |
|---|---|---|
| `VIZMCP_PORT` | `9877` | Port, on both sides |
| `VIZMCP_TOKEN` | none | If set on both sides, every request must carry it |
| `VIZARD_EXE` | auto | Full path to `winviz.exe` |

Screenshots go to `%TEMP%\vizmcp` unless a path is given.

## Security

`run_code` executes arbitrary Python inside your app, and `click` moves the real mouse. The socket binds to `127.0.0.1` only, so other machines cannot reach it, but any program on the same PC can. Set `VIZMCP_TOKEN` on shared machines, and do not start vizmcp in builds you hand to end users.

## Limitations and tips

- The free Vizard licence runs one Vizard process at a time. `launch_app` fails if the Vizard IDE is already running a script; stop it first.
- If the app writes a node's transform every frame, `set_node` changes are overwritten on the next frame. Change the app's own state with `run_code` instead.
- With vizconnect the view is re-driven every frame. Use `camera(..., hold=true)` and `camera(release=true)`.
- `run_code` blocks the frame while it runs. Do not `time.sleep` in it; use `wait` or `wait_until`.
- `click` needs the Vizard window visible on screen. A mouse-look navigator may also react to the click.
- Scripts that open a dialog at startup (e.g. hardware selection) must be told to skip it through `env`, or `launch_app` waits until its timeout.

## What still needs a live Vizard run

The offline tests use stand-ins that follow the calls the code makes. What has and has not been run against a real Vizard 8.3:

- **`gui_list` / `click_button`**: the widget class names (`VizButton`, `VizSlider`, ...) and the text, enabled and value accessors (`getMessage`, `getEnabled`, `getState`, ...) are probed by name and report `null` when missing; whether a given widget type exposes them is unverified. Window coordinates rely on `getBoundingBox().center` being normalized with a bottom-left origin (converted with `1 - y`); the demo's Reset button is checked in `tests/live_test.py`. Telling screen-space from world-space widgets is done by walking parents up to the scene's roots, which is unverified for widgets on a vizconnect wrist panel.

## Running the tests

The test suite is offline: it needs neither Vizard nor Windows (CI runs it on Linux and Windows).

```bash
pip install -e ".[test]"
pytest
```

It covers the socket client (against a fake vizmcp server), the image helpers, the API index (built from a small fake Vizard tree) and the MCP server (all 25 tools, error handling, stdio handshake), plus vizmcp's commands (GUI listing and clicking, `scene_stats`, click coordinates) run against a stand-in for Vizard's `viz` module, and `doctor` / `install` with temporary `HOME` and `APPDATA` folders.

CI also builds the sdist and wheel, runs `twine check`, and installs the wheel in a clean environment (nothing is published).

Two scripts need more and are not part of the default run:

```bash
python tests/test_mcp_stdio.py   # prints the tool list and status over a real stdio session
set VIZMCP_LIVE=1 && python tests/live_test.py   # launches examples/demo_scene.py in a real Vizard and checks the tools
```

## Contact

Built by Zarrar Abbas, who develops research and exhibition VR simulations for civil engineering in Vizard.
Questions, ideas, the full edition and collaboration: **zarrarabbas73@gmail.com**, or open an issue on this repository.

## License

MIT. See [LICENSE](LICENSE).
