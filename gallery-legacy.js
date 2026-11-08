/* Creation Films gallery: fallbacks for old TV browsers (no flex-gap / aspect-ratio). Does nothing on modern browsers. */
(function(){
'use strict';
var D={"ar": [["", ".tile", 1.0, 1.0], ["", ".thumb-grid .thumb", 1.0, 1.0], ["", ".masonry .tile", 0, 0], ["", ".video-frame", 16.0, 9.0]], "gap": [["", ".gallery-header", 12.0, 12.0], ["", ".toolbar", 10.0, 10.0], ["", ".grid", 6.0, 6.0], ["", ".lightbox .lb-bar", 16.0, 16.0], ["", ".new-gallery-form", 10.0, 10.0], ["", ".thumb-grid", 6.0, 6.0], ["", ".upload-progress .info", 10.0, 10.0], ["", ".failed-upload", 10.0, 10.0], ["", ".icon-bar .icon-group", 18.0, 18.0], ["", ".icon-bar button", 6.0, 6.0], ["", ".masonry", 0, 8.0], ["", ".masonry-column", 8.0, 8.0], ["", ".hero-loading-box", 10.0, 10.0], ["", ".client-video-grid", 22.0, 22.0], ["(max-width:600px)", ".client-video-grid", 14.0, 14.0], ["(max-width:600px)", "#collectionsGrid", 10.0, 10.0], ["", ".footer-links", 12.0, 12.0], ["", ".footer-link", 9.0, 9.0], ["(max-width:600px)", ".footer-links", 8.0, 8.0]]};
var d=document, root=d.documentElement;
function flexGapOk(){try{var a=d.createElement('div');a.style.cssText='display:flex;flex-direction:column;row-gap:1px;position:absolute;visibility:hidden';a.appendChild(d.createElement('div'));a.appendChild(d.createElement('div'));a.firstChild.style.height='1px';a.lastChild.style.height='1px';d.body.appendChild(a);var ok=a.scrollHeight===3;d.body.removeChild(a);return ok}catch(e){return true}}
var needAR=!(window.CSS&&CSS.supports&&CSS.supports('aspect-ratio','1/1'));
var needGap=false, setAR=[], setGap=[];
function mq(m){if(!m)return true;try{return window.matchMedia?window.matchMedia(m).matches:true}catch(e){return true}}
function q(sel){try{return d.querySelectorAll(sel)}catch(e){return[]}}
function clear(){
  setAR.forEach(function(el){el.style.height=''}); setAR=[];
  setGap.forEach(function(o){o.el.style.marginLeft=o.l;o.el.style.marginTop=o.t;o.el.style.marginRight=o.r;o.el.style.marginBottom=o.b}); setGap=[];
}
function run(){
  clear();
  if(needAR){
    var map=[];
    D.ar.forEach(function(r){ if(!mq(r[0]))return; [].forEach.call(q(r[1]),function(el){el.__ar=r[2]?[r[2],r[3]]:null; if(map.indexOf(el)<0)map.push(el)}) });
    map.forEach(function(el){ var a=el.__ar; if(!a)return; var w=el.offsetWidth; if(w>0){el.style.height=(w*a[1]/a[0])+'px'; setAR.push(el)} });
  }
  if(needGap){
    var gm=[];
    D.gap.forEach(function(r){ if(!mq(r[0]))return; [].forEach.call(q(r[1]),function(el){el.__gap=[r[2],r[3]]; if(gm.indexOf(el)<0)gm.push(el)}) });
    gm.forEach(function(el){
      var cs=window.getComputedStyle(el), disp=cs.display; if(disp!=='flex'&&disp!=='inline-flex')return;
      var g=el.__gap, col=g[0], row=g[1], column=(cs.flexDirection||'').indexOf('column')===0, wrap=(cs.flexWrap||'nowrap')!=='nowrap';
      var kids=[].filter.call(el.children,function(k){return window.getComputedStyle(k).position!=='absolute'&&window.getComputedStyle(k).display!=='none'});
      kids.forEach(function(k,i){
        setGap.push({el:k,l:k.style.marginLeft,t:k.style.marginTop,r:k.style.marginRight,b:k.style.marginBottom});
        if(wrap){ if(i<kids.length-1)k.style.marginRight=col+'px'; k.style.marginBottom=row+'px'; }
        else if(column){ if(i>0)k.style.marginTop=row+'px'; }
        else { if(i>0)k.style.marginLeft=col+'px'; }
      });
    });
  }
}
var t=null; function later(){clearTimeout(t); t=setTimeout(run,120)}
function start(){
  needGap=!flexGapOk()||!!window.__cfForce;
  if(!needAR&&!needGap)return;
  root.classList.add('legacy-poly');
  run();
  window.addEventListener('resize',later);
  window.addEventListener('load',function(){run(); setTimeout(run,600)});
  d.addEventListener('load',later,true); // every image load can change widths
  try{ new MutationObserver(later).observe(d.body,{childList:true,subtree:true}) }catch(e){}
}
if(d.readyState==='loading')d.addEventListener('DOMContentLoaded',start); else start();
})();
