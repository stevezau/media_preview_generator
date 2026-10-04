// Public documentation only. GoatCounter's stable /count parameters:
// https://www.goatcounter.com/help/pixel . No external script or automatic event binding.
(function () {
  'use strict';
  var config = document.currentScript;
  if (!config || location.origin !== 'https://mediapreviewgenerator.dev' || window.top !== window.self) return;
  var endpoint = config.dataset.endpoint || '';
  var path = config.dataset.page || '';
  if (!/^https:\/\/[a-z0-9][a-z0-9-]*\.goatcounter\.com\/count$/.test(endpoint)) return;
  // The path comes from Jekyll, never the visitor's URL or search input.
  if (!/^\/(?:[a-z0-9-]+\/)*$/.test(path)) return;
  var disclosure = document.querySelector('.footer__usage');
  if (disclosure) disclosure.hidden = false;
  var toggle = document.getElementById('docs-analytics-toggle');
  var status = document.getElementById('docs-analytics-status');
  var browserOptOut = navigator.doNotTrack === '1' || window.doNotTrack === '1' || navigator.globalPrivacyControl === true;
  var storageAvailable = true;
  var excluded = false;
  try { excluded = localStorage.getItem('skipgc') === 't'; }
  catch (_) { storageAvailable = false; }

  function updatePreference() {
    if (!toggle || !status) return;
    toggle.disabled = browserOptOut || !storageAvailable;
    toggle.textContent = excluded ? 'Allow my visits to be counted' : 'Exclude my visits';
    status.textContent = browserOptOut ? 'Your browser privacy preference is respected.'
      : !storageAvailable ? 'Usage counts are off because browser storage is unavailable.'
        : excluded ? 'Your visits are excluded on this browser.' : '';
  }
  updatePreference();
  if (toggle) toggle.addEventListener('click', function () {
    try {
      excluded = !excluded;
      if (excluded) localStorage.setItem('skipgc', 't');
      else localStorage.removeItem('skipgc');
    } catch (_) { storageAvailable = false; }
    updatePreference();
  });

  function allowed() {
    // Re-read the shared GoatCounter opt-out so a second tab takes effect immediately.
    try { excluded = localStorage.getItem('skipgc') === 't'; }
    catch (_) { storageAvailable = false; }
    return storageAvailable && !excluded && !browserOptOut && !navigator.webdriver && !document.prerendering;
  }
  function referrerOrigin() {
    try {
      var ref = new URL(document.referrer);
      // Ignore same-site and local/IP referrers; send no path, credentials, query or fragment.
      if (ref.origin === location.origin || !/^https?:$/.test(ref.protocol)) return '';
      if (!/^[a-z0-9-]+(?:\.[a-z0-9-]+)*\.[a-z]{2,}$/i.test(ref.hostname)) return '';
      if (/\.(local|localhost|internal|test|invalid)$/i.test(ref.hostname)) return '';
      return ref.origin;
    } catch (_) { return ''; }
  }
  function count(name, event) {
    if (!allowed()) return;
    var query = new URLSearchParams({p: name, r: referrerOrigin(), t: '', rnd: Math.random().toString(36).slice(2, 7)});
    if (event) query.set('e', 'true');
    // Explicit no-referrer and omitted credentials also protect the HTTP request itself.
    fetch(endpoint + '?' + query.toString(), {
      mode: 'no-cors', credentials: 'omit', referrerPolicy: 'no-referrer', keepalive: true
    }).catch(function () {});
  }
  var counted = false;
  function pageView() {
    if (counted || document.visibilityState !== 'visible' || !allowed()) return;
    counted = true;
    count(path, false);
  }
  pageView();
  document.addEventListener('visibilitychange', pageView);
  document.addEventListener('prerenderingchange', pageView);
  document.addEventListener('click', function (event) {
    var link = event.target.closest && event.target.closest('a[href]');
    if (!link) return;
    var target;
    try { target = new URL(link.href); } catch (_) { return; }
    var name = '';
    if (target.origin === location.origin && target.pathname === '/getting-started/') name = 'get-started';
    if (target.origin === 'https://hub.docker.com' && target.pathname.replace(/\/$/, '') === '/r/stevezzau/media_preview_generator') name = 'install-docker-hub';
    if (target.origin === 'https://github.com' && /^\/stevezau\/media_preview_generator\/releases(?:\/latest)?\/?$/.test(target.pathname)) name = 'install-github-releases';
    if (name) count(name, true);
  });
})();
