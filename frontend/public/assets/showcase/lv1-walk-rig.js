/* A two-bone walk rig using the existing generated robot artwork. */
const poseURL=new URL('pets/lv1/walk-rig-pose-e636832997a2.png',import.meta.url).href;
const NS='http://www.w3.org/2000/svg';
const points={near:{hip:[84,149],knee:[80,171],ankle:[76,185],ground:185},far:{hip:[117,149],knee:[118,171],ankle:[117,185],ground:184}};
const clips={
 body:'M40 20H152V102H40Z M77 101L122 101L129 119L124 141L110 155L98 155L94 143L81 132L78 125Z',
 nearArm:'M62 103L79 103L86 113L80 126L74 143L75 154L69 172L52 173L43 161L47 140L51 121Z',
 farArm:'M119 103L134 106L139 125L145 140L147 157L141 168L130 168L126 154L127 139L124 127L121 121Z',
 nearThigh:'M79 139L93 147L93 164L87 176L71 176L68 166L73 148Z',
 nearShin:'M72 165L90 165L87 186L71 188L66 180Z',
 nearFoot:'M65 180L85 180L94 188L96 200L60 200L59 189Z',
 farThigh:'M113 141L125 144L128 165L125 177L110 177L107 166Z',
 farShin:'M109 165L126 166L129 187L109 189L105 180Z',
 farFoot:'M108 180L127 181L138 187L143 198L105 200L102 190Z'
};
let serial=0;
const len=(a,b)=>Math.hypot(b[0]-a[0],b[1]-a[1]);
function mapBone(el,a,b,c,d){const sx=b[0]-a[0],sy=b[1]-a[1],dx=d[0]-c[0],dy=d[1]-c[1],n=sx*sx+sy*sy;const p=(dx*sx+dy*sy)/n,q=(dy*sx-dx*sy)/n;el.setAttribute('transform',`matrix(${p} ${q} ${-q} ${p} ${c[0]-p*a[0]+q*a[1]} ${c[1]-q*a[0]-p*a[1]})`);}
function kneeFor(hip,ankle,l1,l2){const dx=ankle[0]-hip[0],dy=ankle[1]-hip[1],dist=Math.hypot(dx,dy),d=Math.min(l1+l2-.001,Math.max(.001,dist));const a=(l1*l1-l2*l2+d*d)/(2*d),h=Math.sqrt(Math.max(0,l1*l1-a*a));return[hip[0]+dx/dist*a+dy/dist*h,hip[1]+dy/dist*a-dx/dist*h];}
export function createWalkRig(){
 const prefix='lv-gait-'+(++serial),svg=document.createElementNS(NS,'svg');svg.classList.add('lumi-walk-rig');svg.setAttribute('viewBox','0 0 192 208');svg.setAttribute('aria-hidden','true');svg.setAttribute('focusable','false');
 svg.innerHTML=`<defs>${Object.entries(clips).map(([name,path])=>`<clipPath id="${prefix}-${name}"><path d="${path}"/></clipPath>`).join('')}</defs>`;
 const layers={};for(const name of ['farArm','farThigh','farShin','farFoot','nearThigh','nearShin','nearFoot','body','nearArm']){
  const g=document.createElementNS(NS,'g'),cut=document.createElementNS(NS,'g'),img=document.createElementNS(NS,'image');cut.setAttribute('clip-path',`url(#${prefix}-${name})`);img.setAttribute('href',poseURL);img.setAttributeNS('http://www.w3.org/1999/xlink','xlink:href',poseURL);img.setAttribute('width','192');img.setAttribute('height','208');cut.append(img);g.append(cut);g.dataset.limb=name;svg.append(g);layers[name]=g;
 }
 function render(cycle){
  const phase=((cycle%1)+1)%1,bob=4+1.5*Math.sin(phase*Math.PI*2)**2;
  layers.body.setAttribute('transform',`translate(0 ${bob})`);
  for(const [side,offset,hipX] of [['near',0,94],['far',.5,100]]){
   const source=points[side],p=(phase+offset)%1,support=p<.5,u=support?p*2:(p-.5)*2;
   const stride=192*.3/2;
   const x=support?stride/2-stride*u:-stride/2+stride*(u*u*(3-2*u));
   const lift=support?0:11*Math.sin(Math.PI*u);
   const hip=[hipX,source.hip[1]+bob],ankle=[hipX+x,source.ground-lift];
   const knee=kneeFor(hip,ankle,len(source.hip,source.knee),len(source.knee,source.ankle));
   mapBone(layers[side+'Thigh'],source.hip,source.knee,hip,knee);mapBone(layers[side+'Shin'],source.knee,source.ankle,knee,ankle);
   const pitch=support?0:-10*Math.sin(Math.PI*u);
   layers[side+'Foot'].setAttribute('transform',`translate(${ankle[0]-source.ankle[0]} ${ankle[1]-source.ankle[1]}) rotate(${pitch} ${source.ankle[0]} ${source.ankle[1]})`);
   svg.dataset[side+'Support']=String(support);svg.dataset[side+'Lift']=lift.toFixed(2);svg.dataset[side+'X']=ankle[0].toFixed(4);svg.dataset[side+'Y']=ankle[1].toFixed(4);
  }
  layers.nearArm.setAttribute('transform',`translate(0 ${bob}) rotate(${-Math.cos(phase*Math.PI*2)*13} 75 114)`);
  layers.farArm.setAttribute('transform',`translate(0 ${bob}) rotate(${Math.cos(phase*Math.PI*2)*13} 126 114)`);
  svg.dataset.phase=phase.toFixed(4);
 }
 render(0);return{svg,render};
}
