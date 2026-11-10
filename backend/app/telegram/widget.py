"""Compact, static Telegram Bot API 10.4 notification widgets.

Notification widgets are static. The menu uses Telegram's sandboxed query API;
unsupported chats use rich messages or a text menu.
"""
from __future__ import annotations

import html
import re
import zlib
from urllib.parse import urlsplit

from app.telegram.format import html_to_plain

_CSS = '''<style>
body{margin:0;font-family:system-ui,sans-serif;color:#ededed;background:#000}
.card{padding:16px 17px 24px;background:#0a0a0a;border:1px solid #242424;border-radius:22px}
.brand{font-size:.66em;font-weight:700;letter-spacing:.1em;color:#a1a1a1;margin-bottom:9px}
.title{font-size:1.05em;font-weight:700;line-height:1.3;margin-bottom:11px;overflow-wrap:anywhere}
.row{display:flex;justify-content:space-between;gap:12px;margin:6px 0;line-height:1.45;font-size:.9em}
.label{color:#a1a1a1;flex-shrink:0}.value{text-align:right;overflow-wrap:anywhere;font-weight:600}
.note{margin:6px 0;font-size:.9em;line-height:1.45;overflow-wrap:anywhere}
a{color:inherit;text-decoration:underline}
@media(prefers-color-scheme:light){body{background:#fafafa;color:#171717}.card{background:#fff;border-color:#e5e5e5}.brand,.label{color:#666}}
</style>'''
_LINK = re.compile(r'^\s*<a href="([^"<>]+)">(.*?)</a>\s*$', re.IGNORECASE)
_ROW = re.compile(r'^\s*<b>(.*?)</b>\s+(.+)$', re.IGNORECASE)


def _text(value: str) -> str:
    return html_to_plain(value).replace('\x00', '\ufffd').strip()


def build_notification_widget(message_html: str) -> str | None:
    """Turn the existing escaped notification into a static, self-contained card.

    Decline long messages: their complete original text will be sent as a rich
    message instead of clipping the widget.
    """
    plain = _text(message_html)
    if not plain or len(plain) > 450 or len(plain.encode('utf-16-le')) // 2 > 980:
        return None
    lines = [line.strip() for line in message_html.splitlines() if _text(line)]
    if len(lines) > 12 or any(len(_text(line)) > 160 for line in lines):
        return None
    esc = lambda value: html.escape(_text(value), quote=True)
    title = esc(lines[0])
    rows = []
    for line in lines[1:]:
        link_match = _LINK.fullmatch(line)
        if link_match:
            url = html.unescape(link_match.group(1))
            try:
                parsed = urlsplit(url)
                valid = (parsed.scheme == 'https' and parsed.hostname and not parsed.username
                         and not parsed.password and len(url) <= 256)
            except ValueError:
                valid = False
            if not valid:
                return None
            rows.append(f'<div class="note"><a href="{html.escape(url, quote=True)}">'
                        f'{esc(link_match.group(2))}</a></div>')
            continue
        # Do not silently lose a link that cannot be represented in the widget.
        if '<a ' in line.lower():
            return None
        match = _ROW.match(line)
        if match:
            rows.append(f'<div class="row"><span class="label">{esc(match.group(1))}</span>'
                        f'<span class="value">{esc(match.group(2))}</span></div>')
        else:
            rows.append(f'<div class="note">{esc(line)}</div>')
    widget = ('<!doctype html><html><head>' + _CSS + '</head><body><div class="card">'
              '<div class="brand">LV COPYTRADER</div><div class="title">' + title + '</div>'
              + ''.join(rows) + '</div></body></html>')
    raw = widget.encode('utf-8')
    if len(raw) > 32768 or len(zlib.compress(raw, 9)) > 4096:
        return None
    return widget


def build_menu_widget() -> str:
    """Single read-only menu with buttons rendered inside the widget."""
    return ('''<!doctype html><html><head>''' + _CSS + '''<style>
.tabs{display:grid;grid-template-columns:1fr 1fr;gap:8px;margin-top:16px}
.tab{border:1px solid #242424;border-radius:999px;background:#141414;color:#ededed;padding:11px 8px;font:600 .85em system-ui,sans-serif;cursor:pointer;transition:transform .16s,background .16s}
.tab.active{background:#ededed;color:#171717;border-color:#ededed}.tab:active{transform:scale(.97)}
.tab:disabled{color:#a1a1a1}.feedback{color:#a1a1a1;font-size:.78em;min-height:1.2em;margin-top:8px}
@media(prefers-color-scheme:light){.tab{background:#f5f5f5;color:#171717;border-color:#e5e5e5}.tab.active{background:#171717;color:#fafafa;border-color:#171717}.tab:disabled,.feedback{color:#666}}
@media(prefers-reduced-motion:reduce){.tab{transition:none}}
</style></head><body><div class="card">
<div class="brand">LV COPYTRADER</div><div class="title" id="title"></div><div id="content"></div>
<div class="tabs"><button class="tab" data-tab="status"></button><button class="tab" data-tab="account"></button>
<button class="tab" data-tab="positions"></button><button class="tab" data-tab="orders"></button>
<button class="tab" data-tab="signals"></button><button class="tab" data-tab="help"></button></div>
<div class="feedback" id="feedback"></div></div><script>
(function(){
 const w=window.TelegramWidget, title=document.getElementById('title'), content=document.getElementById('content');
 const feedback=document.getElementById('feedback'), buttons=Array.from(document.querySelectorAll('.tab'));
 const labels={en:{status:'Status',account:'Account',positions:'Positions',orders:'Orders',signals:'Signals',help:'Help'},ru:{status:'Статус',account:'Счёт',positions:'Позиции',orders:'Ордера',signals:'Сигналы',help:'Помощь'}};
 let lang='en', busy=false;
 function draw(){
  let state={title:'LV Copytrader',lines:[],tab:'status',locale:'en'};
  try { const parsed=JSON.parse(w.chatData || '{}'); if(parsed && typeof parsed==='object') state=parsed; } catch(e) {}
  lang=state.locale==='ru'?'ru':'en';
  title.textContent=typeof state.title==='string'?state.title:'LV Copytrader';
  content.textContent='';
  if(Array.isArray(state.lines)) state.lines.forEach(function(line){
   if(typeof line!=='string') return;
   const node=document.createElement('div');node.className='note';node.textContent=line;content.appendChild(node);
  });
  buttons.forEach(function(button){const tab=button.dataset.tab;button.textContent=labels[lang][tab];button.classList.toggle('active',tab===state.tab);});
 }
 buttons.forEach(function(button){button.addEventListener('click',function(){
  if(!w || busy) return;
  busy=true;buttons.forEach(function(b){b.disabled=true;});feedback.textContent=lang==='ru'?'Загрузка…':'Loading…';
  try {if(w.HapticFeedback) w.HapticFeedback.selectionChanged();} catch(e) {}
  try {
   w.request('lvct:menu:'+button.dataset.tab).then(function(answer){
    let result={ok:false};try {result=JSON.parse(answer.result || '{}');} catch(e) {}
    feedback.textContent=result.ok?'':(lang==='ru'?'Не удалось обновить. Попробуйте ещё раз.':'Could not refresh. Please try again.');
    busy=false;buttons.forEach(function(b){b.disabled=false;});
   },function(){feedback.textContent=lang==='ru'?'Нет связи. Попробуйте ещё раз.':'Connection lost. Please try again.';busy=false;buttons.forEach(function(b){b.disabled=false;});});
  } catch(e){feedback.textContent=lang==='ru'?'Не удалось обновить.':'Could not refresh.';busy=false;buttons.forEach(function(b){b.disabled=false;});}
 });});
 if(w){if(typeof w.setResizeAnchor==='function') w.setResizeAnchor('bottom');w.onEvent('chatDataChanged',draw);draw();w.ready();}
})();
</script></body></html>''')
