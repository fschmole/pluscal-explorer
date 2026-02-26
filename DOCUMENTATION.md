# PlusCal Explorer — User Documentation

## What Is the Explorer?

The PlusCal Explorer is a **web-based, self-contained HTML application** generated
from a PlusCal formal model. It lets you interactively browse every TLC-verified
execution trace as a PlantUML sequence diagram — no install required, just open
the HTML file in a browser.

Each explorer is built from a model's `explorer.json` configuration, which
defines the model constants, participants, channel colors, and visualization
options. The build pipeline exhaustively sweeps all parameter combinations
through TLC, deduplicates the resulting traces, and packages them into a single
`index.html` file.

### Key Features

| Feature | Description |
|---------|-------------|
| **Parameter dropdowns** | Cascading dropdowns for every model constant. Selecting values filters to the matching TLC trace. |
| **Sequence diagrams** | Each trace is rendered as a PlantUML sequence diagram. Participants and arrow colors are pinned across all traces for visual consistency. |
| **PlantUML server** | Connect to a PlantUML server to render diagrams as SVG. Without a server, raw PlantUML text is displayed and can be copied. |
| **TLC live server** | Optionally connect to `tlc_server.py` for on-demand model checking — edit the PlusCal source in the built-in Monaco editor and re-run TLC without rebuilding. |
| **PlusCal editor** | Embedded Monaco editor with TLA+/PlusCal syntax highlighting. Visible in both static and live modes. |
| **Copy & export** | Copy PlantUML source to clipboard or download the rendered SVG. |
| **URL parameters** | Dropdown selections are reflected in the URL query string, so you can bookmark or share specific traces. |
| **Channel legend** | Color-coded legend for message channels (when `channelColors` is configured). |
| **Sweep-consistent rendering** | All diagrams share the same participant order, channel palette, and abbreviations — pinned by the explorer config, not auto-discovered per trace. |

---

## Prerequisites

| Dependency | Required | Notes |
|------------|----------|-------|
| **Python 3.8+** | Yes | For the build pipeline (`build.py`, `tlc_sweep.py`). |
| **Java (JDK 11+)** | Yes | For running TLC. Must be on PATH or configured. |
| **tla2tools.jar** | Yes | Bundled in `tools/`. The TLA+ model checker and PlusCal translator. |
| **PlantUML server** | Optional | For SVG rendering in the browser. Without it, raw PlantUML text is shown. |
| **Docker** | Optional | Easiest way to run a local PlantUML server. |

---

## Project Layout

```
pluscal-explorer/
├── tools/
│   ├── build.py              # Unified CLI: sweep / build / deploy / all
│   ├── build_explorer.py     # HTML explorer generator
│   ├── tlc_sweep.py          # TLC parameter sweep runner
│   ├── tlc_server.py         # Optional live TLC server (HTTP API)
│   ├── pcal_config.py        # Config loader for explorer.json
│   ├── gen_skip_rules.py     # Skip-rule generator utility
│   └── tla2tools.jar         # Bundled TLA+ tools
├── models/
│   └── mesi_coherence/       # Bundled example model
│       ├── mesi_coherence.pcal
│       ├── mesi_coherence.explorer.json
│       ├── mesi_coherence.cfg
│       └── distrib/          # Build output (index.html, traces/)
├── DOCUMENTATION.md           # This file
├── PLAN.md
└── README.md
```

---

## Explorer Configuration (`explorer.json`)

Every model has an `explorer.json` file that drives the entire pipeline.
Here is the MESI model's configuration as an example:

```jsonc
{
  "module": "mesi_coherence",
  "pcal":   "mesi_coherence.pcal",
  "title":  "MESI Cache Coherence Protocol",

  // Sweep constants — cartesian product of all values
  "constants": {
    "Operation":   ["Read", "Write"],
    "InitState":   ["M", "E", "S", "I"],
    "RemoteState": ["M", "E", "S", "I"]
  },

  // Skip impossible/redundant combos
  "skip": [
    { "InitState": "M", "RemoteState": "M" },
    { "InitState": "E", "RemoteState": "E" }
    // ... additional skip rules
  ],

  // TLC invariants and properties to check
  "invariants": ["MESICoherence"],
  "properties": ["RequestCompletes"],

  // Trace extraction variables (in the PlusCal source)
  "traceVariable": "trace",
  "doneVariable":  "done",

  // Visualization: fixed participant order across all diagrams
  "participants": ["Proc1", "Bus", "Proc2", "Memory"],

  // Channel colors and abbreviations (optional)
  "channels": [],
  "channelColors": {},
  "abbreviations": {},

  // PlantUML server URL (pre-populated in the explorer sidebar)
  "plantUmlServer": "",

  // TLC server URL (pre-populated in the explorer sidebar)
  "tlcServer": "http://127.0.0.1:18080",

  // First combo to display on page load
  "warmup": {
    "Operation":   "Read",
    "InitState":   "I",
    "RemoteState": "S"
  }
}
```



### Config Fields Reference

| Field | Required | Description |
|-------|----------|-------------|
| `module` | Yes | Module name (must match the `.pcal` filename without extension). |
| `pcal` | No | PlusCal source filename. Defaults to `<module>.pcal`. |
| `title` | No | Title shown in the explorer top bar. Defaults to module name. |
| `constants` | No | Dict of constant names to lists of values. The sweep generates the cartesian product. |
| `skip` | No | Array of partial assignments to skip. Unmentioned constants act as wildcards. |
| `invariants` | No | TLC invariants to check during sweep. |
| `properties` | No | TLC temporal properties to check during sweep. |
| `traceVariable` | No | Name of the trace sequence variable in PlusCal. Default: `"trace"`. |
| `doneVariable` | No | Name of the termination flag variable. Default: `"done"`. |
| `participants` | No | Fixed participant order for all diagrams. If omitted, auto-discovered per trace. |
| `channels` | No | Fixed channel list for color assignment order. |
| `channelColors` | No | Explicit `{ "ChannelName": { "stroke": "#hex", "label": "#hex" } }` color map. |
| `abbreviations` | No | Human-readable labels for channel name components. |
| `plantUmlServer` | No | PlantUML server URL to pre-populate in the explorer. |
| `tlcServer` | No | TLC server URL to pre-populate in the explorer. Default: `http://127.0.0.1:18080`. |
| `warmup` | No | Initial parameter combo to display on page load. Defaults to first value of each constant. |
| `branding` | No | Optional classification banner, contact, and footer links. |
| `deploy` | No | Deploy configuration (local or WebDAV targets). |

---

## Building an Explorer

The unified CLI is `build.py` in the `tools/` directory. All commands take
the path to an `explorer.json` config file as their argument.

### Step 1: Sweep (Run TLC Over All Parameter Combinations)

```bash
python tools/build.py sweep models/mesi_coherence/mesi_coherence.explorer.json
```

This:
1. Translates PlusCal → TLA+ (via `tla2tools.jar`)
2. Runs TLC for every combination of constants (minus skipped combos)
3. Deduplicates identical traces
4. Writes PlantUML `.puml` files to `distrib/traces/`

**Example output:**
```
[sweep] Model: mesi_coherence
[sweep] 32 total combos, 8 skipped, 24 to run
[sweep] Translating PlusCal -> TLA+ ...
  [Read.I.M]  PASS  (4 msgs)
  [Read.I.E]  PASS  (4 msgs)
  ...
[sweep] 24 passing -> 18 unique traces
```

### Step 2: Build (Generate the HTML Explorer)

```bash
python tools/build.py build models/mesi_coherence/mesi_coherence.explorer.json
```

This reads the `.puml` traces and assembles a self-contained `index.html`:

```
[build] Model: mesi_coherence
[build] Output: models/mesi_coherence/distrib/index.html
```

The output file contains all traces, the PlusCal source, a Monaco editor,
and all JavaScript — no server or network connection needed to browse
static traces.

### Step 3: Deploy (Optional)

```bash
# Preview what would be deployed
python tools/build.py deploy models/mesi_coherence/mesi_coherence.explorer.json --dry-run

# Deploy (copy to configured target)
python tools/build.py deploy models/mesi_coherence/mesi_coherence.explorer.json

# Deploy with archiving (backs up existing files at target before overwriting)
python tools/build.py deploy models/mesi_coherence/mesi_coherence.explorer.json --archive
```

Deploy targets are configured in the `deploy` section of `explorer.json`.
Supported targets: `local` (copy to a directory) and `webdav` (UNC path).

### Full Pipeline (Sweep → Build → Deploy)

```bash
python tools/build.py all models/mesi_coherence/mesi_coherence.explorer.json
python tools/build.py all models/mesi_coherence/mesi_coherence.explorer.json --archive
```

---

## Using the Explorer

Open `distrib/index.html` in any modern browser (Chrome, Edge, Firefox, Safari).

### The Interface

The explorer has a **dark sidebar on the left** and a **light diagram pane on the right**:

```
┌──────────────────────────────────────────────────────────────┐
│  Model Title                                    42 flows  STATIC │
├──────────────┬───────────────────────────────────────────────┤
│              │                                               │
│  FLOW        │  PlusCal Editor          Sequence Diagram     │
│  SELECTION   │  (Monaco, TLA+          ┌─────────────────┐  │
│  ┌────────┐  │   syntax highlighting)  │                 │  │
│  │DropDown│  │                         │   PlantUML SVG   │  │
│  └────────┘  │                         │   (or raw text)  │  │
│  ┌────────┐  │                         │                 │  │
│  │DropDown│  │                         └─────────────────┘  │
│  └────────┘  │                                               │
│              │  Tips: Hover arrow for details ...             │
│  CHANNEL     ├───────────────────────────────────────────────┤
│  LEGEND      │                                               │
│              │                                               │
│  PLANTUML    │                                               │
│  SERVER      │                                               │
│  [Connect]   │                                               │
│              │                                               │
│  TLC SERVER  │                                               │
│  [Connect]   │                                               │
│              │                                               │
└──────────────┴───────────────────────────────────────────────┘
```

### Browsing Traces (Static Mode)

1. **Select parameter values** from the cascading dropdowns. Each dropdown
   filters the next — only valid combinations are shown.
2. The **sequence diagram** updates immediately from the embedded data.
3. The **PlusCal source** is shown in the editor pane (read-only in static mode).

### Connecting a PlantUML Server

Without a PlantUML server, diagrams are displayed as raw PlantUML text
(which can still be copied). To render SVGs:

1. Enter the PlantUML server URL in the **PlantUML Server** input
   (e.g., `http://localhost:8080` or `https://www.plantuml.com/plantuml`).
2. Click **Connect**. The status dot turns green on success.
3. All diagrams now render as SVG. The **Download SVG** button becomes available.

**Running a local PlantUML server with Docker:**

```bash
docker run -d --name plantuml -p 8080:8080 plantuml/plantuml-server:jetty
```

### Connecting a TLC Live Server

The TLC server enables **live model checking** — edit the PlusCal source and
re-run TLC without rebuilding the explorer.

1. Start the server from the model directory:

   ```bash
   cd models/mesi_coherence
   python ../../tools/tlc_server.py
   ```

2. In the explorer, enter `http://127.0.0.1:18080` in the **TLC Server** input
   and click **Connect**. The badge changes from **STATIC** to **LIVE**.

3. The **Run TLC** button appears in the editor toolbar. Changes to the PlusCal
   source are sent to the server, and the diagram updates with the fresh trace.

**Server options:**

```bash
python tools/tlc_server.py --port 18082      # Custom port
python tools/tlc_server.py --host 0.0.0.0    # Bind all interfaces
```

### Copying and Exporting

| Action | How |
|--------|-----|
| **Copy PlantUML** | Click the 📋 **Copy PlantUML** button in the diagram toolbar. |
| **Download SVG** | Click the ⬇ **SVG** button (visible only when a PlantUML server is connected and the diagram is rendered). |
| **Share a trace** | Copy the browser URL — parameter selections are encoded in the query string. |

### URL Parameters

Dropdown selections are automatically synced to URL query parameters:

```
index.html?Operation=Read&InitState=I&RemoteState=S
```

This means you can bookmark or share a direct link to any specific trace.

---

## Creating a New Model

1. **Create a model directory** with a PlusCal source file:

   ```
   models/my_model/
   ├── my_model.pcal
   └── my_model.explorer.json
   ```

2. **Write the explorer.json** config (see [Config Fields Reference](#config-fields-reference) above).
   At minimum:

   ```json
   {
     "module": "my_model",
     "constants": {
       "Param1": ["A", "B"],
       "Param2": ["X", "Y"]
     },
     "traceVariable": "trace",
     "doneVariable": "done",
     "participants": ["Alice", "Bob", "Server"]
   }
   ```

3. **Ensure the PlusCal source** appends messages to the `trace` variable
   (matching `traceVariable`) and sets `done` (matching `doneVariable`)
   to `TRUE` when the algorithm completes.

4. **Run the pipeline:**

   ```bash
   python tools/build.py all models/my_model/my_model.explorer.json
   ```

5. **Open** `models/my_model/distrib/index.html` in a browser.

---

## Troubleshooting

| Symptom | Cause | Fix |
|---------|-------|-----|
| "No traces found. Run 'sweep' first." | Build was run before sweep. | Run `python tools/build.py sweep <config>` first, or use `all`. |
| PlantUML shows raw text, not SVG | No PlantUML server connected. | Enter a server URL and click Connect. |
| PlantUML Connect fails | Server unreachable or CORS blocked. | Check the URL, ensure the server is running, and verify CORS headers. |
| TLC server Connect fails | `tlc_server.py` not running, or wrong port. | Start the server with `python tools/tlc_server.py` from the model directory. |
| Sweep produces 0 traces | All combos fail TLC. | Check invariants/properties in the config. Run a single combo manually to debug. |
| Dropdowns are empty | No valid parameter combinations. | Check that `constants` in `explorer.json` has non-empty value lists. |
