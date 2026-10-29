(() => {
  try {
    const saved = localStorage.getItem('sp-theme');
    document.documentElement.dataset.theme = saved === 'dark' || saved === 'light' ? saved : matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light';
    if (location.pathname === '/') {
      const savedLocale = localStorage.getItem('sp-locale');
      const locale = /^(en|ru|uz)$/.test(savedLocale || '') ? savedLocale : (navigator.language || 'en').slice(0,2);
      if (['ru','uz'].includes(locale)) location.replace('/' + locale + '/' + location.search + location.hash);
    }
  } catch { document.documentElement.dataset.theme = 'light'; }
})();
