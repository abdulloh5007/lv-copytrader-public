/* One reversible story clock: receive, travel, recognize, hold, travel, execute. */
(function(){
 'use strict';
 var clamp=function(v){return Math.max(0,Math.min(1,v));},smooth=function(v){v=clamp(v);return v*v*(3-2*v);};
 function plan(entry){return{entry:entry,telegram:entry-3.1,firstStart:entry-2.65,bot:entry-1.75,secondStart:entry-1.05,mt:entry-.22};}
 function flash(t,at){var age=t-at;return age<0?0:smooth(age/.09)*(1-smooth((age-.09)/.65));}
 function state(t,entry){
  var p=plan(entry),first=smooth((t-p.firstStart)/(p.bot-p.firstStart)),second=smooth((t-p.secondStart)/(p.mt-p.secondStart));
  return{received:smooth((t-p.telegram)/.22),recognized:smooth((t-p.bot)/.28),mtReady:smooth((t-p.mt)/.18),executed:smooth((t-entry)/.38),
   route:(first+second)/2,packet:(t>=p.firstStart&&t<p.bot)||(t>=p.secondStart&&t<p.mt)?1:0,
   flashes:[flash(t,p.telegram),flash(t,p.bot),flash(t,p.mt)],
   phase:t<p.telegram?'waiting':t<p.firstStart?'received':t<p.bot?'to-bot':t<p.secondStart?'recognized':t<p.mt?'to-mt5':t<entry?'connected':'executed'};
 }
 function cards(t,timings){return timings.map(function(at){return{ready:smooth((t-at)/.32),flash:flash(t,at)};});}
 function bell(t,at){var age=t-at;if(age<0||age>1.25)return 0;return Math.sin(age*31)*Math.exp(-age*3.4)*19*smooth(age/.05)*(1-smooth((age-1)/.25));}
 window.LVSignalRoute={plan:plan,state:state,cards:cards,bell:bell};
})();
