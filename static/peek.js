/*
 * peek.js - hover a value, see everything behind it.
 *
 * Every linked value on every page opts in the same way:
 *
 *     <a href="/vendors/tin:102156501" class="peek" data-peek="vendor:tin:102156501">
 *       VILLAGE SUPERMARKET LTD.
 *     </a>
 *
 * Two deliberate properties follow from that markup.
 *
 * The target is a real anchor with a real href. Hovering is an enhancement, never the
 * only way in: the link works with this file absent, opens in a new tab on
 * middle-click, is reachable by keyboard and is readable by a screen reader. Anything
 * built as a div with a click handler loses all four.
 *
 * The behaviour is delegated from `document` rather than bound per element. The
 * dashboard table is rendered by Alpine and re-rendered on every filter change, sort,
 * page turn and server-sent update; per-element listeners would have to be re-attached
 * each time and would leak the ones they replaced. One listener on the document
 * survives all of it and costs nothing per row.
 *
 * Nothing here writes innerHTML. Card content is vendor names, item descriptions and
 * tax-office names that originated on a page served by somebody else, and it is put on
 * screen through textContent so a supplier who names their shop after a script tag is
 * a supplier with a peculiar name and not an execution.
 */
(function () {
  'use strict';

  // Long enough that a pointer crossing the row on its way somewhere else does not
  // open anything; short enough to feel like it was already there.
  const OPEN_DELAY_MS = 140;
  // The pointer has to be able to leave the target and land on the card, which means
  // crossing the gap between them without the card vanishing en route.
  const CLOSE_DELAY_MS = 220;
  const GAP_PX = 8;
  // Breathing room between the card and the edge of the viewport.
  const EDGE_PX = 8;
  const CARD_WIDTH_PX = 340;

  const TONES = {
    good: 'text-green-700',
    warn: 'text-amber-700',
    bad: 'text-red-700',
    info: 'text-blue-700',
    muted: 'text-gray-500',
  };
  const NOTE_TONES = {
    good: 'bg-green-50 text-green-900',
    warn: 'bg-amber-50 text-amber-900',
    bad: 'bg-red-50 text-red-900',
    info: 'bg-blue-50 text-blue-900',
    muted: 'bg-gray-50 text-gray-600',
  };
  const BADGE_TONES = {
    good: 'bg-green-50 text-green-700 ring-green-600/20',
    warn: 'bg-amber-50 text-amber-700 ring-amber-600/20',
    bad: 'bg-red-50 text-red-700 ring-red-600/20',
    info: 'bg-blue-50 text-blue-700 ring-blue-600/20',
    muted: 'bg-gray-50 text-gray-600 ring-gray-500/20',
  };
  // Same glyphs the receipt page prints against its checks, so a check means the same
  // thing whichever surface it is read on.
  const CHECK_MARKS = {
    pass: ['✓', 'bg-green-50 text-green-800 ring-green-600/20'],
    warn: ['!', 'bg-amber-50 text-amber-800 ring-amber-600/20'],
    fail: ['✕', 'bg-red-50 text-red-800 ring-red-600/20'],
    info: ['i', 'bg-blue-50 text-blue-800 ring-blue-600/20'],
    na: ['–', 'bg-gray-50 text-gray-600 ring-gray-500/20'],
  };

  /*
   * Answers already fetched, keyed 'kind:key'.
   *
   * A card's contents cannot change while the page is open - they are aggregates over
   * stored receipts - so the second hover over a vendor is free, and so is the tenth.
   * A promise is cached rather than a value, which makes two hovers that overlap in
   * flight share one request instead of racing.
   */
  const cache = new Map();

  let card = null;         // The single card element, created once and reused.
  let target = null;       // What it is currently describing.
  let openTimer = null;
  let closeTimer = null;
  let pointerInCard = false;
  let sequence = 0;        // Rising edge, so a slow fetch cannot overwrite a newer one.

  const coarsePointer = window.matchMedia && window.matchMedia('(hover: none)').matches;

  function load(spec) {
    if (cache.has(spec)) return cache.get(spec);

    // Split on the first colon only: vendor keys are themselves 'tin:102156501'.
    const separator = spec.indexOf(':');
    if (separator < 0) return Promise.reject(new Error('Malformed peek key.'));
    const kind = spec.slice(0, separator);
    const key = spec.slice(separator + 1);

    const request = fetch(`/api/peek/${encodeURIComponent(kind)}/${encodeURIComponent(key)}`, {
      headers: { Accept: 'application/json' },
    }).then((response) => {
      if (!response.ok) throw new Error(response.status === 404 ? 'Nothing recorded for this yet.' : 'Could not load.');
      return response.json();
    }).catch((error) => {
      // A failed lookup is not cached: the next hover should try again rather than
      // display a stale network error for the rest of the session.
      cache.delete(spec);
      throw error;
    });

    cache.set(spec, request);
    return request;
  }

  /*
   * Fetches without showing anything.
   *
   * Called for every vendor on screen once the browser is idle, and for a row's own
   * entities as soon as the pointer enters the row - so by the time it has travelled
   * the width of a cell to the vendor name, the answer is usually already here and the
   * card opens with no loading state at all.
   */
  function warm(spec) {
    if (!spec || cache.has(spec)) return;
    load(spec).catch(() => {});
  }

  function el(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined && text !== null) node.textContent = text;
    return node;
  }

  /*
   * The one card element, created on first use and reused for every target.
   *
   * Everything that decides where it sits - fixed positioning, its width, its
   * stacking - is written as an inline style rather than left to a class. Tailwind
   * here is a CDN script that generates rules when it notices new class names, so
   * there is a moment after this element is inserted when its classes mean nothing
   * yet. A card that is `position: static` for that moment is a block element in the
   * middle of the document flow, which moves the page it was supposed to annotate.
   * Classes are left to carry the things that are only cosmetic.
   *
   * It also starts inert. A card that appears under the pointer and is immediately
   * able to receive events can swallow the click the reader had already begun - and
   * it did, before this was written: mousedown landed on the vendor name, the card
   * opened between the press and the release, mouseup landed on the card, and the
   * browser dispatched the click to their common ancestor instead of following the
   * link. It becomes interactive a frame after it is placed, which is still long
   * before a pointer can travel into it.
   */
  function ensureCard() {
    if (card) return card;
    card = el('div', 'overflow-hidden rounded-lg bg-white text-left shadow-xl ring-1 ring-black ring-opacity-5');
    card.id = 'peek-card';
    card.setAttribute('role', 'tooltip');
    Object.assign(card.style, {
      position: 'fixed', top: '0px', left: '0px', zIndex: '60',
      width: `${CARD_WIDTH_PX}px`, maxWidth: 'calc(100vw - 16px)',
      display: 'none', pointerEvents: 'none',
    });
    card.addEventListener('mouseenter', () => { pointerInCard = true; clearTimeout(closeTimer); });
    card.addEventListener('mouseleave', () => { pointerInCard = false; scheduleClose(); });
    document.body.appendChild(card);
    return card;
  }

  function render(payload) {
    const node = ensureCard();
    node.replaceChildren();

    const head = el('div', 'border-b border-gray-100 px-4 py-3');
    head.appendChild(el('p', 'text-sm font-semibold text-gray-900', payload.title));
    if (payload.subtitle) head.appendChild(el('p', 'mt-0.5 font-mono text-xs text-gray-500', payload.subtitle));
    if (payload.badges && payload.badges.length) {
      const badges = el('div', 'mt-2 flex flex-wrap gap-1');
      payload.badges.forEach((badge) => {
        badges.appendChild(el('span',
          'inline-flex items-center rounded-md px-1.5 py-0.5 text-xs font-medium ring-1 ring-inset ' +
          (BADGE_TONES[badge.tone] || BADGE_TONES.muted), badge.label));
      });
      head.appendChild(badges);
    }
    node.appendChild(head);

    if (payload.stats && payload.stats.length) {
      // Wrapping row rather than equal columns: 'Total spend' can be 167,000.00 and
      // 'Share' can be 26.8%, and forcing those into the same width either overflows
      // into the neighbour or truncates a figure - both of which misreport money.
      const strip = el('div', 'flex flex-wrap gap-x-6 gap-y-2 border-b border-gray-100 bg-gray-50/60 px-4 py-3');
      payload.stats.forEach((stat) => {
        const cell = el('div');
        cell.appendChild(el('p', 'text-[11px] uppercase tracking-wide text-gray-500', stat.label));
        const value = el('p', 'mt-0.5 text-sm font-semibold tabular-nums text-gray-900', stat.value);
        if (stat.sub) value.appendChild(el('span', 'ml-1 text-[11px] font-normal text-gray-400', stat.sub));
        cell.appendChild(value);
        strip.appendChild(cell);
      });
      node.appendChild(strip);
    }

    if (payload.rows && payload.rows.length) {
      const list = el('dl', 'divide-y divide-gray-50 px-4 py-1');
      payload.rows.forEach((row) => {
        const line = el('div', 'flex items-baseline justify-between gap-x-3 py-1.5');
        line.appendChild(el('dt', 'truncate text-xs text-gray-500', row.label));
        line.appendChild(el('dd', 'shrink-0 font-mono text-xs tabular-nums ' + (TONES[row.tone] || TONES.muted), row.value));
        list.appendChild(line);
      });
      node.appendChild(list);
    }

    if (payload.checks && payload.checks.length) {
      const list = el('ul', 'max-h-64 divide-y divide-gray-50 overflow-y-auto border-t border-gray-100 px-4 py-1');
      payload.checks.forEach((check) => {
        const [glyph, style] = CHECK_MARKS[check.status] || CHECK_MARKS.na;
        const line = el('li', 'py-2');
        const head2 = el('div', 'flex items-center gap-x-2');
        head2.appendChild(el('span',
          'inline-flex h-4 w-4 flex-shrink-0 items-center justify-center rounded-full text-[10px] font-bold ring-1 ring-inset ' + style,
          glyph));
        head2.appendChild(el('p', 'text-xs font-medium text-gray-900', check.label));
        line.appendChild(head2);
        line.appendChild(el('p', 'mt-0.5 pl-6 text-xs leading-relaxed text-gray-600', check.detail));
        list.appendChild(line);
      });
      node.appendChild(list);
    }

    (payload.notes || []).forEach((note) => {
      node.appendChild(el('p',
        'border-t border-gray-100 px-4 py-2.5 text-xs leading-relaxed ' + (NOTE_TONES[note.tone] || NOTE_TONES.muted),
        note.text));
    });

    // Wraps rather than truncates: the evidence line is what lets the reader judge
    // the figures above it instead of trusting them, and half of it is no use.
    const foot = el('div', 'flex flex-wrap items-center justify-between gap-x-3 gap-y-1 border-t border-gray-100 px-4 py-2');
    if (payload.evidence) foot.appendChild(el('span', 'text-[11px] leading-snug text-gray-400', payload.evidence));
    if (payload.href) {
      const link = el('a', 'ml-auto shrink-0 text-xs font-semibold text-indigo-600 hover:text-indigo-500',
        (payload.href_label || 'Open') + ' →');
      link.href = payload.href;
      foot.appendChild(link);
    }
    if (foot.childElementCount) node.appendChild(foot);
  }

  function renderPlaceholder(label, message) {
    const node = ensureCard();
    node.replaceChildren();
    const head = el('div', 'px-4 py-3');
    head.appendChild(el('p', 'text-sm font-semibold text-gray-900', label));
    head.appendChild(el('p', 'mt-1 text-xs text-gray-500', message));
    node.appendChild(head);
  }

  /*
   * Places the card beside its target, and never on top of it.
   *
   * Below and left-aligned by default, flipped above when there is more room there,
   * and clamped horizontally so a card anchored to the last column is not half off the
   * screen. What it must never do is cover the value it describes: the reader's next
   * move is usually to click that link, and a card sitting over it swallows the click.
   * So the card is given the space that is actually free on the chosen side as a
   * max-height and scrolls inside it, rather than being nudged back over the anchor to
   * fit - a compliance card with eleven checks is four times the height of a date card
   * and there is not always room for it.
   *
   * Measured, then measured again on the next frame: Tailwind is loaded from a CDN
   * that generates classes as they appear in the DOM, so the first measurement of a
   * freshly built card can be taken before its own styles exist.
   */
  function place(anchor) {
    const node = ensureCard();
    node.style.display = 'block';

    const box = anchor.getBoundingClientRect();
    const belowRoom = window.innerHeight - box.bottom - GAP_PX - EDGE_PX;
    const aboveRoom = box.top - GAP_PX - EDGE_PX;

    node.style.maxHeight = '';
    const wanted = node.offsetHeight;
    const above = wanted > belowRoom && aboveRoom > belowRoom;
    const room = Math.max(120, above ? aboveRoom : belowRoom);

    node.style.maxHeight = `${Math.round(room)}px`;
    node.style.overflowY = wanted > room ? 'auto' : '';
    const height = Math.min(wanted, room);

    const width = node.offsetWidth || CARD_WIDTH_PX;
    const left = Math.max(EDGE_PX, Math.min(box.left, window.innerWidth - width - EDGE_PX));
    const top = above ? box.top - GAP_PX - height : box.bottom + GAP_PX;

    node.style.top = `${Math.round(top)}px`;
    node.style.left = `${Math.round(left)}px`;
  }

  /* Places now, then again once the browser has laid the card out for real. */
  function placeSettled(anchor) {
    place(anchor);
    requestAnimationFrame(() => {
      if (target !== anchor) return;
      place(anchor);
      card.style.pointerEvents = 'auto';
    });
  }

  function open(anchor) {
    const spec = anchor.getAttribute('data-peek');
    if (!spec) return;

    target = anchor;
    const ticket = ++sequence;
    const known = anchor.getAttribute('data-peek-title') || anchor.textContent.trim();

    if (cache.has(spec)) {
      // Already answered: no skeleton, no flicker, no perceptible wait.
      cache.get(spec).then((payload) => {
        if (ticket !== sequence) return;
        render(payload);
        placeSettled(anchor);
      }).catch(() => {});
      return;
    }

    renderPlaceholder(known, 'Looking…');
    placeSettled(anchor);
    anchor.setAttribute('aria-describedby', 'peek-card');

    load(spec).then((payload) => {
      if (ticket !== sequence) return;
      render(payload);
      placeSettled(anchor);
    }).catch((error) => {
      if (ticket !== sequence) return;
      renderPlaceholder(known, error.message);
      placeSettled(anchor);
    });
  }

  function close() {
    sequence += 1;
    pointerInCard = false;
    if (target) target.removeAttribute('aria-describedby');
    target = null;
    if (card) {
      card.style.display = 'none';
      card.style.pointerEvents = 'none';
    }
  }

  function scheduleClose() {
    clearTimeout(closeTimer);
    closeTimer = setTimeout(() => {
      if (!pointerInCard) close();
    }, CLOSE_DELAY_MS);
  }

  function peekTarget(node) {
    return node && node.closest ? node.closest('[data-peek]') : null;
  }

  document.addEventListener('mouseover', (event) => {
    // Rows warm their own entities before the pointer has reached any of them.
    const row = event.target.closest && event.target.closest('[data-peek-warm]');
    if (row) row.getAttribute('data-peek-warm').split(',').forEach((spec) => warm(spec.trim()));

    const anchor = peekTarget(event.target);
    if (!anchor) return;
    if (anchor === target) { clearTimeout(closeTimer); return; }

    warm(anchor.getAttribute('data-peek'));
    if (coarsePointer) return;    // Touch devices open on tap instead; see below.

    clearTimeout(openTimer);
    clearTimeout(closeTimer);
    openTimer = setTimeout(() => open(anchor), OPEN_DELAY_MS);
  });

  document.addEventListener('mouseout', (event) => {
    const anchor = peekTarget(event.target);
    if (!anchor) return;
    // A move within the same target - text to its own child span - is not a leave.
    if (event.relatedTarget && peekTarget(event.relatedTarget) === anchor) return;
    clearTimeout(openTimer);
    scheduleClose();
  });

  // Keyboard: tabbing to a target opens its card, exactly as hovering does, and the
  // card's own link is the next thing in the tab order because it is inside the page.
  document.addEventListener('focusin', (event) => {
    const anchor = peekTarget(event.target);
    if (anchor) open(anchor); else if (!card || !card.contains(event.target)) close();
  });

  /*
   * On a touch screen there is no hover to intend with, so the first tap opens the
   * card and the link inside it is what navigates. Following the target's own href on
   * that first tap would take the reader to the page they were trying to preview.
   */
  document.addEventListener('click', (event) => {
    const anchor = peekTarget(event.target);
    if (!anchor) {
      if (!card || !card.contains(event.target)) close();
      return;
    }
    if (!coarsePointer) return;
    if (anchor === target) return;   // Second tap on an open card follows the link.
    event.preventDefault();
    clearTimeout(closeTimer);
    open(anchor);
  });

  /*
   * A press means the reader has chosen the link, not the card.
   *
   * Without this, a click held for longer than the open delay - which is most clicks -
   * opens a card between the press and the release, and the release lands somewhere
   * else. Cancelling the pending open leaves the target where the pointer put it.
   */
  document.addEventListener('mousedown', () => { clearTimeout(openTimer); }, true);

  document.addEventListener('keydown', (event) => { if (event.key === 'Escape') close(); });
  // A card pinned to a rectangle that has moved is worse than no card.
  window.addEventListener('scroll', () => { if (target) close(); }, true);
  window.addEventListener('resize', () => { if (target) close(); });

  /*
   * Warms every distinct entity on screen once the browser has nothing better to do.
   *
   * Capped, because this is a courtesy and not the page's job: a dashboard showing 50
   * receipts holds perhaps a dozen distinct vendors, and prefetching those covers most
   * of what anyone will hover at the cost of a dozen small aggregate queries.
   */
  function warmVisible(limit = 16) {
    const seen = new Set();
    document.querySelectorAll('[data-peek-eager]').forEach((node) => {
      const spec = node.getAttribute('data-peek');
      if (!spec || seen.has(spec) || seen.size >= limit) return;
      seen.add(spec);
      warm(spec);
    });
  }

  const idle = window.requestIdleCallback || ((fn) => setTimeout(fn, 400));
  idle(() => warmVisible());
  // The dashboard replaces its rows without a page load, so the new ones get the same
  // treatment. Debounced: Alpine writes a table one node at a time.
  let sweep = null;
  new MutationObserver(() => {
    clearTimeout(sweep);
    sweep = setTimeout(() => idle(() => warmVisible()), 500);
  }).observe(document.body, { childList: true, subtree: true });
})();
