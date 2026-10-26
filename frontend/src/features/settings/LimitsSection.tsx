import type { LimitsInput, LimitsOut, TradingWindow } from '@/api/client';
import { putLimits } from '@/api/client';
import { SymbolPicker } from '@/components/SymbolPicker';
import { useT } from '@/i18n';
import type { ReactElement } from 'react';
import { useCallback, useEffect, useId, useState } from 'react';
import {
  errorField,
  errorSentence,
  FIELDSET,
  GRID,
  hasError,
  LEGEND,
  NOTE,
  positiveInt,
  positiveNumber,
  Section,
  SectionCommonProps,
  STATEMENT_BOX,
  SUB_BOX,
  TextField,
  TIME_ZONES,
  toOptionalInt,
  toOptionalNumber,
  useAsyncAction,
  WEEKDAYS,
} from './shared';
interface WindowRow {
  id: string;
  days: number[];
  from: string;
  to: string;
  tz: string;
}

interface LimitsForm {
  maxVolume: string;
  maxPositions: string;
  maxDailyLoss: string;
  /** Broker symbol names, in the order the operator picked them. Empty = all allowed. */
  allowlist: string[];
  windows: WindowRow[];
}

type LimitsErrors = Partial<
  Record<
    'max_volume_per_trade' | 'max_open_positions' | 'max_daily_loss_percent',
    string
  >
>;

let windowRowSeq = 0;

function newWindowRow(window?: TradingWindow): WindowRow {
  windowRowSeq += 1;
  return {
    id: `win-${windowRowSeq}`,
    days: window ? [...window.days] : [0, 1, 2, 3, 4],
    from: window ? window.from : '00:00',
    to: window ? window.to : '23:59',
    tz: window ? window.tz : 'UTC',
  };
}

function limitsForm(limits: LimitsOut): LimitsForm {
  return {
    maxVolume:
      limits.max_volume_per_trade === null
        ? ''
        : String(limits.max_volume_per_trade),
    maxPositions:
      limits.max_open_positions === null
        ? ''
        : String(limits.max_open_positions),
    maxDailyLoss:
      limits.max_daily_loss_percent === null
        ? ''
        : String(limits.max_daily_loss_percent),
    allowlist: cleanAllowlist(limits.symbol_allowlist ?? []),
    windows: (limits.trading_hours ?? []).map((window) => newWindowRow(window)),
  };
}

function cleanAllowlist(list: readonly string[]): string[] {
  const seen = new Set<string>();
  const names: string[] = [];
  for (const entry of list) {
    const name = entry.trim();
    if (!name) continue;
    const key = name.toUpperCase();
    if (seen.has(key)) continue;
    seen.add(key);
    names.push(name);
  }
  return names;
}

export function LimitsSection(props: SectionCommonProps): ReactElement {
  const { settings, onSettings } = props;
  const t = useT();
  const uid = useId();
  const slice = settings.limits;
  const tzListId = `${uid}-tz-list`;

  const [form, setForm] = useState<LimitsForm>(() => limitsForm(slice));
  const [dirty, setDirty] = useState(false);
  const [errors, setErrors] = useState<LimitsErrors>({});
  /** Keyed by window row id, so an incomplete window says so inside its own box. */
  const [windowErrors, setWindowErrors] = useState<Record<string, string>>({});

  const save = useAsyncAction(t);

  useEffect(() => {
    if (!dirty) setForm(limitsForm(slice));
  }, [dirty, slice]);

  const patch = useCallback(
    (next: Partial<LimitsForm>) => {
      setForm((previous) => ({ ...previous, ...next }));
      setDirty(true);
      save.clear();
    },
    [save],
  );

  const setWindow = useCallback(
    (id: string, next: Partial<WindowRow>) => {
      setForm((previous) => ({
        ...previous,
        windows: previous.windows.map((row) =>
          row.id === id ? { ...row, ...next } : row,
        ),
      }));
      setDirty(true);
      save.clear();
      setWindowErrors((previous) => {
        if (!previous[id]) return previous;
        const cleared = { ...previous };
        delete cleared[id];
        return cleared;
      });
    },
    [save],
  );

  const anchor = useCallback(
    (cause: unknown): boolean => {
      const field = errorField(cause);
      if (
        field === 'max_volume_per_trade' ||
        field === 'max_open_positions' ||
        field === 'max_daily_loss_percent'
      ) {
        setErrors({ [field]: errorSentence(cause, t) });
        return true;
      }
      return false;
    },
    [t],
  );

  const onSave = useCallback(() => {
    const next: LimitsErrors = {};
    const volume = positiveNumber(form.maxVolume, t, false);
    if (volume) next.max_volume_per_trade = volume;
    const positions = positiveInt(form.maxPositions, t, false);
    if (positions) next.max_open_positions = positions;
    const dailyLoss = positiveNumber(form.maxDailyLoss, t, false);
    if (dailyLoss) next.max_daily_loss_percent = dailyLoss;
    setErrors(next);

    // A window is all-or-nothing: without days, both ends and a zone there is
    // nothing to evaluate, so the row says what is missing where it stands.
    const windowFaults: Record<string, string> = {};
    for (const row of form.windows) {
      if (row.days.length === 0 || !row.from || !row.to || !row.tz.trim()) {
        windowFaults[row.id] = t('err.required');
      }
    }
    setWindowErrors(windowFaults);
    if (hasError(next) || hasError(windowFaults)) return;

    const payload: LimitsInput = {
      max_volume_per_trade: toOptionalNumber(form.maxVolume),
      max_open_positions: toOptionalInt(form.maxPositions),
      max_daily_loss_percent: toOptionalNumber(form.maxDailyLoss),
      symbol_allowlist: cleanAllowlist(form.allowlist),
      trading_hours: form.windows.map((row) => ({
        days: [...row.days].sort((a, b) => a - b),
        from: row.from,
        to: row.to,
        tz: row.tz.trim(),
      })),
    };

    save.run(async () => {
      const updated = await putLimits(payload);
      setDirty(false);
      onSettings(updated);
    }, anchor);
  }, [anchor, form, onSettings, save, t]);

  return (
    <Section
      id="sp-set-limits"
      title={t('settings.limits_section')}
      dirty={dirty}
      saving={save.busy}
      saved={save.done}
      error={save.error}
      onSave={onSave}
    >
      {/* The whole point of the section, stated before the first input. */}
      <p style={STATEMENT_BOX}>{t('settings.limits_off_note')}</p>

      <div style={GRID}>
        <TextField
          id={`${uid}-max-volume`}
          label={t('settings.max_volume_per_trade')}
          value={form.maxVolume}
          mono
          inputMode="decimal"
          optional
          disabled={save.busy}
          error={errors.max_volume_per_trade ?? null}
          onChange={(value) => patch({ maxVolume: value })}
        />
        <TextField
          id={`${uid}-max-positions`}
          label={t('settings.max_open_positions')}
          value={form.maxPositions}
          mono
          inputMode="numeric"
          optional
          disabled={save.busy}
          error={errors.max_open_positions ?? null}
          onChange={(value) => patch({ maxPositions: value })}
        />
        <TextField
          id={`${uid}-max-daily-loss`}
          label={t('settings.max_daily_loss_percent')}
          value={form.maxDailyLoss}
          mono
          inputMode="decimal"
          optional
          disabled={save.busy}
          error={errors.max_daily_loss_percent ?? null}
          onChange={(value) => patch({ maxDailyLoss: value })}
        />
      </div>

      {/*
       * Picked from the connected account rather than typed from memory: an
       * allowlist full of names this broker does not offer refuses every order
       * while still looking like a limit that works. The picker states what an
       * empty list means, which is the whole hazard of this control (§6).
       */}
      <fieldset style={FIELDSET}>
        <legend style={LEGEND}>
          {t('settings.symbol_allowlist')}
          <span style={NOTE}> · {t('app.optional')}</span>
        </legend>
        <p className="field__hint">{t('settings.symbol_allowlist_hint')}</p>
        <SymbolPicker
          selected={form.allowlist}
          disabled={save.busy}
          showHeader={false}
          onChange={(next) => patch({ allowlist: next })}
        />
      </fieldset>

      <fieldset style={FIELDSET}>
        <legend style={LEGEND}>{t('settings.trading_hours')}</legend>

        {form.windows.length === 0 ? (
          <p style={NOTE}>{t('app.none')}</p>
        ) : (
          form.windows.map((row) => (
            <div style={SUB_BOX} key={row.id}>
              <div className="row row--wrap">
                <span style={LEGEND}>{t('settings.trading_hours_days')}</span>
                <div className="row row--tight row--wrap" role="group">
                  {WEEKDAYS.map((weekday) => {
                    const on = row.days.includes(weekday.day);
                    return (
                      <button
                        key={weekday.day}
                        type="button"
                        className={on ? 'tab is-active' : 'tab'}
                        aria-pressed={on}
                        disabled={save.busy}
                        onClick={() =>
                          setWindow(row.id, {
                            days: on
                              ? row.days.filter((day) => day !== weekday.day)
                              : [...row.days, weekday.day],
                          })
                        }
                      >
                        {t(weekday.key)}
                      </button>
                    );
                  })}
                </div>
              </div>

              <div style={GRID}>
                <TextField
                  id={`${uid}-from-${row.id}`}
                  label={t('settings.trading_hours_from')}
                  value={row.from}
                  type="time"
                  mono
                  disabled={save.busy}
                  onChange={(value) => setWindow(row.id, { from: value })}
                />
                <TextField
                  id={`${uid}-to-${row.id}`}
                  label={t('settings.trading_hours_to')}
                  value={row.to}
                  type="time"
                  mono
                  disabled={save.busy}
                  onChange={(value) => setWindow(row.id, { to: value })}
                />
                <TextField
                  id={`${uid}-tz-${row.id}`}
                  label={t('settings.trading_hours_tz')}
                  value={row.tz}
                  mono
                  disabled={save.busy}
                  list={TIME_ZONES.length > 0 ? tzListId : undefined}
                  onChange={(value) => setWindow(row.id, { tz: value })}
                />
              </div>

              <div className="row row--wrap">
                {windowErrors[row.id] ? (
                  <p className="field__error" role="alert">
                    {windowErrors[row.id]}
                  </p>
                ) : null}
                <span className="spacer" />
                <button
                  type="button"
                  className="btn btn--sm btn--danger"
                  disabled={save.busy}
                  onClick={() => {
                    setForm((previous) => ({
                      ...previous,
                      windows: previous.windows.filter(
                        (item) => item.id !== row.id,
                      ),
                    }));
                    setDirty(true);
                    save.clear();
                    setWindowErrors((previous) => {
                      const cleared = { ...previous };
                      delete cleared[row.id];
                      return cleared;
                    });
                  }}
                >
                  {t('app.remove')}
                </button>
              </div>
            </div>
          ))
        )}

        <p className="field__hint">{t('settings.trading_hours_hint')}</p>

        <div className="row">
          <button
            type="button"
            className="btn btn--sm"
            disabled={save.busy}
            onClick={() => {
              setForm((previous) => ({
                ...previous,
                windows: [...previous.windows, newWindowRow()],
              }));
              setDirty(true);
              save.clear();
            }}
          >
            {t('settings.trading_hours_add')}
          </button>
        </div>

        {TIME_ZONES.length > 0 ? (
          <datalist id={tzListId}>
            {TIME_ZONES.map((zone) => (
              <option key={zone} value={zone} />
            ))}
          </datalist>
        ) : null}
      </fieldset>
    </Section>
  );
}
