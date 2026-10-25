/* Creation Films public site behaviour: header, reveal, hero video, video modal, film room, strips, portfolio filter. */
(function () {
  'use strict';
  var d = document, root = d.documentElement;
  root.classList.remove('no-js');

  /* ---------- header + menu ---------- */
  var header = d.querySelector('.site-header');
  function onScroll() { if (header) header.classList.toggle('stuck', window.scrollY > 60); }
  onScroll(); window.addEventListener('scroll', onScroll, { passive: true });
  var menuBtn = d.querySelector('.menu-btn'), nav = d.querySelector('.nav');
  if (menuBtn && nav) {
    menuBtn.addEventListener('click', function () {
      var open = nav.classList.toggle('open');
      d.body.classList.toggle('menu-open', open);
      menuBtn.textContent = open ? 'Close' : 'Menu';
    });
    nav.addEventListener('click', function (e) { if (e.target.tagName === 'A') { nav.classList.remove('open'); d.body.classList.remove('menu-open'); menuBtn.textContent = 'Menu'; } });
  }

  /* ---------- reveal on scroll ---------- */
  var rev = [].slice.call(d.querySelectorAll('.reveal'));
  if ('IntersectionObserver' in window) {
    var io = new IntersectionObserver(function (es) {
      es.forEach(function (e) { if (e.isIntersecting) { e.target.classList.add('in'); io.unobserve(e.target); } });
    }, { threshold: 0.12, rootMargin: '0px 0px -6% 0px' });
    rev.forEach(function (el) { io.observe(el); });
  } else { rev.forEach(function (el) { el.classList.add('in'); }); }

  /* image fallbacks (cfImgFail / cfYtFail) are defined inline in <head> so they exist before any image can fail */

  /* ---------- video modal ---------- */
  var modal = d.getElementById('vModal');
  var box = modal && modal.querySelector('.modal-box');
  var frameHost = modal && modal.querySelector('.modal-host');
  function ytSrc(id, extra) {
    return 'https://www.youtube-nocookie.com/embed/' + id + '?autoplay=1&rel=0&modestbranding=1&playsinline=1' + (extra || '');
  }
  function openModal(kind) {
    if (!modal) return;
    lastFocus = d.activeElement;
    box.className = 'modal-box' + (kind === 'reel' ? ' v' : '') + (kind === 'photo' ? ' p' : '');
    modal.classList.add('open'); d.body.style.overflow = 'hidden';
    var x = modal.querySelector('.modal-x'); if (x) { try { x.focus(); } catch (e) {} }
  }
  function openVideo(id, kind) {
    if (!modal || !id) return;
    if (root.classList.contains('legacy')) { // old TV browsers cannot run the YouTube embed: offer links that do work
      frameHost.innerHTML = '<div class="lg-video"><img class="lg-thumb" alt="" src="https://img.youtube.com/vi/' + id + '/hqdefault.jpg">' +
        '<div class="lg-btns"><a class="btn solid" href="https://m.youtube.com/watch?v=' + id + '">&#9654; Watch on YouTube</a>' +
        '<a class="btn light" href="https://www.youtube.com/tv#/watch?v=' + id + '">YouTube TV</a>' +
        '<button class="btn light" type="button" data-inline="' + id + '">Play here</button></div></div>';
      openModal(kind === 'reel' ? 'film' : kind);
      var first = frameHost.querySelector('a'); if (first) { try { first.focus(); } catch (e) {} }
      return;
    }
    frameHost.innerHTML = '<div class="modal-frame"><iframe src="' + ytSrc(id) + '" title="Creation Films" allow="autoplay; encrypted-media; picture-in-picture; fullscreen" allowfullscreen></iframe></div>';
    openModal(kind);
  }
  var curPhoto = null, lastFocus = null;
  function photoList(el) { var host = el.parentNode; return [].slice.call(host.querySelectorAll('[data-photo]')).filter(function (x) { return !x.hidden; }); }
  function stepPhoto(dir) {
    var list = photoList(curPhoto), i = list.indexOf(curPhoto), n = list[i + dir];
    if (!n) return;
    curPhoto = n; showPhoto(n.getAttribute('data-photo'), n.getAttribute('data-photo-fb'));
    // preload the next one so flipping is quick
    var nx = list[i + dir + dir]; if (nx) { (new Image()).src = nx.getAttribute('data-photo'); }
  }
  function showPhoto(src, fb) {
    frameHost.innerHTML = '';
    var img = new Image(); img.className = 'modal-img'; img.alt = '';
    img.onerror = function () { if (fb && img.src !== fb) { img.src = fb; } };
    img.src = src; frameHost.appendChild(img);
  }
  function openPhoto(src, fb, el) {
    if (!modal || !src) return;
    curPhoto = el || null;
    frameHost.innerHTML = '';
    var img = new Image(); img.className = 'modal-img'; img.alt = '';
    img.onerror = function () { if (fb && img.src !== fb) { img.src = fb; } };
    img.src = src; frameHost.appendChild(img);
    openModal('photo');
    modal.classList.add('photo-mode');
  }
  function closeModal() {
    if (!modal) return;
    modal.classList.remove('open', 'photo-mode'); frameHost.innerHTML = ''; d.body.style.overflow = ''; curPhoto = null;
    if (lastFocus && lastFocus.focus) { try { lastFocus.focus(); } catch (e) {} }
  }
  if (modal) {
    modal.addEventListener('click', function (e) { if (e.target === modal || e.target.closest('.modal-x')) closeModal(); });
    // Remote control: Back / Escape closes; Left / Right flips through photos of the same grid.
    d.addEventListener('keydown', function (e) {
      var open = modal.classList.contains('open');
      var back = e.key === 'Escape' || e.key === 'BrowserBack' || e.key === 'GoBack' || e.keyCode === 4 || e.keyCode === 27 || e.keyCode === 461 || e.keyCode === 10009;
      if (open && back) { e.preventDefault(); closeModal(); return; }
      if (open && curPhoto && (e.key === 'ArrowRight' || e.key === 'ArrowLeft')) { e.preventDefault(); stepPhoto(e.key === 'ArrowRight' ? 1 : -1); }
    });
  }
  // role="button" cards: Enter / Space opens them too
  d.addEventListener('keydown', function (e) {
    if (e.key !== 'Enter' && e.key !== ' ') return;
    var t = e.target.closest && e.target.closest('[role="button"][data-play]');
    if (t) { e.preventDefault(); openVideo(t.getAttribute('data-play'), t.getAttribute('data-kind')); }
  });
  d.addEventListener('click', function (e) {
    var inl = e.target.closest && e.target.closest('[data-inline]');
    if (inl) { e.preventDefault(); frameHost.innerHTML = '<div class="modal-frame"><iframe src="' + ytSrc(inl.getAttribute('data-inline')) + '" title="Creation Films" allow="autoplay; encrypted-media; fullscreen" allowfullscreen></iframe></div>'; return; }
    var t = e.target.closest('[data-play]');
    if (t) { e.preventDefault(); openVideo(t.getAttribute('data-play'), t.getAttribute('data-kind')); return; }
    var p = e.target.closest('[data-photo]');
    if (p) { e.preventDefault(); openPhoto(p.getAttribute('data-photo'), p.getAttribute('data-photo-fb'), p); }
  });

  /* ---------- hero video ---------- */
  var hv = d.getElementById('heroVideo');
  if (hv) {
    var conn = navigator.connection || {};
    var reduce = window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches;
    if (!conn.saveData && !reduce && !root.classList.contains('tv')) {
      var id = hv.getAttribute('data-yt');
      var f = d.createElement('iframe');
      f.setAttribute('allow', 'autoplay; encrypted-media');
      f.setAttribute('title', 'Creation Films showreel');
      f.setAttribute('tabindex', '-1');
      f.setAttribute('aria-hidden', 'true');
      f.src = 'https://www.youtube-nocookie.com/embed/' + id + '?autoplay=1&mute=1&controls=0&loop=1&playlist=' + id +
        '&playsinline=1&modestbranding=1&rel=0&iv_load_policy=3&disablekb=1&fs=0&enablejsapi=1';
      f.addEventListener('load', function () { setTimeout(function () { hv.classList.add('ready'); }, 1400); });
      hv.appendChild(f);
      var soundBtn = d.getElementById('heroSound');
      var muted = true;
      function cmd(fn, args) { try { f.contentWindow.postMessage(JSON.stringify({ event: 'command', func: fn, args: args || [] }), '*'); } catch (e) {} }
      if (soundBtn) {
        soundBtn.hidden = false;
        soundBtn.addEventListener('click', function () {
          muted = !muted;
          if (muted) cmd('mute'); else { cmd('unMute'); cmd('setVolume', [70]); cmd('playVideo'); }
          soundBtn.setAttribute('data-state', muted ? 'off' : 'on');
          soundBtn.querySelector('span').textContent = muted ? 'Sound off' : 'Sound on';
        });
      }
      // Pause the loop when the hero is off screen, to save data and battery.
      if ('IntersectionObserver' in window) {
        new IntersectionObserver(function (es) {
          es.forEach(function (e) { cmd(e.isIntersecting ? 'playVideo' : 'pauseVideo'); });
        }, { threshold: 0.05 }).observe(hv.closest('.hero'));
      }
    }
  }

  /* ---------- film room (swap the featured film) ---------- */
  var room = d.getElementById('cinemaRoom');
  if (room) {
    var poster = room.querySelector('.poster'), pimg = poster.querySelector('img');
    var ctitle = room.querySelector('.cap .t'), cnum = room.querySelector('.bar .cur');
    [].forEach.call(room.querySelectorAll('.ep'), function (ep) {
      ep.addEventListener('click', function () {
        [].forEach.call(room.querySelectorAll('.ep'), function (x) { x.classList.remove('on'); });
        ep.classList.add('on');
        var id = ep.getAttribute('data-yt');
        poster.setAttribute('data-play', id);
        pimg.removeAttribute('data-tried'); pimg.style.display = '';
        pimg.src = 'https://img.youtube.com/vi/' + id + '/maxresdefault.jpg';
        ctitle.textContent = ep.getAttribute('data-title');
        if (cnum) cnum.textContent = ep.getAttribute('data-n');
        if (window.innerWidth < 1000) poster.scrollIntoView({ behavior: 'smooth', block: 'center' });
      });
    });
  }

  /* ---------- horizontal strips ---------- */
  [].forEach.call(d.querySelectorAll('[data-strip]'), function (wrap) {
    var strip = wrap.querySelector('.strip');
    function step(dir) { var c = strip.querySelector('.card-v'); var w = c ? c.getBoundingClientRect().width + 26 : 320; if (strip.scrollBy) { try { strip.scrollBy({ left: dir * w, behavior: 'smooth' }); } catch (e) { strip.scrollLeft += dir * w; } } else { strip.scrollLeft += dir * w; } }
    var prev = wrap.querySelector('[data-prev]'), next = wrap.querySelector('[data-next]');
    if (prev) prev.addEventListener('click', function () { step(-1); });
    if (next) next.addEventListener('click', function () { step(1); });
  });

  /* ---------- portfolio filter ---------- */
  var grid = d.getElementById('pfGrid');
  if (grid) {
    var pills = [].slice.call(d.querySelectorAll('.pill'));
    function apply(cat) {
      pills.forEach(function (p) { p.classList.toggle('active', p.getAttribute('data-cat') === cat); });
      [].forEach.call(grid.children, function (it) { it.hidden = !(cat === 'all' || it.getAttribute('data-cat') === cat); });
    }
    pills.forEach(function (p) { p.addEventListener('click', function () { apply(p.getAttribute('data-cat')); }); });
    var want = new URLSearchParams(location.search).get('cat');
    if (want && pills.some(function (p) { return p.getAttribute('data-cat') === want; })) apply(want);
  }
})();
