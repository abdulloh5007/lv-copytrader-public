import {createFilmMarket} from './market-engine.js?v=story-speed-4x';
import {createStoryAudio} from './story-audio.js?v=1';
import {advanceStoryTime} from './story-speed.js?v=1';
const {t:tr}=window.LV_I18N;
window.LV_I18N.apply(document);
const $ = selector => document.querySelector(selector);
const $$ = selector => [...document.querySelectorAll(selector)];
const clamp = (n,a=0,b=1) => Math.max(a,Math.min(b,n));
const smooth = n => {n=clamp(n);return n*n*(3-2*n);};
const lerp = (a,b,t) => a+(b-a)*t;
const reduced = matchMedia('(prefers-reduced-motion: reduce)');
const scenes = $$('.scene');
const chapters = $$('.chapter');
const chapterNames = [tr('Работает без остановки.'),tr('Рынок не подождал.'),'LV Copy Trader',tr('Ваш выбор. Его скорость.'),tr('Два режима. Один результат.'),tr('Не только Buy и Sell'),tr('Один сигнал. Два сценария.'),tr('Ваш вечер — свободен.'),tr('Вы выбираете канал. Бот открывает сделки.')];
const storyAudio=createStoryAudio();
const speedBadge=document.createElement('span');speedBadge.className='story-speed';speedBadge.textContent='4×';speedBadge.setAttribute('aria-hidden','true');document.body.append(speedBadge);
let metrics=[],current=-1,local=0,globalProgress=0,queued=false,replayFrame=0,replayOverride=null;
const sceneDurations=[7,92,6,8,13,7,92,9,5];
let sceneTime=0,ambientTime=0,previous=-1,transitionTime=1,lastFrame=performance.now(),lastPaint=0;
let marketState={price:4133.48,entry:4133.48,sl:4121.08,tp:4158.28,reaction:4148.19,elapsed:0,focus:0};
let marketScene=-1,marketSceneTime=0;
const signalText=[];const signalWalker=document.createTreeWalker($('#stage'),NodeFilter.SHOW_TEXT);let textNode;
while(textNode=signalWalker.nextNode())if(/4133\.48|4121\.08|4158\.28/.test(textNode.nodeValue))signalText.push({node:textNode,template:textNode.nodeValue});
let boundSignal=null;
function syncSignal(){const key=marketState.trade?.id||marketState.entry;if(boundSignal===key)return;boundSignal=key;for(const {node,template} of signalText)if(node.isConnected)node.nodeValue=template.replace(/4133\.48/g,marketState.entry.toFixed(2)).replace(/4121\.08/g,marketState.sl.toFixed(2)).replace(/4158\.28/g,marketState.tp.toFixed(2));const side=marketState.dir>0?'BUY':'SELL';for(const item of signalText)if(item.node.isConnected)item.node.nodeValue=item.node.nodeValue.replace(/(?:BUY|SELL)(?= XAUUSD)/g,side);for(const el of document.querySelectorAll('.message-side b,.parser-feed .trade-side'))el.textContent=side;}
let cubeHalf=200;
function delayMotion(u){
 const t=u*sceneDurations[1],still=reduced.matches,focus=marketState.focus;
 const reaction=smooth((t-23.4)/.7),elapsed=marketState.elapsed;
 const label=String(Math.floor(elapsed/60)).padStart(2,'0')+':'+String(elapsed%60).padStart(2,'0');
 $('#elapsed').textContent=label;$('#focus-elapsed').textContent=label;$('#delay-price').textContent=marketState.price.toFixed(2);const values=marketState.trade?window.LVMarketSim.amounts(marketState.trade,marketState.price):{missed:0,remaining:0};$('#price-distance').textContent=window.LVMarketSim.formatMoney(values.missed);$('#trade-remaining').textContent=window.LVMarketSim.formatMoney(values.remaining);
 scenes[1].style.setProperty('--focus',focus);scenes[1].style.setProperty('--reaction',reaction);scenes[1].style.setProperty('--scrub',u);scenes[1].dataset.phase=t<3.2?'signal':t<3.4?'zoom':t<4?'entry':t<23.4?'market':t<25.2?'reaction':'overview';
 $('.delay-layout').style.opacity=1-focus;$('.delay-layout').style.transform=still?'none':`translate3d(0,${-focus*105}px,${-focus*240}px) scale(${1-focus*.26})`;
 $('.delay-question').style.opacity=(1-focus)*smooth((t-1.6)/.5);
}

// Keep the full title available to assistive technology while animating its visible characters.
const finishTitle=$('#finish-title'),finishChars=[];
finishTitle.setAttribute('aria-label',finishTitle.textContent.replace(/\s+/g,' ').trim());
const finishWalker=document.createTreeWalker(finishTitle,NodeFilter.SHOW_TEXT),finishNodes=[];
while(finishWalker.nextNode())finishNodes.push(finishWalker.currentNode);
for(const node of finishNodes){const fragment=document.createDocumentFragment();for(const char of Array.from(node.nodeValue)){const span=document.createElement('span');span.setAttribute('aria-hidden','true');span.textContent=char;finishChars.push(span);fragment.append(span);}node.replaceWith(fragment);}
document.body.classList.add('finish-animated');
function finishMotion(time){
 const still=reduced.matches,t=still?Infinity:time,move=still?1:smooth((t-.65)/.8);
 const brand=$('.finish-brand'),route=$('.finish-route'),video=$('.finish-video'),icons=$('.finish-integrations');
 brand.style.opacity=still?1:smooth(t/.35);brand.style.transform=`translateY(${(1-move)*Math.min(innerHeight*.24,160)}px)`;
 finishChars.forEach((char,i)=>{char.style.opacity=still||t>=1.55+i*.035?'1':'0';});
 const end=1.55+finishChars.length*.035;
 const reveal=(element,start)=>{const value=still?1:smooth((t-start)/.45);element.style.opacity=value;element.style.transform=`translateY(${(1-value)*10}px)`;element.inert=value<.95;};
 reveal(route,end+.1);reveal(video,end+.65);reveal(icons,end+1.15);
}

const motionGroups = scenes.map(scene=>[...scene.querySelectorAll('.motion')]);
function measure(){metrics=chapters.map(el=>({top:el.offsetTop,height:el.offsetHeight}));window.CardRelief.fitFaces($('.parser-cards'),$$('.parser-card'));const rect=$('.parser-cards').getBoundingClientRect();cubeHalf=rect.width/2;$('.parser-cube').style.setProperty('--cube-size',rect.width+'px');$('.parser-cube').style.setProperty('--cube-height',rect.height+'px');lastPaint=0;update();}
function setMotion(index,u){
 const still=reduced.matches;
 motionGroups[index].forEach((el,j)=>{
  const p=still||index===0?1:smooth((u+.16-j*.018)/.16);
  el.style.opacity=p;
  el.style.transform=still?'none':el.classList.contains('depth-card')?'translate3d(0,0,0)':`translate3d(0,${(1-p)*26}px,${(1-p)*-45}px) rotateX(${(1-p)*6}deg)`;
 });
}
function sceneDetails(index,u){
 const still=reduced.matches;
 scenes[index].style.setProperty('--local',u);
 if(index===0){$('#hero-price').textContent=(4133.48+Math.sin(u*8)*.16).toFixed(2);if(!still){$('.integration-pills').style.transform=`translateZ(${25-u*30}px) rotateY(${Math.sin(u*3)*5}deg)`;}}
 if(index===1)delayMotion(u);
 if(index===2){$('.solution-icons').style.transform=still?'none':'translate3d(0,0,0)';$('.core-orb').style.transform='none';}
 if(index===3){const t=u*sceneDurations[3],bell=$('.route-node:last-child .node-icon img');bell.style.rotate=(still?0:window.LVSignalRoute.bell(t,4.55)).toFixed(3)+'deg';const timings=[.95,2.15,3.35,4.55],activation=window.LVSignalRoute.cards(still?10:t,timings);$$('.route-node').forEach((el,k)=>{const enter=still?1:smooth((u+.08-k*.115)/.18),ready=activation[k].ready;el.style.opacity=enter;el.style.setProperty('--ready',ready);el.style.setProperty('--route-active',ready);el.style.setProperty('--route-flash',still?0:activation[k].flash);el.dataset.routeActive=ready.toFixed(3);el.style.transform=still?'none':'translate3d(0,0,0)';});$$('.route-link').forEach((el,k)=>{const travel=clamp((t-timings[k]-.2)/(timings[k+1]-timings[k]-.3));el.style.setProperty('--travel',travel);el.style.setProperty('--packet',travel>0&&travel<1?1:0);});}
 if(index===4){
  const t=u*sceneDurations[4],found=still?1:smooth((t-1.3)/1.8),turn=smooth((t-5.7)/1.45),matched=still?1:smooth((t-8)/1.6);
  $('.ai-card').style.setProperty('--found',found);$('.ai-card').style.setProperty('--scan',clamp((t-1)/2.4));$('.ai-card').style.setProperty('--scanning',still?0:smooth((t-.8)/.3)*(1-smooth((t-3.5)/.4)));$('.template-card').style.setProperty('--matched',matched);
  window.CardRelief.sizeCube($('.parser-cards'),turn);$('.parser-cube').style.transform=still?'none':`translateZ(${-cubeHalf}px) rotateY(${-turn*90}deg)`;$('.parser-cube').style.rotate='none';$('.parser-cube').style.translate='none';$('.parser-cube').dataset.angle=(turn*90).toFixed(2);$('.parser-cube').classList.toggle('turning',turn>0&&turn<1);
  $$('.parser-card').forEach((el,k)=>{el.style.opacity='1';el.style.transform='';el.style.zIndex='';el.style.display=still?((k===0)===(turn<.5)?'block':'none'):'block';const active=k===(turn<.5?0:1);el.setAttribute('aria-hidden',String(!active));el.inert=!active;});
 }

 if(index===5){$$('.feature-card').forEach((el,k)=>{const enter=still?1:smooth((u+.10-k*.09)/.23);el.style.opacity=enter;el.style.transform=still?'none':'translate3d(0,0,0)';});}
 if(index===6){
  const t=u*sceneDurations[6],route=window.LVSignalRoute.state(still?10:t,3.7),received=route.received,recognized=route.recognized,executed=route.executed,protectedLevel=still?1:smooth((t-4.8)/.65),r=route.route;
  scenes[6].style.setProperty('--route',r);scenes[6].style.setProperty('--packet',route.packet);scenes[6].dataset.routePhase=route.phase;scenes[6].style.setProperty('--executed',executed);scenes[6].style.setProperty('--protected',protectedLevel);scenes[6].style.setProperty('--focus',marketState.focus);
  $$('.network-port').forEach((el,k)=>{const ready=[received,recognized,route.mtReady][k];el.style.setProperty('--ready',ready);el.style.setProperty('--flash',route.flashes[k]);el.style.opacity='1';el.style.transform=still?'none':`translateZ(${ready*8}px)`;});
  $('#bot-entry').textContent=(marketState.botEntry??marketState.entry).toFixed(2);const values=marketState.trade?window.LVMarketSim.amounts(marketState.trade,marketState.price):{floating:0};$('#bot-distance').textContent=window.LVMarketSim.formatMoney(values.floating,true);scenes[6].dataset.phase=t<2.5?'signal':t<3.7?'recognition':t<4.8?'entry':t<8?'protected':'report';
 }

 if(index===8)finishMotion(sceneTime);
 if(index===7){const p=still?.5:u;$('.watch-orbit').style.setProperty('--angle',`${p*340}deg`);$('.watch-orbit').style.setProperty('--orbit-tilt',`${still?0:18-Math.sin(p*Math.PI)*14}deg`);if(!still)$('.watch-orbit').style.transform=`perspective(900px) rotateY(${Math.sin(p*4)*8}deg)`;const minutes=23*60+59+Math.floor(p*3);$('#night-time').textContent=String(Math.floor(minutes/60)%24).padStart(2,'0')+':'+String(minutes%60).padStart(2,'0');}
}
// Decorative motion has its own unbounded clock. It never replays story events.
const idleElements=[[$('.integration-pills')],[],[],[],[],[],[...$$('.network-port')],[$('.watch-orbit')],[$('.finish-integrations')]];
function idleMotion(index,u,clock){
 idleElements[index].forEach((el,k)=>{const wave=clock*.55+k*1.27,gate=index===1?1-smooth((u*sceneDurations[1]-3.2)/.8):smooth(u*5+.2);el.style.rotate=reduced.matches?'none':`1 .65 0 ${Math.sin(wave)*2.3*gate}deg`;el.style.translate=reduced.matches?'none':`0 ${Math.sin(wave*1.24)*2.6*gate}px`;});
 if(index===0)$('#hero-price').textContent=marketState.price.toFixed(2);
 if(index===7){$('.watch-orbit').style.setProperty('--angle',`${reduced.matches?0:clock*24}deg`);$('.watch-orbit').style.setProperty('--orbit-tilt',`${reduced.matches?0:12+Math.sin(clock*.37)*7}deg`);$('.watch-orbit').style.transform=reduced.matches?'none':`perspective(900px) rotateY(${Math.sin(clock*.35)*7}deg)`;const minutes=23*60+59+Math.floor(sceneTime*.12);$('#night-time').textContent=String(Math.floor(minutes/60)%24).padStart(2,'0')+':'+String(minutes%60).padStart(2,'0');}
}
// One stable local 3D scene per card, matching the NFT Toys detail implementation.
const depthCards=$$('.signal-message,.delay-stat,.route-node,.parser-card,.feature-card,.logo-orb').map((el,i)=>{
 el.classList.add('depth-card');const index=Number(el.closest('.scene').dataset.index);
 const trace=el===$('.route-node')||el.classList.contains('parser-card')||el===$$('.feature-card')[2];
 const relief=window.CardRelief.mount(el,{surfaceClass:'card-surface',input:index===4?$('.parser-cards'):el,phase:index===4?0:i*1.17,trace});
 const order=index===2?motionGroups[index].indexOf($('.solution-icons')):index===3?$$('.route-node').indexOf(el):index===5?$$('.feature-card').indexOf(el):motionGroups[index].indexOf(el);return{el,index,relief,order};
});
function depthMotion(index,u,clock){
 for(const card of depthCards){if(card.index!==index)continue;
  let gate=index===1?1-smooth((u*sceneDurations[1]-3.2)/.8):1;
  if(index===4){const turn=smooth((u*sceneDurations[4]-5.7)/1.45);gate*=1-Math.sin(turn*Math.PI)*.85;}
  let entrance=reduced.matches?1:smooth(transitionTime/.6);
  if(!reduced.matches){if(index===3)entrance=Math.min(entrance,smooth((u+.08-card.order*.115)/.18));else if(index===5)entrance=Math.min(entrance,smooth((u+.10-card.order*.09)/.23));else if(card.order>=0)entrance=Math.min(entrance,index===0?1:smooth((u+.16-card.order*.018)/.16));}
  card.el.style.rotate='none';card.el.style.translate='none';card.relief.render(clock,{gate,entrance});
 }
}
function enterScene(index){
 if(index===current)return;
 stopFastForward();marketScene=-1;previous=current;current=index;sceneTime=0;local=0;transitionTime=previous<0?1:0;document.documentElement.classList.remove('delay-scrubbing');document.documentElement.classList.toggle('demo-scene',index===1||index===6);
 cancelAnimationFrame(replayFrame);replayFrame=0;replayOverride=null;$('#replay-route').disabled=false;
 scenes.forEach((scene,i)=>{scene.setAttribute('aria-hidden',String(i!==index));scene.inert=i!==index;});
 document.body.dataset.sceneNumber=String(index+1).padStart(2,'0');document.body.dataset.scene=String(index);document.body.dataset.sceneName=chapterNames[index];document.documentElement.style.setProperty('--scroll',index/8);
}
function update(){
 queued=false;lastPaint=0;if(!metrics.length)return;
 let index=0;for(let i=1;i<metrics.length;i++)if(scrollY>=metrics[i].top-innerHeight*.5)index=i;
 enterScene(index);
}
function queue(){if(!queued){queued=true;requestAnimationFrame(update);}}
window.addEventListener('scroll',queue,{passive:true});window.addEventListener('resize',measure);reduced.addEventListener('change',()=>{lastPaint=0;});
function goTo(hash,behavior,reverse=false){const i=chapters.findIndex(el=>'#'+el.id===hash);if(i<0)return false;window.scrollTo({top:metrics[i].top,behavior:behavior||(reduced.matches?'instant':'smooth')});return true;}
function navigate(direction){goTo('#'+chapters[clamp(current+direction,0,8)].id,undefined,direction<0);}
document.querySelectorAll('a[href^="#"]').forEach(a=>a.addEventListener('click',e=>{const hash=a.getAttribute('href');if(hash==='#story'){e.preventDefault();return;}if(goTo(hash)){e.preventDefault();history.replaceState(null,'',hash);}}));
// Consume one wheel gesture as one chapter, including trackpad momentum.
let wheelTotal=0,wheelLocked=false,wheelIdle=0,navigationUntil=0;
window.addEventListener('wheel',e=>{
 if(e.ctrlKey||Math.abs(e.deltaX)>Math.abs(e.deltaY)||e.deltaY===0)return;
 e.preventDefault();clearTimeout(wheelIdle);wheelIdle=setTimeout(()=>{wheelLocked=false;wheelTotal=0;},220);
 if(wheelLocked||performance.now()<navigationUntil)return;
 const delta=e.deltaY*(e.deltaMode===1?16:e.deltaMode===2?innerHeight:1);

 wheelTotal+=delta;
 if(Math.abs(wheelTotal)<35)return;
 const direction=Math.sign(wheelTotal);wheelLocked=true;wheelTotal=0;navigationUntil=performance.now()+650;navigate(direction);
},{passive:false});
window.addEventListener('keydown',e=>{
 if(e.altKey||e.ctrlKey||e.metaKey||/INPUT|TEXTAREA|SELECT|BUTTON/.test(e.target.tagName))return;
 let index=current;if(e.key==='ArrowDown'||e.key==='PageDown'||(e.code==='Space'&&!e.shiftKey))index++;else if(e.key==='ArrowUp'||e.key==='PageUp'||(e.code==='Space'&&e.shiftKey))index--;else if(e.key==='Home')index=0;else if(e.key==='End')index=8;else return;
 e.preventDefault();if(e.repeat||performance.now()<navigationUntil)return;

 navigationUntil=performance.now()+650;goTo('#'+chapters[clamp(index,0,8)].id,undefined,index<current);
});
// Hold the right side of either signal scene to play its timeline at 4x.
let fastPointer=null,fastTimer=0,fastForward=false;
function stopFastForward(){clearTimeout(fastTimer);fastTimer=0;fastPointer=null;fastForward=false;document.documentElement.classList.remove('story-fast');}
function signalEndTime(){
 if(marketScene!==current||!marketState.trade||!Number.isFinite(marketState.rawNow))return null;
 return marketSceneTime+(marketState.trade.endAt-marketState.rawNow)/window.LVMarketSim.TIMING.rate;
}
function mayAccelerate(){const end=signalEndTime();return (current===1||current===6)&&(end===null||sceneTime<end);}
window.addEventListener('pointerdown',e=>{
 if(!mayAccelerate()||!e.isPrimary||e.button!==0||e.clientX<innerWidth*.68||e.target.closest('a,button,input,select,textarea'))return;
 stopFastForward();fastPointer={id:e.pointerId,x:e.clientX,y:e.clientY};
 fastTimer=setTimeout(()=>{if(fastPointer&&mayAccelerate()){window.getSelection()?.removeAllRanges();fastForward=true;document.documentElement.classList.add('story-fast');}else stopFastForward();},220);
},{passive:true});
window.addEventListener('pointermove',e=>{if(fastPointer&&e.pointerId===fastPointer.id&&Math.hypot(e.clientX-fastPointer.x,e.clientY-fastPointer.y)>12)stopFastForward();},{passive:true});
for(const event of ['pointerup','pointercancel'])window.addEventListener(event,e=>{if(fastPointer&&e.pointerId===fastPointer.id)stopFastForward();},{passive:true});
window.addEventListener('blur',stopFastForward);
document.addEventListener('contextmenu',e=>{if(fastPointer)e.preventDefault();});
document.addEventListener('selectstart',e=>{if(fastPointer)e.preventDefault();});
let touchStart=null;
window.addEventListener('touchstart',e=>{if(e.touches.length===1)touchStart={x:e.touches[0].clientX,y:e.touches[0].clientY,index:current};else touchStart=null;},{passive:true});
window.addEventListener('touchmove',e=>{if(!touchStart||e.touches.length!==1)return;const dx=touchStart.x-e.touches[0].clientX,dy=touchStart.y-e.touches[0].clientY;if(Math.abs(dy)>10&&Math.abs(dy)>Math.abs(dx))e.preventDefault();},{passive:false});
window.addEventListener('touchcancel',()=>{touchStart=null;},{passive:true});
window.addEventListener('touchend',e=>{if(!touchStart)return;const dy=touchStart.y-e.changedTouches[0].clientY,dx=touchStart.x-e.changedTouches[0].clientX;if(Math.abs(dy)>45&&Math.abs(dy)>Math.abs(dx))goTo('#'+chapters[clamp(touchStart.index+Math.sign(dy),0,8)].id,undefined,dy<0);touchStart=null;},{passive:true});
$('#replay-route').addEventListener('click',()=>{stopFastForward();marketScene=-1;sceneTime=0;filmMarket.restart(6);$('#replay-status').textContent=tr('Сигнал проходит маршрут: Telegram, распознавание, MetaTrader 5.');$('#replay-route').disabled=true;});
document.addEventListener('visibilitychange',()=>{lastFrame=performance.now();if(document.hidden)stopFastForward();});
function animate(now){
 const dt=document.hidden?0:Math.min(Math.max((now-lastFrame)/1000,0),1);lastFrame=now;
 if(current>=0){const clock=advanceStoryTime(sceneTime,dt,fastForward&&(current===1||current===6),signalEndTime());if(clock.stop)stopFastForward();sceneTime+=clock.advance;ambientTime+=clock.advance;transitionTime+=dt;const active=!document.hidden&&(!reduced.matches||sceneTime<sceneDurations[current]+.8||transitionTime<.7);
 if((active||queued||lastPaint===0)&&now-lastPaint>1000/30){
 lastPaint=now;local=clamp(sceneTime/sceneDurations[current],0,.9999);const transition=reduced.matches?1:smooth(transitionTime/.6);
 scenes.forEach((scene,i)=>{const visible=i===current||(i===previous&&transition<1);scene.classList.toggle('active',visible);if(!visible){scene.style.opacity=0;return;}if(i===current){scene.style.opacity=transition;scene.style.transform=reduced.matches?'none':`translate3d(0,${(1-transition)*40}px,${-(1-transition)*160}px) rotateX(${-(1-transition)*5}deg)`;}else{scene.style.opacity=1-transition;scene.style.transform=`translate3d(0,${-transition*35}px,${-transition*230}px) rotateX(${transition*6}deg)`;}});
 marketState=filmMarket.render(current,local,ambientTime,sceneTime);marketScene=current;marketSceneTime=sceneTime;syncSignal();
 storyAudio.update(current,sceneTime);speedBadge.style.top=($('#market').dataset.labelTop||'0')+'px';
 const resultReveal=Math.max(resultCard.update(marketState.trade,marketState.rawNow,current===6),telegramNotice.update(marketState.trade,marketState.rawNow,current===1));document.documentElement.style.setProperty('--trade-result',resultReveal);
 updateTicker(marketState.focus);
 setMotion(current,local);sceneDetails(current,local);
 if(current===6&&marketState.status?.closed&&$('#replay-route').disabled){$('#replay-route').disabled=false;$('#replay-status').textContent=tr('Демонстрационный ордер принят. Stop Loss и Take Profit установлены. Отчёт готов.');}
 idleMotion(current,local,ambientTime);depthMotion(current,local,ambientTime);
 const focus=current===1||current===6?marketState.focus:0;$('#market').style.opacity=.78+focus*.16;$('#market').style.filter=`blur(${(1-focus)*1.1}px)`;$('.vignette').style.opacity=1-focus;$('.grain').style.opacity=(1-focus)*.025;
 scenes[current].dataset.time=sceneTime.toFixed(2);$('#market').dataset.clock=ambientTime.toFixed(2);
 }}
 requestAnimationFrame(animate);
}
const resultCard=window.LVMarketSim.resultCard(document.body),telegramNotice=window.LVMarketSim.telegramNotice(document.body);
const ticker=$('.market-ticker'),tickerElements=scenes.map(scene=>[...scene.querySelectorAll('h1,h2,.route-node,.parser-card,.feature-card,.logo-orb')]);
let lastTickerMeasure=-Infinity;
function updateTicker(focus){const now=performance.now();if(now-lastTickerMeasure<100)return;lastTickerMeasure=now;const band=ticker.getBoundingClientRect();let hide=(current===1||current===6)?smooth((focus-.15)/.4):0;for(const el of tickerElements[current]){const r=el.getBoundingClientRect();if(r.bottom>band.top-12&&r.top<band.bottom+24&&+getComputedStyle(el).opacity>.05)hide=Math.max(hide,smooth((band.bottom+24-r.top)/32));}ticker.style.opacity=String(1-hide);}
const filmMarket=createFilmMarket($('#market'),{reducedMotion:()=>reduced.matches});
document.body.classList.add('market-ready');
measure();document.fonts?.ready.then(measure);if(location.hash)requestAnimationFrame(()=>goTo(location.hash,'instant'));requestAnimationFrame(animate);
