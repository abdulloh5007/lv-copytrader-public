/* No analytics, dependencies, remote fonts or accounts. */
(() => {
  const config = JSON.parse(document.getElementById('page-config').textContent);
  const {ui, locale} = config;
  const announce = value => { document.getElementById('announce').textContent = value; };
  const copyTimers = new WeakMap();
  function resetCopyState(button) {
    clearTimeout(copyTimers.get(button));
    button.classList.remove('copied', 'copy-failed');
    button.setAttribute('aria-label', ui.copy);button.title = ui.copy;
  }
  document.getElementById('language').addEventListener('change', event => {
    const language = event.target.value;
    try { localStorage.setItem('sp-locale', language); } catch {}
    location.assign('/' + language + '/' + (config.slug ? config.slug + '/' : '') + location.hash);
  });
  document.getElementById('theme').addEventListener('click', () => {
    const theme = document.documentElement.dataset.theme === 'dark' ? 'light' : 'dark';
    document.documentElement.dataset.theme = theme;
    try { localStorage.setItem('sp-theme', theme); } catch {}
  });
  const osButtons = [...document.querySelectorAll('[data-os]')];
  const highlight = document.querySelector('.os-highlight');
  const reducedMotion = matchMedia('(prefers-reduced-motion: reduce)');
  let osChoice = null;
  let highlightAnimation, pressAnimation, panelAnimation;
  function moveHighlight(button, animate) {
    if (!highlight) return;
    const before = getComputedStyle(highlight);
    const from = {transform: before.transform, width: before.width};
    const to = {transform: `translateX(${button.offsetLeft}px)`, width: `${button.offsetWidth}px`};
    highlightAnimation?.cancel();
    highlightAnimation = highlight.animate([from, to], {
      duration: animate && !reducedMotion.matches ? 440 : 0,
      easing: 'cubic-bezier(.22,1.2,.36,1)', fill: 'forwards'
    });
    document.querySelector('.os-tabs').classList.add('has-highlight');
  }
  function selectOS(os, automatic = false) {
    const changed = osChoice !== null && osChoice !== os;
    if (changed) resetCopyState(document.querySelector('[data-copy="install-command"]'));
    osChoice = os;
    osButtons.forEach(button => {
      const active = button.dataset.os === os;
      button.classList.toggle('active', active);
      button.setAttribute('aria-selected', String(active));
      button.tabIndex = active ? 0 : -1;
    });
    document.getElementById('install-command').textContent = config.commands[os];
    document.getElementById('platform-note').hidden = os !== 'macos';
    document.getElementById('install-panel').setAttribute('aria-labelledby', 'os-' + os);
    document.getElementById('shell-label').textContent = os === 'windows' ? 'PowerShell' : 'Terminal';
    document.querySelector('.prompt').textContent = os === 'windows' ? 'PS' : '$';
    const active = osButtons.find(button => button.dataset.os === os);
    moveHighlight(active, changed);
    pressAnimation?.cancel();panelAnimation?.cancel();
    if (changed && !automatic && !reducedMotion.matches) {
      pressAnimation = active.animate([
        {transform: 'scale(1)'}, {transform: 'scale(.96)', offset: .2},
        {transform: 'scale(1.025)', offset: .65}, {transform: 'scale(1)'}
      ], {duration: 360, easing: 'ease-out'});
      panelAnimation = document.querySelector('.terminal-code').animate([
        {opacity: .35, transform: 'translateY(5px)'}, {opacity: 1, transform: 'translateY(0)'}
      ], {duration: 220, easing: 'cubic-bezier(.2,.7,.3,1)'});
    }
  }
  osButtons.forEach((button,index) => {
    button.addEventListener('click', () => selectOS(button.dataset.os));
    button.addEventListener('keydown', event => {
      if (!['ArrowLeft','ArrowRight','Home','End'].includes(event.key)) return;
      event.preventDefault();
      const next = event.key === 'Home' ? 0 : event.key === 'End' ? osButtons.length-1 : (index + (event.key === 'ArrowRight' ? 1 : -1) + osButtons.length) % osButtons.length;
      selectOS(osButtons[next].dataset.os); osButtons[next].focus();
    });
  });
  if (osButtons.length) {
    const ua = navigator.userAgent || '';
    const platform = navigator.userAgentData?.platform || navigator.platform || '';
    const mobile = /Android|iPhone|iPad|iPod/i.test(ua) || (/Mac/i.test(platform) && navigator.maxTouchPoints > 1);
    const os = /Win/i.test(platform + ua) ? 'windows' : /Mac/i.test(platform + ua) ? 'macos' : /Linux/i.test(platform + ua) ? 'linux' : null;
    selectOS(mobile ? 'linux' : os || 'linux', !mobile && Boolean(os));
    document.getElementById('mobile-note').hidden = !mobile;
    new ResizeObserver(() => moveHighlight(osButtons.find(button => button.dataset.os === osChoice), false)).observe(document.querySelector('.os-tabs'));
    reducedMotion.addEventListener('change', () => {
      pressAnimation?.cancel();panelAnimation?.cancel();
      moveHighlight(osButtons.find(button => button.dataset.os === osChoice), false);
    });
  }
  document.querySelectorAll('[data-copy]').forEach(button => {
    button.addEventListener('click', async () => {
      const code = document.getElementById(button.dataset.copy);
      const text = code.textContent;
      resetCopyState(button);button.disabled = true;
      let success = false;
      try {
        if (!navigator.clipboard?.writeText) throw new Error('Clipboard unavailable');
        await navigator.clipboard.writeText(text);
        success = true;
      } catch {
        if (code.textContent === text) {
          const range = document.createRange();range.selectNodeContents(code);
          const selection = getSelection();selection?.removeAllRanges();selection?.addRange(range);
        }
      } finally { button.disabled = false; }
      // A pending copy must never mark a newly selected OS command as copied.
      if (code.textContent !== text) return;
      const label = success ? ui.copied : ui.copyretry;
      button.querySelector('.copy-feedback').textContent = label;
      button.classList.add(success ? 'copied' : 'copy-failed');
      button.setAttribute('aria-label', label);button.title = label;
      announce(success ? ui.copied : ui.copyfail);
      if (success && !reducedMotion.matches) {
        button.animate([{transform:'scale(1)'},{transform:'scale(1.12)',offset:.45},{transform:'scale(1)'}],{duration:260,easing:'ease-out'});
      }
      copyTimers.set(button, setTimeout(() => resetCopyState(button), success ? 2600 : 4500));
    });
  });
  const menu = document.getElementById('menu');
  menu?.addEventListener('click', () => {
    const open = menu.getAttribute('aria-expanded') !== 'true';
    menu.setAttribute('aria-expanded', String(open));document.getElementById('sidebar').classList.toggle('is-open',open);
  });
  const dialog = document.getElementById('search-dialog');
  const input = document.getElementById('search-input');
  const results = document.getElementById('search-results');
  let searchIndex = null, searchPromise = null, searchGeneration = 0, selected = -1;
  async function search() {
    const generation = ++searchGeneration;
    results.replaceChildren();selected = -1;
    const query = input.value.toLocaleLowerCase(locale).trim();
    if (!searchIndex) {
      try {
        searchPromise ||= fetch('/search-' + locale + '.json').then(response => { if (!response.ok) throw new Error();return response.json(); });
        searchIndex = await searchPromise;
      } catch { searchPromise = null;if (generation !== searchGeneration) return;const message=document.createElement('p');message.textContent=ui.noresults;results.append(message);return; }
    }
    if (generation !== searchGeneration) return;
    const words = query.split(/\s+/).filter(Boolean);
    const matches = searchIndex.filter(page => words.every(word => (page.title + ' ' + page.text).toLocaleLowerCase(locale).includes(word)));
    matches.slice(0,8).forEach(page => {
      const link = document.createElement('a');link.href = page.url;link.className='search-result';link.setAttribute('role','listitem');
      const title=document.createElement('strong');title.textContent=page.title;
      const description=document.createElement('span');description.textContent=page.description;
      link.append(title,description);results.append(link);
    });
    if (!matches.length) { const empty=document.createElement('p');empty.textContent=ui.noresults;results.append(empty); }
  }
  function openSearch() { dialog.showModal();input.value='';input.focus();search(); }
  document.querySelector('[data-search]').addEventListener('click',openSearch);
  document.getElementById('close-search').addEventListener('click',() => dialog.close());
  dialog.addEventListener('click',event => { if (event.target === dialog) dialog.close(); });
  input.addEventListener('input',search);
  input.addEventListener('keydown',event => {
    const links = [...results.querySelectorAll('a')];
    if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
      event.preventDefault();selected=(selected+(event.key==='ArrowDown'?1:-1)+links.length)%links.length;
      links.forEach((link,index) => link.classList.toggle('search-selected',index===selected));
      links[selected]?.scrollIntoView({block:'nearest'});
    } else if (event.key === 'Enter' && links.length) { event.preventDefault();links[Math.max(0,selected)].click(); }
  });
  document.addEventListener('keydown',event => {
    if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === 'k') { event.preventDefault();dialog.open ? dialog.close() : openSearch(); }
    if (event.key === 'Escape' && menu) { menu.setAttribute('aria-expanded','false');document.getElementById('sidebar').classList.remove('is-open'); }
  });
  if (!/Mac|iPhone|iPad/.test(navigator.platform || navigator.userAgent)) document.querySelector('[data-shortcut]').textContent = 'Ctrl K';
  if ('IntersectionObserver' in window) {
    const sections = document.querySelectorAll('.article section');
    const observer = new IntersectionObserver(entries => {
      entries.forEach(entry => { if (entry.isIntersecting) document.querySelectorAll('.toc a').forEach(link => link.classList.toggle('active',link.hash === '#' + entry.target.id)); });
    },{rootMargin:'-15% 0px -65% 0px'});
    sections.forEach(section => observer.observe(section));
  }
})();
