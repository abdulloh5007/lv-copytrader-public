import type { SignalOut, StreamEvent, StreamEventType } from './client';

export const EVENT_TYPES: readonly StreamEventType[] = [
  'signal.received',
  'run.started',
  'run.thinking',
  'run.text',
  'run.tool_call',
  'run.tool_result',
  'run.finished',
  'provider.request',
  'order.placed',
  'order.cancelled',
  'position.opened',
  'position.modified',
  'position.closed',
  'account.tick',
  'system.warning',
  'status.changed',
  'settings.changed',
];
const types: ReadonlySet<string> = new Set(EVENT_TYPES);
function record(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}
function signal(value: unknown): value is SignalOut {
  return (
    record(value) &&
    Number.isSafeInteger(value.id) &&
    Number.isSafeInteger(value.message_id) &&
    typeof value.text === 'string' &&
    typeof value.status === 'string' &&
    typeof value.kind === 'string' &&
    Array.isArray(value.images)
  );
}
export function isStreamEvent(value: unknown): value is StreamEvent {
  return (
    record(value) &&
    typeof value.type === 'string' &&
    types.has(value.type) &&
    record(value.data) &&
    (value.type !== 'signal.received' || signal(value.data.signal))
  );
}
/** The ingest bus emits a flat signal; normalize live and replayed frames alike.
 * Accept the older nested envelope too, so clients work during rolling upgrades. */
export function normalizeStreamEvent(value: unknown): StreamEvent | null {
  if (!record(value) || !record(value.data)) return null;
  if (value.type === 'signal.received') {
    const incoming = record(value.data.signal) ? value.data.signal : value.data;
    if (!signal(incoming)) return null;
    return {
      ...value,
      data: {
        signal: { ...incoming, deleted_at: incoming.deleted_at ?? null },
      },
    } as StreamEvent;
  }
  return isStreamEvent(value) ? value : null;
}
