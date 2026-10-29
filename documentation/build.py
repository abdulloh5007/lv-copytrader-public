#!/usr/bin/env python3
"""Build fast, framework-free multilingual documentation. No runtime secrets are read."""
from __future__ import annotations
import argparse
import hashlib
from html import escape as E
import json
from pathlib import Path
import shutil
from content import LOCALES,UI,PAGES

ROOT=Path(__file__).resolve().parent
REPO=ROOT.parent
COMMANDS={
 'linux':'curl -fsSL https://docs.lvenc.site/install.sh | sh -s -- --install-python',
 'macos':'curl -fsSL https://docs.lvenc.site/install.sh | sh -s -- --install-python',
 'windows':'curl.exe -fsSL https://docs.lvenc.site/install.ps1 | Out-String | iex',
}
ICONS={
 'search':'<circle cx="10.8" cy="10.8" r="6.8"/><path d="m16 16 4.5 4.5"/>',
 'arrow':'<path d="M4 12h15m-6-6 6 6-6 6"/>',
 'external':'<path d="M14 4h6v6m0-6-10 10M10 4H5a1 1 0 0 0-1 1v14a1 1 0 0 0 1 1h14a1 1 0 0 0 1-1v-5"/>',
 'copy':'<rect x="8" y="8" width="12" height="12" rx="3"/><path d="M15 8V5a2 2 0 0 0-2-2H5a2 2 0 0 0-2 2v8a2 2 0 0 0 2 2h3"/>',
 'check':'<path d="m5 12 4 4L19 6"/>',
 'sun':'<circle cx="12" cy="12" r="4"/><path d="M12 2v2m0 16v2M2 12h2m16 0h2M5 5l1.5 1.5m11 11L19 19M5 19l1.5-1.5m11-11L19 5"/>',
 'menu':'<path d="M4 6h16M4 12h16M4 18h16"/>',
 'close':'<path d="m6 6 12 12M6 18 18 6"/>',
 'book':'<path d="M12 5c-4-3-9-2-9-2v16s5-1 9 2c4-3 9-2 9-2V3s-5-1-9 2Zm0 0v16"/>',
}
def icon(name,css=''):return '<svg class="'+css+'" width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.65" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">'+ICONS[name]+'</svg>'

def copy_button(target,t,css='icon-button'):
    return f'<button class="{css}" data-copy="{target}" aria-label="{t("copy")}" title="{t("copy")}">{icon("copy","copy-symbol")}{icon("check","copy-check")}{icon("close","copy-error")}<span class="copy-feedback" aria-hidden="true">{t("copied")}</span></button>'

def make_assets(destination):
    result={}
    for p in (ROOT/'assets').rglob('*'):
        if p.is_file():
            target=destination/'assets'/p.relative_to(ROOT/'assets');target.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(p,target)
    for name in ('site.css','app.js','start.js'):
        data=(ROOT/name).read_bytes();file=name.rsplit('.',1);hashed=file[0]+'-'+hashlib.sha256(data).hexdigest()[:12]+'.'+file[1]
        (destination/'assets'/hashed).write_bytes(data);result[name]='/assets/'+hashed
    brand=REPO/'frontend/public/assets/brand/mark.svg'
    shutil.copy2(brand,destination/'assets/mark.svg')
    return result

def brand():
    svg=(REPO/'frontend/public/assets/brand/mark.svg').read_text().replace('role="img" aria-label="LV Copytrader"','aria-hidden="true"').replace('<svg ','<svg width="47" height="25" ')
    return svg+'<span>LV <b>Copytrader</b></span>'

def render(locale,assets,version,page=None):
    t=lambda key:UI[key][locale]
    slug=page['slug'] if page else ''
    path=f'/{locale}/'+(slug+'/' if slug else '')
    title=(page['title'][locale]+' · ' if page else '')+'LV Copytrader '+t('documentation')
    description=page['description'][locale] if page else t('intro')
    langs=''.join(f'<option value="{x}" {"selected" if x==locale else ""}>{label}</option>' for x,label in [('en','EN'),('ru','RU'),('uz','UZ')])
    config=json.dumps({'locale':locale,'slug':slug,'ui':{key:value[locale] for key,value in UI.items()},'commands':COMMANDS},ensure_ascii=False).replace('<','\\u003c')
    alternate=''.join(f'<link rel="alternate" hreflang="{x}" href="https://docs.lvenc.site/{x}/{slug+"/" if slug else ""}">' for x in LOCALES)
    nav=f'''<a class="skip" href="#main">{t('skip')}</a><header class="header"><div class="header-inner">
    <a class="brand" href="/{locale}/" aria-label="LV Copytrader">{brand()}</a><span class="brand-divider"></span><a class="docs-label" href="/{locale}/getting-started/">{t('documentation')}</a>
    <div class="header-actions"><button class="search-trigger" data-search>{icon('search')}<span class="search-label">{t('search')}</span><kbd data-shortcut>⌘ K</kbd></button>
    <select id="language" aria-label="{t('language')}">{langs}</select><button class="icon-button" id="theme" aria-label="{t('theme')}">{icon('sun')}</button>
    <a class="header-github" href="https://github.com/abdulloh5007/lv-copytrader-public" target="_blank" rel="noopener noreferrer" aria-label="GitHub"><svg width="20" height="20" viewBox="0 0 24 24" fill="currentColor" aria-hidden="true"><path d="M12 .8a11.2 11.2 0 0 0-3.54 21.83c.56.1.76-.24.76-.54v-2.08c-3.12.68-3.78-1.33-3.78-1.33-.51-1.29-1.25-1.63-1.25-1.63-1.02-.7.08-.68.08-.68 1.13.08 1.72 1.16 1.72 1.16 1 1.72 2.62 1.22 3.26.94.1-.73.39-1.22.71-1.5-2.5-.28-5.13-1.25-5.13-5.54 0-1.22.44-2.22 1.15-3-.12-.28-.5-1.42.11-2.95 0 0 .94-.3 3.08 1.15a10.7 10.7 0 0 1 5.6 0c2.14-1.45 3.08-1.15 3.08-1.15.61 1.53.23 2.67.11 2.95.72.78 1.15 1.78 1.15 3 0 4.3-2.64 5.26-5.15 5.54.4.35.76 1.03.76 2.08v3.14c0 .3.2.65.77.54A11.2 11.2 0 0 0 12 .8Z"/></svg></a></div></div></header>'''
    if not page:
        tabs=''.join(f'<button id="os-{os}" class="os-tab {"active" if os=="linux" else ""}" data-os="{os}" role="tab" tabindex="{0 if os=="linux" else -1}" aria-selected="{"true" if os=="linux" else "false"}" aria-controls="install-panel" {"aria-describedby=platform-note" if os=="macos" else ""}><img src="/assets/os/{os}.svg" width="20" height="20" alt=""><span>{label}</span>{"<span class=beta-badge>"+t("beta")+"</span>" if os=="macos" else ""}</button>' for os,label in [('windows','Windows'),('macos','macOS'),('linux','Linux')])
        body=f'''<main id="main" class="hero"><div class="hero-content"><h1>{t('install')}</h1>
        <div class="installer"><div class="os-tabs" role="tablist" aria-label="{t('system')}"><span class="os-highlight" aria-hidden="true"></span>{tabs}</div>
        <div class="terminal" id="install-panel" role="tabpanel" aria-labelledby="os-linux"><div class="terminal-top"><span id="shell-label">Terminal</span><span class="preview-badge">{t('previewlabel')}</span></div>
        <div class="terminal-code"><span class="prompt" aria-hidden="true">$</span><pre tabindex="0"><code id="install-command">{E(COMMANDS['linux'])}</code></pre>{copy_button('install-command',t,'copy-command')}</div></div>
        <div class="installer-notes"><p class="platform-note" id="platform-note" hidden>{t('macosbeta')}</p><p class="platform-note" id="mobile-note" hidden>{t('mobile')}</p></div></div>
        <div class="hero-links"><a class="primary" href="/{locale}/getting-started/">{t('guide')}{icon('arrow')}</a><a class="text-link" href="https://lvenc.site/demo">{t('demo')}{icon('external')}</a></div></div></main>'''
    else:
        links=''
        for group in ('getting','operate','reference'):
            links+=f'<div class="sidebar-group"><span>{t(group)}</span>'
            for p in PAGES:
                if p['group']==group:links+=f'<a class="{"selected" if p["slug"]==slug else ""}" href="/{locale}/{p["slug"]}/" {"aria-current=page" if p["slug"]==slug else ""}>{p["title"][locale]}</a>'
            links+='</div>'
        toc=''.join(f'<a href="#{s["id"]}">{s["title"][locale]}</a>' for s in page['sections'])
        sections=''
        for section in page['sections']:
            sections+=f'<section id="{section["id"]}"><h2><a href="#{section["id"]}">{section["title"][locale]}</a></h2><p>{E(section["body"][locale])}</p>'
            if section['code']:
                ident='code-'+section['id']
                sections+=f'<div class="code-block"><div class="code-top"><span>Terminal</span>{copy_button(ident,t)}</div><pre tabindex="0"><code id="{ident}">{E(section["code"])}</code></pre></div>'
            sections+='</section>'
        index=PAGES.index(page)
        prev=PAGES[index-1] if index else None;next_page=PAGES[index+1] if index+1<len(PAGES) else None
        pages=''
        for label,item in [('previous',prev),('next',next_page)]:
            if item:pages+=f'<a class="page-nav-{label}" href="/{locale}/{item["slug"]}/"><small>{t(label)}</small>{item["title"][locale]} {icon("arrow")}</a>'
        body=f'''<div class="mobile-docs"><button id="menu" aria-expanded="false" aria-controls="sidebar">{icon('menu')}{t('documentation')}</button><span>{page['title'][locale]}</span></div>
        <div class="docs-grid"><aside class="sidebar" id="sidebar"><a class="sidebar-install" href="/{locale}/">{icon('arrow')}{t('install')}</a>{links}<a class="sidebar-bottom" href="https://github.com/abdulloh5007/lv-copytrader-public" target="_blank" rel="noopener noreferrer">{t('github')}{icon('external')}</a></aside>
        <main id="main" class="article"><div class="article-kicker">{t('reading')} <span>/</span> LVCT {E(version)}</div><h1>{page['title'][locale]}</h1><p class="article-description">{page['description'][locale]}</p><div class="article-rule"></div>{sections}<nav class="page-navigation" aria-label="Pagination">{pages}</nav></main><aside class="toc"><span>{t('onpage')}</span>{toc}</aside></div>'''
    return f'''<!doctype html><html lang="{locale}"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><meta name="color-scheme" content="light dark"><title>{E(title)}</title><meta name="description" content="{E(description,quote=True)}"><link rel="canonical" href="https://docs.lvenc.site{path}">{alternate}<meta property="og:title" content="{E(title,quote=True)}"><meta property="og:description" content="{E(description,quote=True)}"><meta property="og:image" content="https://docs.lvenc.site/assets/social.png"><meta property="og:url" content="https://docs.lvenc.site{path}"><meta name="twitter:card" content="summary_large_image"><link rel="icon" href="/assets/mark.svg" type="image/svg+xml"><script src="{assets['start.js']}"></script><link rel="stylesheet" href="{assets['site.css']}"><script src="{assets['app.js']}" defer></script></head>
    <body class="{'home-page' if not page else 'docs-page'}">{nav}{body}<dialog id="search-dialog" aria-label="{t('search')}"><div class="search-field">{icon('search')}<input type="search" id="search-input" aria-label="{t('search')}" placeholder="{t('search')}" autocomplete="off"><button class="icon-button" id="close-search" aria-label="{t('close')}">{icon('close')}</button></div><div id="search-results" role="list"></div><div class="search-footer">LV Copytrader <span>↑ ↓ &nbsp; Enter &nbsp; Esc</span></div></dialog><p id="announce" role="status" class="sr-only"></p><script type="application/json" id="page-config">{config}</script></body></html>'''


def build(destination):
    destination.mkdir(parents=True,exist_ok=True)
    assets=make_assets(destination)
    # Existing approved brand artwork, no external image requests.
    if not (destination/'assets/social.png').exists():
        shutil.copy2(REPO/'frontend/public/assets/brand/icon.png',destination/'assets/social.png')
    version=(REPO/'VERSION').read_text().strip()
    for locale in LOCALES:
        for page in [None,*PAGES]:
            folder=destination/locale/(page['slug'] if page else '')
            folder.mkdir(parents=True,exist_ok=True)
            (folder/'index.html').write_text(render(locale,assets,version,page),encoding='utf-8')
        search=[{'title':p['title'][locale],'description':p['description'][locale],'url':f'/{locale}/{p["slug"]}/','text':' '.join(s['title'][locale]+' '+s['body'][locale]+' '+(s['code'] or '') for s in p['sections'])} for p in PAGES]
        (destination/f'search-{locale}.json').write_text(json.dumps(search,ensure_ascii=False),encoding='utf-8')
    (destination/'index.html').write_text(render('en',assets,version),encoding='utf-8')
    (destination/'404.html').write_text('<!doctype html><html lang="en"><meta name="viewport" content="width=device-width"><title>Page not found · LVCT</title><link rel="stylesheet" href="'+assets['site.css']+'"><main class="article error-page"><h1>404</h1><p>Page not found · Страница не найдена · Sahifa topilmadi</p><a href="/en/">English →</a> &nbsp; <a href="/ru/">Русский →</a> &nbsp; <a href="/uz/">O‘zbekcha →</a></main></html>',encoding='utf-8')
    urls=['https://docs.lvenc.site/'+lang+'/'+(p['slug']+'/' if p else '') for lang in LOCALES for p in [None,*PAGES]]
    (destination/'sitemap.xml').write_text('<?xml version="1.0" encoding="UTF-8"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'+''.join('<url><loc>'+url+'</loc></url>' for url in urls)+'</urlset>')
    (destination/'robots.txt').write_text('User-agent: *\nAllow: /\nSitemap: https://docs.lvenc.site/sitemap.xml\n')
    print(f'Documentation built: {len(LOCALES)} languages, {len(PAGES)} guides and an installation hero.')

if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--output',type=Path,default=ROOT/'dist');args=parser.parse_args();build(args.output)
