/*
 * Copyright 2026 Abdulloh (github.com/abdulloh5007)
 * Licensed under the Apache License, Version 2.0. See LICENSE and NOTICE at the repository
 * root. This software places real orders on a real brokerage account; NOTICE states the
 * trading risk the licence itself does not cover.
 */
/**
 * LV Copytrader — main.tsx: the entry point, and nothing else.
 *
 * Stylesheet order is load-bearing and is the reason these three imports are the
 * first statements in the file:
 *
 *   1. tokens.css      — defines every colour, size and duration as a custom
 *                        property, in both `[data-theme]` blocks. Nothing else in
 *                        the app contains a colour literal.
 *   2. base.css        — the reset, the document surface, the faces, the focus
 *                        ring and the scrollbars. Reads tokens, defines none.
 *   3. components.css  — the reusable primitives (.card, .btn, .badge, .table,
 *                        .field, .dot, .mono). Reads tokens, overrides base.
 *
 * Swap any two and a primitive loses to the reset, or a token is read before it
 * exists.
 *
 * THEME AND LOCALE ARE NOT SET HERE. The inline script in index.html already
 * stamped `data-theme` and `lang` on <html> before this bundle was even fetched
 * (BUILD_PROMPT §10). Doing it again in an effect is precisely the flash — white
 * before dark, English before Russian — that the pre-paint script exists to
 * prevent. `I18nProvider` READS the stamped `lang`; it does not choose it.
 *
 * StrictMode is on. In development it mounts, unmounts and remounts every
 * component, which is a real test rather than an annoyance: the health poll in
 * App.tsx and the WebSocket in api/stream.ts both abort and close in their
 * cleanups, so the second mount leaves exactly one timer and one socket behind.
 */
import './styles/tokens.css';
import './styles/base.css';
import './styles/components.css';
import './styles/product.css';
import './styles/public.css';

import { StrictMode } from 'react';
import { createRoot } from 'react-dom/client';
import { BrowserRouter } from 'react-router-dom';

import App from './App';
import { I18nProvider } from './i18n';

const container = document.getElementById('root');

if (!container) {
  // index.html ships <div id="root">; if it is gone the build is broken, and
  // failing loudly here beats a blank page with a silent console.
  throw new Error(
    'LV Copytrader: #root is missing from index.html — nothing to mount into.',
  );
}

createRoot(container).render(
  <StrictMode>
    <I18nProvider>
      <BrowserRouter>
        <App />
      </BrowserRouter>
    </I18nProvider>
  </StrictMode>,
);
