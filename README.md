# vizard-mcp

An [MCP](https://modelcontextprotocol.io) server that connects AI assistants such as Claude to a **running [WorldViz Vizard](https://www.worldviz.com/vizard) application**.

The assistant can look at the scene graph by the names your script uses, change nodes, run Python inside the app, **see** the result (screenshots, labelled screenshots, orbits around an object, frames over time), **measure** what the simulation does (record any value every frame and plot it), click and press keys, profile the frame, and look up Vizard's own API, all while the app keeps running.

> Unofficial community project. Not affiliated with or endorsed by WorldViz. "Vizard" and "WorldViz" are trademarks of WorldViz, Inc.

## Why

Vizard apps are Python, so an AI assistant can already write them. What it cannot normally do is **check the running result**: whether a model landed at the right scale, whether a shader turned black, whether a ball really bounces to e²·h, or what the console printed at frame 300. vizard-mcp closes that loop. The assistant changes something, lets the app run, looks and measures, and corrects itself.

Example from the included demo: the assistant records a bouncing ball for two bounces and checks the physics against theory.

| quantity | theory | measured in Vizard |
|---|---|---|
| first impact time, √(2h/g) | 0.639 s | 0.633 s (frame-sampled) |
| peak after first bounce, e²·h₀ | 1.280 m | 1.278 m |

## How it works

```
 Claude / any MCP client
        |  MCP (stdio)
        v
 vizard-mcp server  (normal Python 3.10+, outside Vizard; makes images, charts, API index)
        |  JSON lines over TCP 127.0.0.1:9877
        v
 vizmcp module  (inside Vizard's own Python, standard library only)
        |  vizact.onupdate -> every command runs on Vizard's main thread
        v
 your Vizard app
```

Vizard's scene graph is not thread-safe. The socket threads in `vizmcp` only queue requests, and every command runs on the main thread between frames. Commands that need time (screenshots, orbits, recordings, watches, profiling, clicks) are generators that advance one step per frame, so the app never stalls.

## Requirements

- Windows with WorldViz Vizard 8 (tested with Vizard 8.3.1, Python 3.13, desktop and vizconnect apps)
- Python 3.10+ outside Vizard for the MCP server

## Install

```bash
git clone https://github.com/Zarrar110/vizard-mcp
cd vizard-mcp
python -m venv .venv
.venv\Scripts\pip install -e .
```

### Claude Code

```bash
claude mcp add --scope user vizard -- C:/path/to/vizard-mcp/.venv/Scripts/vizard-mcp.exe
```

### Claude Desktop (or any MCP client using a JSON config)

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

> "Launch C:\MyLab\main.py, orbit the crane and tell me if anything looks wrong."

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
| `vizard_status` | Connected? Script, versions, frame, FPS, window, camera, node count, watches, commands |
| `launch_app` | Start a script with vizmcp injected and wait until it answers (`env` for app switches) |
| `quit_app` | Close the app cleanly |
| `reload_module` | Reload one of the app's own modules after editing it |

**Inspect**

| Tool | What it does |
|---|---|
| `scene_tree` | Scene graph with ids, classes, visibility, positions and the **names the script uses** |
| `find_nodes` | Search nodes by script name: `crane` finds `LAB.site.crane.hook` |
| `node_info` | Local/global transform, world bounding box, parents, children, sub-node names |
| `set_node` | Position, euler, scale, visibility, color, alpha |
| `run_code` | Python in the app's `__main__` on the main thread: stdout, stderr, value of the last expression, traceback |
| `get_log` | Console output since start; incremental, filter by stream or text |
| `vizconnect_info` | Trackers, transports, displays, inputs, avatars, tools and events with world positions |
| `vizard_api` | Search Vizard's installed Python API (6000+ functions, classes, methods, constants) for exact signatures and docstrings; works without a running app |

**See**

| Tool | What it does |
|---|---|
| `screenshot` | The window as rendered now; `annotate=true` tags each named node on screen with its script name |
| `screenshot_diff` | Compare the window with a reference PNG for visual regression: percentage of pixels that differ, a diff image (changes in red, changed areas boxed), and pass/fail against an optional `threshold_pct`. `pixel_tolerance` absorbs anti-aliasing noise; `update_reference=true` writes the current window as the new baseline. The reference must be the same size as the window |
| `multi_view` | Orbit a node (or point) from N angles, or a list of views, in one grid image; the user's view is restored exactly |
| `filmstrip` | N frames over time in one grid, to see motion |
| `record_video` | AVI recording with Vizard's built-in recorder, for people to watch (uncompressed: about 25 MB per second at 800x600) |
| `pick` | What is at a screen point: node, script name or nearest named ancestor, world point, normal, distance; ignores the user's own avatar head |
| `project` | Where world points or node centers appear on screen |
| `camera` | Get/set `viz.MainView`; `hold` keeps the view even when vizconnect drives it |

**Drive**

| Tool | What it does |
|---|---|
| `send_key` | KEYDOWN/KEYUP events, and `viz.key.isDown()` held true for N frames |
| `click` | Real mouse click in the Vizard window (GUI buttons, sliders, mouse handlers). Coordinates are 0..1 of the window's client area; the window is brought to the foreground (and restored if minimised) when it is not already there; refuses if another window covers the point; cursor restored |
| `wait` | Let N frames and/or seconds pass |
| `wait_until` | Run until a Python condition is true in the app (`"truck.x > 30"`) |

**Measure**

| Tool | What it does |
|---|---|
| `watch_start` | Record any expressions every N frames with app time; vectors split into `.x/.y/.z`; stops at `until`, a sample limit or an error |
| `watch_read` | Per column first/last/min/max (with times)/mean, evenly spaced samples, full CSV file |
| `watch_plot` | Line chart image of watched columns against time, max marked |
| `watch_export` | Write every sample of a watch to a `.csv` or `.json` file (`path`; optional `columns` to pick and order them) for validation reports. JSON is `{watch, expressions, stop_reason, samples, columns, rows}` |
| `scene_stats` | Why is a scene slow? Counts of nodes, visible nodes, node types, textures, approximate triangles (all and visible), lights, and the `top` heaviest nodes with their script names. A figure the running Vizard cannot provide is `null` with a note, and heavy nodes are then ranked by sub-node count |
| `profile` | cProfile of all main-thread Python for N frames: frame time mean/p95/max, FPS, Python ms per frame, top functions |
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

## Settings

| Variable | Default | Meaning |
|---|---|---|
| `VIZMCP_PORT` | `9877` | Port, on both sides |
| `VIZMCP_TOKEN` | none | If set on both sides, every request must carry it |
| `VIZARD_EXE` | auto | Full path to `winviz.exe` |

Screenshots, CSV files and recordings go to `%TEMP%\vizmcp` unless a path is given.

## Security

`run_code` executes arbitrary Python inside your app, and `click` moves the real mouse. The socket binds to `127.0.0.1` only, so other machines cannot reach it, but any program on the same PC can. Set `VIZMCP_TOKEN` on shared machines, and do not start vizmcp in builds you hand to end users.

## Limitations and tips

- The free Vizard licence runs one Vizard process at a time. `launch_app` fails if the Vizard IDE is already running a script; stop it first.
- If the app writes a node's transform every frame, `set_node` changes are overwritten on the next frame. Change the app's own state with `run_code` instead.
- With vizconnect the view is re-driven every frame. Use `camera(..., hold=true)` and `camera(release=true)`; `multi_view` handles this itself.
- `run_code` blocks the frame while it runs. Do not `time.sleep` in it; use `wait` or `wait_until`.
- `profile` measures Python only. If Python milliseconds per frame are far below the frame time, the app is bound by rendering or vsync.
- `click` needs the Vizard window visible on screen. A mouse-look navigator may also react to the click.
- Scripts that open a dialog at startup (e.g. hardware selection) must be told to skip it through `env`, or `launch_app` waits until its timeout.

## Running the tests

The test suite is offline: it needs neither Vizard nor Windows (CI runs it on Linux and Windows).

```bash
pip install -e ".[test]"
pytest
```

It covers the socket client (against a fake vizmcp server), the image helpers, the API index (built from a small fake Vizard tree) and the MCP server (all 31 tools, error handling, stdio handshake), plus vizmcp's new commands run against a stand-in for Vizard's `viz` module.

Two scripts need more and are not part of the default run:

```bash
python tests/test_mcp_stdio.py   # prints the tool list and status over a real stdio session
set VIZMCP_LIVE=1 && python tests/live_test.py   # launches examples/demo_scene.py in a real Vizard and checks every tool and the demo's physics
```

## Contact

Built by Zarrar Abbas, who develops research and exhibition VR simulations for civil engineering in Vizard.
Questions, ideas and collaboration: **zarrarabbas73@gmail.com**, or open an issue on this repository.

## License

MIT. See [LICENSE](LICENSE).
