# PlusCal Explorer

A VS Code extension for authoring, model-checking, and visualizing
[PlusCal](https://lamport.azurewebsites.net/tla/pluscal.html) specifications
with [TLC](https://lamport.azurewebsites.net/tla/tools.html).

![PlantUML sequence diagram](https://img.shields.io/badge/rendering-PlantUML-blue)
![VS Code ≥ 1.85](https://img.shields.io/badge/vscode-%E2%89%A5%201.85-blue)

## Features

| Feature | Description |
|---------|-------------|
| **PlusCal syntax highlighting** | Full TextMate grammar for `.pcal` files |
| **Translate to TLA+** | One-key PlusCal → TLA+ translation |
| **TLC model checking** | Run TLC from the editor with configurable JVM args and workers |
| **Sequence diagram visualization** | Render TLC trace output as PlantUML sequence diagrams |
| **Parameter sweep** | Exhaustive sweep over model constants with configurable skip rules |
| **Explorer config** | Per-model `explorer.json` controls participants, channels, colors, and abbreviations |

## Quick Start

1. **Install the extension** from the VS Code Marketplace (or load from source).
2. **Open a `.pcal` file** — the extension activates automatically.
3. Press **Ctrl+Shift+T** to translate PlusCal → TLA+.
4. Press **Ctrl+Shift+R** to run TLC.
5. Run **PlusCal: Show Sequence Diagram** to view the trace.

## Prerequisites

| Dependency | Required | Notes |
|------------|----------|-------|
| **Java** (JDK 11+) | Yes | For running TLC. Set `pluscalExplorer.javaPath` or put `java` on PATH. |
| **tla2tools.jar** | Yes | The TLA+ toolbox. Set `pluscalExplorer.tla2toolsPath` or let auto-detect find it. |
| **PlantUML Server** | Optional | For SVG sequence diagrams. Without it, raw PlantUML text is shown. |

### Setting Up a PlantUML Server

The extension renders sequence diagrams via a PlantUML server.
Configure the server URL in settings:

```jsonc
// .vscode/settings.json
{
  "pluscalExplorer.plantUmlServer": "http://localhost:8080"
}
```

**Run a local server with Docker:**

```bash
docker run -d --name plantuml -p 8080:8080 plantuml/plantuml-server:jetty
```

Or use a public server like `https://www.plantuml.com/plantuml` (not
recommended for sensitive models).

## Explorer Configuration

Place a `<model>.explorer.json` (or `explorer.json`) alongside your `.pcal`
file to control visualization and sweep behavior:

```jsonc
{
  "module": "mesi_coherence",
  "title": "MESI Coherence Explorer",
  "pcal": "mesi_coherence.pcal",

  // TLC trace extraction
  "traceVariable": "trace",
  "doneVariable": "done",

  // Sweep constants — cartesian product of all values
  "constants": {
    "ReqType": ["RdData", "RdInv", "WrInv"],
    "InitState": ["M", "E", "S", "I"]
  },

  // Skip impossible combos
  "skip": [
    { "ReqType": "RdInv", "InitState": "I" }
  ],

  // Pin participant order across all diagrams (sweep-consistent)
  "participants": ["Proc1", "Bus", "Proc2", "Memory"],

  // Human-readable abbreviations for channel names
  "abbreviations": {
    "REQ": "Request",
    "RSP": "Response",
    "SNP": "Snoop"
  },

  // PlantUML server URL (overrides VS Code setting)
  "plantUmlServer": ""
}
```

### Sweep-Consistent Rendering

When `participants`, `channels`, or `channelColors` are specified in the
config, they are applied identically to every diagram in a sweep.  This
ensures consistent layout and color mapping across all parameter
combinations, making visual comparison straightforward.

Fields that are absent fall back to auto-discovery from the trace data.

## Extension Settings

| Setting | Type | Default | Description |
|---------|------|---------|-------------|
| `pluscalExplorer.tla2toolsPath` | string | `""` | Path to `tla2tools.jar` |
| `pluscalExplorer.javaPath` | string | `""` | Path to `java` executable |
| `pluscalExplorer.jvmArgs` | array | see below | JVM arguments for TLC |
| `pluscalExplorer.tlcWorkers` | string | `"auto"` | TLC worker thread count |
| `pluscalExplorer.translateOnSave` | boolean | `true` | Translate on save |
| `pluscalExplorer.checkOnSave` | boolean | `false` | Run TLC on save |
| `pluscalExplorer.tlcTimeout` | number | `60` | TLC timeout (seconds) |
| `pluscalExplorer.diagramTheme` | string | `"auto"` | Diagram color theme |
| `pluscalExplorer.plantUmlServer` | string | `""` | PlantUML server URL |

Default JVM args: `-XX:TieredStopAtLevel=1 -Xms32m -Xmx256m -XX:+UseParallelGC`

## Keyboard Shortcuts

| Shortcut | Command |
|----------|---------|
| `Ctrl+Shift+T` | Translate PlusCal → TLA+ |
| `Ctrl+Shift+R` | Run TLC Model Checker |

## Project Structure

```
pluscal-explorer/
├── src/
│   ├── extension.ts          # Extension activation and commands
│   ├── generators/
│   │   └── tlcTraceToPuml.ts # TLC trace → PlantUML conversion
│   ├── parsers/
│   │   └── traceParser.ts    # Parse TLC output
│   ├── types/
│   │   └── traceTypes.ts     # Shared type definitions
│   ├── webview/
│   │   └── sequencePanel.ts  # PlantUML webview panel
│   └── tlc/
│       ├── tlcRunner.ts      # TLC process management
│       ├── pcalTranslator.ts # PlusCal → TLA+ translation
│       └── javaFinder.ts     # Java & tla2tools.jar discovery
├── tools/                    # Python build pipeline (standalone)
│   ├── build.py              # Unified CLI: sweep / build / deploy
│   ├── build_explorer.py     # HTML explorer generator
│   ├── pcal_config.py        # explorer.json config loader
│   ├── tlc_sweep.py          # TLC parameter sweep engine
│   ├── tlc_server.py         # TLC WebSocket server
│   ├── gen_skip_rules.py     # Skip-rule generator
│   └── tla2tools.jar         # TLA+ Toolbox (Apache 2.0)
├── models/
│   └── mesi_coherence/       # Bundled open-source example model
├── syntaxes/
│   └── pluscal.tmLanguage.json
└── package.json
```

### tools/ Directory

The `tools/` directory contains a self-contained Python build pipeline for
batch model checking, HTML explorer generation, and deployment. It is usable
independently of the VS Code extension:

```bash
# Run a full sweep → build → deploy pipeline
python tools/build.py all path/to/model.explorer.json

# Individual steps
python tools/build.py sweep  path/to/model.explorer.json
python tools/build.py build  path/to/model.explorer.json
python tools/build.py deploy path/to/model.explorer.json --target local
```

The extension also searches `tools/` for `tla2tools.jar` automatically,
so no manual path configuration is needed when using the bundled jar.

## Development

```bash
# Install dependencies
npm install

# Compile
npm run compile

# Watch mode
npm run watch

# Lint
npm run lint
```

Press **F5** in VS Code to launch the Extension Development Host.

## License

MIT — see [LICENSE](LICENSE).
