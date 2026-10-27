import {armPose} from './lv1-arm-kinematics.js?v=1';
/* Continuous activity poses, skinned with the existing approved robot artwork. */
const NS='http://www.w3.org/2000/svg';
const idleURL=new URL('pets/lv1/walk-rig-pose-e636832997a2.png',import.meta.url).href;
const atlasURL=new URL('pets/lv1/sprites-b322f4a58a72.webp',import.meta.url).href;
const billURL=new URL('pets/lumi/banknote-858b9eb56732.svg',import.meta.url).href;
const clip={
 torso:'M77 101L122 101L129 119L124 141L110 155L98 155L94 143L81 132L78 125Z',head:'M40 20H153V104H40Z',
 nearUpper:'M62 103L79 103L86 113L80 128L72 138L49 138L49 126Z',nearLower:'M47 131L73 131L76 152L70 174L52 174L43 161Z',
 farUpper:'M119 103L134 106L141 126L143 138L126 141L123 128Z',farLower:'M126 131L144 132L148 157L142 170L129 170L125 154Z',
 nearThigh:'M79 139L93 147L93 164L87 176L71 176L68 166L73 148Z',nearShin:'M72 165L90 165L87 186L71 188L66 180Z',nearFoot:'M65 180L85 180L94 188L96 200L60 200L59 189Z',
 farThigh:'M113 141L125 144L128 165L125 177L110 177L107 166Z',farShin:'M109 165L126 166L129 187L109 189L105 180Z',farFoot:'M108 180L127 181L138 187L143 198L105 200L102 190Z',
 seatedHead:'M25 15H165V103H25Z',workBody:'M20 100H190V208H20Z M79 127L97 127L110 139L101 150L79 146Z',
 workHand:'M79 127L97 127L110 139L101 150L79 146Z',sleepBody:'M20 102H175V208H20Z'
};
const bone={near:{hip:[84,149],knee:[80,171],ankle:[76,185],shoulder:[75,114],elbow:[60,135],hand:[60,157]},far:{hip:[117,149],knee:[118,171],ankle:[117,185],shoulder:[126,114],elbow:[135,136],hand:[137,157]}};
const clamp=(n,a=0,b=1)=>Math.max(a,Math.min(b,n)),smooth=n=>(n=clamp(n),n*n*(3-2*n)),lerp=(a,b,u)=>a+(b-a)*u;
const length=(a,b)=>Math.hypot(b[0]-a[0],b[1]-a[1]);
function map(el,a,b,c,d){const x=b[0]-a[0],y=b[1]-a[1],dx=d[0]-c[0],dy=d[1]-c[1],n=x*x+y*y,p=(dx*x+dy*y)/n,q=(dy*x-dx*y)/n;el.setAttribute('transform',`matrix(${p} ${q} ${-q} ${p} ${c[0]-p*a[0]+q*a[1]} ${c[1]-q*a[0]-p*a[1]})`);}
function joint(a,b,l1,l2,bend=1){const dx=b[0]-a[0],dy=b[1]-a[1],n=Math.max(.001,Math.hypot(dx,dy)),d=Math.min(l1+l2-.001,n),along=(l1*l1-l2*l2+d*d)/(2*d),out=Math.sqrt(Math.max(0,l1*l1-along*along))*bend;return[a[0]+dx/n*along+dy/n*out,a[1]+dy/n*along-dx/n*out];}
let serial=0;
export function createActivityRig(){
 const id='lv-activity-'+(++serial),svg=document.createElementNS(NS,'svg');svg.classList.add('lumi-action-rig');svg.setAttribute('viewBox','0 0 192 208');svg.setAttribute('aria-hidden','true');svg.setAttribute('focusable','false');
 svg.innerHTML=`<defs>${Object.entries(clip).map(([name,path])=>`<clipPath id="${id}-${name}"><path d="${path}" fill-rule="evenodd" clip-rule="evenodd"/></clipPath>`).join('')}<linearGradient id="${id}-glass" x2="0.3" y2="1"><stop stop-color="#051321"/><stop offset=".55" stop-color="#01080f"/><stop offset="1" stop-color="#06111d"/></linearGradient></defs>`;
 function group(parent,name){const g=document.createElementNS(NS,'g');g.dataset.part=name;parent.append(g);return g;}
 const atlasImages=[];
 function art(parent,name,mask,row=null){const g=group(parent,name),cut=group(g,'skin'),img=document.createElementNS(NS,'image');cut.setAttribute('clip-path',`url(#${id}-${mask})`);if(row===null){img.setAttribute('href',idleURL);img.setAttributeNS('http://www.w3.org/1999/xlink','xlink:href',idleURL);}else atlasImages.push(img);img.setAttribute('width',row===null?'192':'768');img.setAttribute('height',row===null?'208':'1664');if(row!==null)img.setAttribute('y',String(-row*208));cut.append(img);return g;}
 const standing=group(svg,'standing'),parts={};
 for(const name of ['farUpper','farLower','farThigh','farShin','farFoot','nearThigh','nearShin','nearFoot','torso','head','nearUpper','nearLower'])parts[name]=art(standing,name,name);
 const face=group(parts.head,'face');face.innerHTML=`<path d="M80 53Q106 48 136 52L136 79Q111 87 81 81Z" fill="url(#${id}-glass)"/><path d="M83 55Q108 50 132 54" fill="none" stroke="#75bafa" stroke-opacity=".08"/>`;
 const normal=group(face,'normal'),happy=group(face,'happy'),angry=group(face,'angry'),dizzy=group(face,'dizzy');
 normal.innerHTML='<ellipse cx="97" cy="68" rx="4.6" ry="7" fill="#27d9ff"/><ellipse cx="127" cy="68" rx="3.6" ry="6.5" fill="#27d9ff"/>';
 happy.innerHTML='<path d="M91 71Q97 58 103 71M122 71Q127 59 132 71" fill="none" stroke="#27d9ff" stroke-width="3.5" stroke-linecap="round"/>';
 angry.innerHTML='<path d="M91 63L103 67M122 67L132 63" fill="none" stroke="#20cefa" stroke-width="3" stroke-linecap="round"/><ellipse cx="98" cy="71" rx="4" ry="3" fill="#24d7ff"/><ellipse cx="127" cy="71" rx="3.2" ry="3" fill="#24d7ff"/>';
 dizzy.innerHTML='<path d="M92 68a5 5 0 1 1 10 0 3 3 0 1 1-6 0 1 1 0 1 1 2 0M122 68a4.5 4.5 0 1 1 9 0 2.7 2.7 0 1 1-5.4 0 1 1 0 1 1 2 0" fill="none" stroke="#27d9ff" stroke-width="1.7" stroke-linecap="round"/>';
 const money=group(standing,'money'),stack=group(money,'stack'),note=group(money,'note');
 for(let i=0;i<3;i++){const img=document.createElementNS(NS,'image');img.setAttribute('href',billURL);img.setAttributeNS('http://www.w3.org/1999/xlink','xlink:href',billURL);img.setAttribute('width','26');img.setAttribute('height','17');img.setAttribute('x',String(-9+i*2));img.setAttribute('y',String(-11-i*2));stack.append(img);}
 const single=document.createElementNS(NS,'image');single.setAttribute('href',billURL);single.setAttributeNS('http://www.w3.org/1999/xlink','xlink:href',billURL);single.setAttribute('width','29');single.setAttribute('height','19');single.setAttribute('x','-9');single.setAttribute('y','-17');note.append(single);
 const guns=group(standing,'cash-guns'),nearGun=group(guns,'near-gun'),farGun=group(guns,'far-gun');
 const gunArt='<path d="M-4-7 H16 Q20-7 20-3 V2 H7 L5 11 H-2 L0 2 H-4 Z" fill="#142c40" stroke="#83c5ed" stroke-width="1.4" stroke-linejoin="round"/><path d="M0-8 H14 V-12 H3 Z" fill="#8fd6ab" stroke="#d9f7df" stroke-width=".8"/><path d="M6-10 H12 M3-3 H14" stroke="#d6f5ff" stroke-width="1.2" stroke-linecap="round"/><circle cx="3" cy="5" r="1.3" fill="#57baff"/>';
 nearGun.innerHTML=gunArt;farGun.innerHTML=gunArt;
 const working=group(svg,'working'),workBody=art(working,'body','workBody',3),workHead=art(working,'head','seatedHead',3),workHand=art(working,'hand','workHand',3);
 const sleeping=group(svg,'sleeping'),sleepBody=art(sleeping,'body','sleepBody',4),sleepHead=art(sleeping,'head','seatedHead',4);
 let lastClock=null,currentState='idle',lastHands={near:[60,157],far:[137,157]};
 const angle=(a,b)=>Math.atan2(b[1]-a[1],b[0]-a[0])*180/Math.PI;
 const rest={nearUpper:angle(bone.near.shoulder,bone.near.elbow),nearLower:angle(bone.near.elbow,bone.near.hand),farUpper:angle(bone.far.shoulder,bone.far.elbow),farLower:angle(bone.far.elbow,bone.far.hand)};
 const pose={nearX:60,nearY:157,farX:137,farY:157,head:0,bob:1,joy:0,anger:0,dizzy:0,sit:0,work:0,sleep:0,money:0,jump:0,...rest,guns:0};
 function render(state,time,clock,reduced=false){
  const dt=lastClock===null?1/60:clamp(clock-lastClock,0,.1);lastClock=clock;const t=reduced?0:time;
  const target={nearX:60,nearY:157,farX:137,farY:157,head:Math.sin(t*1.5)*1.1,bob:1+Math.sin(t*2)*.5,joy:0,anger:0,dizzy:0,sit:0,work:0,sleep:0,money:0,jump:0,...rest,guns:0};
  if(state==='waving'){target.farUpper=-10+Math.sin(t*4)*3;target.farLower=-78+Math.sin(t*6)*18;target.head=-2;}
  if(state==='happy'){target.nearUpper=165;target.nearLower=-70+Math.sin(t*6)*7;target.farUpper=15;target.farLower=-80-Math.sin(t*6)*7;target.joy=1;target.bob=2+Math.sin(t*5)**2*2;target.head=Math.sin(t*2)*2;}
  if(state==='annoyed'){const palm=smooth((t-.15)/.7)*(1-smooth((t-3)/.8));target.nearUpper=lerp(rest.nearUpper,-15,palm);target.nearLower=lerp(rest.nearLower,-100,palm);target.anger=1;target.head=-5+Math.sin(t*1.4);}
  if(state==='celebrating'){
   const phase=(t%1.6)/1.6,keys=[[0,68,85],[.2,30,115],[.42,8,20],[.6,-12,-65],[.73,-10,-78],[1,68,85]];
   let arm=keys[keys.length-1];for(let i=1;i<keys.length;i++)if(phase<=keys[i][0]){const a=keys[i-1],b=keys[i],u=smooth((phase-a[0])/(b[0]-a[0]));arm=[phase,lerp(a[1],b[1],u),lerp(a[2],b[2],u)];break;}
   target.nearUpper=105;target.nearLower=65;target.farUpper=arm[1];target.farLower=arm[2];target.money=1;target.joy=1;target.head=-2;
  }
  if(state==='cash-guns'){
   const phase=reduced?1.6:t,draw=smooth(phase/.35)*(1-smooth((phase-.5)/.35));
   const aim=smooth((phase-.55)/.65)*(1-smooth((phase-6)/.7));
   target.nearUpper=lerp(lerp(rest.nearUpper,100,draw),166,aim);target.nearLower=lerp(lerp(rest.nearLower,45,draw),190+(reduced?0:Math.sin(phase*8)*3),aim);
   target.farUpper=lerp(lerp(rest.farUpper,83,draw),14,aim);target.farLower=lerp(lerp(rest.farLower,138,draw),-7-(reduced?0:Math.sin(phase*8)*3),aim);
   target.guns=smooth((phase-.22)/.25)*(1-smooth((phase-6.2)/.45));target.joy=1;target.head=reduced?0:Math.sin(phase*2)*1.5;
  }
  if(state==='shaken'||state==='dizzy'){target.dizzy=1;target.head=reduced?0:Math.sin(t*9)*8;target.sit=state==='dizzy'?(reduced?1:smooth((t-.15)/.55)):0;}
  if(state==='working')target.work=1;
  if(state==='sleeping')target.sleep=1;
  if(state==='jumping')target.jump=-12*Math.sin(Math.PI*clamp(t/.7));
  const blend=reduced?1:1-Math.exp(-dt*13);
  for(const key of Object.keys(pose))pose[key]=lerp(pose[key],target[key],blend);
  const standingOpacity=clamp(1-pose.work-pose.sleep);standing.style.opacity=String(standingOpacity);working.style.opacity=String(pose.work);sleeping.style.opacity=String(pose.sleep);
  standing.setAttribute('transform',`translate(0 ${pose.jump})`);
  const bodyBob=pose.bob+pose.sit*27;parts.torso.setAttribute('transform',`translate(0 ${bodyBob})`);parts.head.setAttribute('transform',`translate(0 ${bodyBob}) rotate(${pose.head} 96 101)`);
  for(const side of ['near','far']){
   const b=bone[side],hip=[b.hip[0],b.hip[1]+bodyBob],ankle=[lerp(b.ankle[0],side==='near'?64:145,pose.sit),b.ankle[1]],standingKnee=joint(hip,ankle,length(b.hip,b.knee),length(b.knee,b.ankle));
   const knee=[lerp(standingKnee[0],side==='near'?59:139,pose.sit),lerp(standingKnee[1],179,pose.sit)];
   map(parts[side+'Thigh'],b.hip,b.knee,hip,knee);map(parts[side+'Shin'],b.knee,b.ankle,knee,ankle);parts[side+'Foot'].setAttribute('transform',`translate(${ankle[0]-b.ankle[0]} ${ankle[1]-b.ankle[1]})`);
   const shoulder=[b.shoulder[0],b.shoulder[1]+bodyBob];
   const {elbow,hand}=armPose(shoulder,length(b.shoulder,b.elbow),length(b.elbow,b.hand),pose[side+'Upper'],pose[side+'Lower']);
   map(parts[side+'Upper'],b.shoulder,b.elbow,shoulder,elbow);map(parts[side+'Lower'],b.elbow,b.hand,elbow,hand);lastHands[side]=hand;
  }
  const blinkPhase=t%4.6,open=reduced?1:blinkPhase>4.32?1-Math.sin((blinkPhase-4.32)/.28*Math.PI)**2:1;
  face.style.opacity=String(clamp(pose.joy+pose.anger+pose.dizzy+1-open));normal.style.opacity=String(clamp(1-pose.joy-pose.anger-pose.dizzy));happy.style.opacity=String(pose.joy);angry.style.opacity=String(pose.anger);dizzy.style.opacity=String(pose.dizzy);
  normal.children[0].setAttribute('ry',String(Math.max(.6,7*open)));normal.children[1].setAttribute('ry',String(Math.max(.6,6.5*open)));
  money.style.opacity=String(pose.money);stack.setAttribute('transform',`translate(${lastHands.near}) rotate(-18)`);note.setAttribute('transform',`translate(${lastHands.far}) rotate(-35)`);
  guns.style.opacity=String(pose.guns);nearGun.setAttribute('transform',`translate(${lastHands.near}) scale(-1 1)`);farGun.setAttribute('transform',`translate(${lastHands.far})`);
  const release=(t%1.6)/1.6;note.style.opacity=state==='celebrating'&&release<.5?'1':'0';
  working.setAttribute('transform',`translate(0 ${reduced?0:Math.sin(t*2.2)*.45})`);
  workHead.setAttribute('transform',`rotate(${reduced?0:Math.sin(t*1.6)*1.6} 96 100)`);
  workHand.setAttribute('transform',`translate(0 ${reduced?0:Math.sin(t*14)*1.2}) rotate(${reduced?0:Math.sin(t*11)*1.5} 84 137)`);
  sleeping.setAttribute('transform',`translate(0 ${reduced?0:Math.sin(t*1.3)*.7})`);sleepHead.setAttribute('transform',`rotate(${reduced?0:Math.sin(t*1.1)*1.8} 96 104)`);
  svg.dataset.state=state;svg.dataset.pose=Object.values(pose).map(n=>n.toFixed(3)).join(',');svg.dataset.clock=t.toFixed(3);currentState=state;
 }
 function enableAtlas(){for(const image of atlasImages){image.setAttribute('href',atlasURL);image.setAttributeNS('http://www.w3.org/1999/xlink','xlink:href',atlasURL);}}
 render('idle',0,0,true);return{svg,render,enableAtlas,hand:()=>lastHands.far.slice(),hands:()=>({near:lastHands.near.slice(),far:lastHands.far.slice()})};
}
