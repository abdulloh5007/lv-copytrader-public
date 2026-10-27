const {t:tr}=window.LV_I18N;
// Adapted directly from the original dist/film/index.html candle renderer.
// One canvas, one market, one world-space camera, including both automatic trade demonstrations.
export function createFilmMarket(host,{reducedMotion=()=>false}={}){
 const cv=document.createElement('canvas');cv.id='film-market';cv.setAttribute('aria-hidden','true');host.append(cv);
 const cx=cv.getContext('2d'),chartLayer=document.createElement('canvas'),chartCx=chartLayer.getContext('2d');if(!cx)throw new Error('Canvas 2D unavailable');
 const GB=4133.48,CW=14,PER=window.LVMarketSim.TIMING.candleSeconds,RATE=window.LVMarketSim.TIMING.rate;
 let W=432,H=768,scale=1,camX=0,camY=0,snapCamera=true;
 let view={scaleX:1,scaleY:H*.14},trade=null,replayStart=0,clockOffset=0,lastIndex=-1,lastNow=120,lastClock=0,lastAmbient=0,restartMode=-1,holding=false,frozen=false;
 const seedQuery=new URLSearchParams(location.search).get("seed");const simulation=window.LVMarketSim.create(seedQuery===null?undefined:+seedQuery);
 const clamp=v=>Math.max(0,Math.min(1,v));
 const smooth=v=>{v=clamp(v);return v*v*(3-2*v);};
 const mix=(a,b,t)=>a+(b-a)*t;
 function baseMarket(t){return simulation.ambient(t);}
 function prepareTrade(start,bot){
  trade=simulation.newTrade({at:start+(bot?3.7:3.4)*RATE});frozen=false;
 }
 function market(t){return simulation.price(trade,t,holding);}
 // Closed candles are immutable for one simulated trade. Reuse their exact OHLC values.
 const candleCache=new Map();let candleContext='';
 function candle(k,now){
  const context=String(trade?.id||'ambient')+':'+holding;
  if(context!==candleContext){candleCache.clear();candleContext=context;}
  const from=k*PER,to=Math.min((k+1)*PER,now),closed=to===(k+1)*PER;
  if(closed&&candleCache.has(k))return candleCache.get(k);
  const o=market(from),c=market(to);let hi=Math.max(o,c),lo=Math.min(o,c);
  for(let q=from;q<to;q+=window.LVMarketSim.TIMING.tickSeconds){const v=market(q);hi=Math.max(hi,v);lo=Math.min(lo,v);}
  const value={from,o,c,hi,lo};
  if(closed){candleCache.set(k,value);if(candleCache.size>256)candleCache.delete(candleCache.keys().next().value);}
  return value;
 }
 function layout(){return window.LVMarketSim.chartLayout(W,H,simulation.targetReward,Math.min(innerWidth/432,innerHeight/768));}
 function marketScale(){return layout().scaleY;}
 function wy(price){return -(price-GB)*view.scaleY;}
 function worldX(time){return time/PER*CW*view.scaleX-camX;}
 function worldY(price){return wy(price)-camY;}
 function cameraTargets(now,phase){
  const price=market(now),frame=layout(),zoom=1+.5*(1-phase.focus),sy=marketScale()*zoom,liveY=-(price-GB)*sy-frame.center;
  const fixedY=trade?-((trade.sl+trade.tp)/2-GB)*sy-frame.center:liveY;
  return{x:now/PER*CW*zoom-W*.68,y:mix(liveY,fixedY,phase.focus),scaleX:zoom,scaleY:sy};
 }
 function roundedRect(x,y,w,h,r){cx.beginPath();cx.roundRect(x,y,w,h,r);}
 function dot(x,y,color){cx.fillStyle=color;cx.shadowColor=color;cx.shadowBlur=12;cx.beginPath();cx.arc(x,y,4.5,0,Math.PI*2);cx.fill();cx.shadowBlur=0;}
 function entryAccent(x,y,age,color){
  if(age<0)return;const pulse=smooth(age/.18)*(1-smooth((age-.35)/1.25));
  if(pulse>0&&!reducedMotion()){cx.save();cx.globalAlpha*=pulse;cx.strokeStyle=color;cx.lineWidth=1.8;for(const delay of [0,.3]){const u=clamp((age-delay)/1.35);if(age<delay)continue;cx.beginPath();cx.arc(x,y,7+29*u,0,Math.PI*2);cx.stroke();}cx.restore();}
  cx.save();cx.shadowColor=color;cx.shadowBlur=18;cx.fillStyle=color;cx.beginPath();cx.arc(x,y,4.5+3*pulse,0,Math.PI*2);cx.fill();cx.restore();
 }
 function drawOverlay(now,phase){
  if(!trade)return;
  const age=phase.t-(phase.bot?3.7:3.4),reveal=smooth(age/.38),reaction=smooth((phase.rawNow-trade.reactionAt)/(.7*RATE));
  const result=window.LVMarketSim.drawTradeOverlay(cx,{width:W,height:H,trade,layout:layout(),worldX,worldY,price:market(now),opacity:phase.focus*reveal,age,protectedLevel:phase.bot?smooth((age-1.1)/.65):1,reaction,bot:phase.bot,pulse:entryAccent});
  if(result)for(const [key,value] of Object.entries(result))host.dataset[key]=(value*scale).toFixed(2);
  host.dataset.botOpened=reveal.toFixed(3);host.dataset.botProtected=smooth((age-1.1)/.65).toFixed(3);
 }
 function drawBackground(now,phase,dt){
  // The unfocused backdrop needs no feather mask or second full-resolution canvas copy.
  // Paint it directly; keep the original layered compositor for focused trade scenes.
  const cx=phase.focus===0?cv.getContext('2d'):chartCx;
  const price=market(now),target=cameraTargets(now,phase),closed=holding&&trade&&now>=trade.endAt,ease=snapCamera||reducedMotion()?1:1-Math.exp(-Math.max(dt,.001)*4.6);
  // One camera glides from the background into the same candles; the canvas is never swapped.
  if(!frozen||!closed||snapCamera){view.scaleX=phase.focus>0?target.scaleX:view.scaleX+(target.scaleX-view.scaleX)*ease;view.scaleY=phase.focus>0?target.scaleY:view.scaleY+(target.scaleY-view.scaleY)*ease;camX+=(target.x-camX)*ease;camY=phase.focus>.999?target.y:camY+(target.y-camY)*ease;}frozen=closed;snapCamera=false;
  cx.globalAlpha=1;cx.clearRect(0,0,W,H);cx.save();const plot=layout();cx.lineWidth=1;cx.strokeStyle='rgba(255,255,255,.045)';
  const stepRaw=65/view.scaleY,stepPrice=stepRaw<1?1:stepRaw<2?2:stepRaw<5?5:10;
  const high=GB-camY/view.scaleY+50/view.scaleY,low=GB-(H+camY)/view.scaleY-50/view.scaleY;
  for(let p=Math.floor(low/stepPrice)*stepPrice;p<=high;p+=stepPrice){const yy=worldY(p);cx.beginPath();cx.moveTo(0,yy);cx.lineTo(W,yy);cx.stroke();}
  let maxBody=0;const first=Math.floor((camX-50)/(CW*view.scaleX))-1,current=Math.floor(now/PER);
  for(let gx=Math.floor(first/3)*3;gx<=current+30;gx+=3){const xx=worldX(gx*PER);if(xx>W+50)break;cx.beginPath();cx.moveTo(xx,0);cx.lineTo(xx,H);cx.stroke();}
  for(let k=first;k<=current;k++){
   const {from,o,c,hi,lo}=candle(k,now);
   const x=worldX(from),width=CW*view.scaleX,m=x+width/2,yo=worldY(o),yc=worldY(c),up=c>=o,bw=Math.max(3,8*view.scaleX);
   if(x>-CW&&x<W&&Math.max(yo,yc)>0&&Math.min(yo,yc)<H)maxBody=Math.max(maxBody,Math.abs(yc-yo));
   cx.strokeStyle=k===current?'rgba(142,197,255,.85)':up?'rgba(237,237,237,.38)':'rgba(237,237,237,.27)';cx.lineWidth=1.5;
   cx.beginPath();cx.moveTo(m,worldY(hi));cx.lineTo(m,worldY(lo));cx.stroke();
   cx.fillStyle=up?'#030507':'rgba(237,237,237,.20)';cx.fillRect(m-bw/2,Math.min(yo,yc),bw,Math.max(2,Math.abs(yc-yo)));cx.strokeRect(m-bw/2,Math.min(yo,yc),bw,Math.max(2,Math.abs(yc-yo)));
  }
  const y=worldY(price);cx.setLineDash([4,5]);cx.strokeStyle='rgba(116,189,255,.30)';cx.beginPath();cx.moveTo(0,y);cx.lineTo(W,y);cx.stroke();cx.setLineDash([]);
  cx.fillStyle='#9acaff';cx.fillRect(W-65,y-11,64,22);cx.fillStyle='#07121d';cx.font='500 10px "IBM Plex Mono",monospace';cx.textAlign='center';cx.textBaseline='middle';cx.fillText(price.toFixed(2),W-33,y);cx.textAlign='left';cx.textBaseline='alphabetic';
  cx.restore();if(phase.focus!==0)window.LVMarketSim.compositeChart(cv.getContext('2d'),chartLayer,{width:W,height:H,layout:plot,focus:phase.focus});drawOverlay(now,phase);host.dataset.overflow='blurred';host.dataset.maxBodyHeight=(maxBody*scale).toFixed(2);host.dataset.barWidth=(8*view.scaleX*scale).toFixed(2);host.dataset.demo=phase.bot?'bot':phase.focus>0?'human':'background';host.dataset.latestCandle=String(current);host.dataset.scaleY=view.scaleY.toFixed(3);host.dataset.cameraX=camX.toFixed(3);host.dataset.cameraY=camY.toFixed(3);host.dataset.price=price.toFixed(2);host.dataset.focus=phase.focus.toFixed(3);host.dataset.engine='original-film-canvas';
  return price;
 }
 function resize(){
  scale=Math.min(innerWidth/432,innerHeight/768);W=innerWidth/scale;H=innerHeight/scale;const dpr=Math.min(devicePixelRatio||1,2);cv.width=Math.round(innerWidth*dpr);cv.height=Math.round(innerHeight*dpr);cx.setTransform(dpr*scale,0,0,dpr*scale,0,0);chartLayer.width=cv.width;chartLayer.height=cv.height;chartCx.setTransform(dpr*scale,0,0,dpr*scale,0,0);snapCamera=true;view.scaleY=marketScale();
 }
 function render(index,u,ambient,storyTime=0){
  const clock=reducedMotion()?0:ambient,dt=Math.max(0,Math.min(.1,clock-lastAmbient));lastAmbient=clock;
  const demo=index===1||index===6,wasDemo=lastIndex===1||lastIndex===6,bot=index===6,duration=92,t=u*duration,runTime=reducedMotion()?Math.min(storyTime,duration):storyTime;
  if(lastIndex!==index||restartMode===index){
   const current=lastIndex<0?120+clock*RATE+clockOffset:lastNow+(frozen&&wasDemo?0:dt*RATE);
   simulation.cancelPending(current);
   if(demo){replayStart=current-runTime*RATE;prepareTrade(replayStart,bot);}
   else clockOffset=current-(120+clock*RATE);
   restartMode=-1;frozen=false;
  }
  const focus=demo?(bot?smooth((t-.2)/1.4):smooth((t-3.2)/.8)):0;
  holding=demo;const rawNow=demo?(replayStart+runTime*RATE):120+clock*RATE+clockOffset;
  const now=demo?Math.min(rawNow,trade.endAt):rawNow;
  const phase={bot,rawNow,t:demo?t:-1,focus,zoom:demo?(bot?smooth((t-7)/3):smooth((t-6.5)/3.1)):0};
  const price=drawBackground(now,phase,dt);lastIndex=index;lastNow=now;lastClock=clock;host.dataset.closed=String(!!(trade&&demo&&rawNow>=trade.endAt));host.dataset.side=trade?.dir>0?"BUY":"SELL";host.dataset.outcome=trade&&demo&&rawNow>=trade.endAt?trade.outcome:"";host.dataset.marketTime=now.toFixed(4);host.dataset.marketSeed=simulation.seed;if(trade){host.dataset.lot=String(trade.lot);host.dataset.pnl=String(simulation.snapshot(trade,rawNow).pnl);host.dataset.tradeId=trade.id;host.dataset.entry=trade.entry.toFixed(2);host.dataset.sl=trade.sl.toFixed(2);host.dataset.tp=trade.tp.toFixed(2);host.dataset.endAt=trade.endAt;host.dataset.rawNow=rawNow.toFixed(4);host.dataset.reactionAt=trade.reactionAt;host.dataset.reactionVisible=String(rawNow>=trade.reactionAt);host.dataset.rate=RATE;}
  return{price,entry:trade?.entry??4133.48,botEntry:trade?.entry??4133.48,sl:trade?.sl??4121.08,tp:trade?.tp??4158.28,reaction:trade?.reaction??4148.19,elapsed:trade?Math.round(clamp((rawNow-trade.at)/(20*RATE))*120):0,focus,zoom:phase.zoom,dir:trade?.dir??1,trade,now,rawNow,status:trade?simulation.snapshot(trade,rawNow):null};
 }
 function restart(index){restartMode=index;}
 resize();window.addEventListener('resize',resize);host.dataset.engine='original-film-canvas';
 return{render,restart,canvas:cv};
}
