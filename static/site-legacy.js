/* Creation Films: fallbacks for old TV browsers (no flex-gap / aspect-ratio). Does nothing on modern browsers. */
(function(){
'use strict';
var D={"ar": [["", ".hero.is-reel .hero-video", 9.0, 16.0], ["", ".story .lead-ph .arch", 4.0, 5.0], ["", ".story .smalls .sm", 3.0, 4.0], ["", ".screen .poster", 21.0, 9.0], ["", ".ep .th", 16.0, 10.0], ["", ".film-cell .frame", 16.0, 9.0], ["", ".phone", 9.0, 16.0], ["", ".arch-card", 3.0, 4.4], ["", ".gram a", 1.0, 1.0], ["", ".pf-item", 4.0, 5.0], ["", ".about-img", 4.0, 5.0], ["", ".cg-cover", 4.0, 5.0], ["", ".modal-frame", 16.0, 9.0], ["(max-width:760px)", ".hero.is-reel .hero-video", 0, 0], ["(max-width:760px)", ".screen .poster", 16.0, 10.0], ["", ".scene-smalls div", 3.0, 4.0], ["", ".arch-card", 3.0, 4.3], ["(max-width:1000px)", ".scene-img", 4.0, 5.0], ["", ".arches.cat-tiles .arch-card", 4.0, 5.0], ["", ".album-img", 4.0, 5.0], ["", ".vid-thumb", 16.0, 9.0], ["", ".vid-thumb.tall", 9.0, 16.0], ["", ".mz", 4.0, 5.0], ["", ".mz-note", 4.0, 5.0], ["", ".lw-photo img", 4.0, 5.0], ["", ".ig-grid a", 1.0, 1.0], ["", ".tm-photo img", 5.0, 6.0], ["(max-width:760px)", ".mz, .mz-note", 3.0, 4.0], ["", ".ai-a img", 4.0, 5.0], ["", ".ai-b img", 4.0, 5.0]], "gap": [["", ".eyebrow", 14.0, 14.0], ["", ".btn", 12.0, 12.0], ["", ".nav", 38.0, 38.0], ["", ".hero-actions", 14.0, 14.0], ["", ".scroll-cue", 14.0, 14.0], ["", ".hero-ctrl", 10.0, 10.0], ["", ".ctrl", 9.0, 9.0], ["", ".ribbon", 20.0, 20.0], ["", ".ribbon span", 20.0, 20.0], ["", ".story", 28.0, 0], ["", ".story .num", 14.0, 14.0], ["", ".story .meta", 18.0, 18.0], ["", ".story .smalls", 16.0, 16.0], ["", ".cinema", 30.0, 30.0], ["", ".ep", 16.0, 16.0], ["", ".strip", 26.0, 26.0], ["", ".arrows", 12.0, 12.0], ["", ".arches", 22.0, 22.0], ["", ".gram", 6.0, 6.0], ["", ".footer-top", 50.0, 50.0], ["", ".footer ul", 13.0, 13.0], ["", ".copyright", 14.0, 14.0], ["", ".pills", 12.0, 12.0], ["", ".pf-grid", 24.0, 24.0], ["", ".about-grid", 90.0, 90.0], ["", ".contact-grid", 80.0, 80.0], ["", ".cg-grid", 30.0, 30.0], ["(max-width:1000px)", ".story", 0, 34.0], ["(max-width:1000px)", ".cg-grid", 20.0, 20.0], ["(max-width:1000px)", ".pf-grid", 14.0, 14.0], ["(max-width:1000px)", ".about-grid, .contact-grid", 50.0, 50.0], ["(max-width:760px)", ".nav", 10.0, 10.0], ["(max-width:760px)", ".ribbon", 14.0, 22.0], ["(max-width:760px)", ".ep", 12.0, 12.0], ["(max-width:760px)", ".strip", 16.0, 16.0], ["(max-width:760px)", ".arches", 12.0, 12.0], ["(max-width:760px)", ".gram", 4.0, 4.0], ["(max-width:760px)", ".cg-grid", 12.0, 12.0], ["(max-width:760px)", ".footer-top", 36.0, 36.0], ["", ".scene-txt .meta", 20.0, 20.0], ["", ".scene-smalls", 12.0, 12.0], ["", ".link-line", 18.0, 18.0], ["", ".crumbs", 12.0, 12.0], ["", ".arches.cat-tiles", 26.0, 26.0], ["", ".album-grid", 40.0, 28.0], ["", ".vid-grid", 40.0, 28.0], ["(max-width:760px)", ".arches.cat-tiles", 12.0, 12.0], ["(max-width:760px)", ".album-grid", 30.0, 14.0], ["(max-width:760px)", ".vid-grid", 34.0, 34.0], ["(max-width:760px)", ".vid-grid.reels", 24.0, 14.0], ["", ".mosaic-grid", 2.0, 2.0], ["", ".mz-note", 6.0, 6.0], ["", ".lw-grid", 76.8, 76.8], ["", ".lw-by", 18.0, 18.0], ["", ".ig-profile", 22.0, 22.0], ["", ".ig-grid", 11.0, 11.0], ["", ".tm-row", 89.6, 89.6], ["(max-width:760px)", ".ig-profile", 14.0, 14.0], ["(max-width:1280px)", ".nav", 26.0, 26.0], ["(max-width:1120px)", ".nav", 10.0, 10.0], ["", ".ai-grid", 51.2, 0], ["(max-width:760px)", ".ai-grid", 0, 22.0], ["", ".ig-grid", 11.0, 11.0]]};
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
}
if(d.readyState==='loading')d.addEventListener('DOMContentLoaded',start); else start();
})();
