/*
 * router.js - why the scan app is one document.
 *
 * Scan, History and Diagnostics used to be three server-rendered pages, and moving
 * between them was an ordinary navigation. That is a comfortable way to build a web
 * app and a bad way to build this one, because a navigation tears the document down,
 * and the camera goes with it.
 *
 * What that cost, in order of how much it hurt:
 *
 *   - A permission prompt. WebKit scopes a getUserMedia grant to the document that
 *     asked for it, so on iOS every trip back from History was a fresh prompt. A field
 *     user checking whether a receipt sent, then going back to scanning, paid a dialog
 *     for it - every time, forever. That is the "a lot of clicks" this file exists to
 *     delete.
 *   - A cold start. Each navigation re-parsed Tailwind, re-booted Alpine, re-opened
 *     IndexedDB and re-warmed the camera: roughly a second of black screen to look at
 *     a list the app had already cached.
 *   - The decoder. The ZXing module is a megabyte of WASM that has to be compiled
 *     before it can read anything, and every navigation threw that away and did it
 *     again on return.
 *
 * So the three views now live in one document and this swaps between them. It is
 * deliberately not a general router: three known paths, no parameters, no nested
 * routes, no data loading. Everything it does not do is a thing that cannot break.
 *
 * The URLs are real. Each one is still a server route rendering the same shell, so a
 * deep link, a bookmark, a refresh and the back button all behave exactly as they did
 * when these were separate pages - the difference is only that we no longer make the
 * browser do it while the app is already open.
 */
(function (global) {
    'use strict';

    var VIEWS = {
        scan: { path: '/scan/', title: 'Scan Receipts', bodyClass: 'scan-camera' },
        history: { path: '/scan/history', title: 'My Receipts', bodyClass: '' },
        diagnostics: { path: '/scan/diagnostics', title: 'Diagnostics', bodyClass: '' },
    };

    var DEFAULT_VIEW = 'scan';

    var current = null;
    var watchers = [];

    function viewForPath(pathname) {
        for (var name in VIEWS) {
            if (VIEWS[name].path === pathname) return name;
        }
        // Anything else under /scan/ - an activation link that has already been spent
        // and rewritten, a stale URL, the service worker's offline fallback serving
        // this shell for a path it could not reach - lands on the scanner. It is what
        // the app is for and the one view that works with nothing else available.
        return DEFAULT_VIEW;
    }

    /*
     * Tells one view whether it is the current one.
     *
     * Called immediately with the view's state at registration, and again on every
     * change, so a component never has to work out where it stands at boot - which
     * matters because it is what decides whether it does its expensive setup at all.
     * Diagnostics runs cache reads, a fetch and a WASM compile; History reads and
     * pages IndexedDB. Neither should happen because the user opened the scanner.
     */
    function watch(name, fn) {
        watchers.push({ name: name, fn: fn });
        try { fn(current === name); } catch (e) { /* a broken view is not fatal */ }
    }

    function apply(name) {
        if (name === current) return;
        current = name;

        var view = VIEWS[name];
        global.document.title = view.title;

        // The scanner is a black full-bleed camera screen and the other two are light
        // scrolling sheets; they disagree about overscroll and about background. On
        // separate pages this came from {% block body_class %}, which no longer has a
        // page boundary to fire on.
        var body = global.document.body;
        for (var other in VIEWS) {
            if (VIEWS[other].bodyClass) body.classList.remove(VIEWS[other].bodyClass);
        }
        if (view.bodyClass) body.classList.add(view.bodyClass);

        for (var i = 0; i < watchers.length; i++) {
            try { watchers[i].fn(watchers[i].name === name); } catch (e) { /* keep going */ }
        }
    }

    /*
     * Moves to a view and puts it in the address bar.
     *
     * `replace` is for the one case that is not a navigation the user made: the
     * scanner rewriting a spent activation token out of the URL, which must not leave
     * a history entry pointing at a credential.
     */
    function go(name, options) {
        options = options || {};
        if (!VIEWS[name]) name = DEFAULT_VIEW;

        var url = VIEWS[name].path;
        try {
            if (options.replace) global.history.replaceState({ view: name }, '', url);
            else if (name !== current) global.history.pushState({ view: name }, '', url);
        } catch (e) { /* file://, or a browser refusing the URL; the swap still works */ }

        apply(name);
        // A view swapped in under a page scrolled halfway down someone else's list
        // would otherwise open mid-content.
        if (!options.keepScroll) global.scrollTo(0, 0);
    }

    function start() {
        if (current !== null) return;
        global.addEventListener('popstate', function (event) {
            var name = (event.state && event.state.view) || viewForPath(global.location.pathname);
            apply(VIEWS[name] ? name : DEFAULT_VIEW);
        });

        var initial = viewForPath(global.location.pathname);
        // Stamped onto the entry we are already on, so going forward to it later
        // restores the same view without having to re-derive it from a path the app
        // may since have rewritten.
        try {
            global.history.replaceState({ view: initial }, '', global.location.href);
        } catch (e) { /* not fatal; popstate falls back to the pathname */ }
        apply(initial);
    }

    global.ScanRouter = {
        start: start,
        go: go,
        watch: watch,
        viewForPath: viewForPath,
        get current() { return current; },
    };
})(window);
