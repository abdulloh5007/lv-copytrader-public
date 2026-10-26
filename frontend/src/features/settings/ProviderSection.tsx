import type {
  Capabilities,
  ModelInfo,
  ProviderKind,
  ProviderSettingsInput,
  ProviderSettingsOut,
} from '@/api/client';
import {
  providerModels,
  providerTest,
  putProviderSettings,
} from '@/api/client';
import type { OverrideName } from '@/components/ModelPicker';
import { ModelPicker } from '@/components/ModelPicker';
import { SecretField } from '@/components/SecretField';
import { useT } from '@/i18n';
import type { ReactElement } from 'react';
import { useCallback, useEffect, useId, useMemo, useState } from 'react';
import {
  ALIGN_UNDER_LABEL,
  CALM_NOTE,
  errorField,
  errorSentence,
  FIELDSET,
  GRID,
  hasError,
  httpUrl,
  LEGEND,
  needsReentry,
  positiveInt,
  requiredText,
  Section,
  SectionCommonProps,
  Segmented,
  SUB_BOX,
  TextField,
  toInt,
  useAsyncAction,
} from './shared';
interface HeaderRow {
  id: string;
  name: string;
  value: string;
}

interface ProviderForm {
  kind: ProviderKind;
  baseUrl: string;
  apiKey: string | null;
  model: string;
  maxTokens: string;
  headers: HeaderRow[];
  visionOverride: boolean | null;
  toolsOverride: boolean | null;
}

type ProviderErrors = Partial<
  Record<
    'base_url' | 'api_key' | 'model' | 'max_tokens' | 'extra_headers',
    string
  >
>;

let headerRowSeq = 0;

function newHeaderRow(name = '', value = ''): HeaderRow {
  headerRowSeq += 1;
  return { id: `hdr-${headerRowSeq}`, name, value };
}

function providerForm(provider: ProviderSettingsOut): ProviderForm {
  return {
    kind: provider.kind === 'anthropic' ? 'anthropic' : 'openai',
    baseUrl: provider.base_url ?? '',
    apiKey: null,
    model: provider.model ?? '',
    maxTokens: String(provider.max_tokens ?? ''),
    headers: Object.entries(provider.extra_headers ?? {}).map(([name, value]) =>
      newHeaderRow(name, value),
    ),
    visionOverride: provider.supports_vision_override,
    toolsOverride: provider.supports_tools_override,
  };
}

function headersMap(rows: readonly HeaderRow[]): Record<string, string> {
  const map: Record<string, string> = {};
  for (const row of rows) {
    const name = row.name.trim();
    if (!name) continue;
    map[name] = row.value.trim();
  }
  return map;
}

export function ProviderSection(props: SectionCommonProps): ReactElement {
  const { settings, onSettings } = props;
  const t = useT();
  const uid = useId();
  const slice = settings.provider;

  const [form, setForm] = useState<ProviderForm>(() => providerForm(slice));
  const [dirty, setDirty] = useState(false);
  const [errors, setErrors] = useState<ProviderErrors>({});
  const [models, setModels] = useState<ModelInfo[]>([]);
  const [capabilities, setCapabilities] = useState<Capabilities | null>(
    slice.capabilities,
  );

  const save = useAsyncAction(t);
  const test = useAsyncAction(t);

  useEffect(() => {
    if (!dirty) {
      setForm(providerForm(slice));
      setCapabilities(slice.capabilities);
    }
  }, [dirty, slice]);

  /**
   * The last successful listing, so the picker is populated on a cold load
   * without calling the provider again. A failure here is not worth a message:
   * the operator can always press Test.
   */
  useEffect(() => {
    let cancelled = false;
    providerModels()
      .then((result) => {
        if (!cancelled) setModels(result.models);
      })
      .catch(() => {
        // No cached listing yet; Test & list models fills it in.
      });
    return () => {
      cancelled = true;
    };
  }, []);

  const patch = useCallback(
    (next: Partial<ProviderForm>) => {
      setForm((previous) => ({ ...previous, ...next }));
      setDirty(true);
      save.clear();
    },
    [save],
  );

  const connection = useMemo(
    () => ({
      kind: form.kind,
      base_url: form.baseUrl.trim(),
      api_key: form.apiKey ?? undefined,
      extra_headers: headersMap(form.headers),
    }),
    [form.apiKey, form.baseUrl, form.headers, form.kind],
  );

  const validateConnection = useCallback((): ProviderErrors => {
    const next: ProviderErrors = {};
    // Empty is legal for both kinds: `normalize_base_url` falls back to the
    // vendor default (api.anthropic.com / api.openai.com), so requiring one here
    // would be a rule the backend does not have.
    const baseUrl = httpUrl(form.baseUrl, t, false);
    if (baseUrl) next.base_url = baseUrl;
    // No api_key check on purpose. A local Ollama, LM Studio or vLLM takes no
    // key at all — §4.4 lists them as supported — and an empty string is the
    // documented way to clear a stored one. A hosted endpoint that does want a
    // key answers 401, which §4.3 renders as "the API key was rejected".
    for (const row of form.headers) {
      if (!row.name.trim() && row.value.trim())
        next.extra_headers = t('err.required');
    }
    return next;
  }, [form.baseUrl, form.headers, t]);

  const anchor = useCallback(
    (cause: unknown): boolean => {
      const field = errorField(cause);
      if (
        field === 'base_url' ||
        field === 'api_key' ||
        field === 'model' ||
        field === 'max_tokens' ||
        field === 'extra_headers'
      ) {
        setErrors({ [field]: errorSentence(cause, t) });
        return true;
      }
      return false;
    },
    [t],
  );

  const onTest = useCallback(() => {
    const next = validateConnection();
    setErrors(next);
    if (hasError(next)) return;

    test.run(async () => {
      const outcome = await providerTest(connection);
      if (!outcome.ok) {
        test.fail(outcome.message || t('settings.test_failed'));
        setModels(outcome.models);
        return;
      }
      setModels(outcome.models);
    }, anchor);
  }, [anchor, connection, t, test, validateConnection]);

  const onSave = useCallback(() => {
    const next = validateConnection();
    const model = requiredText(form.model, t);
    if (model) next.model = model;
    const maxTokens = positiveInt(form.maxTokens, t, true);
    if (maxTokens) next.max_tokens = maxTokens;
    setErrors(next);
    if (hasError(next)) return;

    const payload: ProviderSettingsInput = {
      kind: form.kind,
      base_url: form.baseUrl.trim(),
      api_key: form.apiKey ?? undefined,
      model: form.model.trim(),
      extra_headers: headersMap(form.headers),
      max_tokens: toInt(form.maxTokens),
      supports_vision_override: form.visionOverride,
      supports_tools_override: form.toolsOverride,
    };

    save.run(async () => {
      const updated = await putProviderSettings(payload);
      setDirty(false);
      onSettings(updated);
    }, anchor);
  }, [anchor, form, onSettings, save, t, validateConnection]);

  const onOverrideChange = useCallback(
    (which: OverrideName, value: boolean | null) => {
      patch(
        which === 'vision'
          ? { visionOverride: value }
          : { toolsOverride: value },
      );
    },
    [patch],
  );

  const setHeader = useCallback(
    (id: string, next: Partial<HeaderRow>) => {
      setForm((previous) => ({
        ...previous,
        headers: previous.headers.map((row) =>
          row.id === id ? { ...row, ...next } : row,
        ),
      }));
      setDirty(true);
      save.clear();
    },
    [save],
  );

  const busy = save.busy || test.busy;

  return (
    <Section
      id="sp-set-provider"
      title={t('settings.provider_section')}
      hint={t('settings.provider_hint')}
      dirty={dirty}
      saving={save.busy}
      saved={save.done}
      error={save.error}
      onSave={onSave}
      actions={
        <button
          type="button"
          className="btn btn--sm"
          disabled={busy}
          onClick={onTest}
        >
          {test.busy ? t('settings.testing') : t('settings.list_models')}
        </button>
      }
    >
      <Segmented
        id={`${uid}-kind`}
        label={t('settings.kind')}
        value={form.kind}
        disabled={busy}
        options={[
          { value: 'anthropic', label: t('settings.anthropic') },
          { value: 'openai', label: t('settings.openai_compatible') },
        ]}
        onChange={(value) => patch({ kind: value })}
      />

      <div style={GRID}>
        <TextField
          id={`${uid}-base-url`}
          label={t('settings.base_url')}
          value={form.baseUrl}
          mono
          inputMode="url"
          disabled={busy}
          optional
          hint={t('settings.base_url_hint')}
          error={errors.base_url ?? null}
          onChange={(value) => patch({ baseUrl: value })}
        />
        <SecretField
          id={`${uid}-api-key`}
          label={t('settings.api_key')}
          hasValue={slice.has_api_key}
          value={form.apiKey}
          disabled={busy}
          error={errors.api_key ?? null}
          needsReentry={needsReentry(
            settings.needs_reentry,
            'provider.api_key',
          )}
          onChange={(value) => patch({ apiKey: value })}
        />
        <TextField
          id={`${uid}-max-tokens`}
          label={t('settings.max_tokens')}
          value={form.maxTokens}
          mono
          inputMode="numeric"
          disabled={busy}
          error={errors.max_tokens ?? null}
          onChange={(value) => patch({ maxTokens: value })}
        />
      </div>

      {/* §4.4 — one calm sentence, not an error and not a warning box. */}
      <p style={CALM_NOTE}>{t('settings.subscription_note')}</p>

      {/* ── extra headers: what makes a self-hosted gateway work (§4.2) ──── */}
      <fieldset style={FIELDSET}>
        <legend style={LEGEND}>{t('settings.extra_headers')}</legend>
        {form.headers.map((row) => (
          <div className="row row--wrap row--top" key={row.id}>
            <div style={{ flex: '1 1 200px', minWidth: 0 }}>
              <TextField
                id={`${uid}-hdr-name-${row.id}`}
                label={t('settings.extra_headers_key')}
                value={row.name}
                mono
                disabled={busy}
                onChange={(value) => setHeader(row.id, { name: value })}
              />
            </div>
            <div style={{ flex: '2 1 240px', minWidth: 0 }}>
              <TextField
                id={`${uid}-hdr-value-${row.id}`}
                label={t('settings.extra_headers_value')}
                value={row.value}
                mono
                disabled={busy}
                onChange={(value) => setHeader(row.id, { value })}
              />
            </div>
            <div style={ALIGN_UNDER_LABEL}>
              <button
                type="button"
                className="btn btn--sm"
                disabled={busy}
                onClick={() => {
                  setForm((previous) => ({
                    ...previous,
                    headers: previous.headers.filter(
                      (item) => item.id !== row.id,
                    ),
                  }));
                  setDirty(true);
                  save.clear();
                }}
              >
                {t('settings.remove_header')}
              </button>
            </div>
          </div>
        ))}
        {errors.extra_headers ? (
          <p className="field__error" role="alert">
            {errors.extra_headers}
          </p>
        ) : (
          <p className="field__hint">{t('settings.extra_headers_hint')}</p>
        )}
        <div className="row">
          <button
            type="button"
            className="btn btn--sm"
            disabled={busy}
            onClick={() => {
              setForm((previous) => ({
                ...previous,
                headers: [...previous.headers, newHeaderRow()],
              }));
              setDirty(true);
              save.clear();
            }}
          >
            {t('settings.add_header')}
          </button>
        </div>
      </fieldset>

      {test.error ? (
        <p className="field__error" role="alert">
          {t('settings.test_failed')} — {test.error}
        </p>
      ) : null}

      {/* ── Test → list → select → probe (§4.3) ──────────────────────────── */}
      <div style={SUB_BOX}>
        <ModelPicker
          models={models}
          selected={form.model ? form.model : null}
          loading={test.busy}
          disabled={busy}
          connection={connection}
          capabilities={capabilities}
          visionOverride={form.visionOverride}
          toolsOverride={form.toolsOverride}
          onOverrideChange={onOverrideChange}
          onCapabilities={setCapabilities}
          onSelect={(modelId) => {
            patch({ model: modelId });
            setErrors((previous) => ({ ...previous, model: undefined }));
          }}
        />
        {errors.model ? (
          <p className="field__error" role="alert">
            {t('settings.model')} — {errors.model}
          </p>
        ) : null}
      </div>
    </Section>
  );
}
