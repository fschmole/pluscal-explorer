import * as vscode from 'vscode';
import * as path from 'path';
import * as fs from 'fs';
import { PcalTranslator } from './tlc/pcalTranslator';
import { TlcRunner } from './tlc/tlcRunner';
import { SequencePanel } from './webview/sequencePanel';
import { findJava, findTla2tools } from './tlc/javaFinder';
import { tlcTraceToPuml } from './generators/tlcTraceToPuml';

/** Shape of an explorer.json config file (relevant fields only). */
interface ExplorerConfig {
    traceVariable?: string;
    doneVariable?: string;
    title?: string;
    participants?: string[];
    channels?: string[];
    channelColors?: Record<string, { stroke: string; label: string }>;
    abbreviations?: Record<string, string>;
    plantUmlServer?: string;
    branding?: Record<string, unknown>;
    deploy?: Record<string, unknown>;
}

let translator: PcalTranslator | undefined;
let runner: TlcRunner | undefined;

export async function activate(context: vscode.ExtensionContext) {
    console.log('PlusCal Explorer activating...');

    // Discover Java and tla2tools.jar
    const javaPath = await findJava();
    const tla2toolsPath = await findTla2tools();

    if (!javaPath) {
        vscode.window.showWarningMessage(
            'PlusCal Explorer: Java not found. Set pluscalExplorer.javaPath or install Java.'
        );
    }
    if (!tla2toolsPath) {
        vscode.window.showWarningMessage(
            'PlusCal Explorer: tla2tools.jar not found. Set pluscalExplorer.tla2toolsPath.'
        );
    }

    translator = new PcalTranslator(javaPath, tla2toolsPath);
    runner = new TlcRunner(javaPath, tla2toolsPath);

    // Register commands
    context.subscriptions.push(
        vscode.commands.registerCommand('pluscalExplorer.translate', async () => {
            const editor = vscode.window.activeTextEditor;
            if (!editor || editor.document.languageId !== 'pluscal') {
                vscode.window.showErrorMessage('Open a .pcal file first.');
                return;
            }
            await translateCurrentFile(editor.document);
        }),

        vscode.commands.registerCommand('pluscalExplorer.check', async () => {
            const editor = vscode.window.activeTextEditor;
            if (!editor || editor.document.languageId !== 'pluscal') {
                vscode.window.showErrorMessage('Open a .pcal file first.');
                return;
            }
            await checkCurrentFile(editor.document, context);
        }),

        vscode.commands.registerCommand('pluscalExplorer.showDiagram', () => {
            SequencePanel.createOrShow(context.extensionUri);
        }),

        vscode.commands.registerCommand('pluscalExplorer.openModel', async () => {
            const modelDir = vscode.Uri.joinPath(context.extensionUri, 'models', 'mesi_coherence');
            const pcalFile = vscode.Uri.joinPath(modelDir, 'mesi_coherence.pcal');
            const doc = await vscode.workspace.openTextDocument(pcalFile);
            await vscode.window.showTextDocument(doc);
        })
    );

    // Translate on save
    context.subscriptions.push(
        vscode.workspace.onDidSaveTextDocument(async (doc) => {
            const config = vscode.workspace.getConfiguration('pluscalExplorer');
            if (doc.languageId === 'pluscal' && config.get<boolean>('translateOnSave', true)) {
                await translateCurrentFile(doc);
            }
        })
    );

    // Diagnostics collection for TLC errors
    const diagnostics = vscode.languages.createDiagnosticCollection('pluscal');
    context.subscriptions.push(diagnostics);

    console.log('PlusCal Explorer activated.');
}

async function translateCurrentFile(doc: vscode.TextDocument): Promise<boolean> {
    if (!translator) { return false; }

    return vscode.window.withProgress({
        location: vscode.ProgressLocation.Notification,
        title: 'Translating PlusCal...',
        cancellable: false
    }, async () => {
        const result = await translator!.translate(doc.uri.fsPath);
        if (result.success) {
            vscode.window.showInformationMessage('PlusCal translated successfully.');
            return true;
        } else {
            vscode.window.showErrorMessage(`Translation failed: ${result.error}`);
            return false;
        }
    });
}

async function checkCurrentFile(
    doc: vscode.TextDocument,
    context: vscode.ExtensionContext
): Promise<void> {
    if (!runner) { return; }

    // Translate first
    const translated = await translateCurrentFile(doc);
    if (!translated) { return; }

    // Load explorer.json config if present alongside the .pcal file
    const explorerConfig = loadExplorerConfig(doc.uri.fsPath);

    // Determine PlantUML server URL: model config > user setting > empty
    const userServerSetting = vscode.workspace
        .getConfiguration('pluscalExplorer')
        .get<string>('plantUmlServer', '');
    const plantUmlServer = explorerConfig?.plantUmlServer || userServerSetting || '';

    await vscode.window.withProgress({
        location: vscode.ProgressLocation.Notification,
        title: 'Running TLC...',
        cancellable: true
    }, async (_progress, token) => {
        const result = await runner!.check(doc.uri.fsPath, token);

        if (result.success) {
            vscode.window.showInformationMessage(
                `TLC: No errors. ${result.statesFound} distinct states.`
            );

            // Generate PlantUML from the TLC output even on success
            // (the model may have produced a trace via ALIAS)
            if (result.rawOutput) {
                const pumlText = generatePuml(result.rawOutput, explorerConfig);
                if (pumlText) {
                    SequencePanel.createOrShow(context.extensionUri);
                    SequencePanel.postTrace(pumlText, plantUmlServer);
                }
            }
        } else if (result.trace || result.rawOutput) {
            vscode.window.showWarningMessage(
                `TLC found a counterexample (${result.error}).`
            );
            // Generate PlantUML from raw TLC output
            const rawOutput = result.rawOutput || '';
            const pumlText = generatePuml(rawOutput, explorerConfig);
            if (pumlText) {
                SequencePanel.createOrShow(context.extensionUri);
                SequencePanel.postTrace(pumlText, plantUmlServer);
            } else {
                // Fallback: show panel with empty state
                SequencePanel.createOrShow(context.extensionUri);
                SequencePanel.postTrace('', plantUmlServer);
            }
        } else {
            vscode.window.showErrorMessage(`TLC error: ${result.error}`);
        }
    });
}

/**
 * Generate PlantUML text from TLC output, applying config overrides
 * for sweep-consistent rendering.
 */
function generatePuml(
    rawOutput: string,
    config?: ExplorerConfig | null,
): string | null {
    return tlcTraceToPuml(rawOutput, {
        traceVariable: config?.traceVariable,
        doneVariable: config?.doneVariable,
        title: config?.title,
        participants: config?.participants,
        channels: config?.channels,
        channelColors: config?.channelColors,
        abbreviations: config?.abbreviations,
    });
}

/**
 * Look for an explorer.json file alongside a .pcal file.
 * Tries: <basename>.explorer.json in the same directory.
 */
function loadExplorerConfig(pcalPath: string): ExplorerConfig | null {
    const dir = path.dirname(pcalPath);
    const base = path.basename(pcalPath, '.pcal');

    // Try <base>.explorer.json
    const configPath = path.join(dir, `${base}.explorer.json`);
    if (fs.existsSync(configPath)) {
        try {
            const raw = fs.readFileSync(configPath, 'utf-8');
            return JSON.parse(raw) as ExplorerConfig;
        } catch (e) {
            console.warn(`Failed to parse ${configPath}:`, e);
        }
    }

    // Also try explorer.json (without module prefix)
    const genericPath = path.join(dir, 'explorer.json');
    if (fs.existsSync(genericPath)) {
        try {
            const raw = fs.readFileSync(genericPath, 'utf-8');
            return JSON.parse(raw) as ExplorerConfig;
        } catch (e) {
            console.warn(`Failed to parse ${genericPath}:`, e);
        }
    }

    return null;
}

export function deactivate() {
    // Clean up
}
