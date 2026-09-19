// Sends the page's CSRF token (the <meta name="csrf-token"> tag in base.html) with every state-changing fetch to this
// app, so no call site has to remember the header. The server refuses a POST/PUT/PATCH/DELETE that relies on the
// signed-in session without it. Loaded in <head> ahead of every script that makes a request, so they all go through it.
(function () {
    'use strict';

    const nativeFetch = window.fetch;
    if (typeof nativeFetch !== 'function') return;

    const SAFE_METHODS = new Set(['GET', 'HEAD', 'OPTIONS', 'TRACE']);

    function csrfToken() {
        const meta = document.querySelector('meta[name="csrf-token"]');
        return meta ? meta.getAttribute('content') : '';
    }

    // The token must never leave this app: a request to another origin goes out untouched.
    function isSameOrigin(url) {
        try {
            return new URL(url, window.location.href).origin === window.location.origin;
        } catch (_e) {
            return false;
        }
    }

    window.fetch = function (input, init) {
        const isRequest = typeof Request !== 'undefined' && input instanceof Request;
        const method = String((init && init.method) || (isRequest ? input.method : 'GET')).toUpperCase();
        const url = isRequest ? input.url : String(input);
        if (SAFE_METHODS.has(method) || !isSameOrigin(url)) {
            return nativeFetch.call(window, input, init);
        }
        const headers = new Headers((init && init.headers) || (isRequest ? input.headers : undefined));
        if (!headers.has('X-CSRFToken')) headers.set('X-CSRFToken', csrfToken());
        return nativeFetch.call(window, input, Object.assign({}, init, { headers: headers }));
    };
})();
