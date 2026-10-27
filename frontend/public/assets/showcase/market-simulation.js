/* Shared, seeded demonstration market. No live account or market feed is used. */
(function(){
 'use strict';
 const assetBase=typeof document==='undefined'?'http://localhost/':document.currentScript?.src||location.href;
 const TIMING={rate:6,reactionDelay:20,candleSeconds:60,tickSeconds:.5};
 // Explicit demonstration position: lot count × ounces per standard contract.
 const POSITION=window.LVTradeConfig;
 if(!POSITION||!(POSITION.lot>0)||!(POSITION.contractSize>0))throw new Error('Load a valid trade-config.js before the market model');
 function profit(trade,quote){return +((quote-trade.entry)*trade.dir*trade.lot*trade.contractSize).toFixed(2);}
 function formatLot(trade=POSITION){return String(trade.lot);}
 function formatMoney(value,signed=false){return(value<0?'−':signed?'+':'')+'$'+Math.abs(value).toFixed(2);}
 function amounts(trade,quote=trade.entry){
  const target=profit(trade,trade.tp),missed=profit(trade,trade.reaction);
  return{floating:profit(trade,quote),target,missed,remaining:+(target-missed).toFixed(2)};
 }
 const PROFILES={asia:{rangeMedian:.87,rangeP90:1.651,rangeP99:2.87},europe:{rangeMedian:.89,rangeP90:1.641,rangeP99:2.523},overlap:{rangeMedian:1.117,rangeP90:2.047,rangeP99:3.074},us:{rangeMedian:.59,rangeP90:1.087,rangeP99:1.843}};
 const mix=(a,b,t)=>a+(b-a)*t,clamp=(v,a=0,b=1)=>Math.max(a,Math.min(b,v));
 function hash(seed,index){let n=(seed^Math.imul(index,0x9e3779b1))>>>0;n=Math.imul(n^(n>>>16),0x21f0aaad);n=Math.imul(n^(n>>>15),0x735a2d97);return (n^(n>>>15))>>>0;}
 function randomSeed(){
  if(typeof crypto!=='undefined'&&crypto.getRandomValues)return crypto.getRandomValues(new Uint32Array(1))[0];
  return Math.floor(Math.random()*4294967296);
 }
 function create(seed,options={}){
  seed=seed==null?randomSeed():Number(seed)>>>0;
  let state=seed;const random=()=>{state=(Math.imul(state,1664525)+1013904223)>>>0;return state/4294967296;};
  const hour=new Date().getUTCHours(),query=typeof location==='undefined'?null:new URLSearchParams(location.search).get('session');
  const session=options.session||query||(hour<7?'asia':hour<12?'europe':hour<16?'overlap':hour<21?'us':'asia'),calibrationProfile=PROFILES[session]||PROFILES.europe;
  // One target-sized volatility scale for the whole tape, including the backdrop.
  // The historical shapes are illustrative; their dollar range is enlarged for this promo.
  const targetReward=Math.round((8+random()*4)*100)/100,volatilityScale=targetReward/(5*calibrationProfile.rangeMedian),profile=Object.fromEntries(Object.entries(calibrationProfile).map(([key,value])=>[key,value*volatilityScale]));
  const gaussian=(a,b)=>Math.sqrt(-2*Math.log(Math.max(a,1e-9)))*Math.cos(2*Math.PI*b),normal=(salt,i)=>gaussian((hash(seed^salt,i)+.5)/4294967296,(hash(seed^salt^0x5b21a591,i)+.5)/4294967296);
  const origin=4133.48+(random()-.5)*14,positive=[origin],negative=[origin],ledger=[],sources=new WeakMap();let trades=0,directionRound=0,directionBag=[];
  function nextDirection(){if(!directionBag.length){const first=hash(seed^0x6f213b41,++directionRound)<2147483648?1:-1;directionBag=[first,-first];}return directionBag.pop();}
  // A single session-calibrated process is used before and after every signal.
  // Session-relative shapes use Dukascopy BID M1 observations, scaled to an $8–12 TP.
  function sigma(t){const x=t/300,i=Math.floor(x),u=x-i,vol=Math.exp(.38*mix(normal(0x6916a33,i),normal(0x6916a33,i+1),u));return profile.rangeMedian/(1.45*Math.sqrt(60))*clamp(vol,.55,1.9);}
  function drift(t){
   const x=t/90,i=Math.floor(x),u=x-i;
   return profile.rangeMedian*(.011*mix(normal(0x72dc95,i),normal(0x72dc95,i+1),u)+.0015*normal(0x38fa91,Math.floor(t/180)));
  }
  function ambient(time){
   const coordinate=time/TIMING.tickSeconds,index=Math.floor(coordinate),fraction=coordinate-index;
   function value(i){const list=i>=0?positive:negative,n=Math.abs(i),sign=i>=0?1:-1;while(list.length<=n){const k=list.length,t=(sign>0?k-1:-k)*TIMING.tickSeconds,step=sigma(t)*Math.sqrt(TIMING.tickSeconds)*normal(0x93a1,k*sign)+drift(t)*TIMING.tickSeconds;list.push(list[k-1]+sign*step);}return list[n];}
   return mix(value(index),value(index+1),fraction);
  }
  function atr(time){let total=0;const last=Math.floor(time/60)-1;for(let k=last-13;k<=last;k++){let lo=Infinity,hi=-Infinity;for(let t=k*60;t<=(k+1)*60;t+=1){const v=history(t);lo=Math.min(lo,v);hi=Math.max(hi,v);}total+=hi-lo;}return total/14;}
  function newTrade({at=123.4,entry=history(at),dir,risk,reward,reactionAt=at+120}={}){
   const previous=ledger.slice(),continuation=t=>sample(t,previous),firstTick=continuation(at+1);
   const localATR=atr(at);reward=reward??(risk==null?targetReward:risk*2);risk=risk??reward/2;
   // Larger missed moves are uncommon and only selected in an active tape.
   const strongMove=localATR>profile.rangeMedian*1.05&&hash(seed^0x73a6f259,trades+1)<4294967296*.05;
   const side=dir===1||dir===-1?dir:nextDirection(),horizon=480+Math.floor(random()*61),observedMove=strongMove?4+random()*(Math.min(7,reward-2)-4):3+random();let chosen=null,attempt=0;
   // Condition a noisy path on the two visible story moments. Each half keeps
   // the same tick variance as the backdrop; no jump or new tape at the signal.
   function bridge(start,end,length,offset){
    let noise=0;const values=[start];
    for(let t=1;t<=length;t++){noise+=sigma(at+offset+t)*gaussian(random(),random());values.push(noise);}
    const terminal=noise;
    for(let t=1;t<=length;t++)values[t]=start+(end-start)*t/length+values[t]-terminal*t/length;
    return values;
   }
   for(;attempt<4000&&!chosen;attempt++){
    const firstGain=(firstTick-entry)*side;
    const first=bridge(firstGain,observedMove,119,1),last=bridge(observedMove,reward+profile.rangeMedian*.8,horizon-120,120);
    const gains=[0,...first,...last.slice(1)];let ok=true,pullback=0,peak=0,redMinutes=0,finish=0;
    for(let t=1;t<=horizon;t++){
     const gain=gains[t];peak=Math.max(peak,gain);pullback=Math.max(pullback,peak-gain);
     if(gain<=-risk||t>=120&&gain<=0||t<=20&&Math.abs(gain)>1.2){ok=false;break;}
     if(gain>=reward){if(t<360||t>468){ok=false;}else{finish=t;gains[t]=reward;}break;}
    }
    if(!ok||!finish)continue;
    gains.length=finish+1;
    // Do not turn the signal candle or the final approach into a giant bar.
    const firstCandle=Math.floor(at/60),lastCandle=Math.floor((at+finish)/60);
    for(let k=firstCandle;k<=lastCandle;k++){
     const from=k*60,to=Math.min((k+1)*60,at+finish),values=[];
     for(let t=from;t<=to;t+=1)values.push(t<at?continuation(t):entry+side*gains[Math.min(finish,Math.max(0,Math.round(t-at)))]);
     const body=Math.abs(values.at(-1)-values[0]),range=Math.max(...values)-Math.min(...values);
     const known=from<at?[values[0],entry,...values.slice(0,Math.max(0,Math.ceil(at-from)))]:[],knownBody=known.length?Math.abs(entry-values[0]):0,knownRange=known.length?Math.max(...known)-Math.min(...known):0;
     const impulse=strongMove&&from<at+120?1.45:1;
     if(body>Math.max(2.7*impulse,profile.rangeMedian*1.5*impulse,knownBody+profile.rangeMedian*.4)||range>Math.max(3.8*impulse,profile.rangeMedian*2.2*impulse,knownRange+profile.rangeMedian*.5)){ok=false;break;}
     if(from>=at+120&&(values.at(-1)-values[0])*side<-.15)redMinutes++;
    }
    if(ok&&pullback>.5&&redMinutes>0)chosen={path:gains.map(gain=>entry+side*gain),duration:finish};
   }
   if(!chosen)throw new Error('No gradual demonstration interval found');
   dir=side;const sl=entry-dir*risk,tp=entry+dir*reward,duration=chosen.duration,path=chosen.path,wallDuration=duration/TIMING.rate;
   const trade={id:seed+'-'+(++trades),seed,at,entry,dir,sl,tp,duration,wallDuration,path,endAt:at+duration,exit:tp,outcome:'TP',reward,risk,lot:POSITION.lot,contractSize:POSITION.contractSize,reactionAt,atr:localATR,session,strongMove,attempts:attempt};
   trade.pnl=profit(trade,tp);sources.set(trade,continuation);ledger.push(trade);trade.reaction=price(trade,reactionAt);trade.move=Math.abs(trade.reaction-entry);return trade;
  }
  function sample(time,items){
   for(let i=items.length-1;i>=0;i--){const item=items[i];if(time<item.at)continue;
    if(time>=item.endAt)return item.exit+ambient(time)-ambient(item.endAt);
    if(time<item.at+1){const source=sources.get(item);return item.entry+source(time)-source(item.at);}
    const u=clamp((time-item.at)/item.duration)*(item.path.length-1),index=Math.floor(u);
    return mix(item.path[index],item.path[Math.min(index+1,item.path.length-1)],u-index);
   }
   return ambient(time);
  }
  function history(time){return sample(time,ledger);}
  function cancelPending(now){for(let i=ledger.length-1;i>=0;i--)if(ledger[i].at>now)ledger.splice(i,1);}
  function price(trade,time,hold=true){
   return hold&&trade&&time>=trade.endAt?trade.exit:history(time);
  }
  function snapshot(trade,time){const closed=time>=trade.endAt;return{closed,side:trade.dir>0?'BUY':'SELL',outcome:closed?trade.outcome:null,price:price(trade,time),lot:trade.lot,contractSize:trade.contractSize,pnl:closed?trade.pnl:profit(trade,price(trade,time)),endAt:trade.endAt,exit:closed?trade.exit:null};}
  return{seed,profile,calibrationProfile,volatilityScale,targetReward,session,ambient,atr,newTrade,price,snapshot,history,cancelPending};
 }
 function chartLayout(width,height,targetReward,scale=1){
  const panel=typeof document!=='undefined'?document.getElementById('player-panel'):null,footer=panel&&!panel.hidden?270:190;
  const top=Math.max(height*.30,Math.min(210/scale,height*.38)),bottom=Math.max(top+height*.22,Math.min(height*.76,height-footer/scale)),scaleY=(bottom-top)*.88/(targetReward*1.5);
  return{top,bottom,center:(top+bottom)/2,scaleY,spacing:14,bodyWidth:8,right:width-80};
 }
 const overflowLayers=new WeakMap();
 function compositeChart(output,layer,{width:W,height:H,layout,focus}){
  const source=layer.getContext('2d'),top=layout.top*focus,bottom=H+(layout.bottom-H)*focus,feather=Math.min(42,(bottom-top)*.18),outside=1-focus;
  output.save();output.clearRect(0,0,W,H);
  if('filter' in output){
   if(focus>.001){output.globalAlpha=.42*focus;output.filter='blur(5px)';output.drawImage(layer,0,0,layer.width,layer.height,0,0,W,H);output.filter='none';}
  }else{
   // Safari versions without CanvasFilter use the same frame with CSS blur.
   let blurred=overflowLayers.get(output.canvas);
   if(!blurred){blurred=document.createElement('canvas');blurred.className='market-overflow-blur';blurred.setAttribute('aria-hidden','true');const style=getComputedStyle(output.canvas);blurred.style.cssText='position:absolute;inset:0;width:100%;height:100%;pointer-events:none;filter:blur(5px);z-index:'+(style.zIndex==='auto'?'0':style.zIndex);if(style.position==='static'){output.canvas.style.position='relative';output.canvas.style.zIndex='0';}output.canvas.before(blurred);overflowLayers.set(output.canvas,blurred);}
   if(blurred.width!==layer.width||blurred.height!==layer.height){blurred.width=layer.width;blurred.height=layer.height;}
   const blurContext=blurred.getContext('2d');blurContext.clearRect(0,0,blurred.width,blurred.height);blurContext.drawImage(layer,0,0);blurred.style.opacity=String(.42*focus*(parseFloat(output.canvas.style.opacity)||1));
  }
  source.save();source.globalCompositeOperation='destination-in';
  const mask=source.createLinearGradient(0,0,0,H);
  mask.addColorStop(0,'rgba(0,0,0,'+outside+')');
  mask.addColorStop(Math.max(0,(top-feather)/H),'rgba(0,0,0,'+outside+')');
  mask.addColorStop(Math.min(1,(top+feather)/H),'#000');
  mask.addColorStop(Math.max(0,(bottom-feather)/H),'#000');
  mask.addColorStop(Math.min(1,(bottom+feather)/H),'rgba(0,0,0,'+outside+')');
  mask.addColorStop(1,'rgba(0,0,0,'+outside+')');
  source.fillStyle=mask;source.fillRect(0,0,W,H);source.restore();
  output.globalAlpha=1;output.drawImage(layer,0,0,layer.width,layer.height,0,0,W,H);output.restore();
 }
 function drawTradeOverlay(cx,{width:W,height:H,trade,layout,worldX,worldY,price,opacity,age,protectedLevel=1,reaction=0,bot=false,pulse=()=>{}}){
  if(!trade||opacity<=0||age<0)return;
  const tr=value=>window.LV_I18N?.t(value)||value,entryX=worldX(trade.at),entryY=worldY(trade.entry),currentY=worldY(price);
  cx.save();cx.globalAlpha=opacity;cx.lineWidth=1;
  cx.beginPath();cx.rect(0,layout.top,W,layout.bottom-layout.top);cx.clip();
  const levels=[{p:trade.entry,name:tr(bot?'БОТ · ВХОД':'ВХОД'),color:'#8fcaff',alpha:1},{p:trade.tp,name:'TP',color:'#99d6b0',alpha:protectedLevel},{p:trade.sl,name:'SL',color:'#f5aaa8',alpha:protectedLevel}];
  for(const level of levels){
   const y=worldY(level.p);cx.globalAlpha=opacity*level.alpha;
   cx.strokeStyle=level.color+'80';cx.setLineDash([4,5]);cx.beginPath();cx.moveTo(16,y);cx.lineTo(layout.right,y);cx.stroke();cx.setLineDash([]);
   // The line is never clamped into the screen or moved to follow the quote.
   cx.fillStyle=level.color;cx.font='600 8px Inter,Manrope,sans-serif';cx.fillText(level.name,layout.right+8,y-16);
   if(Math.abs(currentY-y)>26){cx.font='500 10px "IBM Plex Mono",monospace';cx.fillText(level.p.toFixed(2),layout.right+8,y-3);}
  }
  cx.globalAlpha=opacity;cx.strokeStyle='#89caff40';cx.setLineDash([2,4]);cx.beginPath();cx.moveTo(entryX,layout.top);cx.lineTo(entryX,layout.bottom);cx.stroke();cx.setLineDash([]);pulse(entryX,entryY,age,'#89caff');
  // MT5-style position text sits on the entry line, to the left of its marker.
  const positionText=(trade.dir>0?'BUY':'SELL')+' · '+formatLot(trade);
  cx.save();cx.font='500 8px "IBM Plex Mono",monospace';const positionWidth=cx.measureText(positionText).width,positionRight=entryX-12;
  cx.fillStyle='#030406';cx.fillRect(positionRight-positionWidth-4,entryY-6,positionWidth+8,12);
  cx.fillStyle='#b9ddff';cx.textAlign='right';cx.textBaseline='middle';cx.fillText(positionText,positionRight,entryY);cx.restore();
  if(reaction>0){
   const x=worldX(trade.reactionAt),y=worldY(trade.reaction);cx.globalAlpha=opacity*reaction;
   cx.fillStyle='#ffd18b';cx.shadowColor='#ffd18b';cx.shadowBlur=12;cx.beginPath();cx.arc(x,y,4.2,0,Math.PI*2);cx.fill();cx.shadowBlur=0;
   cx.strokeStyle='#ffd18b55';cx.setLineDash([2,4]);cx.beginPath();cx.moveTo(x,layout.top);cx.lineTo(x,layout.bottom);cx.stroke();cx.setLineDash([]);
   cx.strokeStyle='#ffd18b';cx.beginPath();cx.moveTo(x+8,entryY);cx.lineTo(x+13,entryY);cx.lineTo(x+13,y);cx.lineTo(x+8,y);cx.stroke();
  }
  cx.restore();
  if(reaction>0){
   // Keep explanatory text in its own band, outside candles and price levels.
   cx.save();cx.globalAlpha=opacity*reaction;cx.fillStyle='#ffd18b';cx.textAlign='left';cx.font='600 7px Inter,Manrope,sans-serif';cx.fillText(tr('ВЫ ОТКРЫЛИ КАНАЛ'),W*.08,layout.top-30);cx.font='500 12px "IBM Plex Mono",monospace';cx.fillText(trade.reaction.toFixed(2),W*.08,layout.top-12);cx.restore();
  }
  return{entryX,entryY,slY:worldY(trade.sl),tpY:worldY(trade.tp),reactionY:worldY(trade.reaction)};
 }
 function resultCard(container){
  const card=document.createElement('div');card.className='market-result';card.setAttribute('role','status');card.setAttribute('aria-live','polite');card.setAttribute('aria-hidden','true');
  card.innerHTML='<div><small class="result-label"></small><strong class="result-title"></strong><span class="result-detail"></span></div><b class="result-amount"></b>';
  container.append(card);let shown='';
  function update(trade,time,active){
   const tr=value=>window.LV_I18N?.t(value)||value,age=active&&trade?(time-trade.endAt)/TIMING.rate:-1,u=clamp((age-.35)/.8),reveal=u*u*(3-2*u),key=trade?.id;
   if(trade&&shown!==key){shown=key;const profit=trade.pnl>=0;card.classList.toggle('loss',!profit);card.querySelector('.result-label').textContent=tr('Сделка закрыта');card.querySelector('.result-title').textContent=tr(profit?'Прибыль':'Убыток');card.querySelector('.result-detail').textContent=trade.outcome+' · '+(trade.dir>0?'BUY':'SELL')+' XAUUSD · '+tr('Лот')+' '+formatLot(trade)+' · '+tr('Демонстрационный результат');card.querySelector('.result-amount').textContent=formatMoney(trade.pnl,true);}
   card.style.opacity=String(reveal);card.style.transform='translate3d(-50%,'+((1-reveal)*32).toFixed(2)+'px,0)';card.setAttribute('aria-hidden',String(reveal===0));card.dataset.outcome=reveal>0?trade.outcome:'';return reveal;
  }
  return{element:card,update};
 }
 function telegramNotice(container){
  const card=document.createElement('div');card.className='market-notice';card.setAttribute('role','status');card.setAttribute('aria-live','polite');card.setAttribute('aria-hidden','true');
  const avatar=document.createElement('img');avatar.className='notice-avatar';avatar.src=new URL('icons/telegram-mark.svg',assetBase).href;avatar.alt='Telegram';
  const bubble=document.createElement('div');bubble.className='notice-bubble';bubble.innerHTML='<small></small><strong></strong><span></span>';card.append(avatar,bubble);container.append(card);let shown='';
  const smooth=u=>{u=clamp(u);return u*u*(3-2*u);},pop=u=>{u=clamp(u);return 1+2.70158*Math.pow(u-1,3)+1.70158*Math.pow(u-1,2);};
  function update(trade,time,active){
   const tr=value=>window.LV_I18N?.t(value)||value,age=active&&trade?(time-trade.endAt)/TIMING.rate:-1,reduced=matchMedia('(prefers-reduced-motion: reduce)').matches;
   const av=clamp((age-.25)/.6),msg=clamp((age-.58)/.75),a=reduced?1:pop(av),b=reduced?1:pop(msg);
   if(trade&&shown!==trade.id){shown=trade.id;bubble.querySelector('small').textContent=tr('Канал сигналов');bubble.querySelector('strong').textContent=tr('TP получен');bubble.querySelector('span').textContent=(trade.dir>0?'BUY':'SELL')+' XAUUSD · '+trade.tp.toFixed(2);}
   avatar.style.opacity=String(smooth(av*2));avatar.style.transform='translateY('+((1-a)*14).toFixed(2)+'px) scale('+(.55+.45*a).toFixed(4)+')';
   bubble.style.opacity=String(smooth(msg*2));bubble.style.transform='translateX('+((1-b)*-24).toFixed(2)+'px) scale('+(.5+.5*b).toFixed(4)+')';
   card.style.opacity=String(age>.25?1:0);card.setAttribute('aria-hidden',String(av===0));card.dataset.outcome=av>0?'TP':'';return smooth(msg);
  }
  return{element:card,update};
 }
 window.LVMarketSim={create,randomSeed,compositeChart,chartLayout,drawTradeOverlay,resultCard,telegramNotice,TIMING,PROFILES,POSITION,profit,amounts,formatMoney,formatLot};
})();
