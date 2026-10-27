// The standalone story follows the language selected on the main website.
(() => {
  const allowed = ['en', 'ru', 'uz'];
  const navigationStorage = 'lvct.star.internal-navigation.v1';
  let redirecting = false;
  function alignLanguage(internal = false) {
    if (redirecting) return;
    let language;
    try { language = localStorage.getItem('sp-locale'); } catch { return; }
    if (!allowed.includes(language)) return;
    const target = new URL(location.href);
    target.pathname = target.pathname.replace(/\/(en|ru|uz)(?=\/|$)/, `/${language}`);
    target.searchParams.delete('lang');
    if (target.pathname === location.pathname) {
      if (target.href !== location.href) history.replaceState(history.state, '', target.href);
      return;
    }
    // Preserve welcome-effect suppression when an internal link needs a locale redirect.
    try {
      const pending = JSON.parse(sessionStorage.getItem(navigationStorage) || 'null');
      if (internal || (pending?.path === location.pathname + location.search && Date.now() - pending.at < 30_000)) {
        sessionStorage.setItem(navigationStorage, JSON.stringify({path: target.pathname + target.search, at: Date.now()}));
      }
    } catch { /* The locale itself does not depend on session storage. */ }
    redirecting = true;
    document.documentElement.style.visibility = 'hidden';
    location.replace(target.href);
  }
  alignLanguage();
  addEventListener('pageshow', () => alignLanguage(true));
  addEventListener('storage', event => { if (event.key === 'sp-locale') alignLanguage(true); });
})();
