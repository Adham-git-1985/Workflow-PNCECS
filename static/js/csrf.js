function getCSRFToken() {
    const meta = document.querySelector('meta[name="csrf-token"]');
    return meta ? meta.getAttribute("content") : null;
}

function csrfFetch(url, options = {}) {
    options.headers = options.headers || {};

    const token = getCSRFToken();
    if (token) {
        options.headers["X-CSRFToken"] = token;
    }

    return fetch(url, options);
}
