/* Small polyfills so the site also works on old TV browsers. Each one only installs itself when the method is missing. */
(function(){
'use strict';
var P=Array.prototype,S=String.prototype,E=Element.prototype,O=Object;
function def(o,k,f){if(!o[k])try{Object.defineProperty(o,k,{value:f,writable:true,configurable:true})}catch(e){o[k]=f}}
if(typeof Promise!=='undefined'){
  def(Promise,'allSettled',function(a){return Promise.all([].map.call(a,function(p){return Promise.resolve(p).then(function(v){return{status:'fulfilled',value:v}},function(r){return{status:'rejected',reason:r}})}))});
  def(Promise.prototype,'finally',function(f){var C=this.constructor||Promise;return this.then(function(v){return C.resolve(f()).then(function(){return v})},function(r){return C.resolve(f()).then(function(){throw r})})});
}
def(P,'includes',function(x,i){return this.indexOf(x,i)>-1});
def(P,'find',function(f,t){for(var i=0;i<this.length;i++)if(f.call(t,this[i],i,this))return this[i]});
def(P,'findIndex',function(f,t){for(var i=0;i<this.length;i++)if(f.call(t,this[i],i,this))return i;return -1});
def(P,'flat',function(d){d=d===undefined?1:d;var r=[];(function f(a,n){for(var i=0;i<a.length;i++){if(Array.isArray(a[i])&&n>0)f(a[i],n-1);else r.push(a[i])}})(this,d);return r});
def(P,'flatMap',function(f,t){return P.map.call(this,f,t).flat(1)});
if(!Array.from)Array.from=function(a,f,t){var r=[],n=a&&a.length!==undefined?a.length:0;for(var i=0;i<n;i++)r.push(f?f.call(t,a[i],i):a[i]);return r};
if(!Array.of)Array.of=function(){return P.slice.call(arguments)};
def(O,'assign',function(t){for(var i=1;i<arguments.length;i++){var s=arguments[i];if(s)for(var k in s)if(O.prototype.hasOwnProperty.call(s,k))t[k]=s[k]}return t});
if(!O.assign)O.assign=function(t){for(var i=1;i<arguments.length;i++){var s=arguments[i];if(s)for(var k in s)if(O.prototype.hasOwnProperty.call(s,k))t[k]=s[k]}return t};
if(!O.values)O.values=function(o){return O.keys(o).map(function(k){return o[k]})};
if(!O.entries)O.entries=function(o){return O.keys(o).map(function(k){return[k,o[k]]})};
if(!O.fromEntries)O.fromEntries=function(a){var o={};[].forEach.call(a,function(e){o[e[0]]=e[1]});return o};
def(S,'includes',function(s,i){return this.indexOf(s,i)>-1});
def(S,'startsWith',function(s,i){i=i||0;return this.substr(i,s.length)===s});
def(S,'endsWith',function(s,l){var t=l===undefined?this.length:l;return this.substring(t-s.length,t)===s});
def(S,'repeat',function(n){return new Array(n+1).join(this)});
def(S,'padStart',function(n,c){var s=String(this);c=c===undefined?' ':String(c);while(s.length<n)s=c+s;return s.slice(s.length-Math.max(n,String(this).length))});
def(S,'padEnd',function(n,c){var s=String(this);c=c===undefined?' ':String(c);while(s.length<n)s=s+c;return s});
def(S,'replaceAll',function(a,b){return typeof a==='string'?this.split(a).join(b):this.replace(a,b)});
def(S,'trimStart',function(){return this.replace(/^\s+/,'')});
def(S,'trimEnd',function(){return this.replace(/\s+$/,'')});
def(E,'matches',E.msMatchesSelector||E.webkitMatchesSelector||function(s){var m=(this.document||this.ownerDocument).querySelectorAll(s),i=m.length;while(--i>=0&&m[i]!==this);return i>-1});
def(E,'closest',function(s){var e=this;while(e&&e.nodeType===1){if(e.matches(s))return e;e=e.parentElement}return null});
def(E,'remove',function(){if(this.parentNode)this.parentNode.removeChild(this)});
function nodes(a){var f=document.createDocumentFragment();[].forEach.call(a,function(n){f.appendChild(typeof n==='string'?document.createTextNode(n):n)});return f}
def(E,'append',function(){this.appendChild(nodes(arguments))});
def(E,'prepend',function(){this.insertBefore(nodes(arguments),this.firstChild)});
def(E,'after',function(){if(this.parentNode)this.parentNode.insertBefore(nodes(arguments),this.nextSibling)});
def(E,'before',function(){if(this.parentNode)this.parentNode.insertBefore(nodes(arguments),this)});
def(E,'replaceChildren',function(){while(this.firstChild)this.removeChild(this.firstChild);if(arguments.length)this.appendChild(nodes(arguments))});
if(window.NodeList&&!NodeList.prototype.forEach)NodeList.prototype.forEach=P.forEach;
if(window.HTMLCollection&&!HTMLCollection.prototype.forEach)HTMLCollection.prototype.forEach=P.forEach;
/* scroll helpers that ignore the options object in very old browsers */
try{document.createElement('div').scrollIntoView({behavior:'smooth'})}catch(e){var oi=E.scrollIntoView;E.scrollIntoView=function(){return oi.call(this)}}
if(!E.scrollBy)E.scrollBy=function(o,y){if(typeof o==='object'){this.scrollLeft+=o.left||0;this.scrollTop+=o.top||0}else{this.scrollLeft+=o;this.scrollTop+=y}};
if(!window.IntersectionObserver){window.IntersectionObserver=function(cb){this._cb=cb;this._els=[]};window.IntersectionObserver.prototype={observe:function(el){var s=this;setTimeout(function(){s._cb([{target:el,isIntersecting:true,intersectionRatio:1}],s)},0)},unobserve:function(){},disconnect:function(){}}}
})();
