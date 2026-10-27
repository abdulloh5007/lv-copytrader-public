
(function(){
var i18n=window.LV_I18N,tr=i18n.t;i18n.apply(document);
var D=[6,92,9.5,10.5,12,8,92,10,5.6],T=D.reduce(function(a,b){return a+b},0);
var st=document.getElementById('stage'),sc=[].slice.call(document.querySelectorAll('.s')),bar=document.getElementById('bar'),ctl=document.getElementById('ctl'),cap=document.getElementById('cap'),tm=document.getElementById('tm'),hp=document.getElementById('hp'),pr=document.getElementById('pr'),fade=document.getElementById('fade'),sel=document.getElementById('vsel');
var motionClock=0,idleT=0,LEAD=0,el=LEAD,cur=-1,play=false,done=false,started=false,music=true,voiceOn=true,last=performance.now(),fired=[],AC,master,bus,dly,nb,en=0,nextT=0,step=0,sT=0,vc,vlist=[];
var vp=document.getElementById('viewport'),panel=document.getElementById('player-panel'),seek=document.getElementById('seek'),resumeButton=document.getElementById('resume'),seeked=false;
document.getElementById('top-chrome').append(document.getElementById('tk'));
vp.append(ctl,bar);document.getElementById('audio-controls').append(document.getElementById('hud'));seek.max=T;document.getElementById("total-time").textContent=clockLabel(T);
var flowNodes=Array.from(sc[3].querySelectorAll('.node')),flowLinks=Array.from(sc[3].querySelectorAll('.cn'));
var flowActivation=[1.85,3.25,5.25,7.45];
var flowLabels=[tr('Источник подключён'),tr('Параметры извлечены'),tr('Подключение активно'),tr('Отчёт готов')];
flowNodes.forEach(function(n){n.dataset.original=n.querySelector('small').textContent});
var aiCard=sc[4].querySelector('.card'),aiFeed=aiCard.querySelector('.feed'),templateFeed=sc[4].querySelectorAll('.feed')[1];
aiFeed.classList.add('parser-feed');templateFeed.classList.add('template-feed');
var parseResult=document.createElement('div');parseResult.className='parse-result';aiCard.append(parseResult);
var parseStatus=document.createElement('div');parseStatus.className='parse-status';parseStatus.textContent=tr('✓ Параметры готовы к отправке');aiCard.append(parseStatus);
var filmMotion=window.createFilmMotion({scenes:sc,stage:st});
function sceneMotion(t,i){
 if(i===3){var bell=flowNodes[3].querySelector('img');bell.style.rotate=window.LVSignalRoute.bell(t,flowActivation[3]).toFixed(3)+'deg';var activation=window.LVSignalRoute.cards(t,flowActivation);flowNodes.forEach(function(n,k){var complete=activation[k].ready,hot=activation[k].flash;n.style.setProperty('--hot',hot);n.style.setProperty('--complete',complete);n.style.setProperty('--route-active',complete);n.style.setProperty('--route-flash',hot);n.dataset.routeActive=complete.toFixed(3);var label=complete>.5?flowLabels[k]:n.dataset.original;if(n.querySelector('small').textContent!==label)n.querySelector('small').textContent=label});flowLinks.forEach(function(n,k){var start=flowActivation[k]+.2,end=flowActivation[k+1]-.1,u=clamp((t-start)/(end-start));n.style.setProperty('--travel',u);n.style.setProperty('--packet',u>0&&u<1?Math.sin(Math.PI*u):0)})}
 if(i===4){aiFeed.style.setProperty('--scan',clamp((t-1.5)/2));aiFeed.style.setProperty('--scanning',smooth((t-1.3)/.3)*(1-smooth((t-3.5)/.4)));aiFeed.style.setProperty('--classified',smooth((t-2.8)/.8));aiCard.style.setProperty('--found',smooth((t-3.1)/.7));templateFeed.querySelectorAll('.ok').forEach(function(n,k){n.style.setProperty('--matched',smooth((t-7.7-k*.7)/.55))})}
}
/* ---------- audio engine: procedural music ---------- */
function initAudio(){if(AC)return;var C=window.AudioContext||window.webkitAudioContext;if(!C)return;AC=new C({latencyHint:"interactive"});var cm=AC.createDynamicsCompressor();cm.connect(AC.destination);voiceGain=AC.createGain();voiceGain.gain.value=.88;voiceGain.connect(cm);master=AC.createGain();master.gain.setValueAtTime(0,AC.currentTime);master.connect(cm);
bus=AC.createGain();setBus();bus.connect(master);dly=AC.createDelay(1);dly.delayTime.value=.45;var fb=AC.createGain();fb.gain.value=.2;var df=AC.createBiquadFilter();df.type='lowpass';df.frequency.value=2400;dly.connect(df);df.connect(fb);fb.connect(dly);df.connect(bus);
nb=AC.createBuffer(1,AC.sampleRate,AC.sampleRate);var d=nb.getChannelData(0);for(var i=0;i<d.length;i++)d[i]=Math.random()*2-1;nextT=AC.currentTime+.1;setInterval(sched,30)}
// The envelope follows film time, including scrubbing and the final silent frame.
function musicEnvelopeAt(seconds,total){return smooth(seconds/3.5)*(1-smooth((seconds-(total-1.6))/1.6));}
function syncMusicEnvelope(instant){
 var target=music?musicEnvelopeAt(el,T):0;st.dataset.musicEnvelope=target.toFixed(6);
 if(!master||!AC)return;var now=AC.currentTime;master.gain.cancelScheduledValues(now);
 if(instant||target===0)master.gain.setValueAtTime(target,now);else master.gain.setTargetAtTime(target,now,.025);
}

function setBus(){if(bus)bus.gain.value=voiceOn?.55:1}
function osc(f,type,t,att,dur,vol,send,lpf,f2){var o=AC.createOscillator(),g=AC.createGain();o.type=type;o.frequency.setValueAtTime(f,t);if(f2)o.frequency.exponentialRampToValueAtTime(f2,t+dur);g.gain.setValueAtTime(.0001,t);g.gain.linearRampToValueAtTime(vol,t+att);g.gain.exponentialRampToValueAtTime(.0001,t+dur);if(lpf){var q=AC.createBiquadFilter();q.type='lowpass';q.frequency.value=lpf;o.connect(q);q.connect(g)}else o.connect(g);g.connect(bus);if(send){var sg=AC.createGain();sg.gain.value=.25;g.connect(sg);sg.connect(dly)}o.start(t);o.stop(t+dur+.05);return o}
function hit(t,vol,dur,hp){var s=AC.createBufferSource();s.buffer=nb;var f=AC.createBiquadFilter();f.type='highpass';f.frequency.value=hp;var g=AC.createGain();g.gain.setValueAtTime(vol,t);g.gain.exponentialRampToValueAtTime(.0001,t+dur);s.connect(f);f.connect(g);g.connect(bus);s.start(t);s.stop(t+dur+.02)}
function sweep(dur,f0,f1,vol,dl){if(!AC||!music)return;var t=AC.currentTime+(dl||0),s=AC.createBufferSource();s.buffer=nb;s.loop=true;var f=AC.createBiquadFilter();f.type='bandpass';f.Q.value=1.1;var g=AC.createGain();f.frequency.setValueAtTime(f0,t);f.frequency.exponentialRampToValueAtTime(f1,t+dur);g.gain.setValueAtTime(.0001,t);g.gain.linearRampToValueAtTime(vol,t+dur*.45);g.gain.exponentialRampToValueAtTime(.0001,t+dur);s.connect(f);f.connect(g);g.connect(bus);s.start(t);s.stop(t+dur+.05)}
function tn(f,dur,vol,type,dl,f2){if(!AC||!music)return;osc(f,type||'sine',AC.currentTime+(dl||0),.005,dur,vol,0,0,f2)}
var SP=.15,CHS=[[110,[220,261.6,329.6]],[87.3,[174.6,220,261.6]],[130.8,[261.6,329.6,392]],[98,[196,246.9,293.7]]],ARP=[0,1,2,1,2,1,0,1];
function sched(){if(!AC)return;var now=AC.currentTime;if(!music||!play||!en){nextT=now+.1;return}
while(nextT<now+.2){note(step,nextT);step++;nextT+=SP}}
function note(s,t){var ch=CHS[Math.floor(s/16)%4],k=s%16;
if(k===0)ch[1].forEach(function(f){osc(f,'triangle',t,.9,2.7,.045,0,900)});
if(en===1&&(k===0||k===3))osc(120,'sine',t,.005,.35,.5,0,0,42);
if(en>=2){if(k===0||k===10){osc(ch[0],'sine',t,.01,.5,.2);osc(ch[0]*2,'triangle',t,.01,.4,.05,0,700)}
if(k%2===0)osc(ch[1][ARP[(k/2)%8]]*2,'triangle',t,.005,.55,.05,1,2800)}
if(en===2&&(k===0||k===8))osc(130,'sine',t,.005,.3,.4,0,0,45);
if(en>=3){if(k%4===0)osc(140,'sine',t,.004,.28,.6,0,0,45);if(k%4===2)hit(t,.07,.05,7000);if(k===4||k===12)hit(t,.08,.14,1800)}}
var FX={whoosh:function(d){sweep(1.1,400,3600,.3,d)},riser:function(d){sweep(2.8,200,6500,.45,d)},thud:function(d){tn(95,.8,.8,'sine',d,36)},pop:function(d,j){var f=[523.25,587.33,659.25,783.99,880][j%5];tn(f,.16,.07,'sine',d);tn(f*2,.08,.02,'sine',d)},pad:function(n){en=({tense:1,mid:2,bright:2,end:3})[n]||0}};
FX.telegramTone=function(){if(!AC||!music||!play)return;tn(784,.14,.045,'sine');tn(1046.5,.18,.035,'sine',.075)};
FX.recognizedTone=function(){if(!AC||!music||!play)return;tn(523.25,.13,.045,'sine');tn(783.99,.20,.04,'sine',.09)};
FX.executedTone=function(){if(!AC||!music||!play)return;tn(659.25,.15,.04,'sine');tn(830.61,.17,.035,'sine',.065);tn(987.77,.24,.04,'sine',.13)};

FX.bellTone=function(){if(!AC||!music||!play)return;[0,.19].forEach(function(delay){[1046.5,2812.2,4127.1].forEach(function(f,k){osc(f,'sine',AC.currentTime+delay,.003,[.9,.52,.28][k],[.06,.02,.008][k]*(delay?.65:1),0,0)})});};
FX.routeCardTone=function(index){if(!AC||!music||!play)return;if(index===3){FX.bellTone();return;}var f=[587.33,659.25,783.99,987.77][index];tn(f,.15,.045,'sine');tn(f*1.25,.21,.035,'sine',.085)};

var endingVoices=[];
function stopEndingVoices(){endingVoices.forEach(function(o){try{o.stop();o.disconnect()}catch(e){}});endingVoices=[];}
FX.outroTone=function(offset){
 if(!AC||!music||!play)return;stopEndingVoices();var age=Number(offset)||0,duration=Math.max(.1,D[8]-age-.2),now=AC.currentTime;
 [130.8128,261.6256,329.6276,391.9954].forEach(function(f,k){endingVoices.push(osc(f,'triangle',now,.16,duration,k===0?.025:.035,0,1100))});
};

/* ---------- voice ---------- */
var narration=(i18n.lang==='ru'?window.PROMO_NARRATION:window.PROMO_NARRATIONS?.[i18n.lang])||{},recordedVoice=true,voiceSource=null,voiceGain,voiceBuffers={},voiceDownloads={},voiceGeneration=0;
Object.values(narration).forEach(function(track){voiceDownloads[track.src]=fetch(track.src).then(function(r){if(!r.ok)throw new Error('Audio unavailable');return r.arrayBuffer()}).catch(function(){return null})});
function loadVoices(){
 var selected=sel.value||'recorded';vlist=window.speechSynthesis?speechSynthesis.getVoices().filter(function(v){return new RegExp('^'+i18n.lang,'i').test(v.lang)}):[];
 sel.replaceChildren();var recorded=document.createElement('option');recorded.value='recorded';recorded.textContent=tr('Денис · нейросетевая озвучка · русский');sel.appendChild(recorded);
 vlist.forEach(function(v,i){var o=document.createElement('option');o.value=i;o.textContent=tr('Системный: ')+v.name;sel.appendChild(o)});
 sel.value=selected;if(!sel.value)sel.value='recorded';recordedVoice=sel.value==='recorded';vc=recordedVoice?null:vlist[+sel.value];
}
if(window.speechSynthesis)speechSynthesis.onvoiceschanged=loadVoices;loadVoices();
sel.onchange=function(){recordedVoice=sel.value==='recorded';vc=recordedVoice?null:vlist[+sel.value]};
function stopRecorded(){voiceGeneration++;if(voiceSource){voiceSource.onended=null;try{voiceSource.stop();voiceSource.disconnect()}catch(e){}voiceSource=null}if(cs2){cs2.sourceStarted=false;cs2.waiting=false}activeVoice=false}
function recordingBuffer(track){
 if(!voiceBuffers[track.src])voiceBuffers[track.src]=(voiceDownloads[track.src]||fetch(track.src).then(function(r){return r.arrayBuffer()})).then(function(bytes){if(!bytes)throw new Error('Audio unavailable');return AC.decodeAudioData(bytes.slice(0))});return voiceBuffers[track.src];
}
function playRecorded(c,offset){
 if(!voiceOn||!play||cs2!==c)return;stopRecorded();initAudio();var generation=voiceGeneration;activeVoice=true;c.waiting=true;
 recordingBuffer(c.track).then(function(buffer){
  if(generation!==voiceGeneration||cs2!==c)return;c.waiting=false;if(!play||!voiceOn){activeVoice=false;return}
  var start=Math.max(0,Math.min(offset||0,buffer.duration-.01)),source=AC.createBufferSource();source.buffer=buffer;source.connect(voiceGain);voiceSource=source;c.clock=start;c.playedAt=AC.currentTime-start;c.sourceStarted=true;activeVoice=true;
  source.onended=function(){if(cs2===c&&voiceSource===source){voiceSource=null;activeVoice=false;c.complete=true;c.clock=c.dur;paintWords(c,c.words.length)}};source.start(0,start);
 }).catch(function(){if(cs2===c){activeVoice=false;c.waiting=false;c.recorded=false;c.boundaries=false}});
}
function wordAt(c,seconds){var k=0;for(var j=0;j<c.track.starts.length;j++)if(c.track.starts[j]<=seconds)k=j;return k}
var cs2=null,activeVoice=false;
/* Original-word to spoken-character mapping also covers the English brand. */
function chunks(words){
 var out=[],a=0;
 while(a<words.length){var b=a,len=0;while(b<words.length&&b-a<4&&(len+words[b].length<25||b===a)){len+=words[b].length+1;b++}out.push([a,b]);a=b}
 return out;
}
function paintWords(c,index){
 var group=c.groups.findIndex(function(g){return index>=g[0]&&index<g[1]});
 if(group<0)group=c.groups.length-1;
 if(c.group!==group){c.group=group;cap.replaceChildren();var g=c.groups[group];for(var j=g[0];j<g[1];j++){var x=document.createElement('span');x.textContent=c.words[j];x.dataset.word=j;cap.appendChild(x)}}
 [].forEach.call(cap.children,function(x){var k=+x.dataset.word;x.className=k<index?'rd':k===index?'cu':''});c.idx=index;
}
function capTick(dt){
 var c=cs2;if(!c||!play)return;
 if(c.recorded){
  if(c.sourceStarted&&!c.complete)c.clock=Math.max(0,AC.currentTime-c.playedAt);else if(!c.waiting)c.clock+=dt;
  if(!c.complete){var index=wordAt(c,c.clock);if(index!==c.idx)paintWords(c,index);if(c.clock>=c.dur&&!c.waiting){c.complete=true;activeVoice=false;paintWords(c,c.words.length)}}
 }else{
  c.clock+=dt;if(activeVoice&&c.clock>c.dur*2.5+5){activeVoice=false;c.complete=true}
  if(!c.boundaries&&!c.complete){var f=Math.min(.985,c.clock/c.dur),k=0;for(var i=0;i<c.starts.length;i++)if(c.starts[i]/c.spoken.length<=f)k=i;if(k!==c.idx)paintWords(c,k)}
  if(!activeVoice&&!c.complete&&c.clock>=c.dur){c.complete=true;paintWords(c,c.words.length)}
 }
 st.dataset.narration=c.recorded?'recorded':'system';st.dataset.voiceClock=c.clock.toFixed(3);st.dataset.voiceActive=activeVoice;
 if(c.complete){c.hold+=dt;if(c.hold>.6)cap.style.opacity=0}
}
function say(txt,p,r){
 var sourceText=txt;txt=tr(txt);
 stopRecorded();
 var rate=(r||.98)*.99,words=txt.trim().split(/\s+/),starts=[],spoken='';
 words.forEach(function(w){starts.push(spoken.length);spoken+=(i18n.lang==='ru'?w.replace(/^LV$/,'Эл Ви').replace(/^Copy$/,'Копи').replace(/^Trader([.,!?]?)$/,'Трейдер$1').replace(/waitlist/i,'списку ожидания'):w)+' '});
 var me=cs2={words:words,starts:starts,spoken:spoken,groups:chunks(words),group:-1,clock:0,dur:spoken.length/(13*rate)+.3,boundaries:false,complete:false,hold:0,idx:-1};
 cap.style.opacity=1;paintWords(me,0);
 var track=recordedVoice?narration[sourceText]:null;if(track){me.recorded=true;me.track=track;me.dur=track.duration;me.boundaries=true;if(voiceOn)playRecorded(me,0);return}
 if(voiceOn&&window.speechSynthesis&&vlist.length){
  speechSynthesis.cancel();activeVoice=true;var u=new SpeechSynthesisUtterance(spoken);u.lang=i18n.locale;if(vc)u.voice=vc;u.pitch=1+((p||1)-1)*.5;u.rate=rate;
  u.onstart=function(){if(cs2===me){me.clock=0;paintWords(me,0)}};
  u.onboundary=function(e){if(cs2!==me||(e.name&&e.name!=='word'))return;me.boundaries=true;var k=0;for(var i=0;i<me.starts.length;i++)if(me.starts[i]<=e.charIndex)k=i;paintWords(me,k)};
  u.onend=u.onerror=function(){if(cs2===me){activeVoice=false;me.complete=true;paintWords(me,me.words.length)}};
  speechSynthesis.speak(u);
 }else activeVoice=false;
}

/* ---------- quiet type reveals and numeric reels ---------- */
var typed=[];
document.querySelectorAll('h2.a,#delay-question,section:last-child h1.a').forEach(function(e){
 e.setAttribute('aria-label',e.textContent.trim());var nodes=[],walker=document.createTreeWalker(e,NodeFilter.SHOW_TEXT),n;
 while(n=walker.nextNode())if(n.textContent.trim())nodes.push(n);
 var chars=[];nodes.forEach(function(node){var frag=document.createDocumentFragment();Array.from(node.textContent).forEach(function(c){if(c===' '){frag.appendChild(document.createTextNode(' '));return}var span=document.createElement('span');span.className='typed-char';span.setAttribute('aria-hidden','true');span.textContent=c;frag.appendChild(span);chars.push(span)});node.replaceWith(frag)});
 typed.push({node:e,scene:e.closest('.s'),chars:chars,delay:parseFloat(e.style.getPropertyValue('--d'))||0});
});
function clamp(v){return Math.max(0,Math.min(1,v))}
function smooth(v){v=clamp(v);return v*v*(3-2*v)}
function typeTick(t){typed.forEach(function(item){if(item.scene!==sc[cur])return;var progress=Math.max(0,t-item.delay)*30;item.chars.forEach(function(c,i){var f=clamp(progress-i);c.style.opacity=f;c.style.filter='blur('+((1-f)*1.5)+'px)';c.classList.toggle('caret',f>.5&&i+1>progress&&progress<item.chars.length)})})}
function digits(node,value,instant){
 if(!node)return;value=String(value);if(node.dataset.value===value)return;node.dataset.value=value;node.setAttribute('aria-label',value);node.classList.add('rolling-number');
 var structure=value.replace(/\d/g,'#');
 if(node.dataset.structure!==structure){node.dataset.structure=structure;node.replaceChildren();Array.from(value).forEach(function(c){var slot=document.createElement('span');slot.setAttribute('aria-hidden','true');if(/\d/.test(c)){slot.className='number-slot';var reel=document.createElement('span');reel.className='number-reel';for(var k=0;k<10;k++){var v=document.createElement('i');v.textContent=k;reel.appendChild(v)}slot.appendChild(reel)}else{slot.className='number-punct';slot.textContent=c}node.appendChild(slot)})}
 Array.from(value).forEach(function(c,i){if(!/\d/.test(c))return;var reel=node.children[i].firstChild;reel.style.transition=instant?'none':'';reel.style.transform='translateY(-'+(+c*1.08)+'em)'});
}
/* ---------- timeline: [scene, time, fn, a, b] ---------- */
var L=[
[0,0,'whoosh'],[0,0,'pad','tense'],[0,.15,'say','Канал под присмотром круглосуточно.',1.05,.98],[0,2.3,'thud'],[0,3.1,'say','Рынок вас ждать не будет.',1,.98],
[1,.1,'thud'],[1,.2,'say','Не успели? Не были онлайн?',1,.98],[1,3.4,'say','Вот сигнал. Отметим цену входа.',1,.98],[1,7.8,'say','Цена идёт дальше. А вы всё ещё не в сети.',1,1],[1,12.7,'say','Две минуты. Вы открыли канал.',1,1],[1,16.2,'say','Вход, стоп и цель. Теперь видно разницу.',1,1],[1,32.5,'say','Знакомо? Тогда смотрите.',1.04,.98],
[2,0,'riser'],[2,.1,'pad','bright'],[2,.2,'say','А если сигнал скопируется раньше, чем вы его дочитаете?',1.05,1],[2,5.3,'say','Сигналы из вашего канала — сразу на ваш счёт.',1.05,.98],
[3,.1,'whoosh'],[3,.2,'say','Копируйте из канала, которому доверяете.',1,1],[3,2.5,'say','Система распознаёт сигнал.',1,1],[3,4.5,'say','Сделка открывается в MetaTrader.',1,1],[3,7.6,'say','Бот присылает уведомление.',1.05,1],
[4,.1,'whoosh'],[4,.1,'pad','end'],[4,.3,'say','Хаос в постах? ИИ найдёт сигнал.',1,1],[4,3.6,'say','«Доброе утро» в сделку не превратится.',1.04,1],[4,6.9,'say','Единый формат? Копируйте по шаблону, без ИИ.',1.02,1],
[5,.1,'whoosh'],[5,.3,'say','Переносить параметры вручную больше не нужно.',1,1],[5,3.2,'say','Защита и история — в том же процессе.',1,1],
[6,.1,'whoosh'],[6,.1,'pad','bright'],[6,.2,'say','А теперь тот же сигнал — с ботом.',1.05,1],[6,3.6,'say','Сигнал распознан. Позиция уже открыта.',1,1],[6,7.3,'say','Стоп и цель установлены. Вам пришло уведомление.',1,1],[6,12,'say','Цена идёт дальше. А вы уже в сделке.',1.03,1],
[7,.1,'whoosh'],[7,.4,'say','Вы смотрите сериал. Бот смотрит канал.',1.03,.98],[7,3.6,'say','Канал под присмотром круглосуточно.',1,1],
[8,.12,'outroTone'],[8,.9,'say','LV Copy Trader. Скоро.',1,.98]
];

/* ---------- one continuous candle engine, one background camera ---------- */
// Illustrative XAUUSD data. Same candle duration and sampling before/after a signal.
var cv=document.getElementById('bg'),cx=cv.getContext('2d'),chartLayer=document.createElement('canvas'),chartCx=chartLayer.getContext('2d'),W=432,H=768,GB=4133.48,CW=14,PER=window.LVMarketSim.TIMING.candleSeconds,RATE=window.LVMarketSim.TIMING.rate,camX=0,camY=0,vel=0,lastPrice=GB,bias=0,snapCamera=true;
var view={scaleX:1,scaleY:H*.14},seedOption=new URLSearchParams(location.search).get('seed'),rngState=seedOption===null?window.LVMarketSim.randomSeed():(+seedOption>>>0),runPhase=0,runOffset=0,trade=null,botTrade=null,simulation=window.LVMarketSim.create(rngState),frozenMarket=false,humanHold=0,botHold=0;
var resultCard=window.LVMarketSim.resultCard(vp),telegramNotice=window.LVMarketSim.telegramNotice(vp);
var botStart=D.slice(0,6).reduce(function(a,b){return a+b},0),watchStart=botStart+D[6],outroStart=watchStart+D[7];
[6,7,8].forEach(function(k){sc[k].classList.add("tail-scene")});
function randomRun(){rngState=(Math.imul(1664525,rngState)+1013904223)>>>0;return rngState/4294967296}
function baseMarket(t){return simulation.ambient(t)}
// Place the trade where the viewer hears it, not several candles before the reveal.
function cueWordTime(scene,text,word,fallback){
 var cue=L.find(function(c){return c[0]===scene&&c[2]==='say'&&c[3]===text}),track=narration[text];
 return (cue?cue[1]:fallback)+(track&&track.starts[word]!==undefined?track.starts[word]:0);
}
var humanMarkAt=cueWordTime(1,'Вот сигнал. Отметим цену входа.',i18n.lang==='en'?2:1,3.4);
var reactionMarkAt=humanMarkAt+window.LVFilmClock.observation;
var observedCueAt=reactionMarkAt+.2;
L=L.filter(function(c){return !(c[0]===1&&c[2]==='say'&&(c[3]==='Вход, стоп и цель. Теперь видно разницу.'||c[3]==='Знакомо? Тогда смотрите.'))});
L.forEach(function(c){if(c[0]===1&&c[2]==='say'&&c[3]==='Две минуты. Вы открыли канал.')c[1]=observedCueAt});

var botSignalAt=cueWordTime(6,'Сигнал распознан. Позиция уже открыта.',0,3.6),botMarkAt=botSignalAt+.08/.9;
var botRoute=window.LVSignalRoute.plan(botMarkAt);
L.push([6,botRoute.telegram,'telegramTone'],[6,botRoute.bot,'recognizedTone'],[6,botRoute.mt,'executedTone']);
[1.85,3.25,5.25,7.45].forEach(function(at,k){L.push([3,at,'routeCardTone',k])});
function markerReveal(t,at){return smooth((t-at)/.38)}
var humanPlan=null,botPlan=null,botSceneWorld=0;
function prepareTrade(){
 simulation=window.LVMarketSim.create(Math.floor(randomRun()*4294967296));frozenMarket=false;
 var at=(D[0]+humanMarkAt)*RATE;trade=simulation.newTrade({at:at});var entry=trade.entry,dir=trade.dir;
 document.getElementById('signal-side').textContent=dir>0?'BUY':'SELL';document.getElementById('chart-side').textContent=dir>0?'BUY':'SELL';
 digits(document.getElementById('signal-entry'),entry.toFixed(2));digits(document.getElementById('signal-sl'),trade.sl.toFixed(2));digits(document.getElementById('signal-tp'),trade.tp.toFixed(2));
 aiFeed.querySelector('.ok').textContent=(dir>0?'BUY':'SELL')+' XAUUSD · '+entry.toFixed(2);parseResult.replaceChildren();[['ENTRY',entry],['SL',trade.sl],['TP',trade.tp]].forEach(function(v){var chip=document.createElement('span');chip.textContent=v[0]+' '+v[1].toFixed(2);parseResult.append(chip)});
 var scene=document.getElementById('delay-scene');Object.keys(trade).forEach(function(k){if(typeof trade[k]!=="object")scene.dataset[k]=trade[k]});
 document.querySelectorAll('.notif').forEach(function(n){n.innerHTML=n.innerHTML.replace(/XAUUSD · (BUY|SELL) · [\d.]+/,'XAUUSD · '+(dir>0?'BUY':'SELL')+' · '+entry.toFixed(2)).replace(/Stop Loss: [\d.]+/,'Stop Loss: '+trade.sl.toFixed(2)).replace(/Take Profit: [\d.]+/,'Take Profit: '+trade.tp.toFixed(2))});
 humanPlan=window.LVFilmClock.plan(trade,humanMarkAt,RATE,1.33);D[1]=humanPlan.end;
 botStart=D.slice(0,6).reduce(function(a,b){return a+b},0);
 botSceneWorld=trade.endAt+D.slice(2,6).reduce(function(a,b){return a+b},0)*RATE;
 botTrade=simulation.newTrade({at:botSceneWorld+botMarkAt*RATE});
 botPlan=window.LVFilmClock.plan(botTrade,botMarkAt,RATE,1.15);D[6]=botPlan.end;
 watchStart=botStart+D[6];outroStart=watchStart+D[7];T=D.reduce(function(a,b){return a+b},0);
 seek.max=T;document.getElementById('total-time').textContent=clockLabel(T);
 document.getElementById('bot-side').textContent=(botTrade.dir>0?'BUY':'SELL')+' · XAUUSD';digits(document.getElementById('bot-entry'),botTrade.entry.toFixed(2),true);
 snapCamera=true;
}
function market(t){var human=el>=D[0]&&el<D[0]+D[1],bot=el>=botStart&&el<botStart+D[6];return simulation.price(activeTrade(),t,human||bot)}
function activeTrade(){return el>=botStart?botTrade:trade}
function rawChartClock(){
 if(el<D[0])return el*RATE;
 if(el<D[0]+D[1])return humanPlan.clock(el-D[0]);
 if(el<botStart)return trade.endAt+(el-D[0]-D[1])*RATE;
 if(el<watchStart)return botPlan.clock(el-botStart);
 if(el<outroStart)return botTrade.endAt+(el-watchStart)*RATE;
 var u=Math.max(0,Math.min(3,el-outroStart));
 return botTrade.endAt+(outroStart-watchStart)*RATE+RATE*(u-u*u*u/9+u*u*u*u/54);
}
function chartClock(){var raw=rawChartClock(),human=el>=D[0]&&el<D[0]+D[1],bot=el>=botStart&&el<botStart+D[6],active=activeTrade();return active&&(human||bot)?Math.min(raw,active.endAt):raw}
function detailPhase(){
 var bt=el-botStart;if(bt>=0&&bt<D[6]){var a=smooth((bt-.2)/1.4),leave=smooth((bt-(D[6]-.45))/.45);return{bot:true,arrive:a,leave:leave,focus:a*(1-leave),reaction:0,zoom:smooth((bt-7)/3)*(1-leave),t:bt}}
 var t=el-D[0];if(t<0||t>D[1])return{arrive:0,leave:0,focus:0,reaction:0,zoom:0,t:t};
 var a=smooth((t-3.2)/.8),o=smooth((t-(D[1]-.45))/.45);
 return{arrive:a,leave:o,focus:a*(1-o),reaction:smooth((t-reactionMarkAt)/.7)*(1-o),zoom:smooth((t-6.5)/3.1)*(1-o),t:t};
}
function layout(){return window.LVMarketSim.chartLayout(W,H,simulation.targetReward,Math.min(innerWidth/432,innerHeight/768))}
function marketScale(){return layout().scaleY;}
function wy(price){return -(price-GB)*view.scaleY}
function worldX(time){return time/PER*CW*view.scaleX-camX}
function worldY(price){return wy(price)-camY}
function cameraTargets(now,phase){
 var price=market(now),frame=layout(),zoom=1+.5*(1-phase.focus),sy=marketScale()*zoom,active=activeTrade(),liveY=-(price-GB)*sy-frame.center,fixedY=-((active.sl+active.tp)/2-GB)*sy-frame.center;
 return{x:now/PER*CW*zoom-W*.68,y:liveY+(fixedY-liveY)*phase.focus,scaleX:zoom,scaleY:sy};
}
var chartLabelsKey='';
function positionChartLabels(){
 var scale=Math.min(innerWidth/432,innerHeight/768),by=(H-768)/2,panel=document.getElementById('player-panel'),open=panel&&!panel.hidden,key=W+':'+H+':'+open;
 if(key===chartLabelsKey)return;chartLabelsKey=key;
 var frame=layout();
 vp.style.setProperty('--timewarp-top',(frame.top*scale-44)+'px');
 st.style.setProperty('--chart-meta-top',(frame.top-74-by)+'px');
 st.style.setProperty('--human-summary-top',(H-(open?220:165)/scale-by)+'px');
 st.style.setProperty('--bot-summary-top',(H-(open?220:130)/scale-by)+'px');
}
function rs(){var scale=Math.min(innerWidth/432,innerHeight/768);st.style.transform='scale('+scale+')';W=innerWidth/scale;H=innerHeight/scale;var by=(H-768)/2,safe=parseFloat(getComputedStyle(document.getElementById('top-chrome')).paddingTop)||0;st.style.setProperty('--title-top',((safe+22)/scale-by)+'px');st.style.setProperty('--readout-top',((safe+58)/scale-by)+'px');st.style.setProperty('--note-top',((safe+145)/scale-by)+'px');var d=Math.min(devicePixelRatio||1,2)*scale;cv.width=Math.round(W*d);cv.height=Math.round(H*d);cx.setTransform(d,0,0,d,0,0);chartLayer.width=cv.width;chartLayer.height=cv.height;chartCx.setTransform(d,0,0,d,0,0);snapCamera=true}
function drawBackground(dt){
 positionChartLabels();
 var cx=chartCx;
 var now=chartClock(),price=market(now),phase=detailPhase(),target=cameraTargets(now,phase),ease=snapCamera?1:dt?1-Math.exp(-dt*3.5):0;
 var closed=(cur===1||cur===6)&&now>=activeTrade().endAt;if(!frozenMarket||!closed||snapCamera||phase.leave>0){view.scaleX=target.scaleX;view.scaleY=target.scaleY;camX+=(target.x-camX)*ease;camY=phase.focus>.999?target.y:camY+(target.y-camY)*ease;}frozenMarket=closed;snapCamera=false;
 vel=price-lastPrice;lastPrice=price;cx.globalAlpha=1;cx.clearRect(0,0,W,H);cx.save();var plot=layout();cx.lineWidth=1;cx.strokeStyle='rgba(255,255,255,.045)';
 var stepRaw=65/view.scaleY,stepPrice=stepRaw<1?1:stepRaw<2?2:stepRaw<5?5:10;
 var high=GB-camY/view.scaleY+50/view.scaleY,low=GB-(H+camY)/view.scaleY-50/view.scaleY;
 for(var p=Math.floor(low/stepPrice)*stepPrice;p<=high;p+=stepPrice){var yy=worldY(p);cx.beginPath();cx.moveTo(0,yy);cx.lineTo(W,yy);cx.stroke()}
 var maxBody=0,first=Math.floor((camX-50)/(CW*view.scaleX))-1,current=Math.floor(now/PER);
 for(var gx=Math.floor(first/3)*3;gx<=current+30;gx+=3){var xx=worldX(gx*PER);if(xx>W+50)break;cx.beginPath();cx.moveTo(xx,0);cx.lineTo(xx,H);cx.stroke()}
 for(var k=first;k<=current;k++){
  var from=k*PER,to=Math.min((k+1)*PER,now),o=market(from),c=market(to),hi=Math.max(o,c),lo=Math.min(o,c);
  for(var q=from;q<to;q+=window.LVMarketSim.TIMING.tickSeconds){var v=market(q);hi=Math.max(hi,v);lo=Math.min(lo,v)}
  var x=worldX(from),width=CW*view.scaleX,m=x+width/2,yo=worldY(o),yc=worldY(c),up=c>=o,bw=Math.max(3,8*view.scaleX);
  if(x>-CW&&x<W&&Math.max(yo,yc)>0&&Math.min(yo,yc)<H)maxBody=Math.max(maxBody,Math.abs(yc-yo));
  cx.strokeStyle=k===current?'rgba(142,197,255,.85)':up?'rgba(237,237,237,.38)':'rgba(237,237,237,.27)';cx.lineWidth=1.5;
  cx.beginPath();cx.moveTo(m,worldY(hi));cx.lineTo(m,worldY(lo));cx.stroke();
  cx.fillStyle=up?'#030507':'rgba(237,237,237,.20)';cx.fillRect(m-bw/2,Math.min(yo,yc),bw,Math.max(2,Math.abs(yc-yo)));cx.strokeRect(m-bw/2,Math.min(yo,yc),bw,Math.max(2,Math.abs(yc-yo)));
 }
 st.dataset.maxBodyHeight=(maxBody*Math.min(innerWidth/432,innerHeight/768)).toFixed(2);
 var y=worldY(price);cx.setLineDash([4,5]);cx.strokeStyle='rgba(116,189,255,.30)';cx.beginPath();cx.moveTo(0,y);cx.lineTo(W,y);cx.stroke();cx.setLineDash([]);
 cx.fillStyle='#9acaff';cx.fillRect(W-61,y-10,59,20);cx.fillStyle='#07121d';cx.font='500 10px "IBM Plex Mono",monospace';cx.textAlign='center';cx.textBaseline='middle';cx.fillText(price.toFixed(2),W-31,y);cx.textAlign='left';cx.textBaseline='alphabetic';
 cx.restore();window.LVMarketSim.compositeChart(cv.getContext('2d'),chartLayer,{width:W,height:H,layout:plot,focus:phase.focus});drawOverlay(now,phase);st.dataset.overflow='blurred';
 var scene=document.getElementById('delay-scene');scene.dataset.zoom=phase.zoom.toFixed(3);scene.dataset.scaleX=view.scaleX.toFixed(4);scene.dataset.scaleY=view.scaleY.toFixed(4);scene.dataset.clock=now.toFixed(3);scene.dataset.candleSeconds=PER;scene.dataset.markerAt=humanMarkAt.toFixed(3);scene.dataset.markerReveal=(!phase.bot?markerReveal(phase.t,humanMarkAt):0).toFixed(3);document.getElementById('bot-scene').dataset.markerAt=botMarkAt.toFixed(3);document.getElementById('bot-scene').dataset.markerReveal=(phase.bot?markerReveal(phase.t,botMarkAt):0).toFixed(3);
 return price.toFixed(2);
}
function roundedRect(ctx,x,y,w,h,r){ctx.beginPath();ctx.roundRect(x,y,w,h,r)}
function dot(x,y,color){cx.fillStyle=color;cx.shadowColor=color;cx.shadowBlur=12;cx.beginPath();cx.arc(x,y,4.5,0,Math.PI*2);cx.fill();cx.shadowBlur=0}
// A single, finite emphasis: expanding rings, a growing level and a firm entry dot.
function entryAccent(x,y,age,color){
 if(age<0)return;
 var pulse=smooth(age/.18)*(1-smooth((age-.35)/1.25));
 if(pulse>0){
  cx.save();cx.globalAlpha*=pulse;cx.strokeStyle=color;cx.lineWidth=1.8;
  [0,.3].forEach(function(delay){var u=clamp((age-delay)/1.35);if(age<delay)return;cx.beginPath();cx.arc(x,y,7+29*u,0,Math.PI*2);cx.stroke()});
  cx.restore();
 }
 cx.save();cx.shadowColor=color;cx.shadowBlur=18;cx.fillStyle=color;cx.beginPath();cx.arc(x,y,4.5+3*pulse,0,Math.PI*2);cx.fill();cx.restore();
}
function drawOverlay(now,phase){
 var active=activeTrade(),at=phase.bot?botMarkAt:humanMarkAt,age=phase.t-at,reveal=markerReveal(phase.t,at);
 var result=window.LVMarketSim.drawTradeOverlay(cx,{width:W,height:H,trade:active,layout:layout(),worldX:worldX,worldY:worldY,price:market(now),opacity:phase.focus*reveal,age:age,protectedLevel:phase.bot?smooth((age-1.1)/.65):1,reaction:smooth((phase.t-at-window.LVFilmClock.observation)/.7),bot:!!phase.bot,pulse:entryAccent});
 if(result)Object.keys(result).forEach(function(key){st.dataset[key]=(result[key]*Math.min(innerWidth/432,innerHeight/768)).toFixed(2)});
}
function botMotion(t){
 var route=window.LVSignalRoute.state(t,botMarkAt),received=route.received,recognized=route.recognized,executed=route.executed,protectedLevel=smooth((t-botMarkAt-1.1)/.65),leave=smooth((t-(D[6]-2))/2),e=document.getElementById('bot-scene');
 e.style.setProperty('--received',received);e.style.setProperty('--recognized',recognized);e.style.setProperty('--executed',executed);e.style.setProperty('--protected',protectedLevel);e.style.setProperty('--bot-leave',leave);e.style.setProperty('--route',route.route);e.style.setProperty('--packet',route.packet);e.dataset.routePhase=route.phase;document.getElementById('bot-packet').setAttribute('cx',40+264*route.route);
 [['port-telegram',received,tr('Получен'),tr('Сигнал')],['port-bot',recognized,tr('Распознан'),tr('Распознаёт')],['port-mt',route.mtReady,tr('Открыто ✓'),tr('Открывает')]].forEach(function(v,k){var n=document.getElementById(v[0]);n.style.setProperty('--ready',v[1]);n.style.setProperty('--flash',route.flashes[k]);var label=n.querySelector('small');label.textContent=v[1]>.9?v[2]:v[3];label.classList.toggle('done',v[1]>.9)});
 var values=window.LVMarketSim.amounts(botTrade,market(chartClock()));digits(document.getElementById('bot-distance'),window.LVMarketSim.formatMoney(values.floating,true),!play);e.dataset.entry=botTrade.entry;e.dataset.opened=executed.toFixed(3);e.dataset.protected=protectedLevel.toFixed(3);e.dataset.clock=chartClock().toFixed(3);
}
function watchMotion(t){var e=document.getElementById('watch-orbit'),still=matchMedia('(prefers-reduced-motion: reduce)').matches;e.style.setProperty('--angle',(still?0:motionClock*24)+'deg');e.style.setProperty('--orbit-tilt',(still?0:12+Math.sin(motionClock*.37)*7)+'deg');var minutes=23*60+59+Math.floor(t*.12);digits(document.getElementById('night-time'),p2(Math.floor(minutes/60)%24)+':'+p2(minutes%60),!play)}
// Cache authored timings once; no independent CSS entrance clock can run ahead.
var sceneReveals=sc.map(function(scene){return Array.from(scene.querySelectorAll('.a,.qq')).map(function(e){
 return {node:e,delay:parseFloat(e.style.getPropertyValue('--d'))||0,
 out:e.classList.contains('qq')?(parseFloat(e.style.getPropertyValue('--o'))||4):Infinity,
 duration:e.classList.contains('q')?.5:.8};
})});
function sceneMotionClock(t,i){
 sc.forEach(function(scene,k){
  var opacity=0;if(k===i){opacity=smooth(t/(k===0?.4:.65));if(k!==8)opacity*=1-smooth((t-(D[k]-.45))/.45)}
  scene.style.opacity=opacity;scene.style.visibility=k===i?'visible':'hidden';scene.dataset.sceneOpacity=opacity.toFixed(4);
 });
 sceneReveals[i].forEach(function(item){item.node.style.opacity=smooth((t-item.delay)/item.duration)*(1-smooth((t-item.out)/.5))});
}
function endingMotion(t,i){
 var outro=i===8?smooth(t/1.6):0;
 vp.style.setProperty('--outro-focus',outro);
 var rewind=1;
 cv.style.opacity=(.78+.16*detailPhase().focus)*rewind*(1-outro*.68);
}
function updatePresentation(t,i){
 vp.dataset.presentationScene=String(i);
 var plan=i===1?humanPlan:i===6?botPlan:null,warp=document.getElementById('timewarp-indicator'),warpOpacity=plan?plan.indicator(t):0,warpSpeed=plan?plan.speed(t):1;
 warp.style.opacity=String(warpOpacity);warp.style.setProperty('--warp-running',play?'running':'paused');warp.setAttribute('aria-hidden',String(warpOpacity<=0));warp.setAttribute('aria-label',tr('Ускорение времени'));warp.querySelector('span').textContent='×10';warp.style.transform='translateY('+((1-warpOpacity)*5).toFixed(2)+'px)';
 st.dataset.timeScale=warpSpeed.toFixed(4);
 var rawNow=rawChartClock(),active=activeTrade(),resultReveal=Math.max(resultCard.update(active,rawNow,i===6),telegramNotice.update(active,rawNow,i===1));vp.style.setProperty("--trade-result",resultReveal);st.style.setProperty("--chart-summary-visible",1-smooth((rawNow-active.endAt)/(RATE*.2)));
 var phase=detailPhase(),now=chartClock(),passed=Math.round(clamp((now-trade.at)/(trade.reactionAt-trade.at))*120),price=market(now),scene=document.getElementById('delay-scene');
 vp.style.setProperty('--chart-focus',phase.focus);st.style.setProperty('--chart-focus',phase.focus);scene.style.setProperty('--inspect',phase.arrive);scene.style.setProperty('--inspect-out',phase.leave);scene.style.setProperty('--chart-focus',phase.focus);scene.style.setProperty('--reaction',phase.reaction);
 scene.style.setProperty('--signal-in',smooth((t-.2)/.65));scene.style.setProperty('--stats-in',smooth((t-1)/.65));
 scene.dataset.elapsed=passed;scene.dataset.current=price.toFixed(2);scene.dataset.focus=phase.focus.toFixed(3);
 cv.style.filter='blur('+(1.1*(1-phase.focus))+'px)';cv.style.transform='none';cv.style.opacity=.78+.16*phase.focus;
 var vs=Math.min(innerWidth/432,innerHeight/768),by=(H-768)/2,edge=1-phase.focus,mask='linear-gradient(to bottom,rgba(0,0,0,'+edge+') 0px,rgba(0,0,0,'+edge+') '+((by+205)*vs)+'px,#000 '+((by+255)*vs)+'px,#000 '+((by+478)*vs)+'px,rgba(0,0,0,'+edge+') '+((by+545)*vs)+'px,rgba(0,0,0,'+edge+') '+innerHeight+'px)';
 if(phase.bot)mask='linear-gradient(to bottom,rgba(0,0,0,'+edge+') 0px,rgba(0,0,0,'+edge+') '+((by+150)*vs)+'px,#000 '+((by+180)*vs)+'px,#000 '+((by+438)*vs)+'px,rgba(0,0,0,'+edge+') '+((by+477)*vs)+'px,rgba(0,0,0,'+edge+') '+innerHeight+'px)';
 cv.style.maskImage='none';cv.style.webkitMaskImage='none';
 document.getElementById('vig').style.opacity=1-phase.focus;document.getElementById('grain').style.opacity=1-phase.focus;
 if(i===1){digits(tm,p2(Math.floor(passed/60))+':'+p2(passed%60),!play);digits(pr,price.toFixed(2),!play);var values=window.LVMarketSim.amounts(trade,price);digits(document.getElementById('trade-distance'),window.LVMarketSim.formatMoney(values.missed),!play);digits(document.getElementById('trade-remaining'),window.LVMarketSim.formatMoney(values.remaining),!play)}
 var ticker=document.getElementById("tk"),band=ticker.getBoundingClientRect(),hide=(i===1||i===6)?smooth((phase.focus-.15)/.4):0;sc[i].querySelectorAll("h1,h2,.node,.card,.cap").forEach(function(n){var r=n.getBoundingClientRect();if(r.bottom>band.top-12&&r.top<band.bottom+24&&+getComputedStyle(n).opacity>.05)hide=Math.max(hide,smooth((band.bottom+24-r.top)/32));});ticker.style.opacity=String(1-hide);
 st.dataset.marketClosed=String((i===1||i===6)&&rawNow>=active.endAt);st.dataset.marketSeed=simulation.seed;st.dataset.marketPrice=price.toFixed(2);st.dataset.marketTime=now.toFixed(4);st.dataset.marketSide=active.dir>0?"BUY":"SELL";st.dataset.marketOutcome=(i===1||i===6)&&rawNow>=active.endAt?active.outcome:"";
 if(i===6)botMotion(t);if(i===7)watchMotion(t);sceneMotionClock(t,i);typeTick(t);sceneMotion(t,i);filmMotion.render(i,t,motionClock,play);endingMotion(t,i);syncMusicEnvelope(false);
}
function draw(dt){return drawBackground(dt)}
prepareTrade();rs();addEventListener('resize',rs);
/* ---------- engine ---------- */
function schedSfx(i){if(!AC||!music||!play)return;L.forEach(function(c,k){if(c[0]===i&&c[2]!=='say'&&!/Tone$/.test(c[2])){fired[k]=1;if(c[2]==='pad')FX.pad(c[3]);else FX[c[2]](Math.max(0,c[1]-sT))}});
if(i!==3)sc[i].querySelectorAll('.node,.card,.cap,.notif,.bubble,.stat,.deal,.btn').forEach(function(e,j){FX.pop(Math.max(0,(parseFloat(e.style.getPropertyValue('--d'))||0)-sT),j)})}
function show(i){sc.forEach(function(s,k){s.classList.toggle('on',k===i)});cur=i;bias=i<2?.000008:0;fired=[];if(i===8)en=0;stopRecorded();cs2=null;activeVoice=false;cap.style.opacity=0;if(window.speechSynthesis)speechSynthesis.cancel();schedSfx(i)}
function fire(){for(var k=0;k<L.length;k++){var c=L[k];if(c[0]===cur&&!fired[k]&&sT>=c[1]){fired[k]=1;if(c[2]==='say')say(c[3],c[4],c[5]);else FX[c[2]](c[3])}}}
function p2(n){return(n<10?'0':'')+n}
function tick(n){var dt=Math.min(.1,(n-last)/1000);last=n;
if(play&&!done){
 motionClock+=dt;
 var proposed=el+dt;
 // Let the voice finish before the next phrase or scene: never cut off words.
 if(activeVoice&&cur>=0){var base=0;for(var j=0;j<cur;j++)base+=D[j];var barrier=base+D[cur];L.forEach(function(c,k){if(c[0]===cur&&c[2]==='say'&&!fired[k])barrier=Math.min(barrier,base+c[1])});proposed=Math.min(proposed,Math.max(el,barrier-.002))}
 el=proposed;if(el>=T){el=T;done=true;play=false;st.classList.add('paused');en=0;ctl.textContent=tr('Повторить ↺')}
}
var t=el,i=0;while(i<D.length-1&&t>=D[i]){t-=D[i];i++}
sT=t;if(started&&el>=0&&i!==cur)show(i);updatePresentation(t,i);var lp=draw(play?dt:0);
if(started&&play&&el>=0){fire();capTick(dt);idleT+=dt;if(idleT>2.5)st.classList.add('idle')}
if(cur===0){digits(hp,lp,!play);hp.style.color=vel>=0?'':'#ff5b5b'}
bar.style.width=Math.max(0,el/T*100)+'%';syncPlayer();requestAnimationFrame(tick)}
function fadeIn(){return;fade.style.transition='none';fade.style.opacity=1;fade.offsetHeight;fade.style.transition='';fade.style.opacity=0}
var bm=document.getElementById('tm2'),bv=document.getElementById('tv2'),mm=document.getElementById('mm'),mv=document.getElementById('mv');
function lab(){bm.textContent='♪ '+tr('Музыка')+': '+tr(music?'вкл':'выкл');bv.textContent='🎙 '+tr('Озвучка')+': '+tr(voiceOn?'вкл':'выкл');mm.style.opacity=music?1:.4;mv.style.opacity=voiceOn?1:.4}
function togM(e){e.stopPropagation();music=!music;if(music&&started)initAudio();if(music&&play&&cur===8)FX.outroTone(sT);syncMusicEnvelope(true);lab()}
function togV(e){e.stopPropagation();voiceOn=!voiceOn;if(!voiceOn)stopRecorded();else if(play&&cs2&&cs2.recorded&&!cs2.complete)playRecorded(cs2,cs2.clock);if(!voiceOn&&window.speechSynthesis){speechSynthesis.cancel();activeVoice=false;if(cs2){cs2.boundaries=false;cs2.dur=Math.max(cs2.dur,cs2.clock+.8)}}setBus();lab()}
bm.onclick=mm.onclick=togM;bv.onclick=mv.onclick=togV;sel.onclick=function(e){e.stopPropagation()};
document.getElementById('go').onclick=function(e){e.stopPropagation();if(started)prepareTrade();motionClock=0;filmMotion.seek(0);started=true;play=true;done=false;cur=-1;el=LEAD;document.getElementById('start').style.display='none';document.getElementById('hud').style.display='flex';
if(music||voiceOn)initAudio();if(AC&&AC.state==='suspended')AC.resume();if(!recordedVoice&&voiceOn&&window.speechSynthesis){var u=new SpeechSynthesisUtterance(' ');u.volume=0;speechSynthesis.speak(u)}
st.classList.remove('paused');seeked=false;fadeIn();last=performance.now();syncPlayer()};
lab();i18n.initFilmLanguage();
function clockLabel(seconds){seconds=Math.floor(seconds);return p2(Math.floor(seconds/60))+':'+p2(seconds%60)}
function syncPlayer(){
 panel.hidden=!started||(play&&!done);ctl.hidden=!started||!play||done||st.classList.contains('idle');vp.classList.toggle('paused',!play);
 if(!seek.matches(':active'))seek.value=el;seek.style.setProperty('--progress',(el/T*100)+'%');seek.setAttribute('aria-valuetext',clockLabel(el)+tr(' из ')+clockLabel(T));
 var label=clockLabel(el);if(document.getElementById('elapsed-time').textContent!==label)document.getElementById('elapsed-time').textContent=label;
 resumeButton.textContent=done?'↺':'▶';resumeButton.setAttribute('aria-label',done?tr('Смотреть заново'):tr('Продолжить'));
}
function togglePlayback(){
 idleT=0;st.classList.remove('idle');if(!started)return;
 if(done){prepareTrade();motionClock=0;filmMotion.seek(0);el=LEAD;done=false;cur=-1;step=0;snapCamera=true;sc.forEach(function(s){s.classList.remove('on')});play=false;seeked=false}
 play=!play;st.classList.toggle('paused',!play);ctl.textContent=tr('Нажмите — пауза');
 var resumeEnding=play&&seeked&&cur===8;
 if(play&&seeked){var pad=L.filter(function(c){return c[2]==='pad'&&c[0]<=cur}).pop();en=cur===8?0:({tense:1,mid:2,bright:2,end:3})[pad&&pad[3]]||0;if(AC)nextT=AC.currentTime+.1;if(cs2&&!cs2.complete&&voiceOn){if(cs2.recorded)playRecorded(cs2,cs2.clock);else{var remaining=cs2.words.slice(Math.max(0,cs2.idx)).join(' ');say(remaining,1,1)}}seeked=false}
 if((music||voiceOn)&&play)initAudio();if(resumeEnding)FX.outroTone(sT);if(play&&voiceOn&&cs2&&cs2.recorded&&!cs2.complete&&!voiceSource&&!cs2.waiting)playRecorded(cs2,cs2.clock);if(AC){play?AC.resume():AC.suspend()}if(window.speechSynthesis){play?speechSynthesis.resume():speechSynthesis.pause()}last=performance.now();syncPlayer();
}
vp.addEventListener('click',togglePlayback);
panel.addEventListener('click',function(e){e.stopPropagation()});panel.addEventListener('pointerdown',function(e){e.stopPropagation()});
resumeButton.onclick=togglePlayback;
seek.addEventListener('input',function(){window.promo.seek(+seek.value)});
document.getElementById('back').onclick=function(){window.promo.seek(el-5)};document.getElementById('forward').onclick=function(){window.promo.seek(el+5)};
document.addEventListener('keydown',function(e){if(!started||/INPUT|SELECT|BUTTON/.test(e.target.tagName))return;if(e.code==='Space'){e.preventDefault();togglePlayback()}else if(e.code==='ArrowLeft'||e.code==='ArrowRight'){e.preventDefault();window.promo.seek(el+(e.code==='ArrowLeft'?-5:5))}});
// Optional safe-area overlay and deterministic preview controls for visual QA.
if(new URLSearchParams(location.search).has('safe'))st.classList.add('guides');
window.promo={
 duration:T,
 seek:function(seconds){stopEndingVoices();stopRecorded();seeked=true;if(AC)AC.suspend();idleT=0;st.classList.remove('idle');if(window.speechSynthesis)speechSynthesis.cancel();activeVoice=false;cs2=null;el=Math.max(0,Math.min(T,Number(seconds)||0));motionClock=el;filmMotion.seek(el);started=true;play=false;done=el>=T;cur=-1;document.getElementById('start').style.display='none';st.classList.add('paused');var t=el,i=0;while(i<D.length-1&&t>=D[i]){t-=D[i];i++}sT=t;show(i);fired=L.map(function(c){return c[0]===i&&c[1]<=t?1:0});var cue=L.filter(function(c){return c[0]===i&&c[2]==='say'&&c[1]<=t}).pop();if(cue){var saved=voiceOn;voiceOn=false;say(cue[3],cue[4],cue[5]);voiceOn=saved;cs2.clock=t-cue[1];var g=Math.min(cs2.words.length-1,Math.floor(cs2.clock/cs2.dur*cs2.words.length));paintWords(cs2,cs2.recorded?wordAt(cs2,cs2.clock):g);if(cs2.clock>cs2.dur+.6){cs2.complete=true;cap.style.opacity=0}}snapCamera=true;updatePresentation(t,i);syncMusicEnvelope(true);draw(0);syncPlayer()},
 state:function(){var active=activeTrade();return{time:el,scene:cur,playing:play,done:done,activeVoice:activeVoice,audio:{musicEnvelope:music?musicEnvelopeAt(el,T):0},market:{side:active.dir>0?"BUY":"SELL",closed:(cur===1||cur===6)&&rawChartClock()>=active.endAt,entry:active.entry,sl:active.sl,tp:active.tp,endAt:active.endAt,at:active.at,outcome:active.outcome,lot:active.lot,contractSize:active.contractSize,pnl:active.pnl,duration:active.duration,reaction:active.reaction,reactionAt:active.reactionAt,rate:RATE,botStart:botStart,botMarkAt:botMarkAt,closeWall:(el>=botStart?botStart+botPlan.close:D[0]+humanPlan.close),noticeWall:(el>=botStart?botStart+botPlan.notice:D[0]+humanPlan.notice),sceneEnd:(el>=botStart?watchStart:D[0]+D[1]),timeScale:(cur===1?humanPlan.speed(sT):cur===6?botPlan.speed(sT):1),scaleY:view.scaleY,price:market(chartClock()),clock:chartClock(),rawNow:rawChartClock()},timeline:{durations:D.slice(),humanEnd:D[0]+D[1],botStart:botStart,watchStart:watchStart,outroStart:outroStart,total:T},caption:[].map.call(cap.children,function(x){return x.textContent}).join(' '),camera:{x:camX,y:camY}}}
};
var previewFrame=new URLSearchParams(location.search).get('frame');if(previewFrame!==null&&isFinite(+previewFrame))window.promo.seek(+previewFrame);
requestAnimationFrame(tick);
})();
