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
pip install vizard-mcp
vizard-mcp install
vizard-mcp doctor
```

`vizard-mcp install` registers the server in Claude Code (`~/.claude.json`) and Claude Desktop (`%APPDATA%\Claude\claude_desktop_config.json`), whichever are installed. It backs the file up first, leaves your other servers alone, and does nothing when run a second time. `--dry-run` shows the change without writing it; `--claude-code` / `--claude-desktop` pick a client explicitly, `--name` and `--command` override the server name and program. Restart Claude Desktop (or start a new Claude Code session) afterwards.

`vizard-mcp doctor` prints PASS / WARN / FAIL with a fix for each problem: Python, `mcp` and Pillow versions, `winviz.exe` and its Vizard folder, whether port 9877 is free or a vizmcp app answers, whether ffmpeg is available for MP4 videos, whether the server is registered in Claude Code and Claude Desktop, and whether the registered command still exists. It makes no network calls (only a connection to `127.0.0.1`) and exits with 1 if anything FAILs. The assistant can run the same checks with the `doctor` tool.

For MP4 videos you need an ffmpeg: either already on `PATH`, or `pip install "vizard-mcp[video]"` (bundles one through `imageio-ffmpeg`), or point `VIZMCP_FFMPEG` at it.

From a clone (to hack on it):

```bash
git clone https://github.com/Zarrar110/vizard-mcp
cd vizard-mcp
python -m venv .venv
.venv\Scripts\pip install -e ".[video,test]"
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

> "Launch C:\MyLab\main.py, orbit the crane and tell me if anything looks wrong."

**Option B: add two lines to your script** and run it from the Vizard IDE as usual:

```python
import vizmcp
vizmcp.start()
```

Copy `vizmcp.py` next to your script with `vizard-mcp copy-module C:\MyLab`, or add `src/vizard_mcp/vizard_side` to `sys.path`.

`winviz.exe` is found under `C:\Program Files\WorldViz\Vizard*`; set `VIZARD_EXE` to override.

### Exhibition PC / LAN

The app can run on another PC (for example the exhibition machine) while the assistant runs on yours. vizmcp listens on `127.0.0.1` unless it is told otherwise.

**On the exhibition PC** (the one running the Vizard app), set both variables before the app starts:

```bat
set VIZMCP_BIND=192.168.1.50
set VIZMCP_TOKEN=a-long-random-secret
```

`VIZMCP_BIND` is the PC's own LAN address (or `0.0.0.0` for every interface). vizmcp **refuses to listen beyond this PC without `VIZMCP_TOKEN`** and says so in the console. Open the port in the Windows firewall for the private network only, from an administrator prompt:

```bat
netsh advfirewall firewall add rule name="vizmcp" dir=in action=allow protocol=TCP localport=9877 profile=private remoteip=192.168.1.0/24
```

Use your own subnet for `remoteip`, so only that LAN can reach the port. Remove the rule when the show is over: `netsh advfirewall firewall delete rule name="vizmcp"`. The network profile must be *Private*; Windows blocks inbound traffic on *Public* networks by default.

**On your PC**, point the server at it, either with variables before the assistant starts

```bat
set VIZMCP_HOST=192.168.1.50
set VIZMCP_TOKEN=a-long-random-secret
```

or at any time with the `connect` tool (`connect(host="192.168.1.50", token="...")`), which reports what it reaches. `vizard-mcp doctor` shows the target and whether it answers. Because `launch_app` starts `winviz.exe` on the PC the server runs on, it refuses while the target is another PC: start the app there yourself.

**Security.** vizmcp has `run_code`, which runs any Python in the app, and the token is the only protection: it is sent as plain text and the traffic is not encrypted. Use it on a trusted, private LAN only; never forward the port to the internet or expose it on a guest or public network, use a long random token, and stop the app or remove the firewall rule when you are done. Files are written where the code that makes them runs. With an app on another PC, images (`screenshot`, `screenshot_diff`, `multi_view`, `filmstrip`, `record_tour` with `dry_run`, and so `crawl_gui`) are fetched over the same connection, so they work (the `save_to` copy is written on your PC). `record_video` and `record_tour` refuse, because the video is written on the app PC. A `path` for `snapshot_save`, `snapshot_restore`, `watch_export` and the `validate` CSV is opened by the app, so it means a folder on the app PC.

## Tools

Screen coordinates are 0..1 from the top-left corner of a screenshot. Nodes are referenced by numeric id or any Python expression in the app (`"ground"`, `"LAB.site.crane.hook"`, `"cars[2]"`).

**App**

| Tool | What it does |
|---|---|
| `vizard_status` | Connected? Script, versions, frame, FPS, window, camera, node count, watches, commands |
| `connect` | Choose which app the server talks to: `host`, `port`, `token` (settings you leave out stay; the token is never echoed). Reports whether the target answers, whether it is this PC, and the app's script and version. See [Exhibition PC / LAN](#exhibition-pc--lan) |
| `launch_app` | Start a script with vizmcp injected and wait until it answers (`env` for app switches). Only for an app on this PC; with a remote target it refuses and says so |
| `quit_app` | Close the app cleanly |
| `reload_module` | Reload one of the app's own modules after editing it |
| `doctor` | The same checks as `vizard-mcp doctor`: versions, Vizard, port, ffmpeg, Claude registration, with a fix for each problem |

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
| `record_video` | Record the window for people to watch. Default `format="mp4"`: Vizard's recorder writes an uncompressed AVI (about 25 MB per second at 800x600), which the server then converts to H.264 MP4 (yuv420p, even dimensions, fast-start) and deletes. `crf` (quality, default 23), `fps` and `max_size` tune the output. If no ffmpeg is found or the conversion fails, the AVI is kept and the result says why. `format="avi"` keeps the raw AVI |
| `record_tour` | Record a video while the camera moves along a path, then give the user's camera back. Either `keyframes` (`[{position, euler or look_at, t}]`, positions on a Catmull-Rom spline or eased/linear via `interp`, angles turning the short way round) or an `orbit` around a node / `target` point (`radius`, `elevation_deg`, `degrees`, `duration`). Output as `record_video`. `dry_run=true` records nothing and returns a grid of `count` frames along the path to judge it first. `mode="steps"` renders the tour frame by frame for a smooth video even when the scene draws slowly (see below) |
| `pick` | What is at a screen point: node, script name or nearest named ancestor, world point, normal, distance; ignores the user's own avatar head |
| `project` | Where world points or node centers appear on screen |
| `camera` | Get/set `viz.MainView`; `hold` keeps the view even when vizconnect drives it |

**Drive**

| Tool | What it does |
|---|---|
| `send_key` | KEYDOWN/KEYUP events, and `viz.key.isDown()` held true for N frames |
| `click` | Real mouse click in the Vizard window (GUI buttons, sliders, mouse handlers). Coordinates are 0..1 of the window's client area; the window is brought to the foreground (and restored if minimised) when it is not already there; refuses if another window covers the point; cursor restored |
| `gui_list` | The app's 2D GUI widgets (buttons, labels, checkboxes, sliders, textboxes, ...): `id`, script names, class, kind, text, `visible`, `enabled` and `center`/`size` as 0..1 of the window from the top-left (same as screenshots). Widgets parented into the 3D scene, such as a vizconnect wrist or tablet panel, are listed apart as `world_widgets` with a world position and are not clickable |
| `click_button` | Click a 2D widget by `label` (exact text, then ignoring case, then a case-insensitive part of the text) or by `id`, through the same real-mouse path as `click`. Hidden or disabled widgets are skipped; no match or several matches is an error that lists the candidates |
| `crawl_gui` | Smoke-test the 2D GUI: for each visible, enabled button (optionally those matching the regex `label`, at most `max_buttons`) capture the window, click it by id, wait `settle_frames`, read the new console lines and Python exceptions, diff the window against the capture and put the scene back from a snapshot (`restore=true`, built with `snapshot_save`'s `nodes` / `named` / `pattern` / `root` / `state` / `restore_code`). Returns a table (button, changed %, errors, new log lines) plus details and the skipped buttons. Buttons whose text matches `deny` (default `quit|exit|close|delete`) and buttons without text are never clicked. Usable in `make_report` as a `crawl_gui` section |
| `vr_simulate` | Drive vizconnect without a headset: hold head and left/right hand tracker poses (`position`, `euler`) and press, release or hold named input buttons for N frames, to test wrist-tablet menus, laser pointers and grabs on a desktop. Undone by `reset`, on `quit_app` and when the app exits. Errors clearly if the app does not use vizconnect |
| `wait` | Let N frames and/or seconds pass |
| `wait_until` | Run until a Python condition is true in the app (`"truck.x > 30"`) |

**Undo and report**

| Tool | What it does |
|---|---|
| `snapshot_save` | Remember the local position, orientation (quaternion), scale and visibility of chosen nodes under a name, to put back later: `nodes` (ids or expressions), `named` (every named node, optionally filtered by `pattern`) and/or `root` (a node and everything under it). Optionally also app state: `state` is an expression whose value is copied, `restore_code` a statement that puts it back with the copy as `_state`. `path` also writes a JSON file |
| `snapshot_restore` | Put a snapshot back (state first, then nodes), from the app's memory by `name` or from a JSON `path`. Reports the nodes that were `missing` (deleted, or no longer found by name) and any value a node refused; the rest are still restored |
| `snapshot_list` | The snapshots kept in the app; with `path`, describes a snapshot file without needing the app |
| `make_report` | One self-contained HTML report (images inlined as base64, print-friendly CSS, nothing else to carry around; print it to PDF from a browser) from `sections`: `heading`, `text` (paragraphs, lists, bold/italic/code/links), `watch` (summary table + chart), `validate` (PASS/FAIL badge, error statistics, measured-vs-theory chart), `screenshot` (taken now), `image` (an existing file), `keyvalue`, `asset_check` (runs the audit now unless you pass its `result`) and `crawl_gui` (the latest crawl of this server, or the `result` you pass; a report never starts a crawl). The header has the title, author (default Zarrar Abbas), date and what is known of the app and Vizard |

**Measure**

| Tool | What it does |
|---|---|
| `watch_start` | Record any expressions every N frames with app time; vectors split into `.x/.y/.z`; stops at `until`, a sample limit or an error |
| `watch_read` | Per column first/last/min/max (with times)/mean, evenly spaced samples, full CSV file |
| `watch_plot` | Line chart image of watched columns against time, max marked |
| `watch_export` | Write every sample of a watch to a `.csv` or `.json` file (`path`; optional `columns` to pick and order them) for validation reports. JSON is `{watch, expressions, stop_reason, samples, columns, rows}` |
| `validate` | Compare a recorded watch with theory: evaluate an expression over the watch's columns and `constants` (`"P*L**3/(48*E*I)"`, or just a reference column) for every sample and compare it with the `measured` column. Returns max/mean/RMS error, bias, % error, samples within tolerance (`abs_tol` and/or `pct_tol`, optional `t_min`/`t_max` window, `min_pass_pct`) with a pass/fail verdict, a per-sample CSV and a measured-vs-theory chart |
| `scene_stats` | Why is a scene slow? Counts of nodes, visible nodes, node types, textures, approximate triangles (all and visible), lights, and the `top` heaviest nodes with their script names. A figure the running Vizard cannot provide is `null` with a note, and heavy nodes are then ranked by sub-node count |
| `asset_check` | Audit the scene's assets from Vizard 8's `getStats`: totals (primitives, vertices, drawables, textures, texture MB; equal to `viz.MainScene.getStats()`), the `top` nodes by their own primitives, bounding-box **size outliers** (over `max_size_m` = 1000 m or under `min_size_m` = 1 mm on the longest side, or `sibling_ratio` = 100 times the median of their siblings, the usual unit bug of an exported GLB), nodes with nothing to draw, **hidden nodes that still hold primitives**, duplicated heavy subtrees (same file, else identical statistics), texture-memory hogs, and files missing on disk. File names are probed with `getFilename` / `getName` and are `null` where a node has none. `html=true` adds a fragment for reports; `make_report` has an `asset_check` section |
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
| `VIZMCP_TOKEN` | none | If set on both sides, every request must carry it. Required when the app listens beyond this PC |
| `VIZMCP_BIND` | `127.0.0.1` | App side: the interface vizmcp listens on. Anything but loopback needs `VIZMCP_TOKEN` |
| `VIZMCP_HOST` | `127.0.0.1` | Server side: the PC whose app the tools talk to (`connect` changes it at run time) |
| `VIZARD_EXE` | auto | Full path to `winviz.exe` |
| `VIZMCP_FFMPEG` | auto | Full path to `ffmpeg` for MP4 conversion; otherwise `PATH`, then the `imageio-ffmpeg` package |

Screenshots, CSV files and recordings go to `%TEMP%\vizmcp` unless a path is given.

## Security

`run_code` executes arbitrary Python inside your app, and `click` moves the real mouse. By default the socket binds to `127.0.0.1` only, so other machines cannot reach it, but any program on the same PC can. Set `VIZMCP_TOKEN` on shared machines, and do not start vizmcp in builds you hand to end users. Listening on a LAN address is possible only by setting `VIZMCP_BIND` and needs a token; see [Exhibition PC / LAN](#exhibition-pc--lan) for the firewall steps and its limits (no encryption, trusted LAN only).

## Limitations and tips

- The free Vizard licence runs one Vizard process at a time. `launch_app` fails if the Vizard IDE is already running a script; stop it first.
- If the app writes a node's transform every frame, `set_node` changes are overwritten on the next frame. Change the app's own state with `run_code` instead.
- With vizconnect the view is re-driven every frame. Use `camera(..., hold=true)` and `camera(release=true)`; `multi_view` handles this itself.
- `run_code` blocks the frame while it runs. Do not `time.sleep` in it; use `wait` or `wait_until`.
- `profile` measures Python only. If Python milliseconds per frame are far below the frame time, the app is bound by rendering or vsync.
- `click` needs the Vizard window visible on screen. A mouse-look navigator may also react to the click.
- Scripts that open a dialog at startup (e.g. hardware selection) must be told to skip it through `env`, or `launch_app` waits until its timeout.

### `record_tour` in detail

```text
record_tour(orbit="LAB.site.crane", duration=10, degrees=360, elevation_deg=20)
record_tour(keyframes=[{"position": [0, 2, -8], "look_at": [0, 1, 0], "t": 0},
                       {"position": [6, 3, -2], "look_at": [0, 1, 0], "t": 3},
                       {"position": [8, 2, 5],  "look_at": [0, 1, 0], "t": 6}],
            path="C:/out/flythrough.mp4")
record_tour(orbit="ball", dry_run=True, count=6)          # preview only, nothing is recorded
```

The camera pose is a function of the time since the recording started, applied every frame after the app and vizconnect have moved the view (the same hold `camera(hold=true)` uses), so the video follows real time whatever the frame rate. Keyframes use `euler` in all of them or `look_at` in all of them. Without `t` they are spread evenly over `duration`; `duration` longer than the last `t` holds the final pose. When the tour ends, or fails, the hold is released and the user's position and euler (and any hold they had) are put back. For an orbit, `radius` is the camera distance from the centre; for a node it defaults to 2.8 times half its largest bounding-box side, for a point to 5. The conversion to MP4 happens in the server, so no extra software is needed inside Vizard.

#### `mode="steps"`: a smooth tour of a slow scene

```text
record_tour(orbit="LAB.site.crane", duration=10, mode="steps", fps=30)
record_tour(keyframes=[...], mode="steps", fps=24, settle_frames=3, max_size=1280, path="C:/out/flythrough.mp4")
```

The default `mode="realtime"` records what Vizard renders, so a lab that draws about 10 fps on an integrated GPU gives a 10 fps tour. In `steps` mode the tour is rendered one output frame at a time instead. For each of `fps x duration` frames the view is held at the pose for `t = k / fps`, `settle_frames` rendered frames (default 2) are waited so the scene catches up with the camera, and the window is captured to a numbered PNG in a temporary folder. The server then encodes the sequence with ffmpeg to H.264 MP4 at `fps` (default 30; yuv420p, even dimensions, fast-start, `crf` and `max_size` as in `record_video`) and deletes the PNGs; if encoding fails or there is no ffmpeg, the PNGs are kept and the result gives their folder. The result reports `frames` captured and `wall_s`, the time it took. The camera and any earlier hold are put back exactly as in realtime mode.

It takes as long as the app needs to render and capture each frame, so a 10-second 30 fps tour is 300 captures. Only the camera path is stepped: **the simulation keeps running in wall time**, so physics, animation and anything else the app does advances by about `wall_s` seconds while the video covers `duration` seconds of camera. Motion in the scene is therefore not frame-exact in steps mode; pause or script the app yourself if that matters. Steps mode writes MP4 only.

### `snapshot_*` in detail

```text
snapshot_save("before", nodes=["ball", "LAB.site.crane.hook"], root="LAB.site.tower",
              state="LAB.sim.state()", restore_code="LAB.sim.set_state(_state)")
snapshot_save("everything", named=True, path="C:/out/everything.json")
... try something with set_node / run_code ...
snapshot_restore("before")
snapshot_restore(path="C:/out/everything.json")        # another session of the same script
```

Each node's **local** position, orientation (as a quaternion, so there is no euler wrap-around), scale and visibility are recorded; the camera and colours are not. Nodes are found again by the expression they were given by, or by their first script name when chosen with `named` or `root`; in the app that saved the snapshot the saved node objects themselves are used, so a node that was removed is reported as `missing` even though Vizard may already have given its id to a new node. A snapshot file read in another session only trusts names, since ids change between runs: nodes it can only identify by id come back as `missing`. `state` is deep-copied when saved, and passed to `restore_code` as `_state`, which runs in the app's `__main__` before the nodes are restored; if it fails the error is reported and the nodes are still restored. A file keeps `state` only when it is plain JSON data (numbers, strings, lists, dicts); anything else is kept in the app's memory only, and the save says so. If the app writes a node's transform every frame it will overwrite a restored one, as with `set_node`.

### `make_report` in detail

```text
make_report(title="Drop test", path="C:/out/drop_test.html", sections=[
  {"type": "heading", "text": "Results"},
  {"type": "text", "text": "Dropped from **2 m**, e = 0.8.\n\n- bounce 1 peaked at 1.28 m"},
  {"type": "watch", "name": "bounce", "columns": ["h"]},
  {"type": "validate", "watch": "bounce", "measured": "h", "theory": "h0 - v**2/(2*g)",
   "constants": {"h0": 2.0, "g": 9.81}, "abs_tol": 0.02, "t_max": 0.5},
  {"type": "screenshot", "caption": "End state"},
  {"type": "keyvalue", "items": {"Restitution": 0.8}}])
```

Every section is checked before the app is asked for anything, and a section that fails (an unknown watch, say) stops the report with its number and writes nothing. The page uses no external fonts, scripts or images, so it can be moved or e-mailed as one file; `@media print` rules keep figures and tables from splitting across pages, so the browser's Print > Save as PDF gives the PDF. Without a running app the static sections still work and the header says the app was not connected.

### `vr_simulate` in detail

```text
vr_simulate(poses={"head": {"position": [0, 1.7, 0], "euler": [30, 0, 0]},
                   "right_hand": {"position": [0.3, 1.2, 0.4]}})
vr_simulate(press=["right_hand:trigger"], frames=10, button_map={"trigger": 1})
vr_simulate(reset=True)
```

Tracker keys are vizconnect tracker names, or the aliases `head`, `left_hand`, `right_hand` (matched against the names in `vizconnect.getTrackerDict()`; an ambiguous or missing name is an error that lists what vizconnect has). Input buttons are given as a name or an index; with several input devices write `device:button` or pass `input`. `vizconnect_info` shows the names.

How it works: vizconnect links each raw tracker to its tracker node every frame, so a posed tracker gets a new raw object through vizconnect's own `Tracker.setRaw`: a group node that holds the simulated pose. The view, hands and tools then follow it as they would follow real hardware. Button presses go to the raw input device (`getInputDict()[name].getRaw()`), which is what apps poll: its `isButtonDown`, `isButtonUp`, `getButtonState` and `getState` (mouse) are replaced. A built-in device such as the desktop mouse takes no new attributes, so vizconnect is given a proxy device through `Input.setRaw`. Code that kept a reference to the original device before the simulation still reads the real one, but sensor events reach it. Presses also send `viz.SENSOR_DOWN_EVENT` / `SENSOR_UP_EVENT` as `viz.Event(object=device, button=n)`, the form `vizact.onsensordown` and `viztask.waitSensorDown` expect (`send_events=false` turns that off). `reset`, `quit_app` and the app exiting give every wrapper its original raw object back and release held buttons. Hand aliases match the usual names: `right_hand` finds `r_hand_tracker`, `RHand` or `Right Controller`.

### `validate` in detail

```text
watch_start({"defl": "beam.deflection", "P": "load.force"}, name="bend")
validate("bend", measured="defl", theory="P*L**3/(48*E*I)",
         constants={"L": 2.0, "E": 200e9, "I": 8e-6}, pct_tol=2)
```

The theory is parsed, not run: numbers, the watch's columns (`t`, `frame` and the watched labels; `pos.y` is written `pos_y`), your constants, `pi`/`e`/`tau`, `+ - * / // % **`, and the functions `sin cos tan asin acos atan atan2 sinh cosh tanh exp log log10 log2 sqrt pow fabs hypot degrees radians floor ceil copysign abs min max`. Attributes, subscripts, strings, other calls and builtins are rejected. A sample passes if `|measured - theory| <= max(abs_tol, pct_tol % of |theory|)`. Samples where the theory is undefined (division by zero, `sqrt` of a negative) are skipped and counted; percent errors are not defined where the theory is 0 and are counted separately.

## What still needs a live Vizard run

The offline tests use stand-ins that follow the calls the code makes. What has and has not been run against a real Vizard 8.3:

- **`gui_list` / `click_button`**: the widget class names (`VizButton`, `VizSlider`, ...) and the text, enabled and value accessors (`getMessage`, `getEnabled`, `getState`, ...) are probed by name and report `null` when missing; whether a given widget type exposes them is unverified. Window coordinates rely on `getBoundingBox().center` being normalized with a bottom-left origin (converted with `1 - y`); the demo's Reset button is checked in `tests/live_test.py`. Telling screen-space from world-space widgets is done by walking parents up to the scene's roots, which is unverified for widgets on a vizconnect wrist panel.
- **LAN / `connect`**: the bind and token rules, `connect`, `launch_app` refusing a remote target and doctor's remote check are tested offline, with a real vizmcp socket bound to `0.0.0.0` and a fake app standing in for the remote PC. Fetching screenshots over the socket (`fetch_file`) is tested the same way. Not tried across two real PCs, through the Windows firewall rule above, or on a real Vizard app with `VIZMCP_BIND` set. `tests/live_test.py` only connects to `127.0.0.1` explicitly.
- **`asset_check`**: the maths and flags are tested offline against stand-in nodes. Needs a live run: that `getStats()` on a node counts its whole subtree the way the own-cost subtraction assumes (loaded models, groups, instanced children), that `getBoundingBox(viz.ABS_GLOBAL)` gives sensible sizes for models and groups, whether any node type in Vizard 8.3.1 really has `getFilename` / `getName` (otherwise files are `null` and duplicates are judged by statistics), and that the "nothing to draw" list is not noisy for the node types of a real app. `tests/live_test.py` checks that the totals equal `viz.MainScene.getStats()` on the demo scene.
- **`crawl_gui`**: the flow (capture, click by id, settle, log, diff, restore) and the exception extraction are tested offline against a simulated app. Needs a live run on real buttons: the demo's Reset ball button must report a visible change and the `ball dropped` log line (checked in `tests/live_test.py`), and the Python tracebacks that a failing button callback really writes to `stderr` must match the traceback shape the extractor expects. Buttons that open modal dialogs or move the real mouse away have not been tried.
- **`vr_simulate`**: run on a real vizconnect app (a bridge lab on Vizard 8.3.1 with the desktop vizconnect config). A posed head moved `viz.MainView` (yaw +45°, new position), a posed right hand moved the hand node, a press on the hand input showed in the mouse device's `getState()` and reached a `SENSOR_DOWN_EVENT` handler with the right device and button, and `reset` restored the view and hand exactly. Not yet tried with OpenXR or SteamVR controllers. The demo scene has no vizconnect, so `tests/live_test.py` only checks the error path. Button names have no built-in mapping to indexes; pass `button_map` for your device.
- **`validate`**: the math and statistics are covered offline and checked against the demo's free fall in `tests/live_test.py`.
- **`record_video` MP4 / `record_tour`**: run on Windows with Vizard 8.3.1 and the ffmpeg from `imageio-ffmpeg`.
  - The demo scene: a 2.3 s recording went from a 55 MB AVI to a 26 KB H.264 MP4. Orbit and keyframe tours played, and the camera came back exactly.
  - A vizconnect bridge lab: a 90° orbit was recorded over vizconnect, from an 85 MB AVI to an 848 KB MP4, and `viz.MainView` was restored exactly.
  - A video moves only as smoothly as the app renders. Vizard's recorder captures real frames, so a lab drawing 10 fps on an integrated GPU gives a 10 fps tour inside a 30 fps file.
  - Record after the app has finished loading. A tour started while a heavy scene still builds its terrain and shaders caught a single frame.
- **`record_tour` mode `steps`, `snapshot_*` and `make_report`**: run on Windows with Vizard 8.3.1. `tests/live_test.py` on the demo scene passes all 75 checks. Its steps tour gives an MP4 of exactly `fps x 2` frames, with the camera restored and the PNGs deleted. A snapshot is moved and put back from memory and from the JSON file, and the deleted node is reported as missing. The report holds a watch, a validate and a screenshot. A 3 s steps tour (90 frames, 800x600) of a vizconnect bridge lab came out smooth at 30 fps although the lab draws about 20 fps. It took 80 s, about 0.9 s per frame, which is mostly the window capture, so plan about 27 minutes for a one-minute steps video. The live run also showed that Vizard gives a removed node's id to the next new node, and that `_GetObject` of a removed id returns a fresh object that reports `valid()`. `snapshot_restore` therefore keeps the saved node objects in the app and uses those; ids are only a fallback for a file read in a later run.
- **`vizard-mcp install` / `doctor`**: run on Windows with Claude Code. `doctor` reported Python, mcp, Pillow, Vizard 8, the port, ffmpeg and the Claude Code entry as PASS, and the missing Claude Desktop config as WARN. `install --dry-run` found the existing entry and changed nothing. A path written with `/` and one written with `\` count as the same program. Claude Desktop itself has not been tried. The Vizard version is only what the install folder name says: `Vizard8` gives `8`.

## Running the tests

The test suite is offline: it needs neither Vizard nor Windows (CI runs it on Linux and Windows).

```bash
pip install -e ".[test]"
pytest
```

It covers the socket client (against a fake vizmcp server), the image helpers, the API index (built from a small fake Vizard tree) and the MCP server (all 44 tools, error handling, stdio handshake), plus vizmcp's newer commands (GUI listing and clicking, `vr_simulate`, `validate`, `watch_export`, `scene_stats`, `asset_check`, `crawl_gui`, the LAN bind and token rules, the camera-tour maths, `record_tour` in both modes, snapshots and report building) run against stand-ins for Vizard's `viz` and `vizconnect` modules. MP4 conversion is tested with a tiny fake `ffmpeg` script (and a real one when installed), and `doctor` / `install` with temporary `HOME` and `APPDATA` folders.

CI also builds the sdist and wheel, runs `twine check`, and installs the wheel in a clean environment (nothing is published).

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
