import {createActivityRig} from './lv1-action-rig.js?v=4';
import {createShakeDetector} from './pet-shake.js?v=1';
import {createWalkRig} from './lv1-walk-rig.js?v=3';
/* LV-1: original generated artwork and a browser animation controller. */
export function mountPet(){
const controller=new AbortController(), options={signal:controller.signal};
let destroyed=false;
const artURL=new URL('pets/lv1/sprites-b322f4a58a72.webp',import.meta.url).href;
let locale='en',t;
const translations={
 ru:{hello:'Привет!',profit:'TP · +',loss:'SL · −',pet:'LV-1: нажмите для случайной анимации или перетащите',keys:'Enter или пробел — случайная анимация. Стрелки — переместить. Escape — отдых.'},
 uz:{hello:'Salom!',profit:'TP · +',loss:'SL · −',pet:'LV-1: tasodifiy animatsiya uchun bosing yoki sudrang',keys:'Enter yoki bo‘sh joy — tasodifiy animatsiya. Strelkalar — ko‘chirish. Escape — dam olish.'},
 en:{hello:'Hello!',profit:'TP · +',loss:'SL · −',pet:'LV-1: click for a random animation or drag to move',keys:'Enter or Space: random animation. Arrows: move. Escape: rest.'}
};
function updateLanguage(){const lang=document.documentElement.lang.split('-')[0];locale=['ru','uz','en'].includes(lang)?lang:'en';t=translations[locale];if(pet){pet.setAttribute('aria-label',t.pet);root.querySelector('#lumi-help').textContent=t.keys;}}
const states={
 idle:{row:0,sequence:[0,0,0,1,1,2,3,3],fps:3},
 'walk-right':{row:0,sequence:[0,1,2,3,4,5,6,7],fps:8,sheet:'walk'},
 'walk-left':{row:0,sequence:[0,1,2,3,4,5,6,7],fps:8,sheet:'walk'},
 waving:{row:2,sequence:[0,1,2,3,2,3,1,0],fps:5},
 jumping:{row:0,sequence:[0,1,3,0],fps:5},
 working:{row:3,sequence:[0,1,2,1,3,2,1,2],fps:4},
 sleeping:{row:4,sequence:[0,1,2,3,2,1],fps:2},
 celebrating:{row:5,sequence:[0,1,2,3],fps:4},
 happy:{row:6,sequence:[0,1,2,3,1,2,3],fps:4},
 annoyed:{row:7,sequence:[0,1,2,3,3,3],fps:3},
 shaken:{row:7,sequence:[0,1,2,3],fps:4},
 dizzy:{row:4,sequence:[0,1,0,1],fps:2}
};
const root=document.createElement('aside');root.className='lumi';root.hidden=true;root.setAttribute('aria-label','LV-1');
root.innerHTML=`<button type="button" class="lumi-pet" aria-describedby="lumi-help"><span class="lumi-sprite" aria-hidden="true"></span></button><span id="lumi-help" class="sr-only"></span><div class="lumi-bills" aria-hidden="true"></div><div class="lumi-sleep" aria-hidden="true"><span>Z</span><span>Z</span><span>Z</span></div><div class="lumi-dizzy-stars" aria-hidden="true"><span>✦</span><span>✦</span><span>✦</span></div><div class="lumi-bubble" role="status" aria-live="polite" aria-atomic="true"></div>`;
document.body.append(root);
const pet=root.querySelector('.lumi-pet'),sprite=root.querySelector('.lumi-sprite'),bubble=root.querySelector('.lumi-bubble'),bills=root.querySelector('.lumi-bills');
updateLanguage();
const langObserver=new MutationObserver(updateLanguage);langObserver.observe(document.documentElement,{attributes:true,attributeFilter:['lang']});
const walkRig=createWalkRig(),actionRig=createActivityRig();pet.append(actionRig.svg,walkRig.svg);
const reducedQuery=matchMedia('(prefers-reduced-motion: reduce)');
let reduced=reducedQuery.matches,loaded=false,atlasAvailable=false,poseFailed=false,scene=-1,signal=false;
let state='idle',elapsed=0,stateUntil=0,nextAction=0,lastTime=0,lastPaint=-1,raf=0;
let walkDistance=0;
let x=16,y=0,size=112,height=121,goal=null,pointer=null,dragging=false,bubbleUntil=0,lastRandom='',pendingOutcome=null,decisionIndex=0,lastToss=-1;
const rand=(a,b)=>a+Math.random()*(b-a),clock=()=>performance.now()/1000;
const viewport=()=>({w:document.documentElement.clientWidth,h:window.visualViewport?.height||innerHeight});
function bounds(){const {w,h}=viewport(),bottomGap=w<=600?92:38;return {left:10,top:20,right:Math.max(10,w-size-16),bottom:Math.max(20,h-height-bottomGap)};}
function clampPosition(){const b=bounds();x=Math.max(b.left,Math.min(x,b.right));y=Math.max(b.top,Math.min(y,b.bottom));}
function paintPosition(){root.style.transform=`translate3d(${x.toFixed(2)}px,${y.toFixed(2)}px,0)`;root.dataset.x=x.toFixed(1);root.dataset.y=y.toFixed(1);root.dataset.nearTop=String(y<100);const {w}=viewport();root.style.setProperty('--bubble-offset',`${Math.max(-x+6,Math.min(0,w-x-235))}px`);}
function layout(){size=parseFloat(getComputedStyle(root).getPropertyValue('--pet-size'))||112;height=size*208/192;clampPosition();if(goal){const b=bounds();goal.x=Math.max(b.left,Math.min(goal.x,b.right));goal.y=Math.max(b.top,Math.min(goal.y,b.bottom));}paintPosition();}
function speak(text,duration=3.6,tone='neutral',force=false){if((signal&&!force)||!text)return;bubble.textContent=text;bubble.dataset.tone=tone;bubbleUntil=clock()+duration;bubble.classList.add('is-visible');wake();}
function paintFrame(){const walking=states[state].sheet==='walk',frame=states[state].sequence[Math.floor(elapsed*states[state].fps)%states[state].sequence.length]%4;sprite.style.backgroundPosition=`${frame*100/3}% ${states[state].row*100/7}%`;actionRig.render(walking?'idle':state,elapsed,clock(),reduced);if(walking)walkRig.render(walkDistance/(size*.3));root.dataset.frame=String(walking?Math.floor(walkDistance/(size*.3)*8)%8:Math.floor(elapsed*60));}
function setState(value,seconds=0){if(!states[value])return;state=value;elapsed=0;walkDistance=0;lastPaint=-1;lastToss=-1;stateUntil=seconds?clock()+seconds:0;root.dataset.state=state;paintFrame();}
function settle(){goal=null;pendingOutcome=null;delete root.dataset.outcome;setState('idle');nextAction=clock()+rand(8,14);}
function tossMoney(){if(reduced||signal)return;for(let i=0;i<4;i++){const bill=document.createElement('i');bill.className='lumi-bill';const hand=actionRig.hand();bill.style.left=`${hand[0]/192*100}%`;bill.style.top=`${hand[1]/208*100}%`;bill.style.setProperty('--fly-x',`${rand(36,110)}px`);bill.style.setProperty('--fly-y',`${rand(-95,-65)}px`);bill.style.setProperty('--fly-r',`${rand(-160,160)}deg`);bill.style.setProperty('--delay',`${i*.055}s`);bills.append(bill);setTimeout(()=>bill.remove(),2300);}}
function action(value,manual=false){
 if(!atlasAvailable&&(value==='working'||value==='sleeping'))value='waving';
 goal=null;pendingOutcome=null;delete root.dataset.outcome;bills.replaceChildren();bubble.classList.remove('is-visible');bubbleUntil=0;
 const duration={waving:3.2,working:6.8,sleeping:manual?10:16,celebrating:4,happy:4,annoyed:4}[value];if(!duration)return;
 setState(value,duration);decisionIndex++;root.dataset.actionId=String(decisionIndex);
 if(value==='waving')speak(t.hello,duration);
 if(value==='working')pendingOutcome=Math.random()<.7?'TP':'SL';
 nextAction=Infinity;wake();
}
function randomAction(){const options=['waving','working','sleeping','celebrating'].filter(s=>s!==lastRandom);const choice=options[Math.floor(Math.random()*options.length)];lastRandom=choice;action(choice,true);}
function finishWork(){
 const outcome=pendingOutcome;pendingOutcome=null;if(!outcome){settle();return;}
 root.dataset.outcome=outcome;
 const config=window.LVTradeConfig||{lot:.1,contractSize:100};
 const distance=outcome==='TP'?rand(8,12):rand(3,5);
 const amount=(distance*config.lot*config.contractSize).toLocaleString(locale==='ru'?'ru-RU':'en-US',{minimumFractionDigits:2,maximumFractionDigits:2});
 setState(outcome==='TP'?'happy':'annoyed',4.4);
 speak(`${outcome==='TP'?t.profit:t.loss}$${amount}`,4.4,outcome==='TP'?'profit':'loss');
}
function walk(){const b=bounds(),target=rand(b.left,b.right);if(Math.abs(target-x)<size*.75){action('working');return;}goal={x:target,y};setState(target>x?'walk-right':'walk-left');nextAction=Infinity;}
function checkScene(){const current=Number(document.body.dataset.scene??0);if(current===scene)return;scene=current;signal=current===1||current===6;root.dataset.signal=String(signal);pendingOutcome=null;goal=null;bubbleUntil=0;bubble.classList.remove('is-visible');bills.replaceChildren();settle();if(signal){const b=bounds();goal={x:b.left,y:b.bottom,parking:true};}wake();}
function frame(ms){
 raf=0;if(document.hidden||!loaded)return;const now=ms/1000,rawDT=Math.max(0,now-(lastTime||now)),dt=Math.min(.06,rawDT);lastTime=now;checkScene();
 if(!pointer&&goal){const dx=goal.x-x,dy=goal.y-y,dist=Math.hypot(dx,dy);if(goal.parking){const blend=reduced?1:1-Math.exp(-rawDT*5);x+=dx*blend;y+=dy*blend;if(dist<.6||reduced){x=goal.x;y=goal.y;goal=null;}}else if(!reduced){const step=dt*(size<100?24:32);if(dist<step){x=goal.x;y=goal.y;settle();}else{x+=dx/dist*step;y+=dy/dist*step;walkDistance+=step;}}paintPosition();}
 if(bubbleUntil&&now>bubbleUntil){bubble.classList.remove('is-visible');bubbleUntil=0;}
 if(!pointer){if(stateUntil&&now>stateUntil){if(state==='working')finishWork();else settle();}else if(!signal&&!reduced&&!stateUntil&&now>nextAction){const choices=['walk','walk','working','waving','sleeping','celebrating'];const choice=choices[Math.floor(Math.random()*choices.length)];if(choice==='walk')walk();else action(choice);}}
 if(!reduced){elapsed+=rawDT;paintFrame();if(state==='celebrating'&&!signal){const cycle=Math.floor((elapsed-.8)/1.6);if(cycle>=0&&cycle!==lastToss){lastToss=cycle;tossMoney();}}}
 wake();
}
function wake(){if(!destroyed&&!raf&&!document.hidden&&loaded)raf=requestAnimationFrame(frame);}
function releasePointer(e,cancel=false){
 if(!pointer||e.pointerId!==pointer.id)return;e.stopPropagation();const wasDragged=dragging,wasShaken=pointer.shaken;pointer=null;dragging=false;
 try{pet.releasePointerCapture(e.pointerId);}catch{}
 root.classList.remove('is-dragging');root.style.removeProperty('--drag-lean');clampPosition();paintPosition();
 if(cancel)settle();else if(wasShaken){setState('dizzy',5.2);nextAction=Infinity;}else if(wasDragged){setState('jumping',.7);nextAction=clock()+12;}else randomAction();wake();
}
pet.addEventListener('pointerdown',e=>{if(e.button!==0||pointer)return;e.stopPropagation();goal=null;pendingOutcome=null;pointer={id:e.pointerId,startX:e.clientX,startY:e.clientY,offsetX:e.clientX-x,offsetY:e.clientY-y,shaken:false,detector:createShakeDetector(e.clientX,e.clientY,e.timeStamp)};pet.setPointerCapture(e.pointerId);setState('idle');});
pet.addEventListener('pointermove',e=>{if(!pointer||pointer.id!==e.pointerId)return;e.stopPropagation();const distance=Math.hypot(e.clientX-pointer.startX,e.clientY-pointer.startY);if(distance>6&&!dragging){dragging=true;root.classList.add('is-dragging');setState('jumping');}if(dragging){
 const samples=e.getCoalescedEvents?.()||[];for(const point of samples.length?samples:[e])if(!pointer.shaken&&pointer.detector.sample(point.clientX,point.clientY,point.timeStamp)){pointer.shaken=true;bubbleUntil=0;bubble.classList.remove('is-visible');bills.replaceChildren();setState('shaken');}
 const previous=x;x=e.clientX-pointer.offsetX;y=e.clientY-pointer.offsetY;clampPosition();root.style.setProperty('--drag-lean',`${Math.max(-12,Math.min(12,(x-previous)*.5))}deg`);paintPosition();}});
pet.addEventListener('pointerup',e=>releasePointer(e));pet.addEventListener('pointercancel',e=>releasePointer(e,true));pet.addEventListener('lostpointercapture',e=>{if(pointer)releasePointer(e,true);});
pet.addEventListener('click',e=>{e.stopPropagation();if(e.detail===0)randomAction();});
root.addEventListener('keydown',e=>{if(e.key==='Escape'){e.preventDefault();e.stopPropagation();settle();nextAction=clock()+30;bubble.classList.remove('is-visible');bills.replaceChildren();return;}if(e.target!==pet)return;if(e.key.startsWith('Arrow')){e.preventDefault();e.stopPropagation();settle();x+=e.key==='ArrowRight'?20:e.key==='ArrowLeft'?-20:0;y+=e.key==='ArrowDown'?20:e.key==='ArrowUp'?-20:0;clampPosition();paintPosition();nextAction=clock()+15;}});
for(const event of ['touchstart','touchmove','touchend','touchcancel','wheel'])root.addEventListener(event,e=>e.stopPropagation(),{passive:true});
window.addEventListener('resize',layout,{passive:true,...options});window.visualViewport?.addEventListener('resize',layout,{passive:true,...options});
document.addEventListener('visibilitychange',()=>{if(document.hidden){cancelAnimationFrame(raf);raf=0;}else{lastTime=0;wake();}},options);
reducedQuery.addEventListener('change',()=>{reduced=reducedQuery.matches;settle();root.dataset.paused=String(reduced);wake();},options);
function showPet(fallback=false){
 if(destroyed||loaded)return;loaded=true;root.dataset.fallback=fallback?'sprite':'rig';root.hidden=false;layout();y=bounds().bottom;paintPosition();checkScene();root.dataset.paused=String(reduced);requestAnimationFrame(()=>root.classList.add('is-ready'));if(!signal){speak(t.hello,3.6);setState('waving',3.2);}nextAction=clock()+10;wake();
}
// The small standing pose makes the companion appear quickly; the larger atlas is optional.
const posePreload=new Image(),atlas=new Image();posePreload.decoding='async';atlas.decoding='async';
posePreload.onload=()=>showPet(false);posePreload.onerror=()=>{poseFailed=true;if(atlasAvailable)showPet(true);};
atlas.onload=()=>{atlasAvailable=true;if(poseFailed)showPet(true);};
atlas.onerror=()=>{atlasAvailable=false;};
posePreload.src=new URL('pets/lv1/walk-rig-pose-e636832997a2.png',import.meta.url).href;atlas.src=artURL;

return ()=>{destroyed=true;controller.abort();langObserver.disconnect();cancelAnimationFrame(raf);atlas.onload=null;atlas.onerror=null;posePreload.onload=null;posePreload.onerror=null;root.remove();};
}
