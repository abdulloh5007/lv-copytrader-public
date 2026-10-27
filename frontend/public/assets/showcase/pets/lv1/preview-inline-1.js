
import {createActivityRig} from '../../lv1-action-rig.js?v=1';
import {createWalkRig} from '../../lv1-walk-rig.js?v=1';
const states=[['Ожидание','idle',6],['Прогулка','walk',1],['Приветствие','waving',3.2],['Работа за ноутбуком','working',6.8],['Режим ожидания','sleeping',10],['Денежный дождь','celebrating',4],['TP · Радость','happy',4.4],['SL · Раздражение','annoyed',4.4]];
const reduced=matchMedia('(prefers-reduced-motion:reduce)').matches;
document.getElementById('grid').innerHTML=states.map(([name])=>`<div class="tile"><div class="sprite" role="img" aria-label="${name}" style="background:none"></div><span>${name}</span></div>`).join('');
const rigs=states.map(([,state],i)=>{const rig=state==='walk'?createWalkRig():createActivityRig();rig.svg.style.cssText='display:block;width:100%;height:100%;overflow:visible;filter:none';document.querySelectorAll('.sprite')[i].append(rig.svg);return rig;});
let raf=0;function render(ms){const now=ms/1000;rigs.forEach((rig,i)=>{const [,state,duration]=states[i],time=reduced?0:now%duration;if(state==='walk')rig.render(time);else rig.render(state,time,now,reduced);});if(!reduced&&!document.hidden)raf=requestAnimationFrame(render);}render(0);document.addEventListener('visibilitychange',()=>{cancelAnimationFrame(raf);if(!document.hidden)render(performance.now());});
