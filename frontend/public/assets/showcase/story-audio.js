/* The film's procedural score and signal tones, adapted to a scroll-controlled story. */
export function createStoryAudio() {
 const labels={
  en:{start:'Tap to enable sound',on:'Mute sound',off:'Enable sound'},
  ru:{start:'Нажмите, чтобы включить звук',on:'Выключить звук',off:'Включить звук'},
  uz:{start:'Ovozni yoqish uchun bosing',on:'Ovozni o‘chirish',off:'Ovozni yoqish'}
 };
 const lang=document.documentElement.lang.split('-')[0],t=labels[lang]||labels.en;
 const button=document.createElement('button');button.type='button';button.className='story-audio-toggle';
 button.innerHTML='<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M4 9v6h4l5 4V5L8 9H4z"/><path class="sound-waves" d="M16 9a4 4 0 0 1 0 6M18.5 6.5a7.5 7.5 0 0 1 0 11"/><path class="sound-off" d="m16 9 5 6m0-6-5 6"/></svg>';
 document.body.append(button);
 let enabled=true;
 try{enabled=localStorage.getItem('lvct.story.sound')!=='off';}catch{}
 let context=null,master=null,bus=null,delay=null,noise=null,started=false,step=0,nextNote=0,lastScene=-1,lastTime=0;
 const chords=[[110,[220,261.6,329.6]],[87.3,[174.6,220,261.6]],[130.8,[261.6,329.6,392]],[98,[196,246.9,293.7]]],arp=[0,1,2,1,2,1,0,1];
 const cues={1:[[.2,'thud']],2:[[.15,'sweep']],3:[[.95,'card0'],[2.15,'card1'],[3.35,'card2'],[4.55,'bell']],4:[[.2,'sweep']],5:[[.2,'sweep']],6:[[.2,'sweep'],[.6,'telegram'],[1.95,'recognized'],[3.48,'executed']],7:[[.2,'sweep']],8:[[.2,'outro']]};
 function paint(){const playing=enabled&&started;button.classList.toggle('is-on',playing);button.setAttribute('aria-pressed',String(playing));button.setAttribute('aria-label',playing?t.on:enabled?t.start:t.off);button.title=button.getAttribute('aria-label');button.dataset.audio=playing?'on':enabled?'waiting':'off';}
 function init(){
  if(context)return true;
  const Audio=window.AudioContext||window.webkitAudioContext;if(!Audio)return false;
  context=new Audio({latencyHint:'interactive'});const compressor=context.createDynamicsCompressor();compressor.connect(context.destination);
  master=context.createGain();master.gain.value=0;master.connect(compressor);bus=context.createGain();bus.gain.value=.52;bus.connect(master);
  delay=context.createDelay(1);delay.delayTime.value=.45;const feedback=context.createGain();feedback.gain.value=.2;const filter=context.createBiquadFilter();filter.type='lowpass';filter.frequency.value=2400;delay.connect(filter);filter.connect(feedback);feedback.connect(delay);filter.connect(bus);
  noise=context.createBuffer(1,context.sampleRate,context.sampleRate);const data=noise.getChannelData(0);for(let i=0;i<data.length;i++)data[i]=Math.random()*2-1;
  return true;
 }
 function unlock(){if(!enabled||document.hidden||!init())return;started=true;context.resume().catch(()=>{});master.gain.setTargetAtTime(.32,context.currentTime,.25);nextNote=context.currentTime+.05;paint();}
 function osc(f,type,time,attack,duration,volume,send=0,lowpass=0,endFrequency=0){
  const o=context.createOscillator(),gain=context.createGain();o.type=type;o.frequency.setValueAtTime(f,time);if(endFrequency)o.frequency.exponentialRampToValueAtTime(endFrequency,time+duration);
  gain.gain.setValueAtTime(.0001,time);gain.gain.linearRampToValueAtTime(volume,time+attack);gain.gain.exponentialRampToValueAtTime(.0001,time+duration);
  if(lowpass){const filter=context.createBiquadFilter();filter.type='lowpass';filter.frequency.value=lowpass;o.connect(filter);filter.connect(gain);}else o.connect(gain);
  gain.connect(bus);if(send){const wet=context.createGain();wet.gain.value=.25;gain.connect(wet);wet.connect(delay);}o.start(time);o.stop(time+duration+.05);
 }
 function tone(f,duration,volume,offset=0,end=0){osc(f,'sine',context.currentTime+offset,.005,duration,volume,0,0,end);}
 function hit(time,volume,duration,frequency){const source=context.createBufferSource();source.buffer=noise;const filter=context.createBiquadFilter();filter.type='highpass';filter.frequency.value=frequency;const gain=context.createGain();gain.gain.setValueAtTime(volume,time);gain.gain.exponentialRampToValueAtTime(.0001,time+duration);source.connect(filter);filter.connect(gain);gain.connect(bus);source.start(time);source.stop(time+duration+.02);}
 function sweep(){const time=context.currentTime,source=context.createBufferSource();source.buffer=noise;source.loop=true;const filter=context.createBiquadFilter();filter.type='bandpass';filter.Q.value=1.1;filter.frequency.setValueAtTime(400,time);filter.frequency.exponentialRampToValueAtTime(3600,time+1.1);const gain=context.createGain();gain.gain.setValueAtTime(.0001,time);gain.gain.linearRampToValueAtTime(.18,time+.5);gain.gain.exponentialRampToValueAtTime(.0001,time+1.1);source.connect(filter);filter.connect(gain);gain.connect(bus);source.start(time);source.stop(time+1.15);}
 function effect(name){if(!context||!enabled||!started||document.hidden||context.state!=='running')return;
  if(name==='sweep')sweep();
  else if(name==='thud')tone(95,.8,.35,0,36);
  else if(name==='telegram'){tone(784,.14,.045);tone(1046.5,.18,.035,.075);}
  else if(name==='recognized'){tone(523.25,.13,.045);tone(783.99,.20,.04,.09);}
  else if(name==='executed'){tone(659.25,.15,.04);tone(830.61,.17,.035,.065);tone(987.77,.24,.04,.13);}
  else if(name==='bell'){for(const d of [0,.19])for(const [i,f] of [1046.5,2812.2,4127.1].entries())tone(f,[.9,.52,.28][i],[.06,.02,.008][i]*(d?.65:1),d);}
  else if(name.startsWith('card')){const f=[587.33,659.25,783.99][Number(name.slice(-1))];tone(f,.15,.045);tone(f*1.25,.21,.035,.085);}
  else if(name==='outro'){for(const f of [130.8128,261.6256,329.6276,391.9954])osc(f,'triangle',context.currentTime,.16,2.5,.035,0,1100);}
 }
 function note(s,time,energy){const chord=chords[Math.floor(s/16)%4],k=s%16;
  if(k===0)chord[1].forEach(f=>osc(f,'triangle',time,.9,2.7,.045,0,900));
  if(energy===1&&(k===0||k===3))osc(120,'sine',time,.005,.35,.35,0,0,42);
  if(energy>=2){if(k===0||k===10){osc(chord[0],'sine',time,.01,.5,.2);osc(chord[0]*2,'triangle',time,.01,.4,.05,0,700);}if(k%2===0)osc(chord[1][arp[(k/2)%8]]*2,'triangle',time,.005,.55,.05,1,2800);}
  if(energy===2&&(k===0||k===8))osc(130,'sine',time,.005,.3,.25,0,0,45);
  if(energy>=3){if(k%4===0)osc(140,'sine',time,.004,.28,.35,0,0,45);if(k%4===2)hit(time,.07,.05,7000);if(k===4||k===12)hit(time,.08,.14,1800);}
 }
 function update(scene,time){
  if(scene!==lastScene||time<lastTime){lastScene=scene;lastTime=0;}const before=lastTime;lastTime=time;
  if(!enabled||!started||!context||context.state!=='running'||document.hidden)return;
  for(const [at,name] of cues[scene]||[])if(before<at&&time>=at)effect(name);
  const energy=scene<=1?1:scene>=4&&scene<=5?3:2,now=context.currentTime;if(nextNote<now-.5)nextNote=now;
  while(nextNote<now+.16){note(step++,nextNote,energy);nextNote+=.15;}
 }
 const activate=e=>{if(e.target.closest?.('.story-audio-toggle'))return;if(enabled&&!started)unlock();else if(enabled&&context?.state==='suspended')unlock();};
 for(const name of ['pointerdown','touchstart','keydown','wheel'])window.addEventListener(name,activate,{passive:true});
 button.addEventListener('click',()=>{if(!started&&enabled){unlock();return;}enabled=!enabled;try{localStorage.setItem('lvct.story.sound',enabled?'on':'off');}catch{}if(enabled)unlock();else if(master&&context){master.gain.setTargetAtTime(0,context.currentTime,.04);context.suspend().catch(()=>{});}paint();});
 document.addEventListener('visibilitychange',()=>{if(!context)return;if(document.hidden)context.suspend().catch(()=>{});else if(enabled&&started)unlock();});
 paint();return{update};
}
