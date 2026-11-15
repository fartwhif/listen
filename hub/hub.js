// The portal's only script. Every page gen-conf writes (login, pair, logout)
// is a plain <form data-action=...>; this turns it into one fetch to the
// gateway's /api/auth/* surface and paints whatever hubauth said.
//
// No framework, no build step, no dependencies: the door has to work on the
// car's browser and on a phone as well as on a desktop, and it is served
// ungated before anyone is logged in.
//
// The endpoints answer JSON for everything - {"error": "..."} on failure, and
// a {"redirect": "..."} on success. The server owns the redirect target (it
// knows the partition's prefix), so this never builds a URL itself.
(function () {
    'use strict';

    var ENDPOINTS = { login: '/api/auth/login', pair: '/api/auth/pair', logout: '/api/auth/logout' };

    function field(form, name) {
        var el = form.elements[name];
        return el ? el.value.trim() : '';
    }

    function showError(form, message) {
        var box = form.querySelector('[data-error]');
        if (box) box.textContent = message;
    }
    // A gated page bounced here carries the page it came from in ?next=. The
    // same value rides along in the login/pair POST so hubauth can land the
    // human back on that page instead of the partition root.
    var NEXT = new URLSearchParams(location.search).get('next') || '';

    function submit(form) {
        var action = form.getAttribute('data-action');
        var url = ENDPOINTS[action];
        if (!url) return;

        var button = form.querySelector('button[type=submit]') || form.querySelector('button');
        var label = button ? button.textContent : '';
        if (button) { button.disabled = true; button.textContent = 'Working…'; }
        showError(form, '');

        var body = action === 'logout' ? '{}' : JSON.stringify({
            slug: field(form, 'slug'),
            username: field(form, 'username'),
            password: form.elements.password ? form.elements.password.value : '',
            code: field(form, 'code'),
            next: NEXT
        });

        fetch(url, {
            method: 'POST',
            credentials: 'same-origin',
            headers: { 'Content-Type': 'application/json', 'Accept': 'application/json' },
            body: body
        }).then(function (res) {
            // Any answer that names a redirect is a done deal: the session
            // cookie is already on the response, so following it is the whole
            // remaining job.
            return res.json().catch(function () { return {}; }).then(function (data) {
                if (res.ok && data.redirect) { location.assign(data.redirect); return null; }
                var reason = data.error || (res.status === 429 ? 'too many attempts' : 'request failed');
                showError(form, reason);
                if (button) { button.disabled = false; button.textContent = label; }
                return null;
            });
        }).catch(function () {
            // Only a network failure lands here (the container is down, or the
            // tunnel dropped mid-typing); a 4xx/5xx is a real answer.
            showError(form, 'cannot reach the hub');
            if (button) { button.disabled = false; button.textContent = label; }
            return null;
        });
    }

    document.addEventListener('submit', function (ev) {
        var form = ev.target;
        if (!(form instanceof HTMLFormElement) || !form.hasAttribute('data-action')) return;
        ev.preventDefault();
        submit(form);
    });
})();
