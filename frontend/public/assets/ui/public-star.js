// Shared by the React public pages and the standalone product story.
(() => {
  const repo = 'https://github.com/abdulloh5007/lv-copytrader-public';
  const icon = '<svg viewBox="0 0 24 24" width="20" height="20" aria-hidden="true" fill="currentColor"><path d="M12 .8a11.2 11.2 0 0 0-3.54 21.83c.56.1.77-.24.77-.54v-2.1c-3.13.68-3.79-1.33-3.79-1.33-.51-1.3-1.25-1.65-1.25-1.65-1.02-.7.08-.69.08-.69 1.13.08 1.72 1.16 1.72 1.16 1 1.71 2.62 1.22 3.26.93.1-.73.39-1.22.71-1.5-2.5-.28-5.13-1.25-5.13-5.54 0-1.22.44-2.22 1.15-3-.12-.28-.5-1.42.11-2.96 0 0 .94-.3 3.08 1.15a10.7 10.7 0 0 1 5.6 0c2.14-1.45 3.08-1.15 3.08-1.15.61 1.54.23 2.68.11 2.96.71.78 1.15 1.78 1.15 3 0 4.3-2.63 5.25-5.14 5.53.4.35.76 1.03.76 2.08v3.11c0 .3.2.65.77.54A11.2 11.2 0 0 0 12 .8Z"/></svg>';
  const captions = {
    en: ['Give it a star', 'Give it a star on GitHub'],
    ru: ['Поставить звезду', 'Поставить звезду проекту на GitHub'],
    uz: ['Yulduzcha qo‘ying', 'GitHub’da loyihaga yulduzcha qo‘ying'],
  };
  function localize(link) {
    const language = document.documentElement.lang.split('-')[0];
    const [text, label] = captions[language] || captions.en;
    const caption = link.querySelector('span');
    if (caption && caption.textContent !== text) caption.textContent = text;
    link.setAttribute('aria-label', label);
  }
  const reduced = matchMedia('(prefers-reduced-motion: reduce)');
  const navigationStorage = 'lvct.star.internal-navigation.v1';
  const navigation = performance.getEntriesByType('navigation')[0]?.type;
  let pendingNavigation;
  try {
    pendingNavigation = JSON.parse(sessionStorage.getItem(navigationStorage) || 'null');
    sessionStorage.removeItem(navigationStorage);
  } catch { /* The effect still works when session storage is unavailable. */ }
  const currentPath = location.pathname + location.search;
  const internalArrival = pendingNavigation?.path === currentPath &&
    Date.now() - pendingNavigation.at < 30_000;
  let welcomeConsumed = navigation !== 'reload' && (internalArrival || navigation === 'back_forward');
  let previous = window.scrollY, distance = 0, direction = 0, down = false, frame = 0;
  let welcomeTimer = 0, cleanupTimer = 0, hapticUntil = 0;
  let burst;
  const editing = () => document.activeElement?.matches('input:not([type="checkbox"]), textarea, select, [contenteditable="true"]');

  function haptic(pattern) {
    if (reduced.matches || document.hidden || typeof navigator.vibrate !== 'function' ||
        !navigator.userActivation?.hasBeenActive) return false;
    try { return navigator.vibrate(pattern); } catch { return false; }
  }

  function clearWelcome() {
    clearTimeout(welcomeTimer);
    clearTimeout(cleanupTimer);
    hapticUntil = 0;
    burst?.remove();
    burst = undefined;
    document.querySelector('.public-star--welcome')?.classList.remove('public-star--welcome');
  }

  function welcome(link) {
    if (welcomeConsumed || document.hidden || reduced.matches || editing() || down || !link.isConnected) return;
    welcomeConsumed = true;
    link.classList.add('public-star--welcome');
    const rect = link.getBoundingClientRect();
    burst = document.createElement('div');
    burst.className = 'public-star-burst';
    burst.setAttribute('aria-hidden', 'true');
    burst.style.left = `${rect.left + rect.width / 2}px`;
    burst.style.top = `${rect.top + rect.height / 2}px`;
    for (let i = 0; i < 18; i++) {
      const star = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
      star.setAttribute('viewBox', '0 0 24 24');
      star.classList.add('public-star-spark');
      star.innerHTML = '<path d="m12 2 3.09 6.26L22 9.27l-5 4.87 1.18 6.88L12 17.77l-6.18 3.25L7 14.14 2 9.27l6.91-1.01Z"/>';
      star.style.color = ['#d79b24', '#e8b94f', '#f5d889'][i % 3];
      const side = (i % 2 ? 1 : -1);
      const x = side * (16 + Math.random() * 32);
      const height = 42 + Math.random() * 34;
      const spin = side * (90 + Math.random() * 180);
      const size = .55 + Math.random() * .55;
      burst.append(star);
      star.animate([
        { transform: 'translate(-50%, -50%) scale(.2) rotate(0deg)', opacity: 0, offset: 0 },
        { transform: `translate(calc(-50% + ${x * .3}px), -26px) scale(${size}) rotate(${spin * .2}deg)`, opacity: 1, offset: .14 },
        { transform: `translate(calc(-50% + ${x}px), -${height}px) scale(${size}) rotate(${spin * .55}deg)`, opacity: 1, offset: .45, easing: 'cubic-bezier(.55, 0, 1, .7)' },
        { transform: `translate(calc(-50% + ${x * 1.25}px), 70px) scale(${size * .7}) rotate(${spin}deg)`, opacity: 0, offset: 1 },
      ], { duration: 1050 + Math.random() * 200, delay: Math.floor(i / 6) * 650 + (i % 6) * 24, easing: 'cubic-bezier(.15, .6, .35, 1)', fill: 'both' });
    }
    document.body.append(burst);
    hapticUntil = performance.now() + 2500;
    if (haptic([12, 35, 16])) hapticUntil = 0;
    cleanupTimer = setTimeout(clearWelcome, 2800);
  }

  function paint() {
    frame = 0;
    const y = Math.max(0, Math.min(window.scrollY, document.documentElement.scrollHeight - innerHeight));
    const delta = y - previous;
    if (delta) {
      const nextDirection = Math.sign(delta);
      if (direction !== nextDirection) distance = 0;
      direction = nextDirection;
      distance += Math.abs(delta);
      if (distance >= 14) down = direction > 0;
    }
    if (y <= 8) down = false;
    previous = y;
    document.querySelectorAll('[data-public-star]').forEach(host => {
      if (!host.firstElementChild) {
        const link = document.createElement('a');
        link.className = 'public-star'; link.href = repo;
        link.target = '_blank'; link.rel = 'noopener noreferrer';
        link.setAttribute('aria-label', 'Give it a star on GitHub');
        link.innerHTML = icon + '<span>Give it a star</span>';
        localize(link);
        host.append(link);
        if (!welcomeConsumed) {
          clearTimeout(welcomeTimer);
          welcomeTimer = setTimeout(() => welcome(link), 420);
        }
      }
      const hidden = String(down || !!editing());
      if (host.firstElementChild.dataset.hidden !== hidden) host.firstElementChild.dataset.hidden = hidden;
    });
    if (down || editing()) clearWelcome();
  }
  function queue() { if (!frame) frame = requestAnimationFrame(paint); }
  addEventListener('scroll', queue, { passive: true });
  addEventListener('pageshow', queue);
  addEventListener('focusin', queue);
  addEventListener('focusout', queue);
  addEventListener('pagehide', clearWelcome);
  document.addEventListener('visibilitychange', () => {
    if (document.hidden) clearWelcome();
    else {
      queue();
      if (!welcomeConsumed) {
        const link = document.querySelector('.public-star');
        if (link) welcomeTimer = setTimeout(() => welcome(link), 420);
      }
    }
  });
  reduced.addEventListener('change', () => { if (reduced.matches) clearWelcome(); });
  addEventListener('pointerup', event => {
    if (performance.now() < hapticUntil) {
      hapticUntil = 0;
      haptic([12, 35, 16]);
    } else if (event.target.closest?.('.public-star')) haptic(10);
  }, { passive: true });
  addEventListener('click', event => {
    const link = event.target.closest?.('a[href]');
    if (!link) return;
    if (link.matches('.public-star') && event.detail === 0) haptic(10);
    if (event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey ||
        link.target || link.hasAttribute('download')) return;
    const target = new URL(link.href, location.href);
    const path = target.pathname + target.search;
    if (target.origin === location.origin && path !== location.pathname + location.search) {
      welcomeConsumed = true;
      clearWelcome();
      try { sessionStorage.setItem(navigationStorage, JSON.stringify({ path, at: Date.now() })); } catch { /* Optional storage. */ }
    }
  }, { capture: true });
  // Observe route mounts, not every price/text update in the live demo or story.
  new MutationObserver(changes => {
    if (changes.some(change => [...change.addedNodes].some(node => node.nodeType === 1 &&
      (node.matches('[data-public-star]') || node.querySelector('[data-public-star]'))))) queue();
  }).observe(document.body, { childList: true, subtree: true });
  new MutationObserver(() => document.querySelectorAll('.public-star').forEach(localize))
    .observe(document.documentElement, { attributes: true, attributeFilter: ['lang'] });
  queue();
})();
