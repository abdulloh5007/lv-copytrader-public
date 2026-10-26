/**
 * LV Copytrader — ModelPicker
 * ---------------------------------------------------------------------------
 * The second half of the §4.3 provider flow: the operator has pressed "Test &
 * list models", the endpoint answered, and now they pick one.
 *
 * What this component is careful about, all of it from BUILD_PROMPT §4.3:
 *
 *  • ONLY `data[i].id` IS GUARANTEED. OpenRouter drops `object` and `owned_by`
 *    and adds fields of its own, so every column except the id is optional and
 *    simply does not render when the gateway did not say. A `null` capability
 *    means "not stated", which is different from `false`.
 *
 *  • WHERE RICHER METADATA EXISTS, PREFER IT TO A PROBE. `supports_vision` /
 *    `supports_tools` read off the listing are facts; the probe is what settles
 *    the ones that said nothing.
 *
 *  • THE PROBE IS OVERRIDABLE. A self-hosted endpoint defeats any heuristic, so
 *    each capability carries an auto / on / off switch and the override wins.
 *
 *  • NEVER INFER CAPABILITY FROM THE MODEL NAME. Nothing here parses the id.
 */
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import type { CSSProperties, ReactElement } from 'react';

import { isApiError, providerProbe } from '@/api/client';
import type { Capabilities, ModelInfo, ProviderProbeInput } from '@/api/client';
import {
  EMPTY_VALUE,
  formatDateTime,
  formatInteger,
  useCurrentLocale,
  useT,
} from '@/i18n';
import type { TranslationKey, Translate } from '@/i18n';

/* ─────────────────────────────── props ──────────────────────────────────── */

/** Everything `POST /api/provider/probe` needs except the model itself. */
export type ProviderConnection = Omit<ProviderProbeInput, 'model' | 'force'>;

/** Which capability an override switch controls. */
export type OverrideName = 'vision' | 'tools';

export interface ModelPickerProps {
  /** The listing returned by `POST /api/provider/test`. */
  models: ModelInfo[];
  /** The model id currently stored in settings, or null before a first pick. */
  selected: string | null;
  /** Fired with the newly chosen model id. */
  onSelect: (modelId: string) => void;
  /** Connection details forwarded to the probe. */
  connection: ProviderConnection;
  /** Capabilities already known for `selected` (from `GET /api/settings`). */
  capabilities?: Capabilities | null;
  /** Fired whenever a probe settles, so the parent can persist the verdict. */
  onCapabilities?: (capabilities: Capabilities) => void;
  /** `settings.supports_vision_override` — null is "auto". */
  visionOverride: boolean | null;
  /** `settings.supports_tools_override` — null is "auto". */
  toolsOverride: boolean | null;
  /** Fired when the operator moves one of the auto / on / off switches. */
  onOverrideChange: (which: OverrideName, value: boolean | null) => void;
  /**
   * When the stored capabilities were settled. The backend caches the verdict
   * for `PROBE_CACHE_HOURS`; pass the stored timestamp and this shows it,
   * otherwise the time of the probe this component last ran is used.
   */
  probedAt?: string | number | Date | null;
  /** True while the model list itself is being fetched. */
  loading?: boolean;
  /** Greys out selection and probing. */
  disabled?: boolean;
  /** Override the probe call, for a parent that wants to own the request. */
  probe?: (input: ProviderProbeInput) => Promise<Capabilities>;
  /** Set false to require an explicit press of the probe button. */
  autoProbe?: boolean;
}

/* ────────────────────────────── local styles ────────────────────────────── */

const LIST_BOX: CSSProperties = {
  maxHeight: '320px',
  border: 'var(--border-w) solid var(--border)',
  borderRadius: 'var(--radius)',
  background: 'var(--surface)',
};
const OPTION: CSSProperties = {
  display: 'flex',
  alignItems: 'center',
  gap: 'var(--sp-2)',
  width: '100%',
  // A row in a picker is a control: 40px, even when its one line of text is
  // shorter than that. Rows are flush neighbours, so the height is the target
  // — there is no gap for a pseudo-element to grow into.
  minHeight: '40px',
  padding: 'var(--sp-2)',
  textAlign: 'left',
  background: 'transparent',
  border: 0,
  borderBottom: 'var(--border-w) solid var(--border)',
  cursor: 'pointer',
  color: 'var(--text)',
};
const OPTION_ON: CSSProperties = { ...OPTION, background: 'var(--accent-dim)' };
const OPTION_ID: CSSProperties = { flex: '1 1 auto', minWidth: 0, overflow: 'hidden' };
const OPTION_NAME: CSSProperties = {
  display: 'block',
  color: 'var(--text-dim)',
  fontSize: 'var(--fs-1)',
  overflow: 'hidden',
  textOverflow: 'ellipsis',
  whiteSpace: 'nowrap',
};
const CAPS_BOX: CSSProperties = {
  display: 'flex',
  flexDirection: 'column',
  gap: 'var(--sp-2)',
  padding: 'var(--sp-3)',
  background: 'var(--surface-2)',
  border: 'var(--border-w) solid var(--border)',
  borderRadius: 'var(--radius)',
};
const CAPS_LABEL: CSSProperties = {
  color: 'var(--text-dim)',
  fontSize: 'var(--fs-1)',
  fontWeight: 600,
  letterSpacing: '0.04em',
  textTransform: 'uppercase',
};
const NOTE: CSSProperties = { color: 'var(--text-dim)', fontSize: 'var(--fs-1)' };
const WARN_NOTE: CSSProperties = { color: 'var(--warn)', fontSize: 'var(--fs-1)' };
const BAD_NOTE: CSSProperties = { color: 'var(--danger)', fontSize: 'var(--fs-1)' };
const OK_NOTE: CSSProperties = { color: 'var(--ok)', fontSize: 'var(--fs-1)' };

/* ───────────────────────────── small helpers ────────────────────────────── */

/** The three positions of an override switch, in the order they render. */
const OVERRIDE_OPTIONS: { value: boolean | null; key: TranslationKey }[] = [
  { value: null, key: 'settings.override_auto' },
  { value: true, key: 'settings.override_on' },
  { value: false, key: 'settings.override_off' },
];

/** Turn any thrown value into the sentence the panel will show. */
function errorText(cause: unknown, t: Translate): string {
  if (isApiError(cause)) return cause.message;
  if (cause instanceof Error && cause.message) return cause.message;
  return t('err.generic');
}

/** Case-insensitive match on the id and, when the gateway sent one, the name. */
function matches(model: ModelInfo, needle: string): boolean {
  if (!needle) return true;
  const lowered = needle.toLowerCase();
  return (
    model.id.toLowerCase().includes(lowered) ||
    (model.display_name ?? '').toLowerCase().includes(lowered)
  );
}

/* ──────────────────────────── presentational bits ───────────────────────── */

/**
 * One capability chip. `null` means the provider did not say, and an unstated
 * capability renders nothing at all rather than guessing `false`.
 */
function CapabilityChip({
  supported,
  label,
  t,
}: {
  supported: boolean | null;
  label: string;
  t: Translate;
}): ReactElement | null {
  if (supported === null) return null;
  return (
    <span
      className={supported ? 'badge badge--ok' : 'badge badge--dim'}
      title={`${label} — ${supported ? t('app.yes') : t('app.no')}`}
    >
      <span aria-hidden="true">{supported ? '✓' : '✕'}</span>
      {label}
    </span>
  );
}

/** auto / on / off, as three radios so the keyboard reaches every position. */
function OverrideControl({
  label,
  value,
  disabled,
  onChange,
  t,
}: {
  label: string;
  value: boolean | null;
  disabled: boolean;
  onChange: (value: boolean | null) => void;
  t: Translate;
}): ReactElement {
  return (
    <div className="row row--tight" role="radiogroup" aria-label={label}>
      {OVERRIDE_OPTIONS.map((option) => {
        const on = option.value === value;
        return (
          <button
            key={String(option.value)}
            type="button"
            role="radio"
            aria-checked={on}
            className={on ? 'tab is-active' : 'tab'}
            disabled={disabled}
            onClick={() => onChange(option.value)}
          >
            {t(option.key)}
          </button>
        );
      })}
    </div>
  );
}

/** One capability row: what it is, what it resolved to, and the override. */
function CapabilityRow({
  label,
  effective,
  reported,
  override,
  which,
  disabled,
  onOverrideChange,
  t,
}: {
  label: string;
  effective: boolean;
  reported: boolean | null;
  override: boolean | null;
  which: OverrideName;
  disabled: boolean;
  onOverrideChange: (which: OverrideName, value: boolean | null) => void;
  t: Translate;
}): ReactElement {
  return (
    <div className="row row--wrap">
      <span style={CAPS_LABEL}>{label}</span>
      <span
        className={effective ? 'badge badge--ok' : 'badge badge--dim'}
        title={`${label} — ${effective ? t('app.yes') : t('app.no')}`}
      >
        {/*
          This verdict flips under the operator's hand — a probe answers, an
          override is overruled — so both marks stay mounted and cross-fade.
          The chip beside a model in the LIST is a fixed fact about that model
          and is rendered as one glyph, because it never changes.
        */}
        <span className="iconswap" aria-hidden="true">
          <span className="iconswap__icon" data-shown={effective ? 'true' : 'false'}>
            ✓
          </span>
          <span className="iconswap__icon" data-shown={effective ? 'false' : 'true'}>
            ✕
          </span>
        </span>
        {effective ? t('app.on') : t('app.off')}
      </span>
      {override !== null && reported !== null && override !== reported ? (
        /* The operator has overruled what the endpoint reported; say so. */
        <span className="badge badge--warn" title={t('settings.override_hint')}>
          {override ? t('settings.override_on') : t('settings.override_off')}
        </span>
      ) : null}
      <span className="spacer" />
      <OverrideControl
        label={label}
        value={override}
        disabled={disabled}
        onChange={(next) => onOverrideChange(which, next)}
        t={t}
      />
    </div>
  );
}

/* ──────────────────────────────── component ─────────────────────────────── */

export function ModelPicker(props: ModelPickerProps): ReactElement {
  const {
    models,
    selected,
    onSelect,
    connection,
    capabilities = null,
    onCapabilities,
    visionOverride,
    toolsOverride,
    onOverrideChange,
    probedAt = null,
    loading = false,
    disabled = false,
    probe,
    autoProbe = true,
  } = props;

  const t = useT();
  const locale = useCurrentLocale();

  const [search, setSearch] = useState('');
  const [probing, setProbing] = useState(false);
  const [probeResult, setProbeResult] = useState<Capabilities | null>(null);
  const [probeError, setProbeError] = useState<string | null>(null);
  const [probeTime, setProbeTime] = useState<number | null>(null);

  const probeCall = probe ?? providerProbe;
  // Kept in a ref so `runProbe` does not change identity every render and
  // retrigger the effect that calls it.
  const connectionRef = useRef(connection);
  connectionRef.current = connection;
  const probeCallRef = useRef(probeCall);
  probeCallRef.current = probeCall;
  const onCapabilitiesRef = useRef(onCapabilities);
  onCapabilitiesRef.current = onCapabilities;
  const tRef = useRef(t);
  tRef.current = t;

  const filtered = useMemo(
    () => models.filter((model) => matches(model, search.trim())),
    [models, search],
  );

  const selectedModel = useMemo(
    () => models.find((model) => model.id === selected) ?? null,
    [models, selected],
  );

  const runProbe = useCallback(
    (modelId: string, force: boolean) => {
      setProbing(true);
      setProbeError(null);
      probeCallRef
        .current({ ...connectionRef.current, model: modelId, force })
        .then((result) => {
          setProbeResult(result);
          setProbeTime(Date.now());
          onCapabilitiesRef.current?.(result);
        })
        .catch((cause: unknown) => {
          setProbeResult(null);
          setProbeError(errorText(cause, tRef.current));
        })
        .finally(() => setProbing(false));
    },
    [],
  );

  // A fresh selection invalidates the previous verdict, then settles a new one.
  useEffect(() => {
    setProbeResult(null);
    setProbeError(null);
    setProbeTime(null);
    if (!selected || disabled || !autoProbe) return;
    runProbe(selected, false);
  }, [autoProbe, disabled, runProbe, selected]);

  /* The verdict in force: a live probe first, then whatever settings stored. */
  const settled: Capabilities | null = probeResult ?? capabilities;

  /* What the listing itself claimed, which beats a probe when it is present. */
  const declaredVision = selectedModel?.supports_vision ?? null;
  const declaredTools = selectedModel?.supports_tools ?? null;

  const reportedVision = settled ? settled.supports_vision : declaredVision;
  const reportedTools = settled ? settled.supports_tools : declaredTools;

  const effectiveVision = visionOverride ?? reportedVision ?? false;
  const effectiveTools = toolsOverride ?? reportedTools ?? false;

  const shownProbeTime =
    probeTime !== null ? probeTime : probedAt !== null && probedAt !== undefined ? probedAt : null;

  return (
    <div className="stack">
      {/* ── search + count ───────────────────────────────────────────────── */}
      <div className="row row--wrap row--top">
        <div className="field col--grow">
          <label className="field__label" htmlFor="sp-model-search">
            {t('settings.search_models')}
          </label>
          <input
            id="sp-model-search"
            type="search"
            autoComplete="off"
            value={search}
            disabled={disabled || loading}
            placeholder={t('app.search')}
            onChange={(event) => setSearch(event.target.value)}
          />
        </div>
        {/* Re-counts on every keystroke in the search box: tabular figures so
            the word beside it does not step left and right as it does. */}
        <span className="badge badge--dim tnum">
          {t('settings.models_count', { count: filtered.length })}
        </span>
      </div>

      {/* ── the list ─────────────────────────────────────────────────────── */}
      {loading && models.length === 0 ? (
        <p style={NOTE}>{t('app.loading')}</p>
      ) : filtered.length === 0 ? (
        <div className="empty">
          <p className="empty__title">{t('settings.models_empty')}</p>
        </div>
      ) : (
        <div className="scroll-y" style={LIST_BOX} role="radiogroup" aria-label={t('settings.model')}>
          {filtered.map((model) => {
            const on = model.id === selected;
            return (
              <button
                key={model.id}
                type="button"
                role="radio"
                aria-checked={on}
                style={on ? OPTION_ON : OPTION}
                disabled={disabled}
                onClick={() => onSelect(model.id)}
              >
                <span style={OPTION_ID}>
                  <span className="mono">{model.id}</span>
                  {model.display_name && model.display_name !== model.id ? (
                    <span style={OPTION_NAME}>{model.display_name}</span>
                  ) : null}
                </span>
                {model.context_length !== null ? (
                  <span className="badge badge--dim" title={t('run.tokens')}>
                    <span className="badge__num">{formatInteger(model.context_length, locale)}</span>
                    {t('run.tokens')}
                  </span>
                ) : null}
                <CapabilityChip
                  supported={model.supports_vision}
                  label={t('settings.supports_vision')}
                  t={t}
                />
                <CapabilityChip
                  supported={model.supports_tools}
                  label={t('settings.supports_tools')}
                  t={t}
                />
              </button>
            );
          })}
        </div>
      )}

      {/* ── capabilities of the selected model ───────────────────────────── */}
      {selected ? (
        <div style={CAPS_BOX}>
          <div className="row row--wrap">
            <strong>{t('settings.capabilities')}</strong>
            <span className="mono mono--sm">{selected}</span>
            <span className="spacer" />
            <button
              type="button"
              className="btn btn--sm"
              disabled={disabled || probing}
              onClick={() => runProbe(selected, true)}
            >
              {probing ? t('settings.probing') : t('settings.probe')}
            </button>
          </div>

          <p style={NOTE}>{t('settings.model_selected', { model: selected })}</p>

          <CapabilityRow
            label={t('settings.supports_vision')}
            effective={effectiveVision}
            reported={reportedVision}
            override={visionOverride}
            which="vision"
            disabled={disabled || probing}
            onOverrideChange={onOverrideChange}
            t={t}
          />
          <CapabilityRow
            label={t('settings.supports_tools')}
            effective={effectiveTools}
            reported={reportedTools}
            override={toolsOverride}
            which="tools"
            disabled={disabled || probing}
            onOverrideChange={onOverrideChange}
            t={t}
          />

          <div className="row row--wrap">
            <span style={CAPS_LABEL}>{t('settings.thinking_form')}</span>
            <span className="mono mono--sm">{settled?.thinking_form ?? t('app.none')}</span>
            <span className="spacer" />
            <span style={NOTE}>
              {settled?.source === 'probed'
                ? t('settings.capability_probed')
                : t('settings.capability_declared')}
            </span>
          </div>

          <div className="row row--wrap">
            <span style={CAPS_LABEL}>{t('status.last_check')}</span>
            <span className="mono mono--sm">
              {shownProbeTime === null ? EMPTY_VALUE : formatDateTime(shownProbeTime, locale)}
            </span>
          </div>

          <p style={NOTE}>{t('settings.override_hint')}</p>

          {probing ? <p style={NOTE}>{t('settings.probing')}</p> : null}
          {!probing && probeError ? (
            <p style={BAD_NOTE} role="alert">
              {t('settings.probe_failed')} — {probeError}
            </p>
          ) : null}
          {!probing && !probeError && probeResult ? (
            <p style={OK_NOTE} role="status">
              {t('settings.probe_ok')}
            </p>
          ) : null}

          {!effectiveVision ? (
            <p style={WARN_NOTE}>{t('settings.vision_required_note')}</p>
          ) : null}
        </div>
      ) : null}
    </div>
  );
}

export default ModelPicker;
