/* dock.js: the #hk-dock nav tab; hrefs arrive absolute. */
(function () {
  'use strict';

  var cfg = window.__HUB__;
  /* Missing or malformed config: render nothing, silently. */
  if (!cfg || typeof cfg !== 'object' || !Array.isArray(cfg.links) || !document.body) return;

  var NAV = 'Site navigation', a, e, href, best = null, open = false;

  function str(v) { return typeof v === 'string' ? v : ''; }
  /* textContent only: labels hold & < " and unicode. */
  function node(tag, cls, text) {
    var n = document.createElement(tag);
    n.className = cls;
    if (text != null) n.textContent = text;
    return n;
  }

  /* Path to match; null if the href leaves this origin - links can name the public
     host. Trailing slash dropped: /l/x/ and /l/x agree. */
  function pathOf(raw) {
    var u, p;
    try { u = new URL(raw, location.href); } catch (err) { return null; }
    if (u.origin !== location.origin) return null;
    p = u.pathname;
    while (p.length > 1 && p.slice(-1) === '/') p = p.slice(0, -1);
    return p;
  }

  var user = str(cfg.user);
  var items = cfg.links.filter(function (e) { return e && str(e.href); });
  /* Sign in has to bring you back to the page you were on. The server only
     honours a `next` that starts with the prefix of the partition the login
     names (hubauth.login_target), so the hint is offered only from a page that
     sits inside a mount of the partition this payload is about - and left off
     everywhere else (the picker, the front partition's bare aliases), where it
     would be dropped by the server anyway. `cfg.slug` is null exactly on the
     picker, which is why it is checked, not assumed. */
  var mount = cfg.slug ? '/l/' + cfg.slug + '/' : '';
  var signIn = str(cfg.signIn);
  if (!user && signIn && mount && location.pathname.indexOf(mount) === 0) {
    signIn += '?next=' + encodeURIComponent(location.pathname + location.search);
  }
  /* The config is emitted before sign-in is known, so auth goes last. */
  href = user ? str(cfg.signOut) : signIn;
  if (href) items.push({ href: href, label: user ? 'Sign out' : 'Sign in' });

  var dock = node('div', 'hk-closed');
  var tab = node('button', 'hk-tab', '\u2261');
  var panel = node('nav', 'hk-panel');
  dock.id = 'hk-dock';
  tab.id = 'hk-tab';
  panel.id = 'hk-panel';
  panel.hidden = true;
  tab.type = 'button';
  tab.setAttribute('aria-expanded', 'false');
  tab.setAttribute('aria-controls', 'hk-panel');
  tab.setAttribute('aria-label', NAV);
  panel.setAttribute('aria-label', NAV);
  if (str(cfg.name)) panel.appendChild(node('span', 'hk-brand', cfg.name));
  if (user) panel.appendChild(node('span', 'hk-user', user));

  var here = pathOf(location.pathname) || '/';
  var len = -1;
  for (var i = 0; i < items.length; i++) {
    e = items[i];
    a = node('a', 'hk-link', e.label == null ? e.href : e.label);
    a.href = e.href;
    panel.appendChild(a);
    /* '/' prefixes every path: longest match wins. */
    href = pathOf(e.href);
    if (href !== null && here.indexOf(href) === 0 && href.length > len) {
      len = href.length;
      best = a;
    }
  }
  if (best) {
    best.className = 'hk-link hk-active';
    best.setAttribute('aria-current', 'page');
  }

  function setOpen(on) {
    open = on;
    dock.className = on ? 'hk-open' : 'hk-closed';
    tab.setAttribute('aria-expanded', on ? 'true' : 'false');
    panel.hidden = !on;
  }

  /* Escape closes and refocuses; a button toggles on Enter/Space already. */
  tab.addEventListener('click', function () { setOpen(!open); });
  document.addEventListener('keydown', function (ev) {
    if (open && (ev.key === 'Escape' || ev.keyCode === 27)) {
      setOpen(false);
      tab.focus();
    }
  });

  dock.appendChild(tab);
  dock.appendChild(panel);
  document.body.appendChild(dock);
})();
