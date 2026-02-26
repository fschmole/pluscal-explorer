import * as path from 'path';
import * as fs from 'fs';
import { spawn } from 'child_process';

export interface TranslationResult {
    success: boolean;
    tlaPath?: string;
    error?: string;
}

/**
 * Wraps bare PlusCal in (* ... *) TLA+ comment block with translation markers.
 *
 * The .pcal golden source stores the algorithm WITHOUT TLA+ comment
 * delimiters so editors can syntax-highlight PlusCal natively.
 * pcal.trans requires the block inside (* ... *) with BEGIN/END markers.
 */
function wrapPcalForTrans(text: string): string {
    const lines = text.split('\n');
    const out: string[] = [];
    let inAlgo = false;
    let depth = 0;

    for (const line of lines) {
        if (!inAlgo && (line.includes('--fair algorithm') || line.includes('--algorithm'))) {
            // Check if already wrapped in (* ... *)
            if (line.trimStart().startsWith('(*')) {
                out.push(line);
                inAlgo = true;
                depth = (line.match(/\{/g) || []).length - (line.match(/\}/g) || []).length;
                continue;
            }
            out.push('(* ' + line);
            inAlgo = true;
            depth = (line.match(/\{/g) || []).length - (line.match(/\}/g) || []).length;
            continue;
        }

        if (inAlgo) {
            depth += (line.match(/\{/g) || []).length - (line.match(/\}/g) || []).length;
            out.push(line);
            if (depth <= 0) {
                // Check if the line already ends with *)
                if (!line.trimEnd().endsWith('*)')) {
                    out.push('*)');
                }
                // Add translation markers if not present
                const remaining = lines.slice(lines.indexOf(line) + 1).join('\n');
                if (!remaining.includes('BEGIN TRANSLATION')) {
                    out.push('');
                    out.push('\\* BEGIN TRANSLATION');
                    out.push('\\* END TRANSLATION');
                }
                inAlgo = false;
            }
            continue;
        }

        out.push(line);
    }

    return out.join('\n');
}

export class PcalTranslator {
    constructor(
        private javaPath: string | undefined,
        private tla2toolsPath: string | undefined
    ) {}

    async translate(pcalPath: string): Promise<TranslationResult> {
        if (!this.javaPath || !this.tla2toolsPath) {
            return { success: false, error: 'Java or tla2tools.jar not found.' };
        }

        const dir = path.dirname(pcalPath);
        const base = path.basename(pcalPath, '.pcal');
        const tlaPath = path.join(dir, base + '.tla');

        // Read PlusCal source
        const pcalText = fs.readFileSync(pcalPath, 'utf-8');

        // Wrap if needed and write .tla
        const wrapped = wrapPcalForTrans(pcalText);
        fs.writeFileSync(tlaPath, wrapped, 'utf-8');

        // Run pcal.trans
        return new Promise((resolve) => {
            const proc = spawn(this.javaPath!, [
                '-cp', this.tla2toolsPath!,
                'pcal.trans', tlaPath
            ], { cwd: dir });

            let stdout = '';
            let stderr = '';

            proc.stdout.on('data', (data) => { stdout += data.toString(); });
            proc.stderr.on('data', (data) => { stderr += data.toString(); });

            proc.on('close', (code) => {
                if (code === 0 && stdout.includes('Translation completed')) {
                    resolve({ success: true, tlaPath });
                } else {
                    const errorMsg = stderr || stdout || `pcal.trans exited with code ${code}`;
                    resolve({ success: false, error: errorMsg.trim() });
                }
            });

            proc.on('error', (err) => {
                resolve({ success: false, error: err.message });
            });
        });
    }
}
