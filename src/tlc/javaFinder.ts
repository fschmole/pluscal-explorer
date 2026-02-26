import * as vscode from 'vscode';
import * as path from 'path';
import * as fs from 'fs';
import { execFile } from 'child_process';

/**
 * Find Java executable: setting → JAVA_HOME → PATH → common locations.
 */
export async function findJava(): Promise<string | undefined> {
    const config = vscode.workspace.getConfiguration('pluscalExplorer');
    const configured = config.get<string>('javaPath', '');
    if (configured && fs.existsSync(configured)) {
        return configured;
    }

    // JAVA_HOME
    const javaHome = process.env.JAVA_HOME;
    if (javaHome) {
        const bin = path.join(javaHome, 'bin', process.platform === 'win32' ? 'java.exe' : 'java');
        if (fs.existsSync(bin)) { return bin; }
    }

    // Try `java` on PATH
    return new Promise((resolve) => {
        const cmd = process.platform === 'win32' ? 'where' : 'which';
        execFile(cmd, ['java'], (err, stdout) => {
            if (err || !stdout.trim()) {
                resolve(undefined);
            } else {
                resolve(stdout.trim().split('\n')[0].trim());
            }
        });
    });
}

/**
 * Find tla2tools.jar: setting → extension tools/ → workspace root → active file dir → ~/.pluscal-explorer/.
 */
export async function findTla2tools(): Promise<string | undefined> {
    const config = vscode.workspace.getConfiguration('pluscalExplorer');
    const configured = config.get<string>('tla2toolsPath', '');
    if (configured && fs.existsSync(configured)) {
        return configured;
    }

    // Bundled in extension's tools/ directory
    const extDir = path.resolve(__dirname, '..', '..');
    const bundled = path.join(extDir, 'tools', 'tla2tools.jar');
    if (fs.existsSync(bundled)) { return bundled; }

    // Workspace root
    const folders = vscode.workspace.workspaceFolders;
    if (folders) {
        for (const folder of folders) {
            const candidate = path.join(folder.uri.fsPath, 'tla2tools.jar');
            if (fs.existsSync(candidate)) { return candidate; }
        }
    }

    // Active file's directory
    const editor = vscode.window.activeTextEditor;
    if (editor) {
        const dir = path.dirname(editor.document.uri.fsPath);
        const candidate = path.join(dir, 'tla2tools.jar');
        if (fs.existsSync(candidate)) { return candidate; }
    }

    // Home directory
    const home = process.env.HOME || process.env.USERPROFILE || '';
    const homeJar = path.join(home, '.pluscal-explorer', 'tla2tools.jar');
    if (fs.existsSync(homeJar)) { return homeJar; }

    return undefined;
}
