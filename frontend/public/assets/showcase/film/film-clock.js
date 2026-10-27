/* Film-only time mapping. All price samples come from the original market tape. */
(function(){
 'use strict';
 const observation=10,sourceObservation=20,normalFactor=2,multiplier=10,ramp=.6,hold=2;
 const smooth=x=>{x=Math.max(0,Math.min(1,x));return x*x*(3-2*x);};
 function source(elapsed,multiplier){
  if(elapsed<=0)return elapsed;
  if(elapsed<=observation)return elapsed*normalFactor;
  const d=elapsed-observation;
  if(d>=ramp)return sourceObservation+normalFactor*(d*multiplier-(multiplier-1)*ramp/2);
  const u=d/ramp;return sourceObservation+normalFactor*(d+(multiplier-1)*ramp*(u*u*u-u*u*u*u/2));
 }
 function wall(seconds,multiplier){
  if(seconds<=0)return seconds;
  if(seconds<=sourceObservation)return seconds/normalFactor;
  let lo=observation,hi=Math.max(seconds,observation+1);
  for(let i=0;i<52;i++){const mid=(lo+hi)/2;if(source(mid,multiplier)<seconds)lo=mid;else hi=mid;}
  return (lo+hi)/2;
 }
 function plan(trade,mark,rate,ready){
  const close=mark+wall(trade.duration/rate,multiplier),notice=close+ready,end=notice+hold;
  return{close,notice,end,multiplier,reaction:mark+observation,
   clock(local){return local>=close?trade.endAt+(local-close)*rate:trade.at+source(local-mark,multiplier)*rate;},
   speed(local){return local>=close?1:1+(multiplier-1)*smooth((local-mark-observation)/ramp);},
   indicator(local){return smooth((local-mark-observation)/.25)*(1-smooth((local-close-.4)/.4));}
  };
 }
 window.LVFilmClock={source,wall,plan,observation,sourceObservation,normalFactor,multiplier,ramp,hold};
})();
