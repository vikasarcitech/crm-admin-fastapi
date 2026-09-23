/* global window, document */
/**
 * Scroll-in animation for builder pages (/p/{tenant}/{slug}).
 *
 * A block whose design panel asked for "fade" or "rise" is rendered
 * with .sec-anim; this adds .in when it scrolls into view. Two things
 * make it safe to depend on:
 *
 *  - The CSS only hides .sec-anim under `html.motion`, and that class
 *    is added here, first thing. No script (blocked, failed, old
 *    browser) means no class, means every section is simply visible.
 *  - prefers-reduced-motion is honoured in the CSS as well, so a
 *    visitor who asked for less movement gets none.
 */
(function () {
  'use strict';

  var root = document.documentElement;
  if (!('IntersectionObserver' in window)) return;
  root.classList.add('motion');

  function reveal(entries, observer) {
    entries.forEach(function (entry) {
      if (!entry.isIntersecting) return;
      entry.target.classList.add('in');
      observer.unobserve(entry.target);
    });
  }

  function start() {
    var sections = document.querySelectorAll('.sec-anim');
    if (!sections.length) return;
    var observer = new IntersectionObserver(reveal, { rootMargin: '0px 0px -8% 0px', threshold: 0.05 });
    sections.forEach(function (section) { observer.observe(section); });
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', start);
  } else {
    start();
  }
}());
