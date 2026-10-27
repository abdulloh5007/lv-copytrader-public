import { putGeneral } from '@/api/client';
import type { Locale } from '@/i18n';
import { useLocale, useT } from '@/i18n';
import type { ReactElement } from 'react';
import { useCallback, useEffect, useId, useState } from 'react';
import {
  applyTheme,
  errorField,
  errorSentence,
  GRID,
  LadderStatsPanel,
  NARROW,
  positiveInt,
  positiveNumber,
  readTheme,
  Section,
  SectionCommonProps,
  Segmented,
  TextField,
  Theme,
  ToggleRow,
  toInt,
  toNumber,
  useAsyncAction,
} from './shared';
export function GeneralSection(props: SectionCommonProps): ReactElement {
  const { settings, onSettings } = props;
  const t = useT();
  const uid = useId();
  const { locale, setLocale } = useLocale();
  const slice = settings.general;

  const [risk, setRisk] = useState<string>(() =>
    String(slice.default_risk_percent ?? ''),
  );
  const [ladderVolume, setLadderVolume] = useState<string>(() =>
    String(slice.ladder_volume ?? ''),
  );
  const [orders, setOrders] = useState<string>(() =>
    String(slice.orders_per_signal ?? 2),
  );
  const [lotSize, setLotSize] = useState<string>(() =>
    String(slice.order_lot_size ?? 0.2),
  );
  const [fixedLots, setFixedLots] = useState<boolean>(
    () => slice.use_fixed_order_lots !== false,
  );
  const [drift, setDrift] = useState<string>(() =>
    String(slice.max_entry_drift_usd ?? 5),
  );
  const [beAfterTp1, setBeAfterTp1] = useState<boolean>(
    () => slice.be_after_tp1 !== false,
  );
  const [reviewerEnabled, setReviewerEnabled] = useState<boolean>(() =>
    Boolean(slice.reviewer_enabled),
  );
  const [tagFilterEnabled, setTagFilterEnabled] = useState<boolean>(() =>
    Boolean(slice.tag_filter_enabled),
  );
  const [tag, setTag] = useState<string>(() => slice.tag_filter_tag ?? '#soft');
  const [systemPrompt, setSystemPrompt] = useState<string>(
    () => slice.system_prompt ?? '',
  );
  const [dirty, setDirty] = useState(false);
  const [riskError, setRiskError] = useState<string | null>(null);
  const [ladderError, setLadderError] = useState<string | null>(null);
  const [ordersError, setOrdersError] = useState<string | null>(null);
  const [lotError, setLotError] = useState<string | null>(null);
  const [driftError, setDriftError] = useState<string | null>(null);
  const [tagError, setTagError] = useState<string | null>(null);
  const [theme, setTheme] = useState<Theme>(() => readTheme());

  const save = useAsyncAction(t);

  useEffect(() => {
    if (!dirty) {
      setRisk(String(slice.default_risk_percent ?? ''));
      setLadderVolume(String(slice.ladder_volume ?? ''));
      setOrders(String(slice.orders_per_signal ?? 2));
      setLotSize(String(slice.order_lot_size ?? 0.2));
      setFixedLots(slice.use_fixed_order_lots !== false);
      setDrift(String(slice.max_entry_drift_usd ?? 5));
      setBeAfterTp1(slice.be_after_tp1 !== false);
      setReviewerEnabled(Boolean(slice.reviewer_enabled));
      setTagFilterEnabled(Boolean(slice.tag_filter_enabled));
      setTag(slice.tag_filter_tag ?? '#soft');
      setSystemPrompt(slice.system_prompt ?? '');
    }
  }, [dirty, slice]);

  const anchor = useCallback(
    (cause: unknown): boolean => {
      const field = errorField(cause);
      if (field === 'default_risk_percent') {
        setRiskError(errorSentence(cause, t));
        return true;
      }
      if (field === 'ladder_volume') {
        setLadderError(errorSentence(cause, t));
        return true;
      }
      if (field === 'orders_per_signal') {
        setOrdersError(errorSentence(cause, t));
        return true;
      }
      if (field === 'order_lot_size') {
        setLotError(errorSentence(cause, t));
        return true;
      }
      if (field === 'max_entry_drift_usd') {
        setDriftError(errorSentence(cause, t));
        return true;
      }
      if (field === 'tag_filter_tag') {
        setTagError(errorSentence(cause, t));
        return true;
      }
      return false;
    },
    [t],
  );

  const onSave = useCallback(() => {
    const riskMessage = positiveNumber(risk, t, true);
    const ladderMessage = positiveNumber(ladderVolume, t, true);
    let ordersMessage = positiveInt(orders, t, true);
    if (!ordersMessage && toInt(orders) > 12)
      ordersMessage = t('err.orders_range');
    const lotMessage = positiveNumber(lotSize, t, true);
    const driftMessage = positiveNumber(drift, t, true);
    const tagMessage =
      tagFilterEnabled && !tag.trim() ? t('settings.tag_required') : null;
    setRiskError(riskMessage);
    setLadderError(ladderMessage);
    setOrdersError(ordersMessage);
    setLotError(lotMessage);
    setDriftError(driftMessage);
    setTagError(tagMessage);
    if (
      riskMessage ||
      ladderMessage ||
      ordersMessage ||
      lotMessage ||
      driftMessage ||
      tagMessage
    )
      return;
    save.run(async () => {
      const updated = await putGeneral({
        default_risk_percent: toNumber(risk),
        ladder_volume: toNumber(ladderVolume),
        locale: locale === 'uz' ? (slice.locale === 'ru' ? 'ru' : 'en') : locale,
        orders_per_signal: toInt(orders),
        order_lot_size: toNumber(lotSize),
        use_fixed_order_lots: fixedLots,
        max_entry_drift_usd: toNumber(drift),
        be_after_tp1: beAfterTp1,
        reviewer_enabled: reviewerEnabled,
        tag_filter_enabled: tagFilterEnabled,
        tag_filter_tag: tag.trim(),
        system_prompt: systemPrompt,
      });
      setDirty(false);
      onSettings(updated);
    }, anchor);
  }, [
    anchor,
    beAfterTp1,
    drift,
    fixedLots,
    ladderVolume,
    locale,
    slice.locale,
    lotSize,
    onSettings,
    orders,
    reviewerEnabled,
    risk,
    save,
    systemPrompt,
    t,
    tag,
    tagFilterEnabled,
  ]);

  /**
   * The locale switch applies immediately — `setLocale` writes `sp-locale`, and
   * stamps `<html lang>`, which is what the pre-paint script reads next load.
   * Saving the section additionally sends it to the backend, which is where the
   * Telegram notification wording comes from.
   */
  const onLocale = useCallback(
    (next: Locale) => {
      setLocale(next);
      setDirty(true);
      save.clear();
    },
    [save, setLocale],
  );

  /** The theme is a browser preference only; there is no server field for it. */
  const onTheme = useCallback((next: Theme) => {
    setTheme(next);
    applyTheme(next);
  }, []);

  /**
   * The shell header carries a theme toggle of its own, and another tab can
   * change the stored preference. Watching `html[data-theme]` — the single thing
   * both of them write — keeps this control showing what is actually on screen,
   * without the page reaching up into its parent for state.
   */
  useEffect(() => {
    if (
      typeof document === 'undefined' ||
      typeof MutationObserver === 'undefined'
    ) {
      return undefined;
    }
    const root = document.documentElement;
    const observer = new MutationObserver(() => {
      const stamped = root.getAttribute('data-theme');
      if (stamped === 'dark' || stamped === 'light') setTheme(stamped);
    });
    observer.observe(root, {
      attributes: true,
      attributeFilter: ['data-theme'],
    });
    return () => observer.disconnect();
  }, []);

  return (
    <Section
      id="sp-set-general"
      title={t('settings.general_section')}
      dirty={dirty}
      saving={save.busy}
      saved={save.done}
      error={save.error}
      onSave={onSave}
    >
      <div style={NARROW}>
        <TextField
          id={`${uid}-orders`}
          label={t('settings.orders_per_signal')}
          value={orders}
          mono
          inputMode="numeric"
          disabled={save.busy}
          hint={t('settings.orders_per_signal_hint')}
          error={ordersError}
          onChange={(value) => {
            setOrders(value);
            setDirty(true);
            setOrdersError(null);
            save.clear();
          }}
        />
        <TextField
          id={`${uid}-lot`}
          label={t('settings.order_lot_size')}
          value={lotSize}
          mono
          inputMode="decimal"
          disabled={save.busy}
          hint={t('settings.order_lot_size_hint')}
          error={lotError}
          onChange={(value) => {
            setLotSize(value);
            setDirty(true);
            setLotError(null);
            save.clear();
          }}
        />
        <TextField
          id={`${uid}-ladder`}
          label={t('settings.ladder_volume')}
          value={ladderVolume}
          mono
          inputMode="decimal"
          disabled={save.busy}
          hint={t('settings.ladder_volume_hint')}
          error={ladderError}
          onChange={(value) => {
            setLadderVolume(value);
            setDirty(true);
            setLadderError(null);
            save.clear();
          }}
        />
        <TextField
          id={`${uid}-risk`}
          label={t('settings.default_risk_percent')}
          value={risk}
          mono
          inputMode="decimal"
          disabled={save.busy}
          hint={t('settings.default_risk_hint')}
          error={riskError}
          onChange={(value) => {
            setRisk(value);
            setDirty(true);
            setRiskError(null);
            save.clear();
          }}
        />
        <TextField
          id={`${uid}-drift`}
          label={t('settings.max_entry_drift_usd')}
          value={drift}
          mono
          inputMode="decimal"
          disabled={save.busy}
          hint={t('settings.max_entry_drift_usd_hint')}
          error={driftError}
          onChange={(value) => {
            setDrift(value);
            setDirty(true);
            setDriftError(null);
            save.clear();
          }}
        />
      </div>

      <ToggleRow
        id={`${uid}-fixed-lots`}
        label={t('settings.use_fixed_order_lots')}
        checked={fixedLots}
        disabled={save.busy}
        hint={t('settings.use_fixed_order_lots_hint')}
        onChange={(checked) => {
          setFixedLots(checked);
          setDirty(true);
          save.clear();
        }}
      />
      <ToggleRow
        id={`${uid}-be`}
        label={t('settings.be_after_tp1')}
        checked={beAfterTp1}
        disabled={save.busy}
        hint={t('settings.be_after_tp1_hint')}
        onChange={(checked) => {
          setBeAfterTp1(checked);
          setDirty(true);
          save.clear();
        }}
      />
      <ToggleRow
        id={`${uid}-reviewer`}
        label={t('settings.reviewer_enabled')}
        checked={reviewerEnabled}
        disabled={save.busy}
        hint={t('settings.reviewer_hint')}
        onChange={(checked) => {
          setReviewerEnabled(checked);
          setDirty(true);
          save.clear();
        }}
      />
      <ToggleRow
        id={`${uid}-tag-filter`}
        label={t('settings.tag_filter_enabled')}
        checked={tagFilterEnabled}
        disabled={save.busy}
        hint={t('settings.tag_filter_hint')}
        onChange={(checked) => {
          setTagFilterEnabled(checked);
          setDirty(true);
          if (!checked) setTagError(null);
          save.clear();
        }}
      />
      <div style={NARROW}>
        <TextField
          id={`${uid}-tag`}
          label={t('settings.tag_filter_tag')}
          value={tag}
          mono
          disabled={save.busy}
          hint={t('settings.tag_filter_tag_hint')}
          error={tagError}
          onChange={(value) => {
            setTag(value);
            setDirty(true);
            setTagError(null);
            save.clear();
          }}
        />
      </div>

      <div className={tagError ? 'field field--invalid' : 'field'}>
        <label className="field__label" htmlFor={`${uid}-prompt`}>
          {t('settings.system_prompt')}
        </label>
        <textarea
          id={`${uid}-prompt`}
          className="field__control"
          value={systemPrompt}
          disabled={save.busy}
          spellCheck={false}
          style={{ minHeight: '12rem' }}
          onChange={(event) => {
            setSystemPrompt(event.target.value);
            setDirty(true);
            save.clear();
          }}
        />
        <p className="field__hint">{t('settings.system_prompt_hint')}</p>
        <div className="row">
          <button
            type="button"
            className="btn btn--sm"
            disabled={save.busy}
            onClick={() => {
              setSystemPrompt(slice.system_prompt_default ?? '');
              setDirty(true);
              save.clear();
            }}
          >
            {t('settings.system_prompt_reset')}
          </button>
        </div>
      </div>

      <LadderStatsPanel stats={slice.ladder_stats} />

      <div style={GRID}>
        <Segmented
          id={`${uid}-locale`}
          label={t('settings.locale')}
          value={locale}
          disabled={save.busy}
          hint={t('settings.locale_hint')}
          options={[
            { value: 'en', label: t('settings.english') },
            { value: 'ru', label: t('settings.russian') },
            { value: 'uz', label: 'O‘zbekcha' },
          ]}
          onChange={onLocale}
        />
        <Segmented
          id={`${uid}-theme`}
          label={t('settings.theme')}
          value={theme}
          options={[
            { value: 'dark', label: t('settings.dark') },
            { value: 'light', label: t('settings.light') },
          ]}
          onChange={onTheme}
        />
      </div>
    </Section>
  );
}
