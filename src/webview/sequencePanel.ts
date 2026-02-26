import * as vscode from 'vscode';

/**
 * Manages the webview panel for sequence diagram visualization.
 *
 * Renders PlantUML-generated SVG when a PlantUML server is configured,
 * or falls back to showing raw PlantUML text with syntax highlighting.
 * Replaces the previous D3/vanilla-JS renderer.
 */
export class SequencePanel {
    public static currentPanel: SequencePanel | undefined;
    private static readonly viewType = 'pluscalExplorer.sequenceDiagram';

    private readonly panel: vscode.WebviewPanel;
    private readonly extensionUri: vscode.Uri;
    private disposables: vscode.Disposable[] = [];

    private constructor(panel: vscode.WebviewPanel, extensionUri: vscode.Uri) {
        this.panel = panel;
        this.extensionUri = extensionUri;

        this.panel.webview.html = this.getHtml();

        this.panel.onDidDispose(() => this.dispose(), null, this.disposables);
    }

    public static createOrShow(extensionUri: vscode.Uri): void {
        const column = vscode.window.activeTextEditor
            ? vscode.window.activeTextEditor.viewColumn === vscode.ViewColumn.One
                ? vscode.ViewColumn.Two
                : vscode.ViewColumn.One
            : vscode.ViewColumn.One;

        if (SequencePanel.currentPanel) {
            SequencePanel.currentPanel.panel.reveal(column);
            return;
        }

        const panel = vscode.window.createWebviewPanel(
            SequencePanel.viewType,
            'Sequence Diagram',
            column,
            {
                enableScripts: true,
                retainContextWhenHidden: true,
            }
        );

        SequencePanel.currentPanel = new SequencePanel(panel, extensionUri);
    }

    /**
     * Post a trace to the webview for rendering.
     *
     * @param pumlText       Generated PlantUML text.
     * @param plantUmlServer PlantUML server URL (empty string = show raw text).
     */
    public static postTrace(pumlText: string, plantUmlServer: string): void {
        if (SequencePanel.currentPanel) {
            SequencePanel.currentPanel.panel.webview.postMessage({
                type: 'trace',
                pumlText,
                plantUmlServer,
            });
        }
    }

    private dispose(): void {
        SequencePanel.currentPanel = undefined;
        this.panel.dispose();
        while (this.disposables.length) {
            const d = this.disposables.pop();
            d?.dispose();
        }
    }

    private getHtml(): string {
        return /* html */ `<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Sequence Diagram</title>
    <style>
        body {
            margin: 0;
            padding: 16px;
            font-family: var(--vscode-font-family);
            color: var(--vscode-foreground);
            background: var(--vscode-editor-background);
        }
        .empty-state {
            display: flex;
            align-items: center;
            justify-content: center;
            height: 300px;
            color: var(--vscode-descriptionForeground);
            font-size: 14px;
        }
        .toolbar {
            display: flex;
            gap: 8px;
            margin-bottom: 12px;
            flex-wrap: wrap;
        }
        .toolbar button {
            background: var(--vscode-button-background);
            color: var(--vscode-button-foreground);
            border: none;
            padding: 4px 12px;
            cursor: pointer;
            font-size: 12px;
            border-radius: 2px;
        }
        .toolbar button:hover {
            background: var(--vscode-button-hoverBackground);
        }
        #diagram {
            width: 100%;
            min-height: 400px;
            overflow: auto;
        }
        #diagram svg {
            max-width: 100%;
            height: auto;
        }
        #puml-text {
            display: none;
            width: 100%;
            min-height: 400px;
            background: var(--vscode-editor-background);
            color: var(--vscode-editor-foreground);
            border: 1px solid var(--vscode-panel-border);
            padding: 12px;
            font-family: var(--vscode-editor-fontFamily, 'Consolas, monospace');
            font-size: var(--vscode-editor-fontSize, 13px);
            white-space: pre;
            overflow: auto;
            tab-size: 4;
        }
        .zoom-controls {
            display: flex;
            gap: 4px;
            align-items: center;
        }
        .zoom-label {
            font-size: 11px;
            color: var(--vscode-descriptionForeground);
            min-width: 40px;
            text-align: center;
        }
        .status-bar {
            margin-top: 8px;
            font-size: 11px;
            color: var(--vscode-descriptionForeground);
        }
        .error-msg {
            color: var(--vscode-errorForeground);
            padding: 12px;
            border: 1px solid var(--vscode-errorForeground);
            border-radius: 4px;
            margin: 12px 0;
        }
    </style>
</head>
<body>
    <div class="toolbar" id="toolbar">
        <button id="btn-copy-puml" title="Copy PlantUML text to clipboard">Copy PlantUML</button>
        <button id="btn-download-svg" title="Download rendered SVG" style="display:none">Download SVG</button>
        <div class="zoom-controls" id="zoom-controls" style="display:none">
            <button id="btn-zoom-out">-</button>
            <span class="zoom-label" id="zoom-label">100%</span>
            <button id="btn-zoom-in">+</button>
            <button id="btn-zoom-reset">Reset</button>
        </div>
    </div>
    <div id="diagram">
        <div class="empty-state">
            Run TLC (Ctrl+Shift+R) to generate a trace.
        </div>
    </div>
    <pre id="puml-text"></pre>
    <div class="status-bar" id="status-bar"></div>

    <script>
        const vscode = acquireVsCodeApi();

        let currentPuml = '';
        let currentSvg = '';
        let zoomLevel = 100;

        window.addEventListener('message', async (event) => {
            const msg = event.data;
            if (msg.type === 'trace') {
                currentPuml = msg.pumlText || '';
                const serverUrl = msg.plantUmlServer || '';

                if (!currentPuml) {
                    showEmpty('No trace data to display.');
                    return;
                }

                if (serverUrl) {
                    await renderWithServer(currentPuml, serverUrl);
                } else {
                    showRawPuml(currentPuml);
                }
            }
        });

        async function renderWithServer(puml, serverUrl) {
            const diagram = document.getElementById('diagram');
            const pumlText = document.getElementById('puml-text');
            const btnDownload = document.getElementById('btn-download-svg');
            const zoomControls = document.getElementById('zoom-controls');
            const statusBar = document.getElementById('status-bar');

            diagram.innerHTML = '<div class="empty-state">Rendering...</div>';
            pumlText.style.display = 'none';
            statusBar.textContent = '';

            try {
                // PlantUML server expects deflate+base64 encoded text in URL
                const encoded = await encodePuml(puml);
                const url = serverUrl.replace(/\\/+$/, '') + '/svg/' + encoded;

                const response = await fetch(url);
                if (!response.ok) {
                    throw new Error('Server returned ' + response.status + ' ' + response.statusText);
                }

                currentSvg = await response.text();

                if (!currentSvg.includes('<svg')) {
                    throw new Error('Server did not return valid SVG');
                }

                diagram.innerHTML = currentSvg;
                btnDownload.style.display = '';
                zoomControls.style.display = 'flex';
                zoomLevel = 100;
                updateZoom();
                statusBar.textContent = 'Rendered via ' + serverUrl;

            } catch (err) {
                // Fallback to raw PlantUML text
                diagram.innerHTML = '<div class="error-msg">Failed to render SVG: ' +
                    escapeHtml(err.message) +
                    '<br><br>Showing raw PlantUML text instead.</div>';
                showRawPuml(puml, true);
                statusBar.textContent = 'Render failed — showing PlantUML text';
            }
        }

        function showRawPuml(puml, keepDiagram) {
            const diagram = document.getElementById('diagram');
            const pumlText = document.getElementById('puml-text');
            const btnDownload = document.getElementById('btn-download-svg');
            const zoomControls = document.getElementById('zoom-controls');
            const statusBar = document.getElementById('status-bar');

            if (!keepDiagram) {
                diagram.innerHTML = '';
            }
            pumlText.style.display = 'block';
            pumlText.textContent = puml;
            btnDownload.style.display = 'none';
            zoomControls.style.display = 'none';
            statusBar.textContent = 'No PlantUML server configured — showing raw text. Set pluscalExplorer.plantUmlServer to enable SVG rendering.';
        }

        function showEmpty(message) {
            const diagram = document.getElementById('diagram');
            const pumlText = document.getElementById('puml-text');
            diagram.innerHTML = '<div class="empty-state">' + escapeHtml(message) + '</div>';
            pumlText.style.display = 'none';
        }

        function updateZoom() {
            const svg = document.querySelector('#diagram svg');
            if (svg) {
                svg.style.transform = 'scale(' + (zoomLevel / 100) + ')';
                svg.style.transformOrigin = 'top left';
            }
            document.getElementById('zoom-label').textContent = zoomLevel + '%';
        }

        // ── PlantUML encoding (deflate + base64) ──

        async function encodePuml(text) {
            // Use the standard PlantUML text encoding:
            // 1. deflate the UTF-8 bytes
            // 2. re-encode with PlantUML's custom base64 alphabet
            const encoder = new TextEncoder();
            const data = encoder.encode(text);

            // Use CompressionStream API for deflate
            const cs = new CompressionStream('deflate');
            const writer = cs.writable.getWriter();
            writer.write(data);
            writer.close();

            const compressed = await new Response(cs.readable).arrayBuffer();
            return plantUmlBase64Encode(new Uint8Array(compressed));
        }

        function plantUmlBase64Encode(data) {
            // PlantUML uses a custom base64 alphabet
            let result = '';
            for (let i = 0; i < data.length; i += 3) {
                if (i + 2 < data.length) {
                    result += append3bytes(data[i], data[i + 1], data[i + 2]);
                } else if (i + 1 < data.length) {
                    result += append3bytes(data[i], data[i + 1], 0);
                } else {
                    result += append3bytes(data[i], 0, 0);
                }
            }
            return result;
        }

        function append3bytes(b1, b2, b3) {
            const c1 = b1 >> 2;
            const c2 = ((b1 & 0x3) << 4) | (b2 >> 4);
            const c3 = ((b2 & 0xF) << 2) | (b3 >> 6);
            const c4 = b3 & 0x3F;
            return encode6bit(c1) + encode6bit(c2) + encode6bit(c3) + encode6bit(c4);
        }

        function encode6bit(b) {
            if (b < 10) return String.fromCharCode(48 + b);          // 0-9
            b -= 10;
            if (b < 26) return String.fromCharCode(65 + b);          // A-Z
            b -= 26;
            if (b < 26) return String.fromCharCode(97 + b);          // a-z
            b -= 26;
            if (b === 0) return '-';
            if (b === 1) return '_';
            return '?';
        }

        function escapeHtml(str) {
            const div = document.createElement('div');
            div.textContent = str;
            return div.innerHTML;
        }

        // ── Button handlers ──

        document.getElementById('btn-copy-puml').addEventListener('click', () => {
            if (currentPuml) {
                navigator.clipboard.writeText(currentPuml).then(() => {
                    const btn = document.getElementById('btn-copy-puml');
                    const orig = btn.textContent;
                    btn.textContent = 'Copied!';
                    setTimeout(() => { btn.textContent = orig; }, 1500);
                });
            }
        });

        document.getElementById('btn-download-svg').addEventListener('click', () => {
            if (currentSvg) {
                const blob = new Blob([currentSvg], { type: 'image/svg+xml' });
                const url = URL.createObjectURL(blob);
                const a = document.createElement('a');
                a.href = url;
                a.download = 'trace.svg';
                a.click();
                URL.revokeObjectURL(url);
            }
        });

        document.getElementById('btn-zoom-in').addEventListener('click', () => {
            zoomLevel = Math.min(zoomLevel + 25, 400);
            updateZoom();
        });

        document.getElementById('btn-zoom-out').addEventListener('click', () => {
            zoomLevel = Math.max(zoomLevel - 25, 25);
            updateZoom();
        });

        document.getElementById('btn-zoom-reset').addEventListener('click', () => {
            zoomLevel = 100;
            updateZoom();
        });
    </script>
</body>
</html>`;
    }
}
