/* Adapted from NFT Toys' NFT detail scene: fixed-step spring, stable input plane,
   distinct surface/content depths and pointer-driven tilt. */
(function(){
 'use strict';
 const clamp=(v,a=-1,b=1)=>Math.max(a,Math.min(b,v));
 const STEP=1/240,STIFFNESS=300,DAMPING=2*.8*Math.sqrt(STIFFNESS);
 const reduced=matchMedia('(prefers-reduced-motion: reduce)');
 function mount(host,options={}){
  const input=options.input||host,phase=options.phase||0,rig=document.createElement('div');rig.className='relief-rig';rig.style.transform='perspective(920px) rotateX(0deg) rotateY(0deg)';
  const content=[...host.childNodes].filter(n=>n.nodeType===1||n.nodeType===3&&n.textContent.trim());
  content.forEach(node=>{if(node.nodeType===3){const span=document.createElement('span');node.before(span);span.append(node);node=span;}rig.append(node);});
  host.append(rig);
  const surface=document.createElement('div');surface.className='relief-surface '+(options.surfaceClass||'');surface.setAttribute('aria-hidden','true');rig.prepend(surface);
  const layers=[...rig.children].filter(n=>n!==surface).map(node=>{const depth=node.matches('.ico,.node-icon,.ch,.card-heading,.message-top,.signal-meta,.confirm-icon,.bot-check')?54:node.matches('p,small,.parse-status,.parser-status')?30:40;node.setAttribute(options.layerAttribute||'data-depth',depth);node.style.transform='translateZ('+(reduced.matches?0:depth)+'px)';node.style.translate='none';return{el:node,z:depth};});
  if(options.trace){
   const trace=document.createElement('span');trace.className='relief-trace';trace.setAttribute('aria-hidden','true');surface.append(trace);
  }
  function layout(){
   host.classList.remove('relief-host');const style=getComputedStyle(host);
   for(const key of ['display','flexDirection','alignItems','justifyContent','justifyItems','flexWrap','gap','rowGap','columnGap','paddingTop','paddingRight','paddingBottom','paddingLeft'])rig.style[key]=style[key];
   rig.style.minHeight=style.minHeight==='auto'?'0px':`max(0px, ${style.minHeight} - 2px)`;
   host.classList.add('relief-host');
  }
  const state={host,rig,layers,surface,phase,x:0,y:0,vx:0,vy:0,tx:0,ty:0,inputX:0,inputY:0,pointer:false,accumulator:0,lastClock:null,idleClock:0};
  function point(event){if(reduced.matches)return;const rect=input.getBoundingClientRect();state.tx=clamp((event.clientX-rect.left-rect.width/2)/(rect.width/2));state.ty=clamp((event.clientY-rect.top-rect.height/2)/(rect.height/2));state.pointer=true;}
  function reset(){state.tx=state.ty=0;state.pointer=false;}
  input.addEventListener('pointermove',point,{passive:true});input.addEventListener('pointerdown',point,{passive:true});input.addEventListener('pointerleave',reset,{passive:true});input.addEventListener('pointercancel',reset,{passive:true});window.addEventListener('pointerup',e=>{if(e.pointerType!=='mouse')reset();},{passive:true});window.addEventListener('blur',reset);
  function render(clock,{playing=true,gate=1,entrance=1}={}){
   const delta=state.lastClock===null?0:clamp(clock-state.lastClock,0,1),elapsed=Math.min(delta,.05);state.lastClock=clock;
   const settled=entrance>=1;
   if(!settled){state.idleClock=0;state.x=state.y=state.inputX=state.inputY=state.vx=state.vy=state.accumulator=0;}
   else if(playing)state.idleClock+=delta;
   const px=state.pointer?state.tx:0,py=state.pointer?state.ty:0;
   if(reduced.matches){state.x=state.y=state.inputX=state.inputY=state.vx=state.vy=state.accumulator=0;}
   else if(playing&&settled){
    // Springs smooth interaction only. The automatic orbit has no catch-up target.
    state.accumulator+=elapsed;while(state.accumulator>=STEP){state.vx+=((-py*5-state.inputX)*STIFFNESS-state.vx*DAMPING)*STEP;state.vy+=((px*7-state.inputY)*STIFFNESS-state.vy*DAMPING)*STEP;state.inputX+=state.vx*STEP;state.inputY+=state.vy*STEP;state.accumulator-=STEP;}
    const angle=state.idleClock*.32+phase;
    state.x=((Math.sin(angle)-Math.sin(phase))*4.5+state.inputX)*gate;
    state.y=((Math.cos(angle)-Math.cos(phase))*4.5+state.inputY)*gate;
   }
   rig.style.transform=`perspective(920px) rotateX(${state.x.toFixed(4)}deg) rotateY(${state.y.toFixed(4)}deg)`;
   rig.style.setProperty('--light-x',(50+state.y/8*30)+'%');rig.style.setProperty('--light-y',(40-state.x/6*30)+'%');
   rig.style.setProperty('--trace-angle',((clock*32+phase*19)%360).toFixed(3)+'deg');host.dataset.tiltSource=state.pointer?'pointer':'idle';
   host.dataset.tiltX=state.x.toFixed(4);host.dataset.tiltY=state.y.toFixed(4);host.dataset.tiltClock=state.idleClock.toFixed(4);
  }
  function seek(clock){state.lastClock=clock;state.accumulator=0;state.idleClock=0;state.x=state.y=state.inputX=state.inputY=state.vx=state.vy=0;reset();}
  reduced.addEventListener('change',()=>layers.forEach(layer=>{layer.el.style.transform='translateZ('+(reduced.matches?0:layer.z)+'px)';}));
  layout();window.addEventListener('resize',layout);
  render(0);return{...state,render,seek,layout};
 }
 // The cube keeps one shared rotation axis; each face fits its own content.
 const faceSizes=new WeakMap();
 function sizeCube(viewport,progress=0){
  const sizes=faceSizes.get(viewport);if(!sizes)return;
  sizes.progress=progress;const height=sizes.heights[0]+(sizes.heights[1]-sizes.heights[0])*progress;
  viewport.style.height=height.toFixed(2)+'px';viewport.firstElementChild.style.setProperty('--cube-height',height.toFixed(2)+'px');
 }
 function fitFaces(viewport,faces){
  const heights=Array.from(faces,face=>{const rig=face.querySelector('.relief-rig'),oldHeight=rig.style.height,oldMin=rig.style.minHeight,oldDisplay=face.style.display;face.style.display='block';rig.style.height='auto';rig.style.minHeight='0';const natural=Math.ceil(rig.offsetHeight+2);rig.style.height=oldHeight;rig.style.minHeight=oldMin;face.style.display=oldDisplay;face.style.setProperty('--face-height',natural+'px');return natural;});
  const progress=faceSizes.get(viewport)?.progress||0;faceSizes.set(viewport,{heights,progress});sizeCube(viewport,progress);return heights;
 }
 window.CardRelief={mount,fitFaces,sizeCube};
})();
