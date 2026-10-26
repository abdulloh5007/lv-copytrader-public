/**
 * LV Copytrader — RunStream.
 *
 * The live-reasoning column (BUILD_PROMPT §9): what the AI is thinking right
 * now, and what it did about it.
 *
 * ORDER IS THE MESSAGE. A run renders exactly as it happened — thinking text
 * first, then each tool call as a card, in arrival order — because the operator
 * is being asked to judge a chain of reasoning, and a re-ordered chain is a
 * different chain. Events drive it:
 *
 *   run.started      opens a run and makes it the focus
 *   run.thinking     appends to the open thinking block, never remounting it
 *   run.text         appends to the open note block
 *   run.tool_call    pushes a ToolCallCard in `pending`
 *   run.tool_result  resolves that card to ok / failed
 *   run.finished     closes the run and writes its totals
 *
 * FINISHED RUNS COLLAPSE TO ONE LINE THAT EXPANDS — decision, summary,
 * iterations, tokens, latency and model — so a day of history stays readable in
 * one column. Runs finished before this page loaded are seeded from
 * `getRuns()`; expanding one fetches its persisted steps and renders them
 * through the same components as the live path, so nothing looks different
 * because it happened five minutes ago.
 *
 * AN `incomplete` RUN RENDERS IN RED. It means the model never called `finish`
 * (BUILD §5.3), so its conclusion was never recorded — the operator has a run
 * whose decision nobody knows. Making that impossible to miss is the point of
 * the state existing at all.
 *
 * AUTO-SCROLL, AND THE END OF IT. While a run is live the column follows the
 * newest step. The moment the operator scrolls up — the live sentinel leaves the
 * viewport — following stops and a control appears to resume it. Nothing yanks
 * the view back under a reader.
 */
import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react';
import type { ReactElement, ReactNode } from 'react';

import {
  getRun,
  getRuns,
  getSignal,
  isApiError,
  type RunDecision,
  type RunKind,
  type RunOut,
  type RunStatus,
  type RunStepOut,
  type StreamEvent,
} from '../api/client';
import {
  createStream,
  type StreamEventHandler,
  type StreamStatus,
  type StreamStatusHandler,
} from '../api/stream';
import {
  formatDateTime,
  formatDuration,
  formatInteger,
  formatLatency,
  formatNumber,
  useCurrentLocale,
  useT,
  type TKey,
  type Translate,
} from '../i18n';
import ThinkingBlock from './ThinkingBlock';
import {
  ToolCallCard,
  extractToolArgs,
  extractToolCallId,
  extractToolResult,
  type ToolStatus,
} from './ToolCallCard';

/* ───────────────────────── the shared dashboard stream ───────────────────── */

/**
 * How a component attaches to the dashboard's ONE socket.
 *
 * A page that already owns the socket passes its own fan-out; a component with
 * no such parent falls back to `subscribeToDashboardStream`, which multiplexes a
 * single lazily-opened connection across every subscriber. Either way there is
 * one WebSocket, which is the contract in CONTRACTS §19.
 */
export type StreamSubscribe = (
  onEvent: StreamEventHandler,
  onStatus?: StreamStatusHandler,
) => () => void;

/** A fan-out a page can hand to several components while owning one socket. */
export interface StreamFanout {
  /** Pass this as the `subscribe` prop. It is stable for the fan-out's lifetime. */
  subscribe: StreamSubscribe;
  /** Feed it every event from the page's socket. */
  publish: StreamEventHandler;
  /** Feed it every status change from the page's socket. */
  publishStatus: StreamStatusHandler;
}

/**
 * Build a fan-out, so a shell that already owns the dashboard socket can drive
 * every component from it without any of them opening a second one:
 *
 *     const bus = useMemo(createEventFanout, []);
 *     useStream(bus.publish, bus.publishStatus);
 *     <SignalFeed subscribe={bus.subscribe} />
 *     <RunStream  subscribe={bus.subscribe} />
 *
 * `useMemo(..., [])` is what keeps `subscribe` stable; a fan-out rebuilt every
 * render would tear the subscriptions down and rebuild them every render too.
 */
export function createEventFanout(): StreamFanout {
  const events = new Set<StreamEventHandler>();
  const statuses = new Set<StreamStatusHandler>();

  const subscribe: StreamSubscribe = (onEvent, onStatus) => {
    events.add(onEvent);
    if (onStatus) statuses.add(onStatus);
    return () => {
      events.delete(onEvent);
      if (onStatus) statuses.delete(onStatus);
    };
  };

  return {
    subscribe,
    publish: (event) => {
      for (const listener of [...events]) listener(event);
    },
    publishStatus: (status) => {
      for (const listener of [...statuses]) listener(status);
    },
  };
}

const hubEventListeners = new Set<StreamEventHandler>();
const hubStatusListeners = new Set<StreamStatusHandler>();
let hubHandle: ReturnType<typeof createStream> | null = null;
let hubCloseTimer: number | null = null;

/** Grace period before an unsubscribed hub hangs up, so StrictMode's remount reuses the socket. */
const HUB_CLOSE_GRACE_MS = 500;

/**
 * Subscribe to the process-wide dashboard stream, opening it on first use and
 * closing it when the last subscriber leaves.
 */
export const subscribeToDashboardStream: StreamSubscribe = (onEvent, onStatus) => {
  hubEventListeners.add(onEvent);
  if (onStatus) hubStatusListeners.add(onStatus);

  if (hubCloseTimer !== null) {
    window.clearTimeout(hubCloseTimer);
    hubCloseTimer = null;
  }

  hubHandle ??= createStream(
    (event) => {
      // A copy, so a handler that unsubscribes mid-dispatch cannot skip its neighbour.
      for (const listener of [...hubEventListeners]) listener(event);
    },
    (status) => {
      for (const listener of [...hubStatusListeners]) listener(status);
    },
  );

  return () => {
    hubEventListeners.delete(onEvent);
    if (onStatus) hubStatusListeners.delete(onStatus);
    if (hubEventListeners.size > 0 || hubStatusListeners.size > 0) return;
    if (hubCloseTimer !== null) window.clearTimeout(hubCloseTimer);
    hubCloseTimer = window.setTimeout(() => {
      hubCloseTimer = null;
      if (hubEventListeners.size > 0 || hubStatusListeners.size > 0) return;
      hubHandle?.close();
      hubHandle = null;
    }, HUB_CLOSE_GRACE_MS);
  };
};

/** Attach to `subscribe` (or the shared hub) for the lifetime of a component. */
function useStreamSubscription(
  subscribe: StreamSubscribe | undefined,
  onEvent: StreamEventHandler,
  onStatus?: StreamStatusHandler,
): void {
  const eventRef = useRef<StreamEventHandler>(onEvent);
  const statusRef = useRef<StreamStatusHandler | undefined>(onStatus);

  useEffect(() => {
    eventRef.current = onEvent;
    statusRef.current = onStatus;
  });

  useEffect(() => {
    const attach = subscribe ?? subscribeToDashboardStream;
    return attach(
      (event) => { eventRef.current(event); },
      (status) => { statusRef.current?.(status); },
    );
  }, [subscribe]);
}

/* ────────────────────────────── run modelling ────────────────────────────── */

/** One block of the model's own words, growing delta by delta. */
interface LiveProseStep {
  kind: 'thinking' | 'text';
  key: string;
  text: string;
}

/** One tool invocation and, once the executor answers, its result. */
interface LiveToolStep {
  kind: 'tool';
  key: string;
  callId: string;
  name: string;
  input: Record<string, unknown>;
  status: ToolStatus;
  result: Record<string, unknown> | null;
}

type LiveStep = LiveProseStep | LiveToolStep;

/** A run observed on the stream, with everything it has said so far. */
interface LiveRun {
  id: string;
  runKind: RunKind;
  status: RunStatus;
  sourceId: number | null;
  messageRowId: number | null;
  signalRef: string | null;
  providerKind: string | null;
  model: string | null;
  armed: boolean;
  startedAtMs: number;
  finishedAtMs: number | null;
  steps: LiveStep[];
  decision: RunDecision | null;
  summary: string | null;
  error: string | null;
  iterations: number;
  latencyMs: number;
  inputTokens: number;
  outputTokens: number;
}

interface RunStreamState {
  runs: LiveRun[];
  /** Bumped on every applied event; the auto-scroll effect watches it. */
  version: number;
}

/** How many streamed runs stay in memory. Older ones are seeded from REST instead. */
const LIVE_RUN_CAP = 12;

/** Mirrors `MAX_TOOL_ITERATIONS` in `app/config.py` (CONTRACTS §1). */
export const MAX_TOOL_ITERATIONS = 32;

const EMPTY_STATE: RunStreamState = { runs: [], version: 0 };

function newLiveRun(id: string): LiveRun {
  return {
    id,
    runKind: 'signal',
    status: 'running',
    sourceId: null,
    messageRowId: null,
    signalRef: null,
    providerKind: null,
    model: null,
    armed: false,
    startedAtMs: Date.now(),
    finishedAtMs: null,
    steps: [],
    decision: null,
    summary: null,
    error: null,
    iterations: 0,
    latencyMs: 0,
    inputTokens: 0,
    outputTokens: 0,
  };
}

/** Replace one run in the list, preserving order; a no-op when the run is unknown. */
function patchRun(
  state: RunStreamState,
  runId: string,
  patch: (run: LiveRun) => LiveRun,
): RunStreamState {
  const index = state.runs.findIndex((run) => run.id === runId);
  if (index < 0) return state;
  const runs = state.runs.slice();
  runs[index] = patch(runs[index] as LiveRun);
  return { runs, version: state.version + 1 };
}

/**
 * Append a prose delta.
 *
 * When the last step is already the same kind of prose the delta extends it —
 * one block, one `<p>`, patched in place by React — and only a different kind of
 * step in between starts a new one. That is what keeps a ThinkingBlock from
 * remounting on every token.
 */
function appendProse(run: LiveRun, kind: 'thinking' | 'text', delta: string): LiveRun {
  const steps = run.steps.slice();
  const last = steps[steps.length - 1];
  // `last.kind !== 'tool'` is what narrows the union; comparing a discriminant to
  // another VARIABLE does not narrow in TypeScript, only to a literal.
  if (last && last.kind !== 'tool' && last.kind === kind) {
    steps[steps.length - 1] = { ...last, text: last.text + delta };
  } else {
    steps.push({ kind, key: `${run.id}:${kind}:${String(steps.length)}`, text: delta });
  }
  return { ...run, steps };
}

/** Apply one stream event to the run list. Pure: it never mutates what it is given. */
function applyEvent(state: RunStreamState, event: StreamEvent): RunStreamState {
  switch (event.type) {
    case 'run.started': {
      const data = event.data;
      if (state.runs.some((run) => run.id === data.run_id)) return state;
      const run: LiveRun = {
        ...newLiveRun(data.run_id),
        runKind: data.kind,
        sourceId: data.source_id,
        messageRowId: data.message_row_id,
        signalRef: data.signal_ref,
        providerKind: data.provider_kind,
        model: data.model,
        armed: data.armed,
        startedAtMs: event.ts > 0 ? event.ts * 1000 : Date.now(),
      };
      return { runs: [run, ...state.runs].slice(0, LIVE_RUN_CAP), version: state.version + 1 };
    }

    case 'run.thinking':
      return patchRun(state, event.data.run_id, (run) =>
        appendProse(run, 'thinking', event.data.text),
      );

    case 'run.text':
      return patchRun(state, event.data.run_id, (run) => appendProse(run, 'text', event.data.text));

    case 'run.tool_call': {
      const data = event.data;
      return patchRun(state, data.run_id, (run) => {
        const callId = data.id || `${data.name}#${String(data.idx)}`;
        if (run.steps.some((step) => step.kind === 'tool' && step.callId === callId)) return run;
        const step: LiveToolStep = {
          kind: 'tool',
          key: `${run.id}:tool:${callId}`,
          callId,
          name: data.name,
          input: data.input,
          status: 'pending',
          result: null,
        };
        return { ...run, steps: [...run.steps, step] };
      });
    }

    case 'run.tool_result': {
      const data = event.data;
      return patchRun(state, data.run_id, (run) => {
        const callId = data.id || `${data.name}#${String(data.idx)}`;
        const steps = run.steps.slice();
        let index = steps.findIndex((step) => step.kind === 'tool' && step.callId === callId);
        if (index < 0) {
          // The call may predate this client (a reconnect replayed only part of the run):
          // fall back to the most recent unresolved call of the same name.
          for (let i = steps.length - 1; i >= 0; i -= 1) {
            const step = steps[i];
            if (step && step.kind === 'tool' && step.name === data.name && step.status === 'pending') {
              index = i;
              break;
            }
          }
        }
        const resolved: LiveToolStep = {
          kind: 'tool',
          key: index >= 0 ? (steps[index] as LiveToolStep).key : `${run.id}:tool:${callId}`,
          callId,
          name: data.name,
          input: index >= 0 ? (steps[index] as LiveToolStep).input : {},
          status: data.ok ? 'ok' : 'failed',
          result: data.result,
        };
        if (index >= 0) steps[index] = resolved;
        else steps.push(resolved);
        return { ...run, steps };
      });
    }

    case 'run.finished': {
      const data = event.data;
      return patchRun(state, data.run_id, (run) => ({
        ...run,
        status: data.status,
        decision: data.decision,
        summary: data.summary,
        error: data.error,
        iterations: data.iterations,
        latencyMs: data.latency_ms,
        inputTokens: data.input_tokens,
        outputTokens: data.output_tokens,
        finishedAtMs: event.ts > 0 ? event.ts * 1000 : Date.now(),
      }));
    }

    case 'provider.request': {
      const data = event.data;
      if (!data.run_id) return state;
      return patchRun(state, data.run_id, (run) => ({
        ...run,
        providerKind: run.providerKind ?? data.provider_kind,
        model: run.model ?? data.model,
      }));
    }

    default:
      return state;
  }
}

/** Project a streamed run onto the REST shape, so one component renders both. */
function liveToRunOut(run: LiveRun): RunOut {
  return {
    id: run.id,
    source_id: run.sourceId,
    message_row_id: run.messageRowId,
    kind: run.runKind,
    status: run.status,
    decision: run.decision,
    summary: run.summary,
    error: run.error,
    provider_kind: run.providerKind,
    model: run.model,
    input_tokens: run.inputTokens,
    output_tokens: run.outputTokens,
    latency_ms: run.latencyMs,
    iterations: run.iterations,
    armed_at_start: run.armed,
    started_at: new Date(run.startedAtMs).toISOString(),
    finished_at: run.finishedAtMs === null ? null : new Date(run.finishedAtMs).toISOString(),
  };
}

/* ─────────────────────────── persisted step pairing ──────────────────────── */

export type RenderedStep =
  | { kind: 'thinking'; key: string; text: string }
  | { kind: 'text'; key: string; text: string }
  | { kind: 'error'; key: string; text: string }
  | { kind: 'provider'; key: string; payload: Record<string, unknown> }
  | {
      kind: 'tool';
      key: string;
      callId: string | null;
      name: string;
      args: Record<string, unknown>;
      status: ToolStatus;
      result: Record<string, unknown> | null;
    };

/** The identity a persisted tool step pairs on, best available first. */
function stepPairKey(step: RunStepOut): string {
  const id = extractToolCallId(step.payload);
  if (id) return `id:${id}`;
  const idx = step.payload.idx;
  if (typeof idx === 'number') return `idx:${String(idx)}`;
  return `name:${step.name ?? ''}`;
}

/**
 * Turn a flat list of persisted steps into what the column renders, joining each
 * `tool_call` to the `tool_result` that answered it.
 *
 * Pairing prefers the provider's tool_use id, falls back to the call ordinal and
 * finally to "the most recent unanswered call with this name" — a result whose
 * call is genuinely missing still renders, rather than disappearing.
 */
export function pairRunSteps(steps: RunStepOut[]): RenderedStep[] {
  const out: RenderedStep[] = [];
  const openCalls = new Map<string, number>();

  for (const step of steps) {
    const key = `${step.run_id}:${String(step.idx)}:${String(step.id)}`;

    if (step.type === 'thinking' || step.type === 'text') {
      const payloadText = step.payload.text ?? step.payload.content;
      const text = typeof payloadText === 'string' ? payloadText : '';
      out.push(
        step.type === 'thinking'
          ? { kind: 'thinking', key, text }
          : { kind: 'text', key, text },
      );
      continue;
    }

    if (step.type === 'error') {
      const detail = step.payload.message ?? step.payload.error ?? step.payload.detail;
      out.push({
        kind: 'error',
        key,
        text: typeof detail === 'string' ? detail : JSON.stringify(step.payload),
      });
      continue;
    }

    if (step.type === 'provider') {
      out.push({ kind: 'provider', key, payload: step.payload });
      continue;
    }

    if (step.type === 'tool_call') {
      out.push({
        kind: 'tool',
        key,
        callId: extractToolCallId(step.payload),
        name: step.name ?? '',
        args: extractToolArgs(step.payload),
        status: 'pending',
        result: null,
      });
      openCalls.set(stepPairKey(step), out.length - 1);
      continue;
    }

    // tool_result
    const pairKey = stepPairKey(step);
    let index = openCalls.get(pairKey);
    if (index === undefined) {
      for (let i = out.length - 1; i >= 0; i -= 1) {
        const candidate = out[i];
        if (
          candidate &&
          candidate.kind === 'tool' &&
          candidate.name === (step.name ?? '') &&
          candidate.status === 'pending'
        ) {
          index = i;
          break;
        }
      }
    }
    const result = extractToolResult(step.payload);
    const ok = step.ok ?? (typeof result.ok === 'boolean' ? result.ok : true);

    if (index === undefined) {
      out.push({
        kind: 'tool',
        key,
        callId: extractToolCallId(step.payload),
        name: step.name ?? '',
        args: {},
        status: ok ? 'ok' : 'failed',
        result,
      });
    } else {
      const target = out[index];
      if (target && target.kind === 'tool') {
        out[index] = { ...target, status: ok ? 'ok' : 'failed', result };
      }
      openCalls.delete(pairKey);
    }
  }

  return out;
}

/* ────────────────────────────── step rendering ───────────────────────────── */

interface StepListProps {
  steps: RenderedStep[];
  /** True while the run is still producing; the final prose block shows a caret. */
  live?: boolean;
}

function StepList({ steps, live = false }: StepListProps): ReactElement {
  const t = useT();
  const locale = useCurrentLocale();
  const lastIndex = steps.length - 1;

  return (
    <div className="sp-run__steps">
      {steps.map((step, index) => {
        if (step.kind === 'thinking') {
          return (
            <ThinkingBlock
              key={step.key}
              text={step.text}
              live={live && index === lastIndex}
            />
          );
        }
        if (step.kind === 'text') {
          return (
            <section className="sp-run__note" key={step.key}>
              <p className="sp-run__note-label">{t('run.text')}</p>
              <p className="sp-run__note-text">{step.text || t('app.none')}</p>
            </section>
          );
        }
        if (step.kind === 'error') {
          return (
            <p className="sp-run__failure" key={step.key}>
              {t('run.error_detail', { detail: step.text })}
            </p>
          );
        }
        if (step.kind === 'provider') {
          const model = typeof step.payload.model === 'string' ? step.payload.model : null;
          const inputTokens =
            typeof step.payload.input_tokens === 'number' ? step.payload.input_tokens : null;
          const outputTokens =
            typeof step.payload.output_tokens === 'number' ? step.payload.output_tokens : null;
          const latency =
            typeof step.payload.latency_ms === 'number' ? step.payload.latency_ms : null;
          return (
            <p className="sp-run__provider" key={step.key}>
              <span>{t('run.provider_request')}</span>
              {model ? <span className="mono">{model}</span> : null}
              {inputTokens !== null && outputTokens !== null ? (
                <span className="mono">
                  {t('run.tokens_value', {
                    input: formatInteger(inputTokens, locale),
                    output: formatInteger(outputTokens, locale),
                  })}
                </span>
              ) : null}
              {latency !== null ? <span className="mono">{formatLatency(latency, locale)}</span> : null}
            </p>
          );
        }
        return (
          <ToolCallCard
            key={step.key}
            name={step.name}
            args={step.args}
            status={step.status}
            result={step.result}
            callId={step.callId}
            index={index + 1}
          />
        );
      })}
    </div>
  );
}

/** The live path renders the same shapes without a round trip to the database. */
function liveSteps(run: LiveRun): RenderedStep[] {
  return run.steps.map((step) =>
    step.kind === 'tool'
      ? {
          kind: 'tool' as const,
          key: step.key,
          callId: step.callId,
          name: step.name,
          args: step.input,
          status: step.status,
          result: step.result,
        }
      : step.kind === 'thinking'
        ? { kind: 'thinking', key: step.key, text: step.text }
        : { kind: 'text', key: step.key, text: step.text },
  );
}

/* ──────────────────────────────── run summary ────────────────────────────── */

const DECISION_KEYS: Record<RunDecision, TKey> = {
  acted: 'run.decision_acted',
  ignored: 'run.decision_ignored',
  deferred: 'run.decision_deferred',
};

const RUN_KIND_KEYS: Record<RunKind, TKey> = {
  signal: 'run.kind_signal',
  sl_watchdog: 'run.kind_sl_watchdog',
  manual: 'run.kind_manual',
};

function statusLabel(status: RunStatus, t: Translate): string {
  switch (status) {
    case 'running':
      return t('run.running');
    case 'incomplete':
      return t('run.incomplete');
    case 'error':
      return t('run.error');
    default:
      return t('run.finished');
  }
}

function statusBadgeClass(status: RunStatus): string {
  switch (status) {
    case 'running':
      return 'badge badge--accent';
    case 'incomplete':
    case 'error':
      return 'badge badge--short';
    default:
      return 'badge badge--ok';
  }
}

function decisionBadgeClass(decision: RunDecision): string {
  switch (decision) {
    case 'acted':
      return 'badge badge--long';
    case 'deferred':
      return 'badge badge--warn';
    default:
      return 'badge badge--dim';
  }
}

export interface RunSummaryProps {
  run: RunOut;
  /**
   * Steps to render when expanded. Supplied for a live run; omitted for a
   * seeded one, which fetches its own on first expand.
   */
  steps?: RenderedStep[];
  /** True while the run is still producing output. */
  live?: boolean;
  /** Controlled expansion; omit to let the summary own it. */
  expanded?: boolean;
  onToggle?: (run: RunOut) => void;
  /** Initial expansion when uncontrolled. */
  defaultExpanded?: boolean;
  /** Offers the "source message" control when the run came from one. */
  onOpenSignal?: (messageRowId: number, run: RunOut) => void;
  className?: string;
  /** Rendered after the steps — the auto-scroll sentinel, for a live run. */
  children?: ReactNode;
}

/**
 * One run: a single line that expands.
 *
 * Collapsed it carries the verdict — status, decision, the first line of the
 * summary and the three numbers worth glancing at. Expanded it carries the whole
 * chain of reasoning.
 */
export function RunSummary(props: RunSummaryProps): ReactElement {
  const {
    run,
    steps,
    live = false,
    expanded: controlledExpanded,
    onToggle,
    defaultExpanded = false,
    onOpenSignal,
    className,
    children,
  } = props;
  const t = useT();
  const locale = useCurrentLocale();

  const [selfExpanded, setSelfExpanded] = useState(defaultExpanded);
  const expanded = controlledExpanded ?? selfExpanded;

  const [fetched, setFetched] = useState<RunStepOut[] | null>(null);
  const [loading, setLoading] = useState(false);
  const [loadError, setLoadError] = useState<string | null>(null);

  const needsFetch = expanded && steps === undefined && fetched === null && !live;

  useEffect(() => {
    if (!needsFetch) return;
    const controller = new AbortController();
    let cancelled = false;
    setLoading(true);
    setLoadError(null);
    getRun(run.id, { signal: controller.signal })
      .then((detail) => {
        if (!cancelled) setFetched(detail.steps);
      })
      .catch((error: unknown) => {
        if (cancelled) return;
        if (isApiError(error) && error.isAborted) return;
        setLoadError(isApiError(error) ? error.message : String(error));
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
      controller.abort();
    };
  }, [needsFetch, run.id]);

  const renderedSteps = useMemo<RenderedStep[] | null>(() => {
    if (steps !== undefined) return steps;
    if (fetched !== null) return pairRunSteps(fetched);
    return null;
  }, [steps, fetched]);

  const toggle = useCallback(() => {
    if (onToggle) onToggle(run);
    else setSelfExpanded((v) => !v);
  }, [onToggle, run]);

  const durationSeconds =
    run.latency_ms > 0
      ? run.latency_ms / 1000
      : run.started_at && run.finished_at
        ? (new Date(run.finished_at).getTime() - new Date(run.started_at).getTime()) / 1000
        : null;

  const incomplete = run.status === 'incomplete';
  const failed = run.status === 'error';
  const summaryLine = (run.summary ?? '').trim();

  const classes = [
    'sp-run',
    `sp-run--${run.status}`,
    live ? 'is-live' : null,
    expanded ? 'is-expanded' : null,
    className,
  ]
    .filter(Boolean)
    .join(' ');

  return (
    <section className={classes} data-run={run.id}>
      <style href="sp-runstream" precedence="sp-component">{RUN_STREAM_CSS}</style>

      <header className="sp-run__head">
        <button
          type="button"
          className="sp-run__toggle"
          aria-expanded={expanded}
          onClick={toggle}
          title={expanded ? t('run.collapse') : t('run.expand')}
        >
          {/*
            Both carets are mounted; only their `data-shown` changes. A swap
            that unmounts one glyph and mounts the other cannot cross-fade, and
            a disclosure that blinks reads as a redraw rather than as a turn.
          */}
          <span className="sp-run__caret iconswap" aria-hidden="true">
            <span className="iconswap__icon" data-shown={expanded ? 'true' : 'false'}>
              ▾
            </span>
            <span className="iconswap__icon" data-shown={expanded ? 'false' : 'true'}>
              ▸
            </span>
          </span>

          <span className={statusBadgeClass(run.status)}>{statusLabel(run.status, t)}</span>

          {run.decision ? (
            <span className={decisionBadgeClass(run.decision)}>{t(DECISION_KEYS[run.decision])}</span>
          ) : null}

          {run.kind !== 'signal' ? (
            // Not badge--dim. This is the single most consequential thing a reader can
            // misread in the whole product: whether the AI decided something or a human
            // (or a script hitting /api/trading/manual/*) did. A dim chip beside a status
            // and a decision badge disappears into them -- an operator reviewing the feed
            // reads a manual test as the model's own judgement, which is exactly what
            // happened. --warn is the "look at this" token and it is not used for errors.
            <span className="badge badge--warn sp-run__origin" title={t('run.origin_hint')}>
              {t(RUN_KIND_KEYS[run.kind])}
            </span>
          ) : null}

          <span className="sp-run__summary">
            {summaryLine || (live ? t('run.waiting') : t('run.summary'))}
          </span>

          <span className="spacer" />

          <span className="sp-run__metrics">
            {run.iterations > 0 ? (
              <span className="mono" title={t('run.iterations')}>
                {formatInteger(run.iterations, locale)}×
              </span>
            ) : null}
            {run.input_tokens + run.output_tokens > 0 ? (
              <span className="mono" title={t('run.tokens')}>
                {formatInteger(run.input_tokens + run.output_tokens, locale)}
              </span>
            ) : null}
            {run.latency_ms > 0 ? (
              <span className="mono" title={t('run.latency')}>
                {formatLatency(run.latency_ms, locale)}
              </span>
            ) : null}
          </span>
        </button>
      </header>

      {incomplete ? <p className="sp-run__alarm">{t('run.incomplete_hint')}</p> : null}
      {failed && run.error ? (
        <p className="sp-run__alarm">{t('run.error_detail', { detail: run.error })}</p>
      ) : null}

      {expanded ? (
        <div className="sp-run__body">
          <dl className="sp-run__meta">
            <MetaRow label={t('run.model')} value={run.model} mono />
            <MetaRow label={t('run.provider')} value={run.provider_kind} />
            <MetaRow label={t('run.iterations')} value={formatInteger(run.iterations, locale)} mono />
            <MetaRow
              label={t('run.tokens')}
              value={t('run.tokens_value', {
                input: formatInteger(run.input_tokens, locale),
                output: formatInteger(run.output_tokens, locale),
              })}
              mono
            />
            <MetaRow label={t('run.latency')} value={formatLatency(run.latency_ms, locale)} mono />
            <MetaRow label={t('run.started_at')} value={formatDateTime(run.started_at, locale)} mono />
            <MetaRow
              label={t('run.duration')}
              value={durationSeconds === null ? null : formatDuration(durationSeconds, locale)}
              mono
            />
            <MetaRow
              label={t('app.status')}
              value={run.armed_at_start ? t('run.armed_at_start') : t('run.disarmed_at_start')}
            />
          </dl>

          {summaryLine ? (
            <p className="sp-run__summary-full">
              <span className="sp-run__meta-key">{t('run.summary')}</span>
              {summaryLine}
            </p>
          ) : null}

          {run.iterations >= MAX_TOOL_ITERATIONS ? (
            <p className="sp-run__alarm sp-run__alarm--warn">{t('run.iteration_cap')}</p>
          ) : null}

          {onOpenSignal && run.message_row_id !== null ? (
            <div className="sp-run__actions">
              <button
                type="button"
                className="btn btn--ghost btn--sm"
                onClick={() => {
                  if (run.message_row_id !== null) onOpenSignal(run.message_row_id, run);
                }}
              >
                {t('run.signal_link')}
              </button>
            </div>
          ) : null}

          {loading ? (
            <div className="sp-run__loading">
              <span className="skeleton skeleton--text" />
              <span className="skeleton skeleton--text" />
              <span className="skeleton skeleton--block" />
            </div>
          ) : null}

          {loadError !== null ? (
            <p className="sp-run__alarm">{t('run.error_detail', { detail: loadError })}</p>
          ) : null}

          {renderedSteps && renderedSteps.length > 0 ? (
            <StepList steps={renderedSteps} live={live} />
          ) : !loading && loadError === null ? (
            <p className="sp-run__quiet">{live ? t('run.waiting') : t('run.no_thinking')}</p>
          ) : null}

          {children}
        </div>
      ) : null}
    </section>
  );
}

interface MetaRowProps {
  label: string;
  value: string | null;
  mono?: boolean;
}

function MetaRow({ label, value, mono = false }: MetaRowProps): ReactElement | null {
  if (value === null || value === '') return null;
  return (
    <div className="sp-run__meta-row">
      <dt className="sp-run__meta-key">{label}</dt>
      <dd className={mono ? 'sp-run__meta-val mono' : 'sp-run__meta-val'}>{value}</dd>
    </div>
  );
}

/* ─────────────────────────────── the column ──────────────────────────────── */

export interface RunStreamProps {
  /**
   * Attach to the page's socket — build one with `createEventFanout()` and pass
   * a STABLE reference. Omitted, the column shares the process-wide connection
   * with every other component that does the same, so there is still exactly one
   * WebSocket (CONTRACTS §19).
   */
  subscribe?: StreamSubscribe;
  /**
   * Pin the column to one message's runs — the id of the selected `SignalOut`,
   * which is the `messages` row id that `RunOut.message_row_id` points at.
   * `null` or omitted follows everything.
   */
  messageRowId?: number | null;
  /** How many finished runs to seed. */
  limit?: number;
  /** Wires the "source message" control on each run. */
  onOpenSignal?: (messageRowId: number, run: RunOut) => void;
  /** Renders without the surrounding `.card` chrome, for embedding. */
  bare?: boolean;
  className?: string;
}

/** Distance in px from the sentinel at which following is considered "at the end". */
const AUTOSCROLL_SLACK_PX = 24;

const DEFAULT_RUN_LIMIT = 25;

export function RunStream(props: RunStreamProps): ReactElement {
  const {
    subscribe,
    messageRowId = null,
    limit = DEFAULT_RUN_LIMIT,
    onOpenSignal,
    bare = false,
    className,
  } = props;
  const t = useT();
  const locale = useCurrentLocale();

  const [state, setState] = useState<RunStreamState>(EMPTY_STATE);
  const [seeded, setSeeded] = useState<RunOut[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [reloadToken, setReloadToken] = useState(0);
  const [expandedIds, setExpandedIds] = useState<ReadonlySet<string>>(() => new Set<string>());

  /* ---- events ---- */

  const onEvent = useCallback((event: StreamEvent) => {
    setState((prev) => applyEvent(prev, event));
    if (event.type === 'run.started') {
      // The new run becomes the focus; the previous one folds back to its line.
      setExpandedIds(new Set([event.data.run_id]));
    }
  }, []);

  const onStatus = useCallback((status: StreamStatus) => {
    // Every (re)connect refetches over REST: the stream is a tail, not a source of truth.
    if (status === 'open') setReloadToken((v) => v + 1);
  }, []);

  useStreamSubscription(subscribe, onEvent, onStatus);

  /* ---- seed ---- */

  useEffect(() => {
    const controller = new AbortController();
    let cancelled = false;
    setLoading(true);

    const request =
      messageRowId === null
        ? getRuns({ limit }, { signal: controller.signal }).then((out) => out.runs)
        : getSignal(messageRowId, { signal: controller.signal }).then((out) => out.runs);

    request
      .then((runs) => {
        if (cancelled) return;
        setSeeded(runs);
        setError(null);
      })
      .catch((cause: unknown) => {
        if (cancelled) return;
        if (isApiError(cause) && cause.isAborted) return;
        setError(isApiError(cause) ? cause.message : String(cause));
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });

    return () => {
      cancelled = true;
      controller.abort();
    };
  }, [messageRowId, limit, reloadToken]);

  /* ---- merge ---- */

  const rows = useMemo(() => {
    const matches = (candidate: number | null): boolean =>
      messageRowId === null || candidate === messageRowId;

    const live = state.runs.filter((run) => matches(run.messageRowId));
    const liveIds = new Set(live.map((run) => run.id));
    const rest = seeded.filter((run) => !liveIds.has(run.id) && matches(run.message_row_id));

    return [
      ...live.map((run) => ({ run: liveToRunOut(run), steps: liveSteps(run), live: run.status === 'running' })),
      ...rest.map((run) => ({ run, steps: undefined, live: false })),
    ];
  }, [state.runs, seeded, messageRowId]);

  const liveRow = rows.find((row) => row.live) ?? null;
  const hasLive = liveRow !== null;

  /* ---- auto-scroll ---- */

  const scrollRef = useRef<HTMLDivElement | null>(null);
  const sentinelRef = useRef<HTMLDivElement | null>(null);
  const [following, setFollowing] = useState(true);

  const measureFollowing = useCallback(() => {
    const container = scrollRef.current;
    const sentinel = sentinelRef.current;
    if (!container) return;
    if (!sentinel) {
      setFollowing(true);
      return;
    }
    const bottomOfView = container.scrollTop + container.clientHeight;
    setFollowing(sentinel.offsetTop <= bottomOfView + AUTOSCROLL_SLACK_PX);
  }, []);

  const scrollToLive = useCallback(() => {
    const container = scrollRef.current;
    const sentinel = sentinelRef.current;
    if (!container || !sentinel) return;
    container.scrollTop = Math.max(0, sentinel.offsetTop - container.clientHeight + AUTOSCROLL_SLACK_PX);
  }, []);

  useLayoutEffect(() => {
    if (!following || !hasLive) return;
    scrollToLive();
  }, [state.version, following, hasLive, scrollToLive, expandedIds]);

  /* ---- expansion ---- */

  const toggleRun = useCallback((run: RunOut) => {
    setExpandedIds((prev) => {
      const next = new Set(prev);
      if (next.has(run.id)) next.delete(run.id);
      else next.add(run.id);
      return next;
    });
  }, []);

  /* ---- render ---- */

  const body = (
    <div
      className="sp-runs__scroll scroll-y scroll-y--grow"
      ref={scrollRef}
      onScroll={measureFollowing}
    >
      {loading && rows.length === 0 ? (
        <div className="sp-runs__loading">
          <span className="skeleton skeleton--text" />
          <span className="skeleton skeleton--block" />
          <span className="skeleton skeleton--text" />
        </div>
      ) : null}

      {error !== null ? (
        <div className="empty">
          <p className="empty__title">{t('app.request_failed')}</p>
          <p className="empty__text">{error}</p>
          <button
            type="button"
            className="btn btn--sm"
            onClick={() => { setReloadToken((v) => v + 1); }}
          >
            {t('app.retry')}
          </button>
        </div>
      ) : null}

      {!loading && error === null && rows.length === 0 ? (
        <div className="empty">
          <p className="empty__text">{t('run.no_runs')}</p>
        </div>
      ) : null}

      <div className="sp-runs__list">
        {rows.map((row) => (
          <RunSummary
            key={row.run.id}
            run={row.run}
            steps={row.steps}
            live={row.live}
            expanded={expandedIds.has(row.run.id)}
            onToggle={toggleRun}
            onOpenSignal={onOpenSignal}
          >
            {row.live ? <div ref={sentinelRef} className="sp-runs__sentinel" /> : null}
          </RunSummary>
        ))}
      </div>
    </div>
  );

  const shell = (
    <>
      <style href="sp-runstream" precedence="sp-component">{RUN_STREAM_CSS}</style>
      {body}
      {hasLive && !following ? (
        <button type="button" className="btn btn--sm sp-runs__resume" onClick={() => {
          setFollowing(true);
          scrollToLive();
        }}>
          ↓ {t('run.live')}
        </button>
      ) : null}
    </>
  );

  if (bare) {
    return <div className={['sp-runs', 'sp-runs--bare', className].filter(Boolean).join(' ')}>{shell}</div>;
  }

  return (
    <section className={['card', 'card--flush', 'card--grow', 'sp-runs', className].filter(Boolean).join(' ')}>
      <header className="card__header">
        <h2 className="card__title">{t('run.title')}</h2>
        <div className="card__actions">
          {hasLive ? <span className="badge badge--accent">{t('run.live')}</span> : null}
          {messageRowId !== null ? (
            <span className="badge badge--dim">
              {t('trade.signal')}
              <span className="badge__num">{formatNumber(messageRowId, { useGrouping: false }, locale)}</span>
            </span>
          ) : null}
          <button
            type="button"
            className="btn btn--ghost btn--sm"
            onClick={() => { setReloadToken((v) => v + 1); }}
            title={t('app.refresh')}
          >
            {t('app.refresh')}
          </button>
        </div>
      </header>
      <div className="card__body sp-runs__body">{shell}</div>
    </section>
  );
}

export default RunStream;

/* ──────────────────────────────── styles ─────────────────────────────────── */

const RUN_STREAM_CSS = `
.sp-runs {
  position: relative;
  min-height: 0;
}

.sp-runs--bare {
  display: flex;
  flex-direction: column;
  flex: 1 1 auto;
  min-height: 0;
}

.sp-runs__body {
  padding: 0;
  gap: 0;
  min-height: 0;
}

/* The gutter is var(--sp-1) so that the run cards inside it can be
   var(--radius-sm): 4px of card + 4px of gutter is the 8px of the .card that
   contains them, and the two arcs sit concentric instead of doubling up. */
.sp-runs__scroll {
  position: relative;
  flex: 1 1 auto;
  min-height: 0;
  padding: 0;
}

.sp-runs__list {
  display: flex;
  flex-direction: column;
  gap: var(--sp-2);
  min-width: 0;
}

.sp-runs__loading {
  display: flex;
  flex-direction: column;
  gap: var(--sp-2);
  padding: var(--sp-2);
}

.sp-runs__sentinel {
  height: 1px;
}

.sp-runs .sp-runs__resume {
  position: absolute;
  right: var(--sp-3);
  bottom: var(--sp-3);
  z-index: 2;
  border-color: var(--accent);
  color: var(--accent);
}

/* ---- one run ---- */

.sp-run {
  display: flex;
  flex-direction: column;
  min-width: 0;
  background: transparent;
  border: 0;
  border-bottom: var(--border-w) solid var(--border);
  border-radius: 0;
  overflow: hidden;
}

.sp-run.is-live {
  border-bottom-color: var(--accent);
  box-shadow: inset 2px 0 0 var(--accent);
}

/* A run that never called finish: its conclusion was never recorded. In red,
   loudly, because that is the entire reason the state exists (BUILD §5.3). */
.sp-run--incomplete,
.sp-run--error {
  border-bottom-color: var(--short);
  box-shadow: inset 2px 0 0 var(--short);
  background: color-mix(in srgb, var(--short) 5%, var(--bg));
}

.sp-run__head {
  display: flex;
  min-width: 0;
}

.sp-run__toggle {
  display: flex;
  align-items: center;
  gap: var(--sp-2);
  width: 100%;
  min-width: 0;
  /* The whole header IS the disclosure control, so it carries the 44px: a
     pseudo-element skirt here would reach into the run above and below. */
  min-height: var(--touch);
  padding: var(--sp-1) var(--sp-2);
  background: transparent;
  border: 0;
  cursor: pointer;
  text-align: start;
}

.sp-run--incomplete .sp-run__toggle,
.sp-run--error .sp-run__toggle {
  background: color-mix(in srgb, var(--short) 12%, var(--surface-2));
}

.sp-run__toggle:hover {
  background: var(--surface-3);
}

/* The box is .iconswap's (components.css); this only says how it is inked. */
.sp-run__caret {
  font-size: var(--fs-1);
  color: var(--text-dim);
}

/* The one line. Everything past the badges is ellipsised, never wrapped. */
.sp-run__summary {
  flex: 1 1 auto;
  min-width: 0;
  font-size: var(--fs-2);
  color: var(--text);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.sp-run--incomplete .sp-run__summary,
.sp-run--error .sp-run__summary {
  color: var(--short);
  font-weight: 500;
}

.sp-run__metrics {
  display: flex;
  align-items: center;
  gap: var(--sp-2);
  flex: none;
  font-size: var(--fs-1);
  color: var(--text-dim);
}

.sp-run__alarm {
  padding: var(--sp-1) var(--sp-2);
  font-size: var(--fs-1);
  line-height: var(--lh);
  color: var(--short);
  background: color-mix(in srgb, var(--short) 8%, transparent);
  border-top: var(--border-w) solid var(--border);
  overflow-wrap: anywhere;
}

.sp-run__alarm--warn {
  color: var(--warn);
  background: color-mix(in srgb, var(--warn) 8%, transparent);
}

.sp-run__body {
  display: flex;
  flex-direction: column;
  gap: var(--sp-3);
  padding: var(--sp-3);
  border-top: var(--border-w) solid var(--border);
}

.sp-run__meta {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(160px, 1fr));
  gap: var(--sp-1) var(--sp-3);
}

.sp-run__meta-row {
  display: flex;
  align-items: baseline;
  gap: var(--sp-2);
  min-width: 0;
}

.sp-run__meta-key {
  flex: none;
  font-size: var(--fs-1);
  letter-spacing: 0.04em;
  text-transform: uppercase;
  color: var(--text-dim);
}

.sp-run__meta-val {
  font-size: var(--fs-2);
  color: var(--text);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.sp-run__summary-full {
  display: flex;
  flex-direction: column;
  gap: var(--sp-1);
  font-size: var(--fs-2);
  line-height: var(--lh);
  color: var(--text);
  overflow-wrap: anywhere;
}

.sp-run__actions {
  display: flex;
  gap: var(--sp-2);
}

.sp-run__loading {
  display: flex;
  flex-direction: column;
  gap: var(--sp-2);
}

.sp-run__quiet {
  font-size: var(--fs-2);
  font-style: italic;
  color: var(--text-dim);
}

.sp-run__steps {
  display: flex;
  flex-direction: column;
  gap: var(--sp-3);
  min-width: 0;
}

/* The model's visible answer: a neutral rule, so it never reads as thinking. */
.sp-run__note {
  display: flex;
  flex-direction: column;
  gap: var(--sp-1);
  padding-left: var(--sp-3);
  border-left: 2px solid var(--border-strong);
}

.sp-run__note-label {
  font-size: var(--fs-1);
  font-weight: 600;
  letter-spacing: 0.06em;
  text-transform: uppercase;
  color: var(--text-dim);
}

.sp-run__note-text {
  font-size: var(--fs-2);
  line-height: var(--lh);
  color: var(--text);
  white-space: pre-wrap;
  overflow-wrap: anywhere;
}

.sp-run__failure {
  padding: var(--sp-2);
  font-size: var(--fs-2);
  line-height: var(--lh);
  color: var(--short);
  background: color-mix(in srgb, var(--short) 8%, transparent);
  border: var(--border-w) solid var(--short);
  border-radius: var(--radius-sm);
  overflow-wrap: anywhere;
}

.sp-run__provider {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: var(--sp-2);
  font-size: var(--fs-1);
  color: var(--text-dim);
}
`;
