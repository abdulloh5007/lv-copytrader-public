/* The NFT Toys-style local card scene follows the movie's playback clock. */
(function(){
 'use strict';
 function clamp(v){return Math.max(0,Math.min(1,v));}
 function smooth(v){v=clamp(v);return v*v*(3-2*v);}
 function cubeProgress(time){return smooth((time-6)/1.2);}
 window.FilmMotionMath={cubeProgress:cubeProgress};
 window.createFilmMotion=function(options){
  var scenes=options.scenes,stage=options.stage,reduced=matchMedia('(prefers-reduced-motion: reduce)'),viewport=document.querySelector('.film-cube-viewport'),cube=document.querySelector('.film-cube'),faces=Array.from(cube.querySelectorAll('.film-cube-face')),half=172;
  var cards=Array.from(document.querySelectorAll('.node,.cap,.card,.bubble,.stat,.deal,.pill,.solution-orb')).map(function(card,index){
   var scene=scenes.indexOf(card.closest('.s'));card.classList.add('film-depth');
   var trace=card===document.querySelector('.flow .node')||card.classList.contains('film-cube-face')||card===document.querySelectorAll('.grid .cap')[2];
   var rig=window.CardRelief.mount(card,{surfaceClass:'film-surface',layerAttribute:'data-film-depth',input:scene===4?viewport:card,phase:scene===4?0:index*1.17,trace:trace});
   return{el:card,scene:scene,relief:rig};
  });
  function resize(){window.CardRelief.fitFaces(viewport,faces);half=viewport.offsetWidth/2;cube.style.setProperty('--cube-width',viewport.offsetWidth+'px');}
  function seek(clock){cards.forEach(function(card){card.relief.seek(clock);});}
  function render(index,time,clock,playing){
   var turn=cubeProgress(time),turning=index===4&&turn>0&&turn<1;
   if(index===4){
    window.CardRelief.sizeCube(viewport,turn);viewport.style.opacity=smooth((time-.8)/.65);cube.dataset.angle=(turn*90).toFixed(2);cube.classList.toggle('turning',turning);cube.style.rotate='none';
    cube.style.transform=reduced.matches?'none':'translateZ('+(-half)+'px) rotateY('+(-turn*90)+'deg)';
    faces.forEach(function(face,k){var active=k===(turn<.5?0:1);face.setAttribute('aria-hidden',String(!active));face.inert=!active;face.style.visibility=reduced.matches||!turning?(active?'visible':'hidden'):'visible';face.style.opacity='1';});
   }
   cards.forEach(function(card){
    if(card.scene!==index)return;
    var gate=index===1?1-smooth((time-3.2)/.8):1;
    if(index===4)gate*=1-Math.sin(turn*Math.PI)*.85;
    var delay=parseFloat(card.el.style.getPropertyValue('--d'))||0,enter=smooth((time-delay)/(card.el.classList.contains('q')?.5:.8));
    var entrance=reduced.matches?1:Math.min(smooth(time/.65),enter);
    if(index===1)entrance=Math.min(entrance,card.el.id==='signal-card'?smooth((time-.2)/.65):smooth((time-1)/.65));
    if(index===4)entrance=reduced.matches?1:Math.min(smooth(time/.65),smooth((time-.8)/.65));
    card.el.style.rotate='none';card.el.style.translate='none';card.relief.render(clock,{playing:playing,gate:gate,entrance:entrance});
    if(index===3||index===5)card.el.style.transform=reduced.matches?'none':'translate3d(0,0,0)';
   });
   if(index===7){var orbit=document.getElementById('watch-orbit');orbit.style.transform=reduced.matches?'none':'perspective(900px) rotateY('+(Math.sin(clock*.35)*7)+'deg)';}
   stage.dataset.motionClock=clock.toFixed(3);
  }
  resize();document.fonts?.ready.then(resize);window.addEventListener('resize',resize);return{render:render,seek:seek};
 };
})();
