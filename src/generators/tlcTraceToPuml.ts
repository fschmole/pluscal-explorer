/**
 * tlcTraceToPuml.ts — Generate PlantUML directly from TLC trace output.
 *
 * Parses TLC dump output, discovers all interleavings of concurrent
 * processes, computes sequential vs. concurrent step regions, and
 * produces a PlantUML sequence diagram with `par`/`group` blocks.
 *
 * Ports the Python `compute_steps()` algorithm from tlc_sweep.py.
 *
 * Copied from vscode-tlaplus/src/generators/tlcTraceToPuml.ts
 * with adjusted import paths and sweep-consistent rendering overrides.
 */
import { TraceMessage } from '../types/traceTypes';
import { parseTlcTrace, parseAllTerminalTraces } from '../parsers/traceParser';

// ── Auto-color palette ───────────────────────────────────────

const CHANNEL_PALETTE = [
    { stroke: '#e53935', label: '#c62828' },   // red
    { stroke: '#1E88E5', label: '#1565C0' },   // blue
    { stroke: '#43A047', label: '#2E7D32' },   // green
    { stroke: '#FB8C00', label: '#E65100' },   // orange
    { stroke: '#8E24AA', label: '#6A1B9A' },   // purple
    { stroke: '#00ACC1', label: '#00838F' },   // teal
    { stroke: '#F4511E', label: '#BF360C' },   // deep-orange
    { stroke: '#3949AB', label: '#283593' },   // indigo
];

/** Palette for concurrent-chain group boxes. */
const GROUP_COLORS = [
    '#FF6B6B', '#00BBF9', '#6BCB77', '#FF9F1C', '#9B59B6', '#00F5D4',
];

// ── Public API ───────────────────────────────────────────────

/**
 * Convert raw TLC output directly to a PlantUML sequence diagram.
 *
 * If `dumpText` is provided (full TLC dump file), all terminal-state
 * interleavings are extracted and concurrent regions are rendered as
 * `par`/`group` blocks.  Otherwise, a single trace is parsed from
 * the TLC stdout/stderr.
 *
 * Sweep-consistent overrides (`participants`, `channels`, `channelColors`,
 * `abbreviations`) allow pinning the visual layout across all diagrams
 * in a parameter sweep. Any attribute not provided falls back to
 * auto-discovery from the trace.
 *
 * @param tlcOutput       Raw TLC stdout/stderr text.
 * @param options         Configuration options.
 * @returns               PlantUML text string, or null if no trace found.
 */
export function tlcTraceToPuml(
    tlcOutput: string,
    options: {
        traceVariable?: string;
        doneVariable?: string;
        title?: string;
        dumpText?: string;
        // ── Sweep-consistent overrides (new) ──
        /** Fixed participant order (superset of all traces in sweep). */
        participants?: string[];
        /** Fixed channel list for palette assignment. */
        channels?: string[];
        /** Explicit color map per channel key. Overrides palette assignment. */
        channelColors?: Record<string, { stroke: string; label: string }>;
        /** Channel name humanization map for labels. */
        abbreviations?: Record<string, string>;
    } = {},
): string | null {
    const traceVar = options.traceVariable ?? '_seqDiagramTrace';
    const doneVar = options.doneVariable ?? 'flow_complete';

    // ── Extract traces ───────────────────────────────────────
    let allTraces: TraceMessage[][];

    if (options.dumpText) {
        allTraces = parseAllTerminalTraces(options.dumpText, traceVar, doneVar);
    } else {
        const single = parseTlcTrace(tlcOutput, traceVar);
        allTraces = single && single.length > 0 ? [single] : [];
    }
    if (allTraces.length === 0) {
        // Fallback: parse single trace from TLC error output
        const fallback = parseTlcTrace(tlcOutput, traceVar);
        if (fallback && fallback.length > 0) {
            allTraces = [fallback];
        } else {
            return null;
        }
    }

    const canonical = allTraces[0];

    // ── Compute steps (sequential + concurrent) ──────────────
    const steps = computeSteps(allTraces);

    // ── Discover or override participants ────────────────────
    let participantOrder: string[];

    if (options.participants && options.participants.length > 0) {
        // Use config-pinned participant order (superset of all traces)
        participantOrder = [...options.participants];
        // Append any participants found in trace but NOT in config (defensive)
        const configSet = new Set(participantOrder);
        for (const m of canonical) {
            for (const name of [m.src, m.dst]) {
                if (!configSet.has(name)) {
                    configSet.add(name);
                    participantOrder.push(name);
                }
            }
        }
    } else {
        // Auto-discover from canonical trace (original behavior)
        participantOrder = [];
        const seen = new Set<string>();
        for (const m of canonical) {
            for (const name of [m.src, m.dst]) {
                if (!seen.has(name)) {
                    seen.add(name);
                    participantOrder.push(name);
                }
            }
        }
    }

    // ── Discover or override channels and assign colors ──────
    const channelMap = new Map<string, { stroke: string; label: string }>();

    if (options.channelColors && Object.keys(options.channelColors).length > 0) {
        // Explicit color map — use directly
        for (const [key, style] of Object.entries(options.channelColors)) {
            channelMap.set(key, style);
        }
        // Also assign palette colors to any channel found in trace but not in channelColors
        let colorIdx = Object.keys(options.channelColors).length;
        for (const m of canonical) {
            const key = m.ch ?? m.msg;
            if (key && !channelMap.has(key)) {
                channelMap.set(key, CHANNEL_PALETTE[colorIdx % CHANNEL_PALETTE.length]);
                colorIdx++;
            }
        }
    } else if (options.channels && options.channels.length > 0) {
        // Fixed channel list for palette assignment
        for (let i = 0; i < options.channels.length; i++) {
            channelMap.set(options.channels[i], CHANNEL_PALETTE[i % CHANNEL_PALETTE.length]);
        }
        // Also assign palette colors to any channel found in trace but not in the list
        let colorIdx = options.channels.length;
        for (const m of canonical) {
            const key = m.ch ?? m.msg;
            if (key && !channelMap.has(key)) {
                channelMap.set(key, CHANNEL_PALETTE[colorIdx % CHANNEL_PALETTE.length]);
                colorIdx++;
            }
        }
    } else {
        // Auto-discover from canonical trace (original behavior)
        let colorIdx = 0;
        for (const m of canonical) {
            const key = m.ch ?? m.msg;
            if (key && !channelMap.has(key)) {
                channelMap.set(key, CHANNEL_PALETTE[colorIdx % CHANNEL_PALETTE.length]);
                colorIdx++;
            }
        }
    }

    // ── Generate PlantUML ────────────────────────────────────
    const lines: string[] = [];

    lines.push(options.title ? `@startuml ${esc(options.title)}` : '@startuml');
    lines.push('');

    // Skin settings
    lines.push('skinparam backgroundColor transparent');
    lines.push('skinparam sequenceMessageAlign center');
    lines.push('skinparam responseMessageBelowArrow true');
    lines.push('skinparam sequenceGroupBorderThickness 1');
    lines.push('skinparam sequenceBoxBorderColor #999999');
    lines.push('skinparam defaultFontName "Segoe UI", Arial, sans-serif');
    lines.push('skinparam defaultFontSize 12');
    lines.push('skinparam sequenceParticipantBorderColor #666666');
    lines.push('skinparam sequenceParticipantBackgroundColor #F5F5F5');
    lines.push('skinparam sequenceLifeLineBorderColor #BBBBBB');
    lines.push('skinparam sequenceDividerBorderColor #CCCCCC');
    lines.push('');

    lines.push('autonumber');
    lines.push('');

    // Declare participants
    for (const p of participantOrder) {
        lines.push(`participant "${p}" as ${sanitize(p)}`);
    }
    lines.push('');

    // Render steps
    renderSteps(lines, steps, channelMap, options.abbreviations);

    lines.push('');
    lines.push('@enduml');

    return lines.join('\n');
}

// ── compute_steps — port from Python tlc_sweep.py ────────────

/** Hashable signature for a trace message. */
function msgSig(m: TraceMessage): string {
    return `${m.msg}|${m.src}|${m.dst}|${m.ch ?? ''}`;
}

/** A step is either sequential messages or a concurrent region. */
type Step = { type: 'sequential'; messages: TraceMessage[] }
           | { type: 'concurrent'; chains: TraceMessage[][] };

/**
 * Return the relative order of a subset of canonical indices within
 * each trace.  If all orderings are identical, the subset's internal
 * order is *stable* across traces.
 */
function orderOfSubset(
    traces: TraceMessage[][],
    sigIndices: string[],
    subset: Set<number>,
): number[][] {
    const orders: number[][] = [];
    for (const trace of traces) {
        // Build sig→position map for this trace
        const sigToPos = new Map<string, number>();
        for (let pos = 0; pos < trace.length; pos++) {
            sigToPos.set(msgSig(trace[pos]), pos);
        }
        // Sort subset indices by their position in this trace
        const ranked = [...subset].sort(
            (a, b) => (sigToPos.get(sigIndices[a]) ?? a) - (sigToPos.get(sigIndices[b]) ?? b)
        );
        orders.push(ranked);
    }
    return orders;
}

/** Check if all orderings in the list are identical. */
function allOrdersSame(orders: number[][]): boolean {
    if (orders.length <= 1) {return true;}
    const first = orders[0];
    for (let i = 1; i < orders.length; i++) {
        const o = orders[i];
        if (o.length !== first.length) {return false;}
        for (let j = 0; j < first.length; j++) {
            if (o[j] !== first[j]) {return false;}
        }
    }
    return true;
}

/**
 * Recursively split variant indices into causal chains.
 */
function splitVariant(
    traces: TraceMessage[][],
    sigIndices: string[],
    indices: Set<number>,
): number[][] {
    if (indices.size <= 1) {
        return [[...indices]];
    }

    const orders = orderOfSubset(traces, sigIndices, indices);
    if (allOrdersSame(orders)) {
        return [orders[0]];
    }

    const idxList = [...indices];
    const k = idxList.length;

    let bestSplit: {
        sSet: Set<number>;
        sBar: Set<number>;
        sOrder: number[];
        sbOrder: number[];
    } | null = null;

    outer:
    for (let size = 1; size <= Math.floor(k / 2); size++) {
        for (const combo of combinations(idxList, size)) {
            const sSet = new Set(combo);
            const sBar = new Set<number>();
            for (const idx of indices) {
                if (!sSet.has(idx)) {sBar.add(idx);}
            }
            if (sBar.size === 0) {continue;}

            if (size === k - size && Math.min(...sSet) > Math.min(...sBar)) {
                continue;
            }

            const sOrders = orderOfSubset(traces, sigIndices, sSet);
            if (!allOrdersSame(sOrders)) {continue;}

            const sbOrders = orderOfSubset(traces, sigIndices, sBar);
            if (!allOrdersSame(sbOrders)) {continue;}

            bestSplit = {
                sSet, sBar,
                sOrder: sOrders[0],
                sbOrder: sbOrders[0],
            };
            break outer;
        }
    }

    if (!bestSplit) {
        return [...indices].sort((a, b) => a - b).map(i => [i]);
    }

    const left = splitVariant(traces, sigIndices, bestSplit.sSet);
    const right = splitVariant(traces, sigIndices, bestSplit.sBar);
    return [...left, ...right];
}

/**
 * Derive sequential steps and concurrent regions from all trace variants.
 */
function computeSteps(allTraces: TraceMessage[][]): Step[] {
    if (allTraces.length === 0) {return [];}

    const canonical = allTraces[0];
    const n = canonical.length;

    if (allTraces.length === 1) {
        return canonical.map(m => ({ type: 'sequential', messages: [m] }));
    }

    const sigIndices = canonical.map(m => msgSig(m));

    const fixed = new Array<boolean>(n).fill(true);
    for (let t = 1; t < allTraces.length; t++) {
        const trace = allTraces[t];
        for (let p = 0; p < n; p++) {
            if (fixed[p] && msgSig(trace[p]) !== sigIndices[p]) {
                fixed[p] = false;
            }
        }
    }

    const steps: Step[] = [];
    let i = 0;
    while (i < n) {
        if (fixed[i]) {
            steps.push({ type: 'sequential', messages: [canonical[i]] });
            i++;
        } else {
            let j = i;
            while (j < n && !fixed[j]) {j++;}
            const variantIndices = new Set<number>();
            for (let x = i; x < j; x++) {variantIndices.add(x);}

            const chains = splitVariant(allTraces, sigIndices, variantIndices);
            const msgChains = chains.map(chain => chain.map(idx => canonical[idx]));

            if (msgChains.length === 1) {
                for (const m of msgChains[0]) {
                    steps.push({ type: 'sequential', messages: [m] });
                }
            } else {
                steps.push({ type: 'concurrent', chains: msgChains });
            }
            i = j;
        }
    }
    return steps;
}

// ── Combinations generator ───────────────────────────────────

function* combinations<T>(arr: T[], k: number): Generator<T[]> {
    if (k === 0) { yield []; return; }
    if (k > arr.length) {return;}
    for (let i = 0; i <= arr.length - k; i++) {
        for (const rest of combinations(arr.slice(i + 1), k - 1)) {
            yield [arr[i], ...rest];
        }
    }
}

// ── PlantUML rendering ──────────────────────────────────────

function renderSteps(
    lines: string[],
    steps: Step[],
    channelMap: Map<string, { stroke: string; label: string }>,
    abbreviations?: Record<string, string>,
): void {
    for (const step of steps) {
        if (step.type === 'sequential') {
            for (const m of step.messages) {
                renderMessage(lines, m, channelMap, abbreviations);
            }
        } else {
            renderConcurrentStep(lines, step.chains, channelMap, abbreviations);
        }
    }
}

function renderConcurrentStep(
    lines: string[],
    chains: TraceMessage[][],
    channelMap: Map<string, { stroke: string; label: string }>,
    abbreviations?: Record<string, string>,
): void {
    if (chains.length === 0) {return;}
    if (chains.length === 1) {
        for (const m of chains[0]) {
            renderMessage(lines, m, channelMap, abbreviations);
        }
        return;
    }

    lines.push('');
    lines.push('par Concurrent Chains');
    for (let i = 0; i < chains.length; i++) {
        const color = GROUP_COLORS[i % GROUP_COLORS.length];
        lines.push(`  group #${color.replace('#', '')} Chain ${i + 1}`);
        for (const m of chains[i]) {
            lines.push(`    ${renderMessageStr(m, channelMap, abbreviations)}`);
        }
        lines.push('  end');
    }
    lines.push('end');
    lines.push('');
}

function renderMessage(
    lines: string[],
    m: TraceMessage,
    channelMap: Map<string, { stroke: string; label: string }>,
    abbreviations?: Record<string, string>,
): void {
    lines.push(renderMessageStr(m, channelMap, abbreviations));
}

/**
 * Humanize a channel name using the abbreviation map.
 * E.g., "ca_uxi_a2f_ad" → "CA UXI a2f AD" when abbreviations
 * contains { "CA": "CA", "UXI": "UXI", "AD": "AD" }.
 */
function humanizeChannel(ch: string, abbreviations: Record<string, string>): string {
    // Split on underscores and replace each segment using abbreviations
    const parts = ch.split('_');
    const humanized = parts.map(part => {
        const upper = part.toUpperCase();
        if (abbreviations[upper] !== undefined) {
            return abbreviations[upper];
        }
        if (abbreviations[part] !== undefined) {
            return abbreviations[part];
        }
        return part;
    });
    return humanized.join(' ');
}

function renderMessageStr(
    m: TraceMessage,
    channelMap: Map<string, { stroke: string; label: string }>,
    abbreviations?: Record<string, string>,
): string {
    const src = sanitize(m.src);
    const dst = sanitize(m.dst);
    const key = m.ch ?? m.msg;
    const style = key ? channelMap.get(key) : undefined;

    // Arrow with color
    let arrowStr = '-';
    if (style) {
        arrowStr += `[#${style.stroke.replace('#', '')}]`;
    }
    arrowStr += '>';

    // Label with color + optional channel annotation
    let lbl: string;
    if (style && m.ch) {
        const c = style.label.replace('#', '');
        // Humanize channel name if abbreviations provided
        const chLabel = abbreviations && Object.keys(abbreviations).length > 0
            ? humanizeChannel(m.ch, abbreviations)
            : m.ch;
        lbl = `<color:#${c}><b>${esc(m.msg)}</b></color>\\n<color:#${c}><size:9>${esc(chLabel)}</size></color>`;
    } else if (style) {
        const c = style.label.replace('#', '');
        lbl = `<color:#${c}><b>${esc(m.msg)}</b></color>`;
    } else {
        lbl = `<b>${esc(m.msg)}</b>`;
    }

    return `${src} ${arrowStr} ${dst} : ${lbl}`;
}

// ── Helpers ──────────────────────────────────────────────────

function esc(s: string): string {
    return s.replace(/\\/g, '\\\\');
}

function sanitize(name: string): string {
    return name.replace(/[^a-zA-Z0-9_]/g, '_');
}
