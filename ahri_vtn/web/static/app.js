// Live updates: subscribe to the server-sent event stream and refresh regions marked with
// data-live by re-fetching the current page. Refreshes pause while a form field is focused.
(function () {
  var indicator = document.getElementById('live-indicator');
  var timer = null;
  function focused() {
    var el = document.activeElement;
    return el && (el.tagName === 'INPUT' || el.tagName === 'TEXTAREA' || el.tagName === 'SELECT');
  }
  function refresh() {
    timer = null;
    if (focused()) { timer = setTimeout(refresh, 3000); return; }
    var url = location.pathname + location.search.replace(/[?&](flash|kind)=[^&]*/g, '');
    fetch(url, { headers: { 'X-Requested-With': 'live' } }).then(function (r) { return r.text(); }).then(function (html) {
      var doc = new DOMParser().parseFromString(html, 'text/html');
      var fresh = doc.querySelectorAll('[data-live]');
      var current = document.querySelectorAll('[data-live]');
      if (fresh.length !== current.length) { return; }
      for (var i = 0; i < current.length; i++) {
        if (current[i].innerHTML !== fresh[i].innerHTML) { current[i].innerHTML = fresh[i].innerHTML; }
      }
    }).catch(function () {});
  }
  function schedule() { if (!timer) { timer = setTimeout(refresh, 800); } }
  if (window.EventSource && document.querySelector('[data-live]')) {
    var es = new EventSource('/api/stream');
    es.onopen = function () { if (indicator) { indicator.classList.add('on'); } };
    es.onerror = function () { if (indicator) { indicator.classList.remove('on'); } };
    var topics = ['message', 'ven.registered', 'ven.added', 'ven.updated', 'report.update', 'report.registered', 'report.requested',
                  'report.created', 'report.cancelled', 'event.created', 'event.response', 'event.cancelled', 'event.status',
                  'opt.created', 'opt.cancelled', 'run.started', 'run.step', 'run.prompt', 'run.finished', 'tick'];
    topics.forEach(function (t) { es.addEventListener(t, function (e) { if (t !== 'message' && t !== 'tick') { schedule(); } }); });
    // Poll messages are frequent; refresh at most every 5 s for them
    var last = 0;
    es.addEventListener('message', function () { var n = Date.now(); if (n - last > 5000) { last = n; schedule(); } });
    es.addEventListener('tick', function () { var n = Date.now(); if (n - last > 15000) { last = n; schedule(); } });
  }
  // Event form: show the preset's default payload as placeholder
  var preset = document.getElementById('preset');
  if (preset) {
    var payload = document.querySelector('input[name=payload]');
    var update = function () {
      var opt = preset.options[preset.selectedIndex];
      if (payload) { payload.placeholder = opt.dataset.payload || opt.dataset.label || 'value'; }
    };
    preset.addEventListener('change', update); update();
  }
})();
