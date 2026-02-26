import * as vscode from 'vscode';
import * as path from 'path';
import * as fs from 'fs';
import { spawn, ChildProcess } from 'child_process';

export interface TraceEvent {
    src: string;
    dst: string;
    msg: string;
    data: string;
}

export interface TlcResult {
    success: boolean;
    statesFound?: number;
    trace?: TraceEvent[];
    error?: string;
    rawOutput?: string;
}

/**
 * Parse TLC dump output into trace events.
 * Looks for the `trace` variable in each state and extracts the final trace.
 */
function parseTrace(output: string): TraceEvent[] | undefined {
    // Look for the trace variable in TLC output
    const traceMatch = output.match(/trace\s*=\s*<<([\s\S]*?)>>/);
    if (!traceMatch) { return undefined; }

    const events: TraceEvent[] = [];
    const recordPattern = /\[src\s*\|->\s*"([^"]*)",\s*dst\s*\|->\s*"([^"]*)",\s*msg\s*\|->\s*"([^"]*)",\s*data\s*\|->\s*"([^"]*)"\]/g;

    let match;
    while ((match = recordPattern.exec(traceMatch[1])) !== null) {
        events.push({
            src: match[1],
            dst: match[2],
            msg: match[3],
            data: match[4]
        });
    }

    return events.length > 0 ? events : undefined;
}

export class TlcRunner {
    private activeProcess: ChildProcess | undefined;

    constructor(
        private javaPath: string | undefined,
        private tla2toolsPath: string | undefined
    ) {}

    async check(
        pcalPath: string,
        token?: vscode.CancellationToken
    ): Promise<TlcResult> {
        if (!this.javaPath || !this.tla2toolsPath) {
            return { success: false, error: 'Java or tla2tools.jar not found.' };
        }

        const dir = path.dirname(pcalPath);
        const base = path.basename(pcalPath, '.pcal');
        const tlaPath = path.join(dir, base + '.tla');
        const cfgPath = path.join(dir, base + '.cfg');

        if (!fs.existsSync(tlaPath)) {
            return { success: false, error: 'No .tla file found. Translate first.' };
        }

        const config = vscode.workspace.getConfiguration('pluscalExplorer');
        const jvmArgs = config.get<string[]>('jvmArgs', [
            '-XX:TieredStopAtLevel=1', '-Xms32m', '-Xmx256m', '-XX:+UseParallelGC'
        ]);
        const workers = config.get<string>('tlcWorkers', 'auto');
        const timeout = config.get<number>('tlcTimeout', 60) * 1000;

        const args = [
            ...jvmArgs,
            '-cp', this.tla2toolsPath!,
            'tlc2.TLC',
            tlaPath,
            '-deadlock',
            '-workers', workers,
            '-nowarning'
        ];

        if (fs.existsSync(cfgPath)) {
            args.push('-config', cfgPath);
        }

        // Dump file for trace extraction
        const dumpPath = path.join(dir, base + '_dump');
        args.push('-dump', dumpPath);

        return new Promise((resolve) => {
            const proc = spawn(this.javaPath!, args, { cwd: dir });
            this.activeProcess = proc;

            let stdout = '';
            let stderr = '';

            proc.stdout.on('data', (data) => { stdout += data.toString(); });
            proc.stderr.on('data', (data) => { stderr += data.toString(); });

            // Timeout
            const timer = setTimeout(() => {
                proc.kill();
                resolve({ success: false, error: `TLC timed out after ${timeout / 1000}s.` });
            }, timeout);

            // Cancellation
            token?.onCancellationRequested(() => {
                proc.kill();
                clearTimeout(timer);
                resolve({ success: false, error: 'Cancelled by user.' });
            });

            proc.on('close', (code) => {
                clearTimeout(timer);
                this.activeProcess = undefined;

                if (stdout.includes('No error has been found')) {
                    const statesMatch = stdout.match(/(\d+)\s+distinct\s+states?\s+found/);
                    resolve({
                        success: true,
                        statesFound: statesMatch ? parseInt(statesMatch[1]) : undefined,
                        rawOutput: stdout
                    });
                } else {
                    // Try to extract trace from dump
                    let trace: TraceEvent[] | undefined;
                    if (fs.existsSync(dumpPath)) {
                        const dumpContent = fs.readFileSync(dumpPath, 'utf-8');
                        trace = parseTrace(dumpContent);
                    }
                    // Also try from stdout
                    if (!trace) {
                        trace = parseTrace(stdout);
                    }

                    const errorMsg = extractTlcError(stdout) ||
                                     stderr || `TLC exited with code ${code}`;
                    resolve({
                        success: false,
                        trace,
                        error: errorMsg.trim(),
                        rawOutput: stdout
                    });
                }
            });

            proc.on('error', (err) => {
                clearTimeout(timer);
                resolve({ success: false, error: err.message });
            });
        });
    }

    kill(): void {
        this.activeProcess?.kill();
    }
}

/** Extract the first TLC error message from output. */
function extractTlcError(output: string): string | undefined {
    const patterns = [
        /Error:\s*(.*)/,
        /Invariant\s+(\w+)\s+is\s+violated/,
        /Temporal\s+properties\s+were\s+violated/,
        /Unrecoverable\s+error:\s*(.*)/,
    ];

    for (const pat of patterns) {
        const m = output.match(pat);
        if (m) { return m[0]; }
    }
    return undefined;
}
