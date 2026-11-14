'use strict';
const { chromium } = require('playwright');
const { mkdtempSync, readFileSync, mkdirSync, rmSync } = require('node:fs');
const { tmpdir } = require('node:os');
const { join } = require('node:path');
const { spawn, spawnSync } = require('node:child_process');
const assert = require('node:assert/strict');
const net = require('node:net');

async function freePort() {
  return new Promise((resolve) => {
    const s = net.createServer();
    s.listen(0, '127.0.0.1', () => {
      const p = s.address().port;
      s.close(() => resolve(p));
    });
  });
}
const pause = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
/* FOUNDRY_BROWSER_SLOWDOWN: see where the browser is launched in main(). */
const slowdown = (() => {
  const v = process.env.FOUNDRY_BROWSER_SLOWDOWN;
  if (v === undefined || v === '') return 1;
  const n = Number(v);
  if (!Number.isFinite(n) || n < 1)
    throw new Error(`FOUNDRY_BROWSER_SLOWDOWN must be a number of 1 or more, not "${v}"`);
  return n;
})();
/* A timer the page sets for this long or less is waited for by quiet(); a longer one is not. The
   page's message hides after 5,500 ms, and nothing should wait for that. A fault that acts on a
   timer longer than this is out of quiet()'s reach. */
const SHORT_TIMER_MS = 2000;
/* Run in each page before its own script. It follows two things, and changes nothing the page
   does with either:
   - The page's requests whose answers it has not yet finished with. A request leaves the list
     once its answer has been read, or its failure reported, and the page's own code that follows
     has run: that code runs in promise jobs, and every one queued by then runs before a timer
     can fire. A request that code sends in turn is on the list before the one that caused it
     leaves.
   - The page's short timers: each setTimeout of `short` ms or less, given a function, from the
     moment it is set until its function has run or clearTimeout cancels it. A timer or request
     set by that function is followed before the first one leaves.
   Not followed: setInterval, setTimeout given a string, timers longer than `short`, and work the
   browser starts on its own, such as a dialog's close event. quiet() covers what is already
   queued of the last of these by waiting a frame and a task, and checking again. */
function trackPending(short) {
  const open = [];
  window.__browserTestRequests = open;
  const timers = new Set();
  window.__browserTestTimers = timers;
  const later = window.setTimeout,
    cancel = window.clearTimeout;
  window.setTimeout = function (fn, ms, ...args) {
    // no delay, or one that is not a number, is a delay of 0, as the browser counts it
    if (typeof fn !== 'function' || (Number(ms) || 0) > short) return later(fn, ms, ...args);
    const id = later(function () {
      try {
        return fn.apply(this, args);
      } finally {
        timers.delete(id);
      }
    }, ms);
    timers.add(id);
    return id;
  };
  window.clearTimeout = function (id) {
    timers.delete(id);
    return cancel(id);
  };
  // whether anything is pending, leaving out requests matching a hold() ({ path, method })
  window.__browserTestPending = (held) =>
    timers.size > 0 ||
    open.some((r) => !held.some((h) => h.path === r.path && (!h.method || h.method === r.method)));
  // after one frame and one task, whether anything is pending again; its own task is not followed
  window.__browserTestSettled = (held) =>
    new Promise((r) =>
      requestAnimationFrame(() => later(() => r(window.__browserTestPending(held)), 0)),
    );
  const send = window.fetch;
  window.fetch = (input, init) => {
    const entry = {
      path: new URL(String(input), location.href).pathname,
      method: String(init?.method || 'GET').toUpperCase(),
    };
    open.push(entry);
    const finish = () =>
      later(() => {
        const at = open.indexOf(entry);
        if (at >= 0) open.splice(at, 1);
      }, 0);
    return send(input, init).then(
      (response) => {
        const json = response.json;
        response.json = function () {
          const read = json.call(this);
          read.then(finish, finish);
          return read;
        };
        return response;
      },
      (error) => {
        finish();
        throw error;
      },
    );
  };
}
/* The answers each page's hold()s are still holding back, so quiet() does not wait for them. */
const holding = new WeakMap();
const heldOn = (page) => {
  if (!holding.has(page)) holding.set(page, new Set());
  return holding.get(page);
};
/* Resolve once nothing trackPending() follows is pending in the page: every request it has sent
   has been finished with, except those a hold() is still holding back, and every timer of
   SHORT_TIMER_MS or less it has set has run or been cancelled. Then one frame is drawn and one
   task runs, and if that started a request or a short timer, it waits again. What it follows:
   requests by fetch, short timers, and work already queued for the next frame or task, such as
   a dialog's close event. What it does not: a timer longer than SHORT_TIMER_MS, setInterval, a
   timer given a string, and work the browser starts later on its own. A check that something
   did not happen waits for this, so that a page that does the thing a short time later, by a
   request or by a timer, is caught. It never waits for a fixed time. */
async function quiet(page) {
  const held = [...heldOn(page)];
  do await page.waitForFunction((held) => !window.__browserTestPending(held), held);
  while (await page.evaluate((held) => window.__browserTestSettled(held), held));
}
/* Every page the test opens comes from here, so every one is slowed alike. */
async function newPage(owner, options) {
  const p = await owner.newPage(options);
  await p.addInitScript(trackPending, SHORT_TIMER_MS);
  if (slowdown > 1) {
    const cdp = await p.context().newCDPSession(p);
    await cdp.send('Emulation.setCPUThrottlingRate', { rate: slowdown });
  }
  return p;
}

/* Start an isolated server on a temporary database. Never the repository's own .foundry/. */
async function startServer(python, db, logs) {
  const port = await freePort(),
    origin = `http://127.0.0.1:${port}`;
  // The server's stdout carries only uvicorn's access log, a line for every request. Nothing here
  // reads it, and a pipe nobody reads fills up and stops the server at its next write, so it is
  // not a pipe at all. Its stderr, the server's own messages, is read as it arrives.
  const server = spawn(python, ['-m', 'foundry.cli', '--db', db, 'serve', '--port', String(port)], {
    stdio: ['ignore', 'ignore', 'pipe'],
  });
  server.stderr.on('data', (d) => logs.push(d.toString()));
  for (let i = 0; i < 100; i++) {
    try {
      if ((await fetch(origin + '/api/health')).ok) break;
    } catch {}
    await pause(100);
  }
  return { server, origin };
}
/* A workspace has opened: on Today, or on Start here for a business whose guided path is not
   done yet. */
const OPENED = '#list .today-head, #list .start-head';
/* Wait for a promise for at most `ms`; resolves true if it settled in time, fulfilled or not.
   Its timer is cleared either way, so it never keeps this process alive. */
async function within(promise, ms) {
  let timer;
  const late = new Promise((r) => (timer = setTimeout(() => r(false), ms)));
  try {
    return await Promise.race([
      promise.then(
        () => true,
        () => true,
      ),
      late,
    ]);
  } finally {
    clearTimeout(timer);
  }
}
/* End a server for certain: SIGTERM, up to 3 seconds for it to leave, then SIGKILL and up to 5
   more for its exit. A server that has already left is not an error. Returns only once the
   process is gone; throws if it is somehow still there after the SIGKILL. */
async function stopServer(server) {
  if (!server) return;
  const gone = () => server.exitCode !== null || server.signalCode !== null;
  if (gone()) return;
  const exited = new Promise((r) => server.once('exit', r));
  server.kill('SIGTERM');
  if ((await within(exited, 3000)) || gone()) return;
  server.kill('SIGKILL');
  if ((await within(exited, 5000)) || gone()) return;
  throw new Error(`the server (pid ${server.pid}) did not end after SIGKILL`);
}
async function signIn(page, origin, keyFile) {
  await page.goto(origin);
  await page.getByLabel('Access key', { exact: true }).fill(readFileSync(keyFile, 'utf8').trim());
  await page.getByRole('button', { name: 'Open workspace' }).click();
}
const overflow = (page) => page.evaluate(() => document.documentElement.scrollWidth > innerWidth);
/* The note under Today's "Ready to start" heading, or null when there is none. */
const readyNote = (page) =>
  page.evaluate(() => {
    const head = [...document.querySelectorAll('#list h2.grp')].find((x) =>
      x.textContent.startsWith('Ready to start'),
    );
    const next = head?.nextElementSibling;
    return next?.classList.contains('grp-note') ? next.textContent : null;
  });
/* A line's ? and what it opens, as a screen reader meets them: its name, whether it says it is
   open, the text it controls, and whether that text follows it. */
const helpState = (page, button) =>
  button.evaluate((b) => {
    const panel = document.getElementById(b.getAttribute('aria-controls'));
    const after = panel && b.compareDocumentPosition(panel) & Node.DOCUMENT_POSITION_FOLLOWING;
    return {
      tag: b.tagName,
      expanded: b.getAttribute('aria-expanded'),
      shown: Boolean(panel && !panel.hidden && panel.getClientRects().length),
      text: panel?.textContent || '',
      follows: Boolean(after),
      focused: document.activeElement === b,
    };
  });
/* What has keyboard focus: enough to say which control it is and whether a person can see it. */
const focused = (page) =>
  page.evaluate(() => {
    const a = document.activeElement;
    return {
      tag: a.tagName.toLowerCase(),
      id: a.id,
      src: a.dataset?.src || null,
      key: a.dataset?.key || null,
      action: a.dataset?.action || null,
      dataId: a.dataset?.id || null,
      to: a.dataset?.to || null,
      current: a.getAttribute('aria-current'),
      pressed: a.getAttribute('aria-pressed'),
      visible: a !== document.body && a.getClientRects().length > 0,
      pane: a.closest('.pane')?.classList[1] || null,
    };
  });
/* Hold back every response matching a path (and, if given, a method) until release(): the
   request reaches the server at once, so the held response is what the server said then,
   delivered late. `requested` resolves with that response once the server has answered. With
   `lose`, release() drops the connection instead, as a lost answer does: the server acted, and
   the page never hears what it said. A request the page cancels while it is held never gets
   its answer; `cancelled()` then says so, and release() lets the held answer go nowhere.
   Once released, the route stays in place and passes every later request on untouched, as if
   it were gone, so a later hold() or dropNext() on the same path acts as it would without it.
   It is not removed: the page sends its next request as soon as the held answer arrives, and
   a request the browser starts while a route is being removed can be left unanswered for good.
   settle() removes every route, just before the page is loaded again. release() resolves once
   the page has finished with the answer and with anything it sent because of it (quiet()). */
async function hold(page, path, { method = null, lose = false } = {}) {
  const match = (url) => url.pathname === path;
  const mine = (r) => match(new URL(r.url())) && (!method || r.method() === method);
  let open, seen, handled;
  const gate = new Promise((r) => (open = r)),
    requested = new Promise((r) => (seen = r)),
    done = new Promise((r) => (handled = r));
  let released = false,
    cancelled = false;
  const failed = (r) => {
    if (!released && mine(r)) cancelled = true;
  };
  page.on('requestfailed', failed);
  const entry = { path, method };
  heldOn(page).add(entry);
  const handler = async (route) => {
    if (released || (method && route.request().method() !== method)) return route.fallback();
    const response = await route.fetch();
    seen(response);
    await gate;
    try {
      if (lose) await route.abort('connectionreset');
      else await route.fulfill({ response });
    } catch {
      // the page cancelled the request while it was held
      cancelled = true;
    }
    handled();
  };
  await page.route(match, handler);
  return {
    requested,
    cancelled: () => cancelled,
    async release() {
      const finished = page.waitForEvent(lose ? 'requestfailed' : 'requestfinished', mine);
      // a cancelled request has no event left to wait for
      finished.catch(() => {});
      released = true;
      heldOn(page).delete(entry);
      page.off('requestfailed', failed);
      open();
      await Promise.race([done, finished]);
      if (!cancelled) await finished;
      await quiet(page);
    },
  };
}
/* Fail the next request for a path as a dropped connection does; later ones go through. A
   dropped command never reaches the server; a dropped read is a reload that fails. off() stops
   it dropping anything; like hold(), the route stays in place and passes requests on. */
async function dropNext(page, path, method = 'GET') {
  const match = (url) => url.pathname === path;
  let used = false,
    done;
  const dropped = new Promise((r) => (done = r));
  const handler = async (route) => {
    if (used || route.request().method() !== method) return route.fallback();
    used = true;
    await route.abort('connectionreset');
    done();
  };
  await page.route(match, handler);
  // resolves once the page has finished with the failure and anything it sent because of it
  const finished = dropped.then(() => quiet(page));
  // a case that fails before it waits for this, and a page loaded again meanwhile, would
  // otherwise leave its failure unhandled; a case that waits for it still sees the failure
  finished.catch(() => {});
  return {
    dropped: finished,
    off: () => {
      used = true;
    },
  };
}
/* Every list on the page, stated by a role or by ul/ol, holds only list items, and every row of
   the middle list is a button directly inside a list item directly inside a named list. */
const listFaults = (page) =>
  page.evaluate(() => {
    const isList = (el) =>
      el.hasAttribute('role') ? el.getAttribute('role') === 'list' : /^(UL|OL)$/.test(el.tagName);
    const isItem = (el) =>
      el.hasAttribute('role') ? el.getAttribute('role') === 'listitem' : el.tagName === 'LI';
    const name = (el) =>
      el.tagName.toLowerCase() +
      (el.id ? '#' + el.id : '') +
      [...el.classList].map((c) => '.' + c).join('');
    const out = [];
    for (const list of document.querySelectorAll('*'))
      if (isList(list))
        for (const child of list.children)
          if (!isItem(child)) out.push(`${name(list)} holds ${name(child)}`);
    for (const row of document.querySelectorAll('#list .row')) {
      const item = row.parentElement,
        list = item?.parentElement;
      if (row.tagName !== 'BUTTON' || row.hasAttribute('role'))
        out.push(`row ${row.dataset.key} is not a plain button`);
      if (!item || !isItem(item) || !list || !isList(list))
        out.push(`row ${row.dataset.key} is not in a list item in a list`);
      else if (!document.getElementById(list.getAttribute('aria-labelledby'))?.textContent.trim())
        out.push(`the list holding ${row.dataset.key} has no name`);
    }
    return out;
  });
/* Visit every source at desktop, medium and phone widths and check every list on each. */
async function walkLists(page, what) {
  for (const [width, height] of [
    [1440, 1050],
    [900, 1000],
    [390, 844],
  ]) {
    await page.setViewportSize({ width, height });
    const ids = await page.locator('#src-nav .src').evaluateAll((s) => s.map((x) => x.dataset.src));
    for (const id of ids.length ? ids : [null]) {
      if (id) {
        // on a phone, the sources are a pane of their own
        if (await page.locator('#back-list').isVisible()) await page.locator('#back-list').click();
        if (await page.locator('#back-src').isVisible()) await page.locator('#back-src').click();
        await page.locator(`#src-nav .src[data-src="${id}"]`).click();
        await page
          .locator(`#src-nav .src[data-src="${id}"][aria-current="true"]`)
          .waitFor({ state: 'attached' });
        if (id === 'history')
          await page.locator('#list .grp', { hasText: 'Events' }).waitFor({ state: 'attached' });
      }
      const at = `${what}, ${id || 'no business'}, ${width}px`;
      await quiet(page);
      assert.deepEqual(await listFaults(page), [], `${at}: lists hold only list items`);
      assert.equal(
        await page
          .locator('#list')
          .getByRole('list')
          .getByRole('listitem')
          .getByRole('button')
          .count(),
        await page.locator('#list .row:visible').count(),
        `${at}: every row is announced as a button in a list item in a list`,
      );
    }
  }
  await page.setViewportSize({ width: 1440, height: 1050 });
  // the last click was at phone width; leave nothing hovered for later screenshots
  await page.mouse.move(0, 0);
}
/* The colour each line's paint actually computes to, next to the value of the token it should
   use, for every line rule, casing, bullet disc and code on the page. */
const lineColours = (page) =>
  page.evaluate(() => {
    const root = getComputedStyle(document.documentElement);
    const rgb = (v) => {
      v = v.trim().toLowerCase();
      if (v === 'transparent') return 'rgba(0, 0, 0, 0)';
      const m = /^#([0-9a-f]{6})$/.exec(v);
      if (!m) return `unparsed token ${v}`;
      const n = parseInt(m[1], 16);
      return `rgb(${n >> 16}, ${(n >> 8) & 255}, ${n & 255})`;
    };
    const keyOf = (el, prefix) =>
      [...el.classList].find((c) => c.startsWith(prefix))?.slice(prefix.length);
    const out = [];
    const add = (what, el, prefix, got, token) => {
      const k = keyOf(el, prefix);
      out.push({ what, k, got, want: rgb(root.getPropertyValue(`--${token.replace('*', k)}`)) });
    };
    for (const el of document.querySelectorAll('#insp .mp path.ln'))
      add('map line', el, 'ln-', getComputedStyle(el).stroke, 'l-*');
    for (const el of document.querySelectorAll('#insp .mp path.case'))
      add('map casing', el, 'case-', getComputedStyle(el).stroke, 'case-*');
    for (const el of document.querySelectorAll('#insp .mp circle.bd'))
      add('map bullet disc', el, 'bd-', getComputedStyle(el).fill, 'l-*');
    for (const el of document.querySelectorAll('#insp .mp text.bt'))
      add('map bullet code', el, 'bt-', getComputedStyle(el).fill, 'l-*-ink');
    for (const el of document.querySelectorAll('.bul')) {
      add('bullet disc', el, 'lc-', getComputedStyle(el).backgroundColor, 'l-*');
      add('bullet code', el, 'lc-', getComputedStyle(el).color, 'l-*-ink');
    }
    for (const el of document.querySelectorAll('#list .row.ln'))
      add('row line', el, 'lc-', getComputedStyle(el, '::before').backgroundColor, 'l-*');
    for (const el of document.querySelectorAll('#list .grp.ln'))
      add('group line', el, 'lc-', getComputedStyle(el, '::after').backgroundColor, 'l-*');
    return out;
  });

/* Start here and the lines, explained. `page` is signed in as the owner and shows a new start-up
   business. Every sentence checked here is the server's, from the catalog or the guide; the page
   adds none. Leaves `page` on that business again, at desktop width in the light theme. */
async function startHere(page, context, browser, origin, keyFile) {
  const startBid = await page.locator('#business-select').inputValue();
  const startName = await page.locator('#business-select option:checked').innerText();
  const api = (path, method = 'GET', body = null) =>
    page.evaluate(
      async ([p, m, b]) => {
        const r = await fetch('/api' + p, {
          method: m,
          credentials: 'same-origin',
          headers: { 'Content-Type': 'application/json', 'Idempotency-Key': crypto.randomUUID() },
          body: b ? JSON.stringify(b) : undefined,
        });
        const d = await r.json();
        if (!r.ok) throw new Error(d.detail);
        return d;
      },
      [path, method, body],
    );
  const src = async (id) => {
    await page.locator(`#src-nav .src[data-src="${id}"]`).click();
    // on a phone the sources pane gives way to the list once a source is chosen
    await page
      .locator(`#src-nav .src[data-src="${id}"][aria-current="true"]`)
      .waitFor({ state: 'attached' });
  };
  const openBusiness = async (id) => {
    // a business made through the API is in the selector once the page has loaded the list again
    if (!(await page.locator(`#business-select option[value="${id}"]`).count())) {
      await page.evaluate(() => refresh());
      await quiet(page);
    }
    await page.locator('#business-select').selectOption(id);
    await page.locator(`#business-select option[value="${id}"][selected]`).waitFor({
      state: 'attached',
    });
    await page.locator(OPENED).waitFor();
    await quiet(page);
  };
  const current = () => page.locator('#src-nav .src[aria-current="true"]').getAttribute('data-src');

  // "Do this next" opens the form for it: on a new start-up, a customer record.
  await src('start');
  const nextBox = page.locator('#insp .next-box');
  assert.equal(
    await nextBox.getByRole('button').innerText(),
    'Add a customer record',
    'the next action names its action',
  );
  await nextBox.getByRole('button').click();
  await page.locator('#dialog-title', { hasText: 'Add business record' }).waitFor();
  assert.equal(await page.locator('#dialog select[name="kind"]').inputValue(), 'customer');
  await page.locator('#dialog').getByRole('button', { name: 'Cancel', exact: true }).click();
  await page.locator('#dialog').waitFor({ state: 'hidden' });

  // The ? beside a line's name works from the keyboard, and says what it is to a screen reader.
  const marketHelp = page.getByRole('button', { name: 'What the Market line is', exact: true });
  await marketHelp.focus();
  let help = await helpState(page, marketHelp);
  assert.deepEqual(
    [help.tag, help.expanded, help.shown, help.follows],
    ['BUTTON', 'false', false, true],
    'a closed ? is a button, followed by the text it controls',
  );
  await page.keyboard.press('Enter');
  help = await helpState(page, marketHelp);
  assert.deepEqual([help.expanded, help.shown, help.focused], ['true', true, true], 'Enter opens');
  assert(
    help.text.includes('Who buys, what problem they pay to solve, and what you will offer them.'),
    `the catalog's sentence: ${help.text}`,
  );
  assert(help.text.includes('Your next action: Add a customer record.'), help.text);
  await page.keyboard.press('Escape');
  help = await helpState(page, marketHelp);
  assert.deepEqual([help.expanded, help.shown, help.focused], ['false', false, true], 'Escape');
  await page.keyboard.press('Space');
  assert.equal((await helpState(page, marketHelp)).shown, true, 'Space opens');
  await page.keyboard.press('Space');
  assert.equal((await helpState(page, marketHelp)).shown, false, 'Space closes');

  // The lines are explained under each line's heading, and a step says why it waits.
  await page.locator('#src-nav .src').filter({ hasText: 'Rights' }).first().click();
  assert.equal(
    (await page.locator('#list .lx-note').innerText()).split('. ')[0],
    "Who owns what: the founders' arrangements, intellectual property, data and assets",
  );
  await page.locator('#list .row', { hasText: 'Record founder and IP arrangements' }).click();
  const stands = page
    .locator('#insp .i-sec')
    .filter({ has: page.locator('h3', { hasText: 'Where it stands' }) });
  assert.deepEqual(
    (await stands.locator('dd').first().locator('li').allInnerTexts()).map((t) => t.trim()),
    [
      'It cannot be approved until Decide the entity and ownership structure is finished. That step is not started.',
      'It cannot be approved until there is a contract record.',
    ],
  );
  // Its next action opens the form for what it needs: a contract record, already chosen.
  await stands.getByRole('button', { name: 'Add a contract record', exact: true }).click();
  await page.locator('#dialog-title', { hasText: 'Add business record' }).waitFor();
  assert.equal(await page.locator('#dialog select[name="kind"]').inputValue(), 'contract');
  await page.locator('#dialog').getByRole('button', { name: 'Cancel', exact: true }).click();
  await page.locator('#dialog').waitFor({ state: 'hidden' });
  // The step's own ? opens the same text as its line's ? in the source list.
  const stepHelp = page.locator('#insp .i-kicker [data-action="help"]');
  await stepHelp.click();
  const rightsHelp = await helpState(page, stepHelp);
  assert(rightsHelp.shown && rightsHelp.text.startsWith('Rights.'), rightsHelp.text);
  // A step nobody has started offers starting it, not preparing it, as its main move.
  assert.equal(
    await page.locator('#insp button[data-action="tx"].primary').innerText(),
    'Start work…',
  );

  // An acquisition: a new business whose entry path is buying one.
  const acq = await api('/businesses', 'POST', {
    name: 'Browser laundry purchase',
    route: 'acquire',
    concept: 'Synthetic browser test: buy a small laundromat in a made-up town.',
    is_demo: true,
  });
  await openBusiness(acq.id);
  assert.equal(await current(), 'start', 'an acquisition opens on Start here');
  const linesExplained = await page.locator('#list .row.lx .r-t').allInnerTexts();
  assert.deepEqual(
    linesExplained.map((t) => t.trim()),
    ['Acquisition', 'Market', 'Finance', 'Setup', 'Rights', 'Delivery', 'Sales to cash', 'Operate'],
    'the Acquisition line comes first',
  );
  // its stops tick off from its records: attach, review, screen
  const listing = await api(`/businesses/${acq.id}/evidence`, 'POST', {
    title: 'Browser listing text',
    content: 'SYNTHETIC listing: asking price, revenue and earnings.',
  });
  await api(`/businesses/${acq.id}/evidence/${listing.id}/review`, 'POST', {
    note: 'Read the synthetic listing in full.',
    expected_version: 1,
  });
  await api(`/businesses/${acq.id}/deals`, 'POST', {
    target: 'Browser laundry',
    thesis: 'Synthetic thesis for the browser test.',
    annual_revenue: '210000',
    reported_ebitda: '62000',
    purchase_price: '180000',
  });
  await page.evaluate(() => refresh());
  await quiet(page);
  assert.equal(await page.locator('#src-nav .src[data-src="start"] .n').innerText(), '4 of 5');
  // Start-up lines on an acquisition say what they mean there.
  await page.locator('#src-nav .src').filter({ hasText: 'Delivery' }).first().click();
  assert(
    (await page.locator('#list .lx-note').innerText()).includes(
      'When you buy a business, the work already happens. This line is where you write down how, so it runs the same way after the handover.',
    ),
  );
  // Today agrees with Start here: the same next action, and the Acquisition line's step first.
  await src('start');
  await page.locator('#list .row[data-key="stop:5"]').click();
  const startNext = await page.locator('#insp .next-box').getByRole('button').innerText();
  assert.equal(startNext, 'Start work on Approve the acquisition thesis');
  await src('today');
  assert.equal(await page.locator('#list .row[data-key^="next:"] .r-t').innerText(), startNext);
  const readyTitles = await page.evaluate(() => {
    const head = [...document.querySelectorAll('#list h2.grp')].find((x) =>
      x.textContent.startsWith('Ready to start'),
    );
    return [...document.querySelectorAll(`#list ul[aria-labelledby="${head.id}"] .r-t`)].map(
      (x) => x.textContent,
    );
  });
  assert.deepEqual(readyTitles, [
    'Approve the acquisition thesis',
    'Resolve operating and founder jurisdictions',
  ]);
  assert.equal(await readyNote(page), null, 'neither ready step has a prerequisite');
  // "Do this next" for a step opens the step with its decision form at the move it names.
  await src('start');
  await page.locator('#insp .next-box').getByRole('button').click();
  await page.locator('#insp #tx-form').waitFor();
  assert.equal(await page.locator('#insp-title').innerText(), 'Approve the acquisition thesis');
  assert.equal(await page.locator('#tx-form select[name="state"]').inputValue(), 'in_progress');
  await page.locator('#tx-form').getByRole('button', { name: 'Not yet' }).click();

  // The thesis approved and verified: the diligence steps it unlocks have a prerequisite, the
  // jurisdiction step has none, and Today says exactly that.
  const thesisOf = async () =>
    (await api(`/businesses/${acq.id}`)).objects.find(
      (o) => o.kind === 'task' && o.data.key === 'mandate',
    );
  for (const state of ['prepared', 'approved', 'verified']) {
    const t = await thesisOf();
    await api(`/businesses/${acq.id}/tasks/${t.id}/transition`, 'POST', {
      state,
      expected_version: t.version,
      note: 'Synthetic decision for the browser test.',
      evidence_ids: [listing.id],
    });
  }
  // Its path is done: the business now opens on Today.
  await openBusiness(startBid);
  assert.equal(await current(), 'start', 'the start-up path is not done');
  await openBusiness(acq.id);
  assert.equal(await current(), 'today', 'a business whose path is done opens on Today');
  assert.equal(
    await readyNote(page),
    'The prerequisites of 2 of these are complete; the other has none.',
  );

  // An older analysis whose evidence changed is ruled out, not saved again: a newer analysis was
  // saved after it, and saving another can never clear it.
  const two = await api('/businesses', 'POST', {
    name: 'Browser laundry, two listings',
    route: 'acquire',
    concept: 'Synthetic browser test: a purchase whose first listing was withdrawn.',
    is_demo: true,
  });
  const ofTwo = async () => (await api(`/businesses/${two.id}`)).objects;
  const reviewedEvidence = async (title, source_type = 'document') => {
    const e = await api(`/businesses/${two.id}/evidence`, 'POST', {
      title,
      content: 'SYNTHETIC evidence for the browser test.',
      source_type,
    });
    return api(`/businesses/${two.id}/evidence/${e.id}/review`, 'POST', {
      note: 'Read the synthetic item in full.',
      expected_version: e.version,
    });
  };
  const moveTo = (t, state, evidence) =>
    api(`/businesses/${two.id}/tasks/${t.id}/transition`, 'POST', {
      state,
      expected_version: t.version,
      note: 'Synthetic decision for the browser test.',
      evidence_ids: evidence ? [evidence.id] : [],
    });
  const closingReceipt = await reviewedEvidence('Browser closing receipt', 'external_receipt');
  for (const t of await ofTwo())
    if (t.kind === 'task' && !t.data.deal_id) await moveTo(t, 'not_applicable', closingReceipt);
  const screen = (target, e) =>
    api(`/businesses/${two.id}/deals`, 'POST', {
      target,
      thesis: 'Synthetic thesis for the browser test.',
      annual_revenue: '210000',
      reported_ebitda: '62000',
      purchase_price: '180000',
      evidence_ids: [e.id],
    });
  const firstListing = await reviewedEvidence('Browser first listing');
  const firstDeal = await screen('Browser laundry first offer', firstListing);
  await api(`/businesses/${two.id}/evidence/${firstListing.id}/revoke`, 'POST', {
    note: 'The seller withdrew the first listing.',
    expected_version: firstListing.version,
  });
  const secondDeal = await screen(
    'Browser laundry second offer',
    await reviewedEvidence('Browser second listing'),
  );
  // the second analysis's steps up to its closing, which is submitted and waits on its receipt
  for (const key of [
    'mandate',
    'financial_diligence',
    'legal_diligence',
    'investment',
    'closing',
  ]) {
    const external = key === 'closing';
    for (const state of ['prepared', 'approved', external ? 'submitted' : 'verified']) {
      const t = (await ofTwo()).find(
        (o) => o.kind === 'task' && o.data.deal_id === secondDeal.id && o.data.key === key,
      );
      await moveTo(t, state, closingReceipt);
    }
  }
  const oldThesis = (await ofTwo()).find(
    (o) => o.kind === 'task' && o.data.deal_id === firstDeal.id && o.data.key === 'mandate',
  );
  const ruleOut = 'Rule out Approve the acquisition thesis: a newer analysis replaced this one';
  const olderWhy =
    'This step belongs to an older acquisition analysis whose evidence changed. A newer analysis was saved after it, so saving another analysis does not clear it.';
  await openBusiness(two.id);
  assert.equal(await current(), 'today', 'its path is done');
  // Today's "Do this next" row names the rule-out and the older analysis's line.
  const doNextRow = page.locator('#list .row[data-key^="next:"]');
  assert.equal(await doNextRow.locator('.r-t').innerText(), ruleOut);
  assert(
    (await doNextRow.innerText()).includes(
      'Acquisition · Browser laundry first offer line · 18 Approve the acquisition thesis',
    ),
    await doNextRow.innerText(),
  );
  // The step it opens says why, what unlocks it and offers the rule-out.
  await doNextRow.click();
  assert.equal(await page.locator('#insp-title').innerText(), 'Approve the acquisition thesis');
  const where = page
    .locator('#insp .i-sec')
    .filter({ has: page.locator('h3', { hasText: 'Where it stands' }) });
  assert.deepEqual((await where.locator('dd').nth(0).innerText()).trim(), olderWhy, 'why it waits');
  assert.deepEqual(
    (await where.locator('dd').nth(1).locator('li').allInnerTexts()).map((t) => t.trim()),
    [
      'An owner or reviewer rules it out as not applicable, with reviewed evidence.',
      'If you still want this deal, a fresh analysis of it is a new analysis with its own steps; these still need ruling out.',
    ],
  );
  // Its button opens the decision form at not applicable, which asks for reviewed evidence.
  await where.getByRole('button', { name: ruleOut, exact: true }).click();
  await page.locator('#insp #tx-form').waitFor();
  assert.equal(await page.locator('#tx-form select[name="state"]').inputValue(), 'not_applicable');
  assert.equal(
    await page.locator('#tx-text').innerText(),
    'Record a reviewed decision that this step does not apply here. Attach reviewed evidence for that decision.',
  );
  assert.equal(await page.locator('#tx-form [name="evidence_ids"]').count(), 1);
  await page.locator('#tx-form').getByRole('button', { name: 'Not yet' }).click();
  // The older analysis's line heading and its ? give the same next action.
  const olderLine = 'Acquisition · Browser laundry first offer';
  await page.locator('#src-nav .src').filter({ hasText: 'Browser laundry first offer' }).click();
  assert(
    (await page.locator('#list .lx-note').innerText()).includes(`Your next action: ${ruleOut}.`),
    await page.locator('#list .lx-note').innerText(),
  );
  const olderHelp = page
    .locator('#src-nav')
    .getByRole('button', { name: `What the ${olderLine} line is`, exact: true });
  await olderHelp.click();
  const olderText = (await helpState(page, olderHelp)).text;
  assert(olderText.includes(`Your next action: ${ruleOut}.`), olderText);
  await olderHelp.click();
  // The lines explained on Start here: the same reasons and the same button, which opens the form.
  await src('start');
  const lineRow = page.locator('#list .row.lx').filter({ hasText: olderLine });
  await lineRow.click();
  const onBusiness = page
    .locator('#insp .i-sec')
    .filter({ has: page.locator('h3', { hasText: 'On this business' }) });
  assert((await onBusiness.locator('dd').nth(0).innerText()).includes(olderWhy));
  await onBusiness.getByRole('button', { name: ruleOut, exact: true }).click();
  await page.locator('#insp #tx-form').waitFor();
  assert.equal(await page.locator('#insp-title').innerText(), 'Approve the acquisition thesis');
  assert.equal(await page.locator('#tx-form select[name="state"]').inputValue(), 'not_applicable');
  await page.locator('#tx-form').getByRole('button', { name: 'Not yet' }).click();
  // The rule-out the page offers is one the server accepts; then the next older step is next.
  await moveTo(oldThesis, 'not_applicable', closingReceipt);
  await page.evaluate(() => refresh());
  await quiet(page);
  await src('today');
  assert.equal(
    await page.locator('#list .row[data-key^="next:"] .r-t').innerText(),
    'Rule out Reconcile earnings and working capital: a newer analysis replaced this one',
  );

  // Start here at three widths and in both themes: nothing wider than the page, every ? a
  // visible control, and the next stop's action in view.
  await openBusiness(startBid);
  for (const scheme of ['light', 'dark']) {
    await page.emulateMedia({ colorScheme: scheme });
    for (const [width, height] of [
      [1440, 1050],
      [900, 1000],
      [390, 844],
    ]) {
      await page.setViewportSize({ width, height });
      if (await page.locator('#back-list').isVisible()) await page.locator('#back-list').click();
      if (await page.locator('#back-src').isVisible()) await page.locator('#back-src').click();
      await src('start');
      await page.locator('#list .row[aria-current="true"]').click();
      await page.locator('#insp .next-box').waitFor();
      await quiet(page);
      const at = `${width}px, ${scheme}`;
      assert.equal(await overflow(page), false, `${at}: horizontal overflow`);
      assert(await page.locator('#insp .next-box').getByRole('button').isVisible(), at);
      await page.screenshot({ path: `test-results/start-here-${width}-${scheme}.png` });
      if (width === 390) await page.locator('#back-list').click();
      if (await page.locator('#back-src').isVisible()) await page.locator('#back-src').click();
      const box = await page.getByRole('button', { name: 'What the Setup line is' }).boundingBox();
      assert(box && box.width >= 20 && box.height >= 20, `${at}: the ? is a visible target`);
    }
  }
  await page.emulateMedia({ colorScheme: 'light' });
  await page.setViewportSize({ width: 1440, height: 1050 });

  // The ? by tap, on a touch phone.
  const phone = await browser.newContext({
    viewport: { width: 390, height: 844 },
    hasTouch: true,
    isMobile: true,
  });
  try {
    const tapPage = await phone.newPage();
    await phone.addInitScript((b) => localStorage.setItem('foundry.business', b), startBid);
    await signIn(tapPage, origin, keyFile);
    await tapPage.locator('#list .start-head').waitFor();
    await tapPage.locator('#back-src').tap();
    const tapHelp = tapPage.getByRole('button', { name: 'What the Finance line is', exact: true });
    await tapHelp.tap();
    const tapped = await helpState(tapPage, tapHelp);
    assert.deepEqual([tapped.expanded, tapped.shown], ['true', true], 'a tap opens');
    assert(tapped.text.includes('How much cash the business needs and when'), tapped.text);
    await tapHelp.tap();
    assert.equal((await helpState(tapPage, tapHelp)).shown, false, 'a second tap closes');
  } finally {
    await phone.close();
  }

  // Access & assistants tells each kind of user what is true for them.
  await src('access');
  await page.locator('#list .row[data-key="ai:overview"]').click();
  const advice = async () =>
    page
      .locator('#insp .i-sec')
      .filter({ has: page.locator('h3', { hasText: 'Optional assistant review' }) })
      .locator('p.prose')
      .innerText();
  const served = await advice();
  assert(served.includes('in the terminal you start Business Foundry from'), served);
  assert(!served.includes('on the server'), served);
  await page.route('**/api/me', async (route) => {
    const response = await route.fetch();
    const body = await response.json();
    await route.fulfill({
      response,
      json: { ...body, desktop: { database: '/private/path/foundry.db' } },
    });
  });
  // the page asks again who is signed in and loads the same business, on the same source
  await page.evaluate(() => start());
  await quiet(page);
  await src('access');
  await page.locator('#list .row[data-key="ai:overview"]').click();
  const desktop = await advice();
  assert(
    desktop.includes(
      'A desktop app opened from Finder or the Start menu cannot be given a provider key',
    ),
    desktop,
  );
  assert(desktop.includes('foundry start'), desktop);
  await page.unroute('**/api/me');
  // the page asks again who is signed in and loads the same business, on the same source
  await page.evaluate(() => start());
  await quiet(page);
  assert.equal(await page.locator('#business-select').inputValue(), startBid);
  assert.equal(startName, await page.locator('#business-select option:checked').innerText());
}

async function main() {
  const temp = mkdtempSync(join(tmpdir(), 'foundry-browser-')),
    db = join(temp, 'foundry.db'),
    emptyDir = join(temp, 'empty'),
    emptyDb = join(emptyDir, 'foundry.db');
  let server, emptyServer, browser;
  const logs = [],
    errors = [];
  try {
    const python = spawnSync('uv', ['run', 'python', '-c', 'import sys; print(sys.executable)'], {
      encoding: 'utf8',
    }).stdout.trim();
    const seed = spawnSync('uv', ['run', 'foundry', '--db', db, 'demo'], { encoding: 'utf8' });
    assert.equal(seed.status, 0, seed.stderr);
    const started = await startServer(python, db, logs);
    server = started.server;
    const origin = started.origin;
    // Headless, always. Set FOUNDRY_BROWSER_SLOWDOWN to a factor (for example 3 or 6) to run
    // every page this test opens with its processor slowed by that factor, as on a slow CI
    // runner, so that a check relying on the page being quick fails here too. Unset, or 1,
    // means no slow-down: `npm run test:browser` and CI run at full speed.
    browser = await chromium.launch({ headless: true });
    const context = await browser.newContext({ viewport: { width: 1440, height: 1050 } }),
      page = await newPage(context);
    page.on('pageerror', (e) => errors.push(e.message));
    page.on('console', (m) => {
      if (m.type() === 'error' && !m.text().includes('401')) errors.push(m.text());
    });
    let f; // what has focus, read after each step that re-renders the focused control
    await signIn(page, origin, join(temp, 'owner.key'));
    await page.locator('#list .today-head').waitFor();
    // the checks of what Today does not hold wait until the page has finished loading it
    await quiet(page);
    assert.equal(await page.locator('#list-title').innerText(), 'Today');
    // The demo's guided path is done, so it opens on Today, which names the one next action that
    // Start here gives: the first step ready to start. Its only ready step has no prerequisite, so
    // nothing claims its prerequisites are complete.
    assert.equal(
      await page.locator('#src-nav .src[data-src="start"] .n').innerText(),
      '6 of 6',
      'the demo has finished its path',
    );
    const todayNext = page.locator('#list .row[data-key^="next:"]');
    assert.equal(await todayNext.count(), 1, 'Today has one next action');
    assert.equal(
      await todayNext.locator('.r-t').innerText(),
      'Start work on Approve the acquisition thesis',
    );
    assert.equal(await readyNote(page), null, 'no note on steps that have no prerequisites');
    // Today mixes steps from several lines under shared headings, so each step row names its
    // line in text, matching the source list; the spine's colour is never the only cue.
    const lineNames = await page.evaluate(() =>
      Object.fromEntries(
        [...document.querySelectorAll('#src-nav .src')]
          .filter((s) => s.querySelector('.bul'))
          .map((s) => [
            [...s.querySelector('.bul').classList].find((c) => c.startsWith('lc-')).slice(3),
            s.querySelector('.lbl').textContent,
          ]),
      ),
    );
    const todayRows = await page.locator('#list .row.ln').all();
    assert(todayRows.length > 0, 'Today shows step rows');
    for (const r of todayRows) {
      const name = lineNames[await r.getAttribute('data-line')];
      assert(name, 'each Today row belongs to a line in the source list');
      const sub = await r.locator('.r-sub').innerText();
      assert(sub.startsWith(`${name} line`), `Today row names its line: "${sub}"`);
    }
    // Each row is a native button inside a list item: announced as a button, in a list.
    const rowCount = await page.locator('#list .row').count();
    assert.equal(await page.locator('#list .row[role]').count(), 0, 'rows keep the button role');
    assert.equal(await page.locator('#list').getByRole('listitem').count(), rowCount);
    assert.equal(
      await page.locator('#list').getByRole('listitem').getByRole('button').count(),
      rowCount,
      'every list item holds its row as a button',
    );
    // Each group of rows is a list of its own, named by its heading; the headings, the Today
    // header and the notes sit between the lists, never inside one.
    assert.deepEqual(await listFaults(page), [], 'Today: lists hold only list items');
    assert(
      (await page.locator('#list').getByRole('list').count()) > 1,
      'Today has one list per group',
    );
    // Each counted heading names the list beneath it, which holds that many rows.
    const counted = await page
      .locator('#list .grp')
      .evaluateAll((hs) =>
        hs
          .filter((x) => /^\d+$/.test(x.querySelector('.grp-n')?.textContent || ''))
          .map((x) => [
            x.firstElementChild.textContent,
            Number(x.querySelector('.grp-n').textContent),
          ]),
      );
    assert(counted.length >= 1, 'Today has a counted group');
    for (const [heading, n] of counted)
      assert.equal(
        await page
          .locator('#list')
          .getByRole('list', { name: new RegExp('^' + heading) })
          .getByRole('listitem')
          .count(),
        n,
        `the list named ${heading} holds its rows`,
      );
    mkdirSync('test-results', { recursive: true });
    await page.screenshot({ path: 'test-results/overview-desktop.png', fullPage: true });
    if (process.env.FOUNDRY_UPDATE_DOCS_IMAGE) {
      // The README image: the synthetic demo workspace exactly as `foundry demo` seeds it.
      await page.setViewportSize({ width: 1440, height: 900 });
      await page.locator('#list .row').first().click();
      await page.locator('#insp [data-map="frag"]').waitFor();
      await page.mouse.move(0, 0);
      await page.screenshot({ path: 'docs/images/workspace.png' });
      await page.setViewportSize({ width: 1440, height: 1050 });
    }
    // The same holds on every source of a full workspace, at every width.
    await walkLists(page, 'demo');

    const source = async (id) => {
      await page.locator(`#src-nav .src[data-src="${id}"]`).click();
      await page.locator(`#src-nav .src[data-src="${id}"][aria-current="true"]`).waitFor();
    };
    const lineSource = async (name) => {
      await page.locator('#src-nav .src').filter({ hasText: name }).first().click();
    };
    const saved = async () => {
      await page.locator('#dialog').waitFor({ state: 'hidden' });
      await page.getByRole('status').filter({ hasText: 'Saved to the workspace.' }).waitFor();
    };
    const submit = async (name) => {
      await page.locator('#dialog').getByRole('button', { name, exact: true }).click();
      await saved();
    };
    const rowFor = (text) => page.locator('#list .row').filter({ hasText: text }).first();

    // Every source fills the middle list and the inspector.
    for (const id of [
      'journey',
      'today',
      'evidence',
      'records',
      'money',
      'deals',
      'documents',
      'sales',
      'history',
      'access',
    ]) {
      await source(id);
      await page.locator('#insp #insp-title').waitFor();
      assert((await page.locator('#insp').innerText()).length > 100, `${id}: inspector content`);
      assert((await page.locator('#list').innerText()).length > 20, `${id}: list content`);
      assert.equal(
        await page.locator('#q').getAttribute('placeholder'),
        'Search ' + (await page.locator('#list-title').innerText()),
      );
    }
    // Activating a source from the keyboard keeps focus on that source in the re-rendered list,
    // so the next Tab continues from there rather than from the top of the page.
    await page.locator('#src-nav .src[data-src="evidence"]').focus();
    await page.keyboard.press('Enter');
    await page.locator('#src-nav .src[data-src="evidence"][aria-current="true"]').waitFor();
    f = await focused(page);
    assert.deepEqual([f.src, f.current, f.visible], ['evidence', 'true', true], 'source focus');
    await page.keyboard.press('Tab');
    assert.equal((await focused(page)).src, 'records', 'Tab moves on to the next source');
    await page.keyboard.press('Space');
    await page.locator('#src-nav .src[data-src="records"][aria-current="true"]').waitFor();
    assert.equal((await focused(page)).src, 'records');
    // Arrow keys move between the rows, which stay buttons inside list items.
    const firstRow = page.locator('#list .row').first();
    await firstRow.focus();
    await page.keyboard.press('ArrowDown');
    f = await focused(page);
    assert.equal(f.key, await page.locator('#list .row').nth(1).getAttribute('data-key'));
    assert.equal(f.current, 'true', 'the row reached by arrow key is selected');
    await page.keyboard.press('ArrowUp');
    assert.equal((await focused(page)).key, await firstRow.getAttribute('data-key'));
    // Each catalog line is a source with its own coloured bullet, and the list follows it.
    const lineCount = await page.locator('#src-nav .src .bul').count();
    assert(lineCount >= 7, 'demo lines');
    await lineSource('Setup');
    assert.match(await page.locator('#list-title').innerText(), /^Setup$/);
    assert.equal(
      await page.locator('#list .grp.ln').count(),
      1,
      'a line source shows a single line',
    );
    assert(
      (await page.locator('#list .row.ln').count()) >= 4,
      'the line runs through the step rows',
    );
    // Search narrows the current list.
    await page.locator('#q').fill('registrations');
    assert.equal(await page.locator('#list .row:not([hidden])').count(), 1);
    await page.locator('#q').fill('zzzz-nothing');
    await page.getByText('Nothing in Setup matches', { exact: false }).waitFor();
    await page.locator('#q').press('Escape');
    assert((await page.locator('#list .row:not([hidden])').count()) >= 4);
    // Selecting a step shows its stages, place on its line and planning source.
    await rowFor('Decide the entity and ownership structure').click();
    await page
      .locator('#insp-title', { hasText: 'Decide the entity and ownership structure' })
      .waitFor();
    assert.equal(await page.locator('#insp .lad').count(), 4);
    await page.locator('#insp [data-map="frag"]').waitFor();
    assert(
      (await page.locator('#insp').innerText()).includes('Planning guidance, not a compliance'),
    );
    // Money comes from the server and is labelled as the application's calculation.
    await source('money');
    assert(
      (await page.locator('#list').innerText()).includes(
        'Calculated by Business Foundry with exact decimal arithmetic. No assistant was involved.',
      ),
    );
    const demoBid = await page.locator('#business-select').inputValue();
    const demo = await (await context.request.get(origin + '/api/businesses/' + demoBid)).json();
    assert(
      (await page.locator('#list').innerText()).includes(
        new Intl.NumberFormat('en-US', { style: 'currency', currency: 'USD' }).format(
          demo.stats.cash_collected,
        ),
      ),
      'recorded cash is the server figure',
    );

    // The Journey map: tall while the wide map would not fit, wide when it does.
    await source('journey');
    assert.equal(await page.locator('#insp .mapbox').getAttribute('data-fit'), 'tall');
    // Every line paints with its own token, as the browser computes the cascade, in both themes.
    const demoKeys = [...new Set(Object.keys(lineNames))];
    for (const scheme of ['light', 'dark']) {
      await page.emulateMedia({ colorScheme: scheme });
      await quiet(page);
      const painted = await lineColours(page);
      for (const what of [
        'map line',
        'map casing',
        'map bullet disc',
        'map bullet code',
        'bullet disc',
        'bullet code',
        'row line',
        'group line',
      ])
        for (const k of demoKeys)
          assert(
            painted.some((x) => x.what === what && x.k === k),
            `${scheme}: no ${what} measured for line ${k}`,
          );
      const wrong = painted.filter((x) => x.got !== x.want);
      assert.deepEqual(wrong, [], `${scheme}: line paint differs from its token`);
    }
    await page.emulateMedia({ colorScheme: 'light' });
    await page.setViewportSize({ width: 1920, height: 1100 });
    await page.locator('#insp .mapbox[data-fit="wide"]').waitFor();
    await quiet(page);
    const wide = page.locator('#insp .mp-wide');
    assert(await wide.isVisible());
    assert.equal(await page.locator('#insp .mp-tall').isVisible(), false);
    assert.equal(
      await wide.locator('.stn').count(),
      demo.objects.filter((o) => o.kind === 'task').length,
      'one station per step',
    );
    // Map segments are horizontal, vertical or 45 degrees only.
    const angles = await wide.locator('path.ln').evaluateAll((paths) =>
      paths.flatMap((p) => {
        // Straight segments (L) are measured from the previous end point; arcs are the corners.
        const out = [];
        let prev = null;
        for (const cmd of p.getAttribute('d').match(/[MLA][^MLA]+/g) || []) {
          const n = cmd
            .slice(1)
            .trim()
            .split(/[\s,]+/)
            .map(Number);
          const end = n.slice(-2);
          if (cmd[0] === 'L' && prev)
            out.push(Math.round((Math.atan2(end[1] - prev[1], end[0] - prev[0]) * 180) / Math.PI));
          prev = end;
        }
        return out;
      }),
    );
    for (const a of angles)
      assert(
        [0, 45, -45, 90, -90, 180].some((ok) => Math.abs(a - ok) <= 1),
        `segment angle ${a}`,
      );
    // Station labels never overlap one another.
    const boxes = await wide.locator('.stn').evaluateAll((stations) =>
      stations.map((s) => {
        const texts = [...s.querySelectorAll('text')].map((t) => t.getBoundingClientRect());
        return {
          l: Math.min(...texts.map((r) => r.left)),
          r: Math.max(...texts.map((r) => r.right)),
          t: Math.min(...texts.map((r) => r.top)),
          b: Math.max(...texts.map((r) => r.bottom)),
        };
      }),
    );
    for (let i = 0; i < boxes.length; i++)
      for (let j = i + 1; j < boxes.length; j++) {
        const a = boxes[i],
          b = boxes[j];
        const hit = a.l < b.r - 0.5 && b.l < a.r - 0.5 && a.t < b.b - 0.5 && b.t < a.b - 0.5;
        assert(!hit, `station labels ${i} and ${j} overlap`);
      }
    const firstStation = wide.locator('.stn').first();
    const firstLabel = await firstStation.getAttribute('aria-label');
    await firstStation.click();
    await page.locator('#insp .selbar b').waitFor();
    assert((await page.locator('#insp .selbar').innerText()).includes(firstLabel.split(',')[0]));
    assert.equal(
      await page.locator('#list .row[aria-current="true"]').getAttribute('data-key'),
      'jr:' + (await firstStation.getAttribute('data-id')),
    );
    // A station is reachable and operable from the keyboard.
    const second = wide.locator('.stn').nth(1);
    await second.focus();
    await page.keyboard.press('Enter');
    await page.locator('#insp .stn[aria-current="true"]').first().waitFor();
    assert.equal(
      await page.locator('#insp .mp-wide .stn[aria-current="true"]').getAttribute('data-id'),
      await second.getAttribute('data-id'),
    );
    await page.screenshot({ path: 'test-results/journey-wide.png' });
    await page.locator('#insp .selbar').getByRole('button').click();
    await page.locator('#insp [data-map="frag"]').waitFor();
    await page.setViewportSize({ width: 1440, height: 1050 });
    // The Today strip opens the Journey.
    await source('today');
    await page.locator('#mini').click();
    await page.locator('#src-nav .src[data-src="journey"][aria-current="true"]').waitFor();
    await page.setViewportSize({ width: 728, height: 1000 });
    await page.locator('#insp .mapbox[data-fit="tall"]').waitFor();
    assert(await page.locator('#insp .mp-tall').isVisible());
    await quiet(page);
    assert.equal(await overflow(page), false, '728: horizontal overflow');
    await page.screenshot({ path: 'test-results/journey-medium.png' });
    await page.setViewportSize({ width: 1440, height: 1050 });

    // Operate a separate workspace through the same forms a founder uses.
    await page.getByRole('button', { name: '+ New business', exact: true }).click();
    await page.getByLabel('Business name', { exact: true }).fill('Browser workflow test');
    await page
      .getByLabel('Concept', { exact: false })
      .fill('Synthetic browser test: deliver an accepted research report for a business customer.');
    await page.getByLabel('Intended customer', { exact: true }).fill('Fictional planning customer');
    await page
      .getByText('This workspace contains synthetic demonstration data.', { exact: true })
      .click();
    await submit('Create workspace');
    // A new business opens on Start here, at the stop to do next.
    await page.locator('#src-nav .src[data-src="start"][aria-current="true"]').waitFor();
    assert.equal(await page.locator('#list-title').innerText(), 'Start here');
    assert.equal(
      await page.locator('#list .row[aria-current="true"]').getAttribute('data-key'),
      'stop:2',
      'Start here opens on the first stop not done',
    );
    // A new workspace shows every source's empty state; its lists follow the same rule.
    await walkLists(page, 'new workspace');
    await startHere(page, context, browser, origin, join(temp, 'owner.key'));
    await source('records');
    await page.getByRole('button', { name: '+ Add record', exact: true }).click();
    await page.getByLabel('Title', { exact: true }).fill('Browser customer');
    await page
      .getByLabel('Details', { exact: true })
      .fill('Synthetic buyer with a documented need.');
    await submit('Save record');
    await source('evidence');
    await page.getByRole('button', { name: '+ Add evidence', exact: true }).click();
    await page.getByLabel('Evidence title').fill('Browser synthetic receipt');
    await page.getByLabel('Source type').selectOption('external_receipt');
    await page
      .getByLabel('Original source text', { exact: false })
      .fill(
        'SYNTHETIC: agreement, acceptance, issue and payment test receipt. <img src=x onerror="window.injected=true">',
      );
    await submit('Save unverified evidence');
    await rowFor('Browser synthetic receipt').click();
    await page.locator('#insp pre.original').waitFor();
    await quiet(page);
    assert.equal(await page.locator('#insp img').count(), 0);
    assert((await page.locator('#insp pre.original').innerText()).includes('<img src=x'));
    await page.getByRole('button', { name: 'Record human review' }).click();
    await page
      .getByLabel('Review note', { exact: false })
      .fill('Checked this synthetic receipt for the browser workflow test.');
    await submit('Mark evidence reviewed');
    // After the save re-renders the inspector, focus is back on the control that opened the form.
    f = await focused(page);
    assert.deepEqual(
      [f.action, f.dataId, f.visible],
      [
        'review-evidence',
        await page.locator('#insp [data-action="review-evidence"]').getAttribute('data-id'),
        true,
      ],
      'focus after a save',
    );
    // The evidence versions come from the recorded history.
    await page.locator('#insp [data-versions] .att-row').nth(1).waitFor();
    assert((await page.locator('#insp [data-versions]').innerText()).includes('Version 2'));
    const bid = await page.locator('#business-select').inputValue();
    const snapshot = await (await context.request.get(origin + '/api/businesses/' + bid)).json();
    const eid = snapshot.objects.find((o) => o.kind === 'evidence').id;
    const customer = snapshot.objects.find((o) => o.kind === 'customer').id;
    // Human task transition: prepared -> approved -> verified, with source evidence.
    await lineSource('Market');
    // Closing a decision form returns focus to the move that opened it.
    await rowFor('Establish customer demand').click();
    await page.locator('#insp button[data-action="tx"].primary').focus();
    await page.keyboard.press('Enter');
    await page.locator('#insp #tx-form').waitFor();
    await page.locator('#insp #tx-form').getByRole('button', { name: 'Not yet' }).click();
    f = await focused(page);
    // A step nobody has started offers starting it as its main move.
    assert.deepEqual(
      [f.action, f.to, f.visible],
      ['tx', 'in_progress', true],
      'focus after Not yet',
    );
    for (const next of ['prepared', 'approved', 'verified']) {
      await rowFor('Establish customer demand').click();
      await page.locator('#insp button[data-action="tx"].primary').click();
      await page.getByLabel('Next state').selectOption(next);
      await page.getByLabel('Evidence for this decision', { exact: false }).selectOption([eid]);
      await page
        .getByLabel('Decision note', { exact: false })
        .fill('Checked synthetic buyer evidence and recorded this decision.');
      await page.locator('#insp').getByRole('button', { name: 'Record decision' }).click();
      await page.getByRole('status').filter({ hasText: 'Saved to the workspace.' }).waitFor();
      await page.locator('#insp #tx-form').waitFor({ state: 'detached' });
      // The form is gone; focus lands on the step's heading, not on the page body.
      f = await focused(page);
      assert.deepEqual([f.id, f.visible], ['insp-title', true], 'focus after a decision');
    }
    assert.equal(
      await page.locator('#insp .i-state .st').innerText(),
      'Verified',
      'the inspector shows the recorded state',
    );
    await source('money');
    await page.getByRole('button', { name: '+ Build model', exact: true }).click();
    await page.getByLabel('Version name').fill('Browser forecast');
    await page.getByLabel('Forecast months', { exact: true }).fill('12');
    await submit('Calculate and save');
    await page.getByText('All three statements balance.', { exact: false }).waitFor();
    await page.getByRole('button', { name: '+ New version', exact: true }).click();
    await page.getByLabel('Version name').fill('Browser retention forecast');
    await page.getByLabel('Monthly unit retention', { exact: true }).fill('0.5');
    await page.getByLabel('Monthly fixed-cost step', { exact: true }).fill('500');
    await page.getByLabel('Fixed-cost step start month', { exact: true }).fill('2');
    await submit('Calculate and save');
    await page.getByText('2 saved versions', { exact: false }).waitFor();
    await page.getByText('All three statements balance.', { exact: false }).waitFor();
    // Choosing a case re-renders the inspector; focus stays on the chosen case's button.
    await page.locator('#insp [data-action="scenario"][data-id="downside"]').focus();
    await page.keyboard.press('Enter');
    await page.getByText('Summary · Downside case', { exact: false }).waitFor();
    f = await focused(page);
    assert.deepEqual([f.action, f.dataId, f.pressed], ['scenario', 'downside', 'true']);
    // The chart says it is approximate; the exact figures are the tables'.
    assert.match(
      await page.locator('#insp svg.chart').getAttribute('aria-label'),
      /drawn approximately; the exact monthly figures are in the cash flow table/,
    );
    await source('deals');
    await page.getByRole('button', { name: '+ Analyze a business', exact: true }).click();
    await page.getByLabel('Target business', { exact: true }).fill('Browser synthetic target');
    await page
      .getByLabel('Investment thesis', { exact: true })
      .fill('Acquire repeatable delivery capacity subject to independent diligence.');
    // The three figures that describe the target and its price start empty, with the model's
    // example only as placeholder text, and the browser refuses the form until they are filled.
    // Every other figure starts empty too.
    const dealField = (name) => page.locator(`#dialog [name="${name}"]`);
    for (const [name, example] of [
      ['annual_revenue', '1000000'],
      ['reported_ebitda', '200000'],
      ['purchase_price', '800000'],
      ['fees', '30000'],
      ['debt_years', '7'],
    ]) {
      assert.equal(await dealField(name).inputValue(), '', name + ' starts empty');
      assert.equal(await dealField(name).getAttribute('placeholder'), 'Example: ' + example);
    }
    // Financing is the buyer's choice: an empty loan is no loan, with no example amount shown.
    const dealHint = (name) =>
      page.locator(`#dialog label:has([name="${name}"]) .hint`).innerText();
    for (const name of ['senior_debt', 'seller_note']) {
      assert.equal(await dealField(name).inputValue(), '', name + ' starts empty');
      assert.equal(await dealField(name).getAttribute('placeholder'), 'Empty: no loan');
      assert.equal(await dealHint(name), 'Leave empty for no loan.');
    }
    assert.equal(
      await dealHint('debt_years'),
      'Used only when its loan is entered. Leave empty to use the example.',
    );
    const dealPosts = [];
    const onDealPost = (r) => {
      if (r.method() === 'POST' && new URL(r.url()).pathname.endsWith('/deals'))
        dealPosts.push(r.postDataJSON());
    };
    page.on('request', onDealPost);
    await dealField('annual_revenue').fill('900000');
    await dealField('reported_ebitda').fill('180000');
    await page
      .locator('#dialog')
      .getByRole('button', { name: 'Calculate and create diligence work', exact: true })
      .click();
    assert.equal(await dealField('purchase_price').evaluate((e) => e.matches(':invalid')), true);
    assert(
      await page.locator('#dialog').isVisible(),
      'the form stays open without a purchase price',
    );
    assert.equal(dealPosts.length, 0, 'nothing is sent while a required figure is empty');
    await dealField('purchase_price').fill('700000');
    await dealField('debt_years').fill('6');
    // A refusal caused by an example value names it, in the same words as the API.
    const highAssumed =
      'body: Value error, High multiple must not be below low multiple. The high multiple was not given, so its example value 5 was used; give a high multiple.';
    await dealField('multiple_low').fill('6');
    const errorsBefore = errors.length;
    const refused = page.waitForResponse(
      (r) => r.request().method() === 'POST' && new URL(r.url()).pathname.endsWith('/deals'),
    );
    await page
      .locator('#dialog')
      .getByRole('button', { name: 'Calculate and create diligence work', exact: true })
      .click();
    const refusal = await refused;
    assert.equal(refusal.status(), 422);
    assert.equal((await refusal.json()).detail, highAssumed);
    const formError = page.locator('#dialog p.error');
    await formError.filter({ hasText: 'high multiple was not given' }).waitFor();
    assert.equal(await formError.innerText(), highAssumed);
    // The browser logs the refused save's 422 as a console error; that line, and no other, is expected.
    const logged = errors.splice(errorsBefore);
    assert.deepEqual(
      logged.filter((t) => !t.includes('status of 422')),
      [],
    );
    await dealField('multiple_low').fill('');
    dealPosts.length = 0;
    // What was sent is checked before the save is awaited, so a wrong body fails here by name.
    const posted = page.waitForRequest(
      (r) => r.method() === 'POST' && new URL(r.url()).pathname.endsWith('/deals'),
    );
    await page
      .locator('#dialog')
      .getByRole('button', { name: 'Calculate and create diligence work', exact: true })
      .click();
    await posted;
    page.off('request', onDealPost);
    assert.equal(dealPosts.length, 1);
    const sentDeal = dealPosts[0];
    assert.deepEqual(
      [sentDeal.annual_revenue, sentDeal.reported_ebitda, sentDeal.purchase_price],
      ['900000', '180000', '700000'],
    );
    assert.equal(sentDeal.debt_years, 6, 'a filled term is sent as a number');
    for (const name of ['fees', 'seller_years', 'tax_rate'])
      assert(!(name in sentDeal), name + ' was left empty and is not sent');
    await saved();
    const browserDeal = (
      await (await context.request.get(origin + '/api/businesses/' + bid)).json()
    ).objects.find((o) => o.kind === 'deal' && o.title === 'Browser synthetic target');
    assert(browserDeal.data.assumed_inputs.includes('fees'));
    assert(!browserDeal.data.assumed_inputs.includes('debt_years'));
    const assumedLine = await page.locator('#insp .assumed-inputs').innerText();
    assert(
      assumedLine.startsWith("Not given, so the model's example value was used: "),
      assumedLine,
    );
    assert.match(assumedLine, /Transaction fees \$30,000\.00/);
    assert.match(
      assumedLine,
      /New senior debt \$0\.00; Property loan \$0\.00; Seller financing \$0\.00; Investor equity \$0\.00; Annual rent \$0\.00/,
    );
    assert(!assumedLine.includes('Senior term'), 'the term that was given is not listed');
    for (const unused of [
      'Senior annual interest rate',
      'Seller annual interest rate',
      'Seller term',
    ])
      assert(!assumedLine.includes(unused), unused + ' has no loan, so it is not used or listed');
    // No loan was given, so the page says the deal is screened without debt.
    assert.equal(
      await page.locator('#insp .no-debt').innerText(),
      'No debt was given and none was assumed, so the deal is screened as bought without debt. There is no debt service: cash after debt service equals cash available, and there is no coverage ratio.',
    );
    assert.match(
      await page.locator('#insp .figs').innerText(),
      /Debt service due in year 1\s+None/,
    );
    // Opened again from an analysis, the form fills exactly what that analysis gave. Every figure of
    // the form is read, so a field filled or left empty by mistake fails by name.
    const dealNames = [
      'annual_revenue',
      'reported_ebitda',
      'replacement_management',
      'multiple_low',
      'multiple_high',
      'purchase_price',
      'real_estate_price',
      'fees',
      'working_capital',
      'integration_cost',
      'senior_debt',
      'debt_rate',
      'debt_years',
      'property_debt',
      'property_rate',
      'property_years',
      'seller_note',
      'seller_rate',
      'seller_years',
      'investor_equity',
      'annual_rent',
      'annual_capex',
      'annual_working_capital_growth',
      'tax_rate',
      'cash_acquired',
      'debt_assumed',
    ];
    // The figures acquisition 1.1.0 added; an analysis saved before then has none of them.
    const addedDealNames = [
      'real_estate_price',
      'property_debt',
      'property_rate',
      'property_years',
      'investor_equity',
      'annual_rent',
    ];
    // What the form should start with: the given figures filled, every other one empty. The seller
    // note schedule is a choice, not a number, and is read with them.
    const startsWith = (given) => ({
      ...Object.fromEntries(dealNames.map((n) => [n, ''])),
      seller_schedule: '',
      ...given,
    });
    const reopenDeal = async (id, title) => {
      await page.locator(`#list .row[data-key="deal:${id}"]`).click();
      await page.locator('#insp-title', { hasText: title }).waitFor();
      await page.getByRole('button', { name: 'Revise assumptions', exact: true }).click();
      await page.locator('#dialog').waitFor();
      const names = await page
        .locator('#dialog input[type="number"]')
        .evaluateAll((es) => es.map((e) => e.name).filter((n) => n !== 'adjustment_amount'));
      assert.deepEqual(names, dealNames, 'the form has every figure, in order');
      return Object.fromEntries(
        await Promise.all(
          [...dealNames, 'seller_schedule'].map(async (n) => [n, await dealField(n).inputValue()]),
        ),
      );
    };
    const cancelDeal = async () => {
      await page.getByRole('button', { name: 'Cancel', exact: true }).click();
      await page.locator('#dialog').waitFor({ state: 'hidden' });
    };
    // No loan: the senior term was given (6 years), but no loan was, so the term starts empty with
    // the rates and the seller term; only the three required figures were given.
    assert.deepEqual(
      await reopenDeal(browserDeal.id, 'Browser synthetic target'),
      startsWith({ annual_revenue: '900000', reported_ebitda: '180000', purchase_price: '700000' }),
      'a reopened analysis with no loan',
    );
    // Entering only a loan amount then sends no rate or term, and the page lists both as assumed.
    const revised = page.waitForRequest(
      (r) => r.method() === 'POST' && new URL(r.url()).pathname.endsWith('/deals'),
    );
    await dealField('senior_debt').fill('100000');
    await page
      .locator('#dialog')
      .getByRole('button', { name: 'Calculate and create diligence work', exact: true })
      .click();
    const revisedBody = (await revised).postDataJSON();
    assert.equal(revisedBody.senior_debt, '100000');
    for (const name of ['debt_rate', 'debt_years'])
      assert(!(name in revisedBody), name + ' was not given again and is not sent');
    await saved();
    const revisedDeal = (
      await (await context.request.get(origin + '/api/businesses/' + bid)).json()
    ).objects.find(
      (o) => o.kind === 'deal' && o.title === 'Browser synthetic target' && o.id !== browserDeal.id,
    );
    for (const name of ['debt_rate', 'debt_years'])
      assert(revisedDeal.data.assumed_inputs.includes(name), name + ' is recorded as assumed');
    await page.locator('#insp-title', { hasText: 'Browser synthetic target' }).waitFor();
    const revisedLine = await page.locator('#insp .assumed-inputs').innerText();
    assert.match(revisedLine, /Senior annual interest rate 9\.00%/);
    assert.match(revisedLine, /Senior term 7 years/);
    // Three more kinds of saved analysis, served alongside the real ones: one with a list and both
    // loans given (the seller note as zero), one with loans whose terms were not given, and one
    // saved before the list was recorded, holding main's example values where figures were left out.
    const zeros = Object.fromEntries(
      dealNames.filter((n) => !addedDealNames.includes(n)).map((n) => [n, '0']),
    );
    const servedDeal = (suffix, title, inputs, assumed) => {
      const o = structuredClone(browserDeal);
      o.id = browserDeal.id + suffix;
      o.title = title;
      o.data.inputs = { ...o.data.inputs, ...zeros, target: title, ...inputs };
      // Each is saved as before acquisition 1.1.0, so it holds none of the figures that version added.
      for (const n of [...addedDealNames, 'seller_schedule']) delete o.data.inputs[n];
      if (assumed) o.data.assumed_inputs = assumed;
      else delete o.data.assumed_inputs;
      return o;
    };
    const dealKinds = [
      [
        servedDeal(
          '-listed',
          'Served analysis with a list',
          {
            annual_revenue: '950000',
            reported_ebitda: '190000',
            purchase_price: '750000',
            multiple_low: '3',
            multiple_high: '5',
            fees: '30000',
            working_capital: '50000',
            senior_debt: '200000',
            debt_rate: '0.09',
            debt_years: 7,
            seller_rate: '0.07',
            seller_years: 4,
            annual_capex: '15000',
            tax_rate: '0.25',
          },
          [
            'replacement_management',
            'multiple_low',
            'multiple_high',
            'fees',
            'working_capital',
            'integration_cost',
            'annual_capex',
            'annual_working_capital_growth',
            'cash_acquired',
            'debt_assumed',
          ],
        ),
        // Given figures equal to an example are filled; the seller note was given as zero, so its
        // rate and term start empty.
        {
          annual_revenue: '950000',
          reported_ebitda: '190000',
          purchase_price: '750000',
          senior_debt: '200000',
          debt_rate: '0.09',
          debt_years: '7',
          seller_note: '0',
          tax_rate: '0.25',
        },
      ],
      [
        servedDeal(
          '-no-term',
          'Served analysis with a loan but no term',
          {
            annual_revenue: '950000',
            reported_ebitda: '190000',
            purchase_price: '750000',
            multiple_low: '3',
            multiple_high: '5',
            fees: '25000',
            working_capital: '50000',
            senior_debt: '200000',
            debt_rate: '0.1',
            debt_years: 7,
            seller_note: '50000',
            seller_rate: '0.06',
            seller_years: 5,
            annual_capex: '15000',
            tax_rate: '0.25',
          },
          [
            'replacement_management',
            'multiple_low',
            'multiple_high',
            'working_capital',
            'integration_cost',
            'debt_years',
            'seller_rate',
            'seller_years',
            'annual_capex',
            'annual_working_capital_growth',
            'tax_rate',
            'cash_acquired',
            'debt_assumed',
          ],
        ),
        {
          annual_revenue: '950000',
          reported_ebitda: '190000',
          purchase_price: '750000',
          fees: '25000',
          senior_debt: '200000',
          debt_rate: '0.1',
          seller_note: '50000',
        },
      ],
      [
        servedDeal('-before-list', 'Served analysis saved before the list', {
          annual_revenue: '1000000',
          reported_ebitda: '210000',
          purchase_price: '800000',
          multiple_low: '3',
          multiple_high: '6',
          fees: '30000',
          working_capital: '60000',
          senior_debt: '400000',
          debt_rate: '0.1',
          debt_years: 7,
          seller_note: '100000',
          seller_rate: '0.06',
          seller_years: 4,
          annual_capex: '15000',
          tax_rate: '0.3',
        }),
        // Main's examples start empty: its 400,000 senior debt with it, and so that loan's rate and
        // term. The revenue and price equal main's examples too. Only figures that differ are filled.
        {
          reported_ebitda: '210000',
          multiple_high: '6',
          working_capital: '60000',
          seller_note: '100000',
          seller_years: '4',
          tax_rate: '0.3',
        },
      ],
    ];
    const snapshotPath = (url) => url.pathname === '/api/businesses/' + bid;
    const withServed = async (route) => {
      if (route.request().method() !== 'GET') return route.fallback();
      const response = await route.fetch();
      const body = await response.json();
      body.objects.push(...dealKinds.map(([o]) => o));
      await route.fulfill({ response, json: body });
    };
    await page.route(snapshotPath, withServed);
    await page.reload();
    await source('deals');
    for (const [o, given] of dealKinds) {
      assert.deepEqual(await reopenDeal(o.id, o.title), startsWith(given), o.title);
      await cancelDeal();
    }
    await page.unroute(snapshotPath, withServed);
    // The real analysis served as saved before the list was recorded: the page says "not recorded",
    // and the reopened form fills only figures that differ from the model's example.
    const withoutList = async (route) => {
      if (route.request().method() !== 'GET') return route.fallback();
      const response = await route.fetch();
      const body = await response.json();
      for (const o of body.objects)
        if (o.kind === 'deal') {
          delete o.data.assumed_inputs;
          // Saved under acquisition 1.0.1 too: none of the figures 1.1.0 added.
          const a = o.data.analysis;
          a.model_version = '1.0.1';
          for (const k of [
            'debt_service_year',
            'amortizing_annual_debt_service',
            'seller_note_schedule',
          ])
            delete a[k];
          for (const k of [
            'real_estate_price',
            'property_debt',
            'investor_equity',
            'buyer_cash_at_closing',
            'funding_beyond_uses',
          ])
            delete a.sources_and_uses[k];
          for (const c of Object.values(a.scenarios)) delete c.amortizing_coverage_ratio;
        }
      await route.fulfill({ response, json: body });
    };
    await page.route(snapshotPath, withoutList);
    await page.reload();
    await source('deals');
    await page.locator(`#list .row[data-key="deal:${browserDeal.id}"]`).click();
    await page.locator('#insp-title', { hasText: 'Browser synthetic target' }).waitFor();
    assert.equal(
      await page.locator('#insp .assumed-inputs').innerText(),
      "Not recorded. This analysis was saved before Foundry recorded which inputs were given, so any of its figures may be the model's example values. Check each one.",
    );
    // A 1.0.1 analysis shows the figures it has, under their earlier names, and none it lacks.
    const oldFigs = await page.locator('#insp .figs').innerText();
    assert.match(oldFigs, /Buyer equity required\s+\$780,000\.00/);
    assert.match(oldFigs, /Annual debt service\s+None/);
    assert(!/Buyer cash at closing|Real estate/.test(oldFigs), oldFigs);
    assert.equal(await page.locator('#insp .buyer-cash-text, #insp .seller-text').count(), 0);
    assert.deepEqual(
      await reopenDeal(browserDeal.id, 'Browser synthetic target'),
      startsWith({ annual_revenue: '900000', reported_ebitda: '180000', purchase_price: '700000' }),
      'the analysis with no loan, served without its list',
    );
    await cancelDeal();
    await page.unroute(snapshotPath, withoutList);
    await page.reload();
    // A business sold with its building, a property loan, a deferred seller note and investors. The
    // new figures start empty, an empty one is not sent, and the form reopens with what was given.
    await source('deals');
    await page.getByRole('button', { name: '+ Analyze a business', exact: true }).click();
    await page.getByLabel('Target business', { exact: true }).fill('Browser building target');
    await page
      .getByLabel('Investment thesis', { exact: true })
      .fill('Buy the business with its building, financed by a deferred seller note.');
    for (const [name, placeholder] of [
      ['real_estate_price', 'Empty: none'],
      ['investor_equity', 'Empty: none'],
      ['annual_rent', 'Empty: none'],
      ['property_debt', 'Empty: no loan'],
      ['property_rate', 'Example: 0.08'],
      ['property_years', 'Example: 25'],
    ]) {
      assert.equal(await dealField(name).inputValue(), '', name + ' starts empty');
      assert.equal(await dealField(name).getAttribute('placeholder'), placeholder, name);
    }
    assert.equal(await dealField('seller_schedule').inputValue(), '', 'the schedule starts empty');
    for (const [name, value] of [
      ['annual_revenue', '900000'],
      ['reported_ebitda', '200000'],
      ['purchase_price', '600000'],
      ['real_estate_price', '250000'],
      ['property_debt', '200000'],
      ['seller_note', '350000'],
      ['investor_equity', '50000'],
    ])
      await dealField(name).fill(value);
    await dealField('seller_schedule').selectOption('deferred');
    const buildingPost = page.waitForRequest(
      (r) => r.method() === 'POST' && new URL(r.url()).pathname.endsWith('/deals'),
    );
    await page
      .locator('#dialog')
      .getByRole('button', { name: 'Calculate and create diligence work', exact: true })
      .click();
    const buildingBody = (await buildingPost).postDataJSON();
    assert.deepEqual(
      [
        buildingBody.real_estate_price,
        buildingBody.property_debt,
        buildingBody.seller_schedule,
        buildingBody.investor_equity,
      ],
      ['250000', '200000', 'deferred', '50000'],
    );
    for (const name of ['annual_rent', 'property_rate', 'property_years', 'seller_years'])
      assert(!(name in buildingBody), name + ' was left empty and is not sent');
    await saved();
    const buildingDeal = (
      await (await context.request.get(origin + '/api/businesses/' + bid)).json()
    ).objects.find((o) => o.kind === 'deal' && o.title === 'Browser building target');
    for (const name of ['annual_rent', 'property_rate', 'property_years', 'seller_years'])
      assert(buildingDeal.data.assumed_inputs.includes(name), name + ' is recorded as assumed');
    assert(!buildingDeal.data.assumed_inputs.includes('seller_schedule'));
    await page.locator('#insp-title', { hasText: 'Browser building target' }).waitFor();
    // 350,000 at the example 6% deferred over the example 5 years: 350,000 × 1.06^5 = 468,378.95.
    assert.equal(
      await page.locator('#insp .seller-text').innerText(),
      'The seller is paid nothing until year 5, when $468,378.95 is due: $350,000.00 lent plus $118,378.95 of interest. The year-1 coverage does not include it.',
    );
    // Uses 600,000 + 250,000 + 30,000 + 50,000 = 930,000; debt 550,000; investors 50,000.
    assert.equal(
      await page.locator('#insp .buyer-cash-text').innerText(),
      'The buyer puts in $330,000.00 of cash at closing: total uses less all debt and investor equity.',
    );
    assert.match(await page.locator('#insp').innerText(), /Coverage if the seller note amortized/);
    assert.deepEqual(
      await reopenDeal(buildingDeal.id, 'Browser building target'),
      startsWith({
        annual_revenue: '900000',
        reported_ebitda: '200000',
        purchase_price: '600000',
        real_estate_price: '250000',
        property_debt: '200000',
        seller_note: '350000',
        seller_schedule: 'deferred',
        investor_equity: '50000',
      }),
      'a reopened analysis with a building, a deferred note and investors',
    );
    await cancelDeal();
    await source('documents');
    await page.getByRole('button', { name: '+ Generate document', exact: true }).click();
    await submit('Generate draft');
    await page.getByRole('button', { name: 'Read draft' }).click();
    assert((await page.locator('#dialog pre').innerText()).includes('Browser customer'));
    await page.getByRole('button', { name: 'Close dialog', exact: true }).click();
    await source('sales');
    await page.getByRole('button', { name: '+ Draft invoice', exact: true }).click();
    await page.getByLabel('Customer', { exact: true }).selectOption(customer);
    await page.getByLabel('Description', { exact: true }).fill('Browser accepted report');
    await page.getByLabel('Amount (USD)', { exact: true }).fill('100');
    await page.getByLabel('Due date', { exact: true }).fill('2027-01-15');
    await page.getByLabel('Agreement evidence', { exact: true }).selectOption(eid);
    await page.getByLabel('Delivery acceptance evidence', { exact: true }).selectOption(eid);
    await submit('Save draft');
    await page.getByRole('button', { name: 'Record issue', exact: true }).click();
    await page.getByLabel('Issue receipt', { exact: true }).selectOption(eid);
    await page
      .getByLabel('Issue note', { exact: false })
      .fill('Checked a synthetic issue receipt; no real invoice was sent.');
    await submit('Record issue');
    await page.getByRole('button', { name: 'Record payment', exact: true }).click();
    await page.getByLabel('Unique bank or provider reference').fill('BROWSER-RECEIPT-1');
    await page.getByLabel('Reviewed payment receipt', { exact: true }).selectOption(eid);
    await submit('Record payment');
    await page.locator('#insp .i-state .st').getByText('Paid', { exact: true }).waitFor();
    const final = await (await context.request.get(origin + '/api/businesses/' + bid)).json();
    assert.equal(final.stats.cash_collected, '100.00');
    assert.equal(final.stats.outstanding_invoices, '0.00');
    const audit = await (
      await context.request.get(origin + '/api/businesses/' + bid + '/audit')
    ).json();
    assert.equal(audit.integrity.valid, true);
    await page.reload();
    await source('today');
    assert((await page.locator('#list').innerText()).includes('$100'));
    await source('history');
    await page.getByText('History and current records agree', { exact: false }).waitFor();

    // Distinct states. Record decisions through the API the interface uses, then check that
    // prepared, approved, submitted and verified each render as their own shape.
    const call = (path, method, body) =>
      page.evaluate(
        async ([p, m, b]) => {
          const r = await fetch('/api' + p, {
            method: m,
            credentials: 'same-origin',
            headers: { 'Content-Type': 'application/json', 'Idempotency-Key': crypto.randomUUID() },
            body: b ? JSON.stringify(b) : undefined,
          });
          const d = await r.json();
          if (!r.ok) throw new Error(d.detail);
          return d;
        },
        [path, method, body],
      );
    const taskByKey = async (key) =>
      (await call('/businesses/' + bid, 'GET')).objects.find(
        (o) => o.kind === 'task' && o.data.key === key && !o.data.deal_id,
      );
    const move = async (key, ...states) => {
      for (const to of states) {
        const t = await taskByKey(key);
        await call(`/businesses/${bid}/tasks/${t.id}/transition`, 'POST', {
          state: to,
          note: 'Synthetic browser test decision.',
          evidence_ids: [eid],
          expected_version: t.version,
        });
      }
    };
    await call(`/businesses/${bid}/records`, 'POST', {
      kind: 'decision',
      title: 'Browser structure decision',
      details: 'Synthetic decision for the browser test.',
      certainty: 'reported',
    });
    await move('structure', 'prepared', 'approved', 'verified');
    await move('registration', 'prepared', 'approved', 'submitted');
    await move('bank', 'prepared', 'approved');
    await move('tax', 'prepared');
    await page.reload();
    await source('journey');
    const expected = {
      'Establish customer demand': 'verified',
      'Verify applicable registrations': 'submitted',
      'Establish banking and payment access': 'approved',
      'Review and establish tax obligations': 'prepared',
    };
    const shapes = {};
    for (const [title, st] of Object.entries(expected)) {
      const glyph = rowFor(title).locator('.r-g svg');
      assert.equal(await glyph.getAttribute('data-glyph'), st, `${title} list glyph`);
      shapes[st] = await glyph.innerHTML();
      const t = (await call('/businesses/' + bid, 'GET')).objects.find(
        (o) => o.kind === 'task' && o.title === title,
      );
      assert.equal(t.state, st, `${title} server state`);
      assert.equal(
        await page
          .locator(`#insp .mp-tall .stn[data-id="${t.id}"] svg.g`)
          .getAttribute('data-glyph'),
        st,
        `${title} map glyph`,
      );
    }
    assert.equal(new Set(Object.values(shapes)).size, 4, 'four states, four distinct shapes');
    // The approved external step keeps its stages separate: two reached, two to come.
    await rowFor('Establish banking and payment access').click();
    // In the Journey a row selects the stop on the map; the selection bar opens the step itself.
    assert(await page.locator('#insp .mp-tall .stn[aria-current="true"]').isVisible());
    await page.locator('#insp .selbar').getByRole('button').click();
    await page
      .locator('#insp-title', { hasText: 'Establish banking and payment access' })
      .waitFor();
    const reached = await page
      .locator('#insp .lad')
      .evaluateAll((rows) => rows.map((r) => `${r.dataset.stage}:${r.dataset.reached}`));
    assert.deepEqual(reached, ['prepared:yes', 'approved:yes', 'submitted:no', 'verified:no']);
    assert(await page.getByRole('button', { name: 'I’ve submitted it…' }).isVisible());
    await page.screenshot({ path: 'test-results/states-desktop.png' });

    // Phone: the panes stack, list first, detail on tap, with visible back controls.
    await page.setViewportSize({ width: 390, height: 844 });
    await page.reload();
    await page.locator('#list .row').first().waitFor();
    await quiet(page);
    assert.equal(await page.locator('#app').getAttribute('data-level'), 'list');
    assert.equal(await page.locator('#insp').isVisible(), false);
    await page.screenshot({ path: 'test-results/overview-mobile.png', fullPage: true });
    const inPane = async (pane, what) => {
      const at = await focused(page);
      assert(at.visible && at.pane === pane, `${what}: focus is visible in the ${pane} pane`);
      return at;
    };
    await page.locator('#list .row').first().click();
    await quiet(page);
    assert.equal(await page.locator('#app').getAttribute('data-level'), 'detail');
    assert(await page.locator('#back-list').isVisible());
    assert.equal(await page.locator('#list').isVisible(), false);
    assert.equal((await inPane('inspector', 'row to detail')).id, 'back-list');
    await page.locator('#back-list').click();
    assert(await page.locator('#list').isVisible());
    assert.equal((await inPane('list', 'back to the list')).current, 'true', 'the row left');
    for (const id of [
      'today',
      'journey',
      'evidence',
      'money',
      'deals',
      'documents',
      'sales',
      'history',
      'access',
    ]) {
      if (await page.locator('#back-list').isVisible()) await page.locator('#back-list').click();
      await page.locator('#back-src').click();
      assert(await page.locator('#src-nav').isVisible());
      assert.equal((await inPane('sources', `${id}: back to sources`)).current, 'true');
      await page.locator(`#src-nav .src[data-src="${id}"]`).press('Enter');
      await quiet(page);
      assert.equal(await overflow(page), false, `${id}: mobile horizontal overflow (list)`);
      await inPane(id === 'journey' ? 'inspector' : 'list', `${id}: source chosen`);
      if (id !== 'journey') {
        await page.locator('#list .row').first().click();
        await inPane('inspector', `${id}: row chosen`);
      }
      await page.locator('#insp #insp-title').waitFor();
      await quiet(page);
      assert.equal(await overflow(page), false, `${id}: mobile horizontal overflow (detail)`);
    }
    await page.screenshot({ path: 'test-results/detail-mobile.png', fullPage: true });

    // Integrity: withdrawing the evidence reopens what relied on it, as the records say.
    await page.setViewportSize({ width: 1440, height: 1050 });
    await page.reload();
    await source('evidence');
    await rowFor('Browser synthetic receipt').click();
    await page.getByRole('button', { name: 'Withdraw verification' }).click();
    await page
      .getByLabel('Review note', { exact: false })
      .fill('Withdrawn in the browser test to check that dependent work reopens.');
    await submit('Confirm withdrawal');
    await page.locator('#insp .i-state .st').getByText('Withdrawn', { exact: true }).waitFor();
    await source('today');
    const reopenedRow = rowFor('Establish customer demand');
    assert.equal(await reopenedRow.locator('.r-g svg').getAttribute('data-glyph'), 'review');
    await reopenedRow.click();
    await quiet(page);
    const inspector = await page.locator('#insp').innerText();
    assert(inspector.includes('This step was reopened.'));
    assert(inspector.includes('Evidence withdrawn: Browser synthetic receipt'));
    const after = await (await context.request.get(origin + '/api/businesses/' + bid)).json();
    assert.equal(
      after.objects.find((o) => o.title === 'Establish customer demand').state,
      'needs_review',
    );
    // The earlier stages stay visible as history, not as the current standing.
    assert.equal(await page.locator('#insp .lad[data-reached="yes"]').count(), 0);
    assert((await page.locator('#insp .lad').first().innerText()).includes('Earlier'));
    await source('sales');
    assert.equal(
      await rowFor('Browser accepted report').locator('.r-g svg').getAttribute('data-glyph'),
      'review',
    );
    await page.screenshot({ path: 'test-results/reopened-desktop.png' });

    // Late responses. A second page on the same sign-in holds chosen responses back, then
    // releases them after the person has moved on, and checks nothing late reaches the screen.
    const race = await newPage(context);
    race.on('pageerror', (e) => errors.push(e.message));
    // the connections this page's tests drop on purpose are logged by the browser as failed loads
    race.on('console', (m) => {
      if (m.type() === 'error' && !/401|409|net::ERR_CONNECTION_RESET/.test(m.text()))
        errors.push(m.text());
    });
    const demoName = demo.business.data.name,
      testName = 'Browser workflow test';
    const raceSource = async (id) => {
      await race.locator(`#src-nav .src[data-src="${id}"]`).click();
      await race.locator(`#src-nav .src[data-src="${id}"][aria-current="true"]`).waitFor();
    };
    const showing = (name) => race.locator('#list .th-sub', { hasText: name }).waitFor();
    const switchTo = async (id, name) => {
      await race.locator('#business-select').selectOption(id);
      await showing(name);
    };
    const onBusiness = async (id, name) => {
      if ((await race.locator('#business-select').inputValue()) !== id) await switchTo(id, name);
    };
    const auditOf = async (id) =>
      (await context.request.get(`${origin}/api/businesses/${id}/audit`)).json();
    await race.goto(origin);
    await race.locator(OPENED).waitFor();
    await switchTo(demoBid, demoName);

    // (a) One business's history never lands in another's inspector.
    await onBusiness(demoBid, demoName);
    const heldDemoAudit = await hold(race, `/api/businesses/${demoBid}/audit`);
    await raceSource('history');
    await heldDemoAudit.requested;
    await switchTo(bid, testName);
    await raceSource('history');
    const testAudit = await auditOf(bid);
    await race
      .locator('#insp .i-state .nx', { hasText: `${testAudit.integrity.events_checked} events` })
      .waitFor();
    await heldDemoAudit.release();
    assert.notEqual(
      (await auditOf(demoBid)).integrity.events_checked,
      testAudit.integrity.events_checked,
      'the two businesses have different histories',
    );
    assert.equal(
      await race.locator('#insp .i-state .nx').innerText(),
      `${testAudit.integrity.events_checked} events checked`,
      'the inspector shows this business’s integrity check',
    );
    assert.equal(await race.locator('#business-select').inputValue(), bid);
    assert.equal(
      await race.locator('#list .grp', { hasText: 'Events' }).locator('.grp-n').innerText(),
      String(testAudit.events.length),
    );

    // (b) A history requested before a save is not kept as the history after it. A reload
    // starts without a cached history, so opening History has to ask the server.
    await race.reload();
    await race.locator(OPENED).waitFor();
    await onBusiness(bid, testName);
    const beforeSave = await auditOf(bid);
    await raceSource('records');
    const heldTestAudit = await hold(race, `/api/businesses/${bid}/audit`);
    await raceSource('history');
    await heldTestAudit.requested;
    await raceSource('records');
    await race.getByRole('button', { name: '+ Add record', exact: true }).click();
    await race.getByLabel('Title', { exact: true }).fill('Browser late-history record');
    await race
      .getByLabel('Details', { exact: true })
      .fill('Synthetic record saved while a history request was held.');
    await race.locator('#dialog').getByRole('button', { name: 'Save record', exact: true }).click();
    await race.locator('#dialog').waitFor({ state: 'hidden' });
    await race.getByRole('status').filter({ hasText: 'Saved to the workspace.' }).waitFor();
    await heldTestAudit.release();
    const afterSave = await auditOf(bid);
    assert(afterSave.events.length > beforeSave.events.length, 'the save added history');
    await raceSource('history');
    await race.locator('#list .grp', { hasText: 'Events' }).waitFor();
    await quiet(race);
    assert.equal(
      await race.locator('#list .grp', { hasText: 'Events' }).locator('.grp-n').innerText(),
      String(afterSave.events.length),
      'the history list is the one after the save',
    );
    assert(
      (await race.locator('#list').innerText()).includes('Browser late-history record'),
      'the saved record is in the history list',
    );

    // (c) Overlapping refreshes: choosing two businesses quickly shows the one chosen last.
    await raceSource('today');
    await onBusiness(bid, testName);
    const heldDemo = await hold(race, `/api/businesses/${demoBid}`);
    await race.locator('#business-select').selectOption(demoBid);
    await heldDemo.requested;
    const testLoaded = race.waitForResponse(
      (r) => new URL(r.url()).pathname === `/api/businesses/${bid}`,
    );
    await race.locator('#business-select').selectOption(bid);
    await testLoaded;
    await showing(testName);
    await heldDemo.release();
    assert.equal(await race.locator('#business-select').inputValue(), bid);
    assert.match(await race.locator('#list .th-sub').innerText(), new RegExp(`^${testName} ·`));
    assert.equal(
      await race.evaluate(() => state.snapshot.business.data.name),
      testName,
      'the records in memory are the chosen business’s',
    );
    assert.equal(
      await race.evaluate(() => state.businesses.find((b) => b.id === state.bid).data.name),
      testName,
    );

    // A command the server accepted, followed by a reload that fails. The failure is the page's,
    // not the command's: the page says the change is saved, offers to load the records again
    // and invites nothing that would send the command a second time.
    const outdated = race.locator('#outdated');
    const recordsTitled = async (title) =>
      (await (await context.request.get(`${origin}/api/businesses/${bid}`)).json()).objects.filter(
        (o) => o.title === title,
      ).length;
    const addRecord = async (title) => {
      await race.getByRole('button', { name: '+ Add record', exact: true }).click();
      await race.getByLabel('Title', { exact: true }).fill(title);
      await race
        .getByLabel('Details', { exact: true })
        .fill('Synthetic record for the browser test.');
      const answered = race.waitForResponse(
        (r) =>
          r.request().method() === 'POST' &&
          new URL(r.url()).pathname === `/api/businesses/${bid}/records`,
      );
      await race
        .locator('#dialog')
        .getByRole('button', { name: 'Save record', exact: true })
        .click();
      return (await answered).status();
    };

    // (d) A saved record whose reload fails: saved, said so, and listed once loaded.
    await onBusiness(bid, testName);
    await raceSource('records');
    const savedTitle = 'Browser record saved before a failed reload';
    let dropList = await dropNext(race, '/api/businesses');
    assert.equal(await addRecord(savedTitle), 201, 'the server saved the record');
    await dropList.dropped;
    await dropList.off();
    assert(await outdated.isVisible(), 'a saved change whose reload failed says so on the page');
    let notice = await outdated.innerText();
    assert.match(notice, /Your change was saved/);
    assert.match(notice, /no need to enter it again/);
    assert.doesNotMatch(notice, /retry/i, 'the notice does not ask for the form again');
    assert(await race.getByRole('alert').filter({ hasText: 'Your change was saved' }).isVisible());
    assert.equal(await race.locator('#dialog').evaluate((d) => d.open), false, 'the form closed');
    assert.equal(
      await race.locator('#dialog-content').evaluate((c) => c.textContent.includes('Retry')),
      false,
      'nothing is written into the closed form',
    );
    assert.equal(await race.locator('#list .row', { hasText: savedTitle }).count(), 0);
    // The notice outlasts the ordinary message, which hides after 5.5 seconds.
    await pause(6000);
    assert(await outdated.isVisible(), 'the notice stays while the page is out of date');
    // The page's own control loads the records; when that fails too, the notice says so.
    dropList = await dropNext(race, '/api/businesses');
    await outdated.getByRole('button', { name: 'Load current records' }).click();
    await dropList.dropped;
    await dropList.off();
    notice = await outdated.innerText();
    assert.match(notice, /still could not be loaded/);
    assert.match(notice, /Your change was saved/);
    await outdated.getByRole('button', { name: 'Load current records' }).click();
    await outdated.waitFor({ state: 'hidden' });
    await race.locator('#list .row', { hasText: savedTitle }).waitFor();
    f = await focused(race);
    assert.deepEqual([f.pane, f.visible], ['list', true], 'focus after loading the records');
    await quiet(race);
    assert.equal(await recordsTitled(savedTitle), 1, 'the record was saved once');

    // (e) A switch to another business is pending while a save in this one is followed by a
    // failed reload. The switch did not happen, and no later reload makes it unasked.
    await onBusiness(bid, testName);
    await raceSource('records');
    const heldSwitch = await hold(race, `/api/businesses/${demoBid}`);
    await race.locator('#business-select').selectOption(demoBid);
    await heldSwitch.requested;
    const switchTitle = 'Browser record saved during a pending switch';
    dropList = await dropNext(race, '/api/businesses');
    assert.equal(await addRecord(switchTitle), 201);
    await dropList.dropped;
    await dropList.off();
    await heldSwitch.release();
    assert.equal(
      await race.locator('#business-select').inputValue(),
      bid,
      'after a failed reload the selector names the business on screen',
    );
    assert.equal(
      await race.locator('#business-select option:checked').innerText(),
      testName,
      'the selector shows its name',
    );
    assert(await race.locator('#list .row', { hasText: 'Browser customer' }).count());
    notice = await outdated.innerText();
    assert.match(notice, /Your change was saved/);
    assert(
      notice.includes(`${demoName} was not opened; this page still shows ${testName}.`),
      'the notice says the switch did not happen',
    );
    await outdated.getByRole('button', { name: 'Load current records' }).click();
    await outdated.waitFor({ state: 'hidden' });
    await race.locator('#list .row', { hasText: switchTitle }).waitFor();
    await quiet(race);
    assert.equal(
      await race.locator('#business-select').inputValue(),
      bid,
      'the next reload stays on the business shown',
    );
    assert.equal(await recordsTitled(switchTitle), 1);
    // With nothing saved, a switch whose reload fails is reported on its own, and let go.
    dropList = await dropNext(race, '/api/businesses');
    await race.locator('#business-select').selectOption(demoBid);
    await dropList.dropped;
    await dropList.off();
    assert.equal(await race.locator('#business-select').inputValue(), bid);
    assert.equal(
      await race.getByRole('status').innerText(),
      `${demoName} could not be opened. The server could not be reached. This page still shows ${testName}.`,
    );
    assert(await outdated.isHidden(), 'no saved change is claimed');

    // (f) Step decisions. A decision that itself fails keeps its form, and sending it again
    // uses the same key. A decision the server accepted closes its form even when the reload
    // after it fails, and the step offers nothing that would send it again.
    const ready = (
      await (await context.request.get(`${origin}/api/businesses/${bid}`)).json()
    ).objects.find(
      (o) => o.kind === 'task' && o.state === 'pending' && !o.blockers?.length && !o.data.deal_id,
    );
    const txPath = `/api/businesses/${bid}/tasks/${ready.id}/transition`;
    const txKeys = [];
    race.on('request', (r) => {
      if (r.method() === 'POST' && new URL(r.url()).pathname === txPath)
        txKeys.push(r.headers()['idempotency-key']);
    });
    const txAnswered = () =>
      race.waitForResponse(
        (r) => r.request().method() === 'POST' && new URL(r.url()).pathname === txPath,
      );
    const decide = async (to, note) => {
      await race.locator(`#insp [data-action="tx"][data-to="${to}"]`).click();
      await race.getByLabel('Decision note', { exact: false }).fill(note);
      await race.locator('#insp').getByRole('button', { name: 'Record decision' }).click();
    };
    await raceSource('today');
    await race.locator(`#list .row[data-key="step:${ready.id}"]`).click();
    await race.locator('#insp-title', { hasText: ready.title }).waitFor();
    const dropTx = await dropNext(race, txPath, 'POST');
    await decide('in_progress', 'Synthetic browser test: work started.');
    await dropTx.dropped;
    await dropTx.off();
    assert.equal(
      await race.locator('#tx-form .error').innerText(),
      'Connection interrupted. Retry the unchanged form to check the same request safely.',
      'a decision that failed keeps its form and says so',
    );
    let answered = txAnswered();
    await race.locator('#insp').getByRole('button', { name: 'Record decision' }).click();
    assert.equal((await answered).status(), 200);
    await race.locator('#insp #tx-form').waitFor({ state: 'detached' });
    await quiet(race);
    assert.equal(txKeys.length, 2);
    assert.equal(txKeys[1], txKeys[0], 'sending a failed decision again uses the same key');
    txKeys.length = 0;
    await race.locator('#insp .i-state .st', { hasText: 'In progress' }).waitFor();
    dropList = await dropNext(race, '/api/businesses');
    answered = txAnswered();
    await decide('prepared', 'Synthetic browser test: prepared for approval.');
    assert.equal((await answered).status(), 200, 'the server accepted the decision');
    await dropList.dropped;
    await dropList.off();
    assert.equal(
      await race.locator('#tx-form').count(),
      0,
      'the inline form closes once the server has accepted the decision',
    );
    assert(await outdated.isVisible(), 'an accepted decision whose reload failed says so');
    assert.match(await outdated.innerText(), /Your change was saved/);
    assert.equal(
      await race.locator('#insp [data-action="tx"]').count(),
      0,
      'the step offers no move that would send the decision again',
    );
    assert.match(
      await race.locator('#insp [data-accepted]').innerText(),
      /Your decision to move this step to Prepared is recorded/,
    );
    assert.doesNotMatch(await race.locator('body').innerText(), /Cannot read|TypeError|undefined/);
    assert.equal((await focused(race)).id, 'insp-title', 'focus after an accepted decision');
    // The step's own control loads the records too, and the step shows the recorded state.
    await race
      .locator('#insp [data-accepted]')
      .getByRole('button', { name: 'Load current records' })
      .click();
    await outdated.waitFor({ state: 'hidden' });
    await race.locator('#insp .i-state .st', { hasText: 'Prepared' }).waitFor();
    await quiet(race);
    assert.equal(txKeys.length, 1, 'the accepted decision was sent once');
    f = await focused(race);
    assert.deepEqual([f.id, f.visible], ['insp-title', true], 'focus after loading the records');
    const decided = (
      await (await context.request.get(`${origin}/api/businesses/${bid}`)).json()
    ).objects.find((o) => o.id === ready.id);
    assert.equal(decided.state, 'prepared');
    assert.deepEqual(
      decided.data.history.map((e) => e.to),
      ['in_progress', 'prepared'],
      'each decision is recorded once',
    );

    // Late answers belong to the session and the business they were sent from. Each case below
    // starts from a freshly loaded, signed-in page; every case runs, and the failures are
    // reported together, so a regression shows each case it breaks. No access key is printed:
    // a key read from a held answer is only ever compared, never put into a message.
    const ownerKey = () => readFileSync(join(temp, 'owner.key'), 'utf8').trim();
    const signInAgain = async () => {
      await race.getByLabel('Access key', { exact: true }).fill(ownerKey());
      await race.getByRole('button', { name: 'Open workspace' }).click();
      await race.locator(OPENED).waitFor();
    };
    const signOut = async () => {
      await race.locator('.toolbar [data-action="logout"]').click();
      await race.getByLabel('Access key', { exact: true }).waitFor();
    };
    const settle = async () => {
      await race.unrouteAll({ behavior: 'ignoreErrors' });
      // the page is loaded again, so nothing a case left held is waited on any more
      heldOn(race).clear();
      await race.setViewportSize({ width: 1440, height: 1050 });
      await race.goto(origin);
      await race.waitForFunction(
        () =>
          !document.querySelector('#login').hidden ||
          document.querySelector('#list .today-head, #list .row'),
      );
      if (await race.locator('#login').isVisible()) await signInAgain();
    };
    const toastText = () => race.locator('#toast').innerText();
    const rowIn = (text) => race.locator('#list .row').filter({ hasText: text }).first();
    const dialogOpen = () => race.locator('#dialog').evaluate((d) => d.open);
    const dialogTitle = () => race.locator('#dialog-title').innerText();
    const inDialog = (name) =>
      race.locator('#dialog').getByRole('button', { name, exact: true }).click();
    const snapshotOf = async (id) =>
      (await context.request.get(`${origin}/api/businesses/${id}`)).json();
    const recordsIn = async (id, title) =>
      (await snapshotOf(id)).objects.filter((o) => o.title === title).length;
    const principalsNamed = async (name) =>
      (await (await context.request.get(`${origin}/api/principals`)).json()).filter(
        (p) => p.name === name,
      );
    // whether a secret is anywhere in the page, or waiting in memory to be shown
    const pageHolds = (secret) =>
      race.evaluate(
        (s) =>
          document.documentElement.innerHTML.includes(s) ||
          (typeof waiting !== 'undefined' && waiting.some((w) => w.html.includes(s))),
        secret,
      );
    const holdKey = () => hold(race, '/api/principals', { method: 'POST' });
    const openKeyForm = async (name) => {
      await raceSource('access');
      await race.getByRole('button', { name: '+ Create access key', exact: true }).click();
      await race.getByLabel('Person or agent name', { exact: true }).fill(name);
    };
    const keyFrom = async (held) => (await (await held.requested).json()).token;
    const shownKeyIs = (secret) =>
      race.locator('#dialog pre').evaluate((p, s) => p.textContent === s, secret);
    const openRecordForm = async (title) => {
      await race.getByRole('button', { name: '+ Add record', exact: true }).click();
      await race.getByLabel('Title', { exact: true }).fill(title);
      await race
        .getByLabel('Details', { exact: true })
        .fill('Synthetic record for the late-answer browser checks.');
    };
    // a switch to the demo business whose records are held back until release()
    const pendingSwitch = async () => {
      const held = await hold(race, `/api/businesses/${demoBid}`);
      await race.locator('#business-select').selectOption(demoBid);
      await held.requested;
      return held;
    };
    // steps that can still start work: a pending step may, whatever its prerequisites
    const readyTasks = async () =>
      (await snapshotOf(bid)).objects.filter(
        (o) => o.kind === 'task' && o.state === 'pending' && !o.data.deal_id && o.id !== ready.id,
      );
    const showStep = async (step) => {
      await race.evaluate((id) => go(`journey|step:${id}`), step.id);
      await race.locator('#insp-title', { hasText: step.title }).waitFor();
    };
    // what a page holds of the last person's records, in the page and in memory; after a sign-out
    // or a 401 it is blankPage
    const pageForgotten = (p) =>
      p.evaluate(() => ({
        snapshot: state.snapshot,
        businesses: state.businesses.length,
        runs: state.runs.length,
        principals: state.principals,
        audit: state.audit,
        actor: state.actor,
        steps: M.steps.length,
        html: ['#src-nav', '#src-foot', '#list', '#list-right', '#insp', '#dialog-content'].map(
          (s) => document.querySelector(s).innerHTML,
        ),
        options: document.querySelector('#business-select').options.length,
        appHidden: document.querySelector('#app').hidden,
      }));
    const blankPage = {
      snapshot: null,
      businesses: 0,
      runs: 0,
      principals: null,
      audit: null,
      actor: null,
      steps: 0,
      html: ['', '', '', '', '', ''],
      options: 0,
      appHidden: true,
    };
    const late = [];
    const lateCase = (name, run) => late.push([name, run]);

    lateCase('finding 1: a key that arrives after sign-out is discarded', async () => {
      await onBusiness(bid, testName);
      const held = await holdKey();
      await openKeyForm('Browser key answered after sign-out');
      await inDialog('Create key');
      const secret = await keyFrom(held);
      await race.keyboard.press('Escape');
      await signOut();
      await held.release();
      assert(await race.locator('#login').isVisible(), 'the sign-in page is showing');
      assert.equal(await dialogOpen(), false, 'no dialog opens over the sign-in page');
      assert(!(await pageHolds(secret)), 'the key is nowhere in the page or waiting in memory');
    });
    lateCase('a key that arrives after sign-out and a new sign-in is discarded', async () => {
      await onBusiness(bid, testName);
      const held = await holdKey();
      await openKeyForm('Browser key answered after a new sign-in');
      await inDialog('Create key');
      const secret = await keyFrom(held);
      await race.keyboard.press('Escape');
      await signOut();
      await signInAgain();
      await held.release();
      assert.equal(await dialogOpen(), false, 'no key dialog in the new session');
      assert(!(await pageHolds(secret)), 'the key is nowhere in the new session');
    });
    lateCase('a key waiting behind a form is discarded when a 401 ends the session', async () => {
      await onBusiness(bid, testName);
      const held = await holdKey();
      await openKeyForm('Browser key waiting when the session ends');
      await inDialog('Create key');
      const secret = await keyFrom(held);
      await race.keyboard.press('Escape');
      await race.locator('.toolbar [data-action="new-business"]').click();
      await race.getByLabel('Business name', { exact: true }).fill('Browser business never made');
      await held.release();
      assert.equal(await dialogTitle(), 'Create a business', 'the key does not replace the form');
      await context.clearCookies();
      await race
        .getByLabel('Concept', { exact: true })
        .fill('Synthetic concept refused because the session has ended.');
      await inDialog('Create workspace');
      await race.getByLabel('Access key', { exact: true }).waitFor();
      await quiet(race);
      assert.equal(await dialogOpen(), false, 'nothing opens over the sign-in page');
      assert(!(await pageHolds(secret)), 'the waiting key left with the session');
      await signInAgain();
      await quiet(race);
      assert.equal(await dialogOpen(), false, 'the waiting key is not shown in the next session');
      assert(!(await pageHolds(secret)), 'nor kept for it');
    });
    lateCase(
      'a key waits for an open form, is shown once, and a change of business leaves it open',
      async () => {
        await onBusiness(bid, testName);
        const switching = await pendingSwitch();
        const held = await holdKey();
        await openKeyForm('Browser key shown after another form');
        await inDialog('Create key');
        const secret = await keyFrom(held);
        await race.keyboard.press('Escape');
        await race.locator('.toolbar [data-action="new-business"]').click();
        await race.getByLabel('Business name', { exact: true }).fill('Browser business kept open');
        await held.release();
        assert.equal(await dialogTitle(), 'Create a business', 'the open form is not replaced');
        assert.equal(
          await race.getByLabel('Business name', { exact: true }).inputValue(),
          'Browser business kept open',
          'nor what was typed in it',
        );
        await inDialog('Cancel');
        await race.locator('#dialog-title', { hasText: 'Save this access key' }).waitFor();
        assert(await shownKeyIs(secret), 'the key shown is the one the server made');
        await switching.release();
        await showing(demoName);
        assert.equal(await dialogTitle(), 'Save this access key', 'the key stays open');
        assert(await shownKeyIs(secret), 'still the same key');
        await race.getByRole('button', { name: 'Close dialog', exact: true }).click();
        await quiet(race);
        assert.equal(await dialogOpen(), false, 'the key is shown once');
        assert(!(await pageHolds(secret)), 'a closed key dialog leaves the key nowhere');
      },
    );
    lateCase(
      'a key waiting behind a form that a change of business closes is shown on the new page',
      async () => {
        await onBusiness(bid, testName);
        const switching = await pendingSwitch();
        const held = await holdKey();
        await openKeyForm('Browser key shown on another business');
        await inDialog('Create key');
        const secret = await keyFrom(held);
        await race.keyboard.press('Escape');
        await raceSource('records');
        const typed = 'Browser record typed while a key waited';
        await openRecordForm(typed);
        await held.release();
        assert.equal(await dialogTitle(), 'Add business record', 'the key waits for the form');
        await switching.release();
        await showing(demoName);
        await race.locator('#dialog-title', { hasText: 'Save this access key' }).waitFor();
        assert(await shownKeyIs(secret), 'the key is shown over the business now on screen');
        // the key's own reload is the one that brought the other business in
        assert.equal(
          await toastText(),
          `Saved to the workspace. “Add business record” was closed without saving; this page now shows ${demoName}.`,
        );
        assert.equal((await recordsIn(bid, typed)) + (await recordsIn(demoBid, typed)), 0);
        await race.getByRole('button', { name: 'Close dialog', exact: true }).click();
      },
    );
    lateCase('finding 2: a form opened for one business is closed when another loads', async () => {
      await onBusiness(bid, testName);
      await raceSource('records');
      const switching = await pendingSwitch();
      const typed = 'Browser record typed for one business';
      await openRecordForm(typed);
      await switching.release();
      await showing(demoName);
      assert.equal(await dialogOpen(), false, 'the form for the previous business is closed');
      assert.equal(
        await toastText(),
        `“Add business record” was closed without saving; this page now shows ${demoName}.`,
      );
      assert.equal(await race.locator('#dialog-content').innerHTML(), '', 'and its fields left');
      assert.equal(await recordsIn(bid, typed), 0, 'nothing was saved to the first business');
      assert.equal(await recordsIn(demoBid, typed), 0, 'nor to the one now on screen');
    });
    lateCase('a decision form open when another business loads is closed and says so', async () => {
      await onBusiness(bid, testName);
      const [step] = await readyTasks();
      await showStep(step);
      const switching = await pendingSwitch();
      await race.locator('#insp [data-action="tx"][data-to="in_progress"]').click();
      await race
        .getByLabel('Decision note', { exact: false })
        .fill('Synthetic browser test: typed while another business loaded.');
      await switching.release();
      await showing(demoName);
      assert.equal(await race.locator('#tx-form').count(), 0, 'the decision form is gone');
      assert.equal(
        await toastText(),
        `The decision form for “${step.title}” was closed without saving; this page now shows ${demoName}.`,
      );
      assert.equal((await snapshotOf(bid)).objects.find((o) => o.id === step.id).state, 'pending');
    });
    lateCase('a form sent while a switch is pending saves to its own business', async () => {
      await onBusiness(bid, testName);
      await raceSource('records');
      const switching = await pendingSwitch();
      const title = 'Browser record saved while another business loaded';
      assert.equal(await addRecord(title), 201, 'sent to the business the form was opened for');
      await switching.release();
      await showing(demoName);
      await race.locator('#toast', { hasText: 'Saved' }).waitFor();
      assert.equal(await toastText(), `Saved to ${testName}.`, 'the message names its business');
      assert.equal(await recordsIn(bid, title), 1, 'the record is in its own business');
      assert.equal(await recordsIn(demoBid, title), 0, 'and not in the one now shown');
    });
    lateCase('finding 4: a save that lands on another business only says so', async () => {
      await onBusiness(bid, testName);
      await raceSource('records');
      const title = 'Browser record saved after the page moved on';
      const held = await hold(race, `/api/businesses/${bid}/records`, { method: 'POST' });
      await openRecordForm(title);
      await inDialog('Save record');
      await held.requested;
      await race.keyboard.press('Escape');
      await switchTo(demoBid, demoName);
      await raceSource('records');
      await race.locator('#list .row').nth(1).click();
      const view = await race.evaluate(() => [state.source, state.selected]);
      const where = await focused(race);
      const drop = await dropNext(race, '/api/businesses');
      await held.release();
      await drop.off();
      assert.equal(await outdated.isVisible(), false, 'this page is not marked out of date');
      assert.equal(await race.evaluate(() => state.behind), false);
      assert.equal(await toastText(), `Saved to ${testName}.`);
      assert.deepEqual(await race.evaluate(() => [state.source, state.selected]), view);
      assert.deepEqual(await focused(race), where, 'focus stays where the person left it');
      assert.equal(await recordsIn(bid, title), 1);
      assert.equal(await recordsIn(demoBid, title), 0);
    });
    lateCase('a decision that lands on another business only says so', async () => {
      await onBusiness(bid, testName);
      const [step] = await readyTasks();
      await showStep(step);
      const held = await hold(race, `/api/businesses/${bid}/tasks/${step.id}/transition`, {
        method: 'POST',
      });
      await decide('in_progress', 'Synthetic browser test: started, answered late.');
      await held.requested;
      await switchTo(demoBid, demoName);
      await race.locator('#list .row').nth(1).click();
      const view = await race.evaluate(() => [state.source, state.selected]);
      const where = await focused(race);
      const drop = await dropNext(race, '/api/businesses');
      await held.release();
      await drop.off();
      assert.equal(await outdated.isVisible(), false, 'this page is not marked out of date');
      assert.deepEqual(
        await race.evaluate(() => [state.behind, state.accepted, state.open]),
        [false, {}, null],
        'nothing is recorded for the page on screen',
      );
      assert.equal(await race.locator('#insp [data-accepted]').count(), 0);
      assert.equal(await toastText(), `Saved to ${testName}.`);
      assert.deepEqual(await race.evaluate(() => [state.source, state.selected]), view);
      assert.deepEqual(await focused(race), where, 'focus stays where the person left it');
      assert.equal(
        (await snapshotOf(bid)).objects.find((o) => o.id === step.id).state,
        'in_progress',
      );
    });
    lateCase('a refused save that arrives on another business names its business', async () => {
      await onBusiness(bid, testName);
      await raceSource('records');
      await rowIn('Browser customer').click();
      await race.locator('#insp').getByRole('button', { name: 'Edit', exact: true }).click();
      // the record changes on the server while its form is open, so the form's version is old
      const cust = (await snapshotOf(bid)).objects.find((o) => o.title === 'Browser customer');
      await call(`/businesses/${bid}/records/${cust.id}`, 'PUT', {
        kind: cust.kind,
        title: cust.title,
        details: cust.data.details,
        certainty: cust.data.certainty,
        owner: cust.data.owner,
        evidence_ids: cust.data.evidence_ids || [],
        related_ids: cust.data.related_ids || [],
        due_date: cust.data.due_date || null,
        properties: { ...(cust.data.properties || {}), synthetic_change: String(cust.version) },
        expected_version: cust.version,
      });
      const held = await hold(race, `/api/businesses/${bid}/records/${cust.id}`, {
        method: 'PUT',
      });
      await inDialog('Save record');
      const answer = await held.requested;
      assert.equal(answer.status(), 409, 'the server refused the old version');
      const reason = (await answer.json()).detail;
      await race.keyboard.press('Escape');
      await switchTo(demoBid, demoName);
      await held.release();
      assert.equal(await toastText(), `Not saved to ${testName}. ${reason}`);
    });
    lateCase('a refused decision that arrives on another business names its business', async () => {
      await onBusiness(bid, testName);
      const [step] = await readyTasks();
      await showStep(step);
      await race.locator('#insp [data-action="tx"][data-to="in_progress"]').click();
      await race
        .getByLabel('Decision note', { exact: false })
        .fill('Synthetic browser test: refused as the step changed.');
      await call(`/businesses/${bid}/tasks/${step.id}/transition`, 'POST', {
        state: 'in_progress',
        note: 'Synthetic browser test: changed elsewhere.',
        evidence_ids: [],
        expected_version: step.version,
      });
      const held = await hold(race, `/api/businesses/${bid}/tasks/${step.id}/transition`, {
        method: 'POST',
      });
      await race.locator('#insp').getByRole('button', { name: 'Record decision' }).click();
      const answer = await held.requested;
      assert.equal(answer.status(), 409, 'the server refused the old version');
      const reason = (await answer.json()).detail;
      await switchTo(demoBid, demoName);
      await held.release();
      assert.equal(await toastText(), `Not saved to ${testName}. ${reason}`);
    });
    lateCase('the business form sends the version it was filled from', async () => {
      await onBusiness(bid, testName);
      await raceSource('records');
      const held = await hold(race, `/api/businesses/${bid}/records`, { method: 'POST' });
      await openRecordForm('Browser record whose reload refreshes an open form');
      await inDialog('Save record');
      await held.requested;
      await race.keyboard.press('Escape');
      await raceSource('journey');
      await race
        .locator('#insp')
        .getByRole('button', { name: 'Edit business', exact: true })
        .click();
      await race
        .getByLabel('Intended customer', { exact: true })
        .fill('Customer typed in the form');
      // the business changes elsewhere, and the earlier save's reload brings that into memory
      const b = (await snapshotOf(bid)).business;
      const kept = 'Fictional customer saved elsewhere';
      await call(`/businesses/${bid}`, 'PUT', {
        name: b.data.name,
        concept: b.data.concept,
        route: b.data.route,
        jurisdiction: b.data.jurisdiction,
        currency: b.data.currency,
        customer: kept,
        owner: b.data.owner,
        is_demo: b.data.is_demo,
        expected_version: b.version,
      });
      await held.release();
      // the toast sits behind the open form, outside the accessibility tree while it is open
      await race.locator('#toast', { hasText: 'Saved to the workspace.' }).waitFor();
      assert.equal(await race.evaluate(() => state.snapshot.business.version), b.version + 1);
      const answered = race.waitForResponse(
        (r) =>
          r.request().method() === 'PUT' && new URL(r.url()).pathname === `/api/businesses/${bid}`,
      );
      await inDialog('Save changes');
      assert.equal((await answered).status(), 409, 'the older form is refused, not merged');
      await quiet(race);
      assert.equal((await snapshotOf(bid)).business.data.customer, kept, 'nothing was overwritten');
      await race.locator('#dialog .error', { hasText: 'The business changed' }).waitFor();
    });
    for (const [what, src, row, button] of [
      ['a document', 'documents', null, 'Read draft'],
      ['an invoice’s details', 'sales', 'Browser accepted report', 'Details'],
    ])
      lateCase(`the view of ${what} closes when another business loads`, async () => {
        await onBusiness(bid, testName);
        await raceSource(src);
        if (row) await rowIn(row).click();
        const switching = await pendingSwitch();
        await race.locator('#insp').getByRole('button', { name: button, exact: true }).click();
        assert.equal(await dialogOpen(), true, `${button} opens`);
        await switching.release();
        await showing(demoName);
        assert.equal(await dialogOpen(), false, 'the view of the other business closed');
        assert.equal(await race.locator('#dialog-content').innerHTML(), '', 'and left the page');
        assert(await race.locator('#toast').isHidden(), 'a view closes without a message');
      });
    lateCase('a viewer’s read-only record view closes when another business loads', async () => {
      const viewerKey = (
        await call('/principals', 'POST', {
          name: 'Browser viewer for late views',
          role: 'viewer',
          business_ids: [bid, demoBid],
        })
      ).token;
      const viewer = await browser.newContext({ viewport: { width: 1440, height: 1050 } });
      try {
        const v = await newPage(viewer);
        v.on('pageerror', (e) => errors.push(e.message));
        await v.goto(origin);
        await v.getByLabel('Access key', { exact: true }).fill(viewerKey);
        await v.getByRole('button', { name: 'Open workspace' }).click();
        await v.locator(OPENED).waitFor();
        if ((await v.locator('#business-select').inputValue()) !== bid) {
          await v.locator('#business-select').selectOption(bid);
          await v.locator('#list .th-sub', { hasText: testName }).waitFor();
        }
        await v.locator('#src-nav .src[data-src="records"]').click();
        await v.locator('#list .row', { hasText: 'Browser customer' }).click();
        const held = await hold(v, `/api/businesses/${demoBid}`);
        await v.locator('#business-select').selectOption(demoBid);
        await held.requested;
        await v.locator('#insp').getByRole('button', { name: 'View', exact: true }).click();
        assert.equal(await v.locator('#dialog').evaluate((d) => d.open), true, 'View opens');
        await held.release();
        await v.locator('#list .th-sub', { hasText: demoName }).waitFor();
        assert.equal(
          await v.locator('#dialog').evaluate((d) => d.open),
          false,
          'the read-only record view of the other business closed',
        );
      } finally {
        await viewer.close();
      }
    });
    lateCase('a business that leaves the list closes its open form', async () => {
      await onBusiness(bid, testName);
      await raceSource('records');
      const savedFirst = 'Browser record saved before its business left the list';
      const typed = 'Browser record typed as its business left the list';
      const held = await hold(race, `/api/businesses/${bid}/records`, { method: 'POST' });
      await openRecordForm(savedFirst);
      await inDialog('Save record');
      await held.requested;
      await race.keyboard.press('Escape');
      await openRecordForm(typed);
      // the next list of businesses, built by hand, no longer holds the one on screen; once
      // that has shown, the route passes requests on rather than being removed (see hold())
      const listPath = (url) => url.pathname === '/api/businesses';
      let leaving = true;
      const without = async (route) => {
        if (!leaving || route.request().method() !== 'GET') return route.fallback();
        const response = await route.fetch();
        const json = (await response.json()).filter((x) => x.id !== bid);
        await route.fulfill({ response, json });
      };
      await race.route(listPath, without);
      await held.release();
      await showing(demoName);
      leaving = false;
      assert.equal(await dialogOpen(), false, 'the form for the business that left is closed');
      assert.equal(
        await toastText(),
        `Saved to ${testName}. “Add business record” was closed without saving; this page now shows ${demoName}.`,
      );
      assert.equal(await recordsIn(bid, savedFirst), 1);
      assert.equal((await recordsIn(bid, typed)) + (await recordsIn(demoBid, typed)), 0);
    });
    lateCase('finding 3: a lost answer to key creation promises no safe retry', async () => {
      await onBusiness(bid, testName);
      const lostName = 'Browser key whose answer was lost';
      await openKeyForm(lostName);
      const held = await hold(race, '/api/principals', { method: 'POST', lose: true });
      const listAgain = race.waitForRequest(
        (r) => r.method() === 'GET' && new URL(r.url()).pathname === '/api/principals',
      );
      // not awaited until the message is checked; a failure meanwhile must not go unhandled
      listAgain.catch(() => {});
      await inDialog('Create key');
      await held.requested;
      await held.release();
      await race.locator('#dialog-title', { hasText: 'Access key not confirmed' }).waitFor();
      assert.equal(
        await race.locator('#dialog-content p').innerText(),
        `Connection interrupted, so this page cannot tell whether the access key “${lostName}” was created. A key that was created cannot be shown again. Look at the access keys under Access & assistants: if there is a key named “${lostName}” that you did not receive, revoke it, then create a new one.`,
      );
      assert.doesNotMatch(await race.locator('#dialog').innerText(), /retry|safe/i);
      assert.equal(
        await race.locator('#dialog').getByRole('button', { name: 'Create key' }).count(),
        0,
        'no control sends the creation again',
      );
      assert.equal((await focused(race)).action, 'close-dialog', 'focus is on Close');
      // the access list was loaded again, and shows the key the server made
      await listAgain;
      await race.locator('#list .row', { hasText: lostName }).waitFor();
      assert.equal((await principalsNamed(lostName)).length, 1, 'the server made one key');
      await inDialog('Close');
      // when that reload fails too, the notice still stands
      const again = 'Browser key lost while the list cannot load';
      await openKeyForm(again);
      const held2 = await hold(race, '/api/principals', { method: 'POST', lose: true });
      const drop = await dropNext(race, '/api/businesses');
      await inDialog('Create key');
      await held2.requested;
      await held2.release();
      await drop.dropped;
      await drop.off();
      assert.equal(await dialogTitle(), 'Access key not confirmed');
      assert((await race.locator('#dialog-content p').innerText()).includes(`“${again}”`));
      assert.equal(await toastText(), 'The server could not be reached.');
    });
    lateCase('an interrupted command says what is true once its form is closed', async () => {
      await onBusiness(bid, testName);
      await raceSource('records');
      const held = await hold(race, `/api/businesses/${bid}/records`, {
        method: 'POST',
        lose: true,
      });
      await openRecordForm('Browser record whose answer was lost');
      await inDialog('Save record');
      await held.requested;
      await race.keyboard.press('Escape');
      await held.release();
      assert.equal(
        await toastText(),
        'Connection interrupted. This page cannot tell whether your change was saved; load the current records and check before entering it again.',
      );
    });
    lateCase('an interrupted revoke says that revoking again is safe', async () => {
      await onBusiness(bid, testName);
      const name = 'Browser key revoked over a lost connection';
      const made = await call('/principals', 'POST', { name, role: 'viewer', business_ids: [bid] });
      await race.reload();
      await race.locator(OPENED).waitFor();
      await raceSource('access');
      await rowIn(name).click();
      await race.locator('#insp').getByRole('button', { name: 'Revoke', exact: true }).click();
      const held = await hold(race, `/api/principals/${made.id}`, { method: 'DELETE', lose: true });
      await inDialog('Revoke key');
      await held.requested;
      await held.release();
      assert.equal(
        await race.locator('#dialog .error').innerText(),
        'Connection interrupted, so this page cannot tell whether the key was revoked. Revoking it again is safe.',
      );
      await inDialog('Revoke key');
      await race.locator('#dialog').waitFor({ state: 'hidden' });
      await race.locator('#toast', { hasText: 'Saved to the workspace.' }).waitFor();
      assert.equal((await principalsNamed(name))[0].revoked, true);
    });
    lateCase('an interrupted sign-out and sign-in each say that repeating is safe', async () => {
      let drop = await dropNext(race, '/api/logout', 'POST');
      await race.locator('.toolbar [data-action="logout"]').click();
      await drop.dropped;
      await drop.off();
      assert.equal(
        await toastText(),
        'Connection interrupted, so this page cannot tell whether you were signed out. Signing out again is safe.',
      );
      assert(await race.locator('#app').isVisible(), 'still signed in');
      await signOut();
      drop = await dropNext(race, '/api/login', 'POST');
      await race.getByLabel('Access key', { exact: true }).fill(ownerKey());
      await race.getByRole('button', { name: 'Open workspace' }).click();
      await drop.dropped;
      await drop.off();
      assert.equal(
        await race.locator('#login-error').innerText(),
        'Connection interrupted. Signing in again is safe.',
      );
      await race.getByRole('button', { name: 'Open workspace' }).click();
      await race.locator(OPENED).waitFor();
    });
    lateCase('a sign-out refused because the session ended leaves no message', async () => {
      await context.clearCookies();
      await race.locator('.toolbar [data-action="logout"]').click();
      await race.getByLabel('Access key', { exact: true }).waitFor();
      await quiet(race);
      assert.equal(
        await race.locator('#toast').isVisible(),
        false,
        'nothing over the sign-in page',
      );
    });
    lateCase('finding 5: after Dismiss, focus is on a visible control', async () => {
      await onBusiness(bid, testName);
      await raceSource('records');
      let drop = await dropNext(race, '/api/businesses');
      assert.equal(await addRecord('Browser record before Dismiss on a computer'), 201);
      await drop.dropped;
      await drop.off();
      await outdated.getByRole('button', { name: 'Dismiss' }).focus();
      await race.keyboard.press('Enter');
      assert(await outdated.isHidden());
      let at = await focused(race);
      assert.deepEqual(
        [at.pane, at.current, at.visible],
        ['list', 'true', true],
        'the selected row',
      );
      // on a phone showing the inspector, the list is hidden
      await race.setViewportSize({ width: 390, height: 844 });
      if (await race.locator('#back-list').isVisible()) await race.locator('#back-list').click();
      drop = await dropNext(race, '/api/businesses');
      assert.equal(await addRecord('Browser record before Dismiss on a phone'), 201);
      await drop.dropped;
      await drop.off();
      await race.locator('#list .row').first().click();
      await quiet(race);
      assert.equal(await race.locator('#list').isVisible(), false);
      await outdated.getByRole('button', { name: 'Dismiss' }).focus();
      await race.keyboard.press('Enter');
      at = await focused(race);
      assert.deepEqual([at.id, at.visible], ['insp-title', true], 'phone: the inspector heading');
    });

    // Focus when a dialog closes. The browser hands focus back to the control that opened the
    // dialog; when the page behind it was drawn again meanwhile, that control is gone, and focus
    // has to land on the control drawn in its place, never on nothing.
    const focusAfterClose = async () => {
      await race.waitForFunction(() => !document.querySelector('#dialog').open);
      // the dialog's close event, and anything it shows in turn, runs after close()
      await quiet(race);
      return focused(race);
    };
    const onKeyControl = (at, what) =>
      assert.deepEqual(
        [at.action, at.visible, at.pane],
        ['new-key', true, 'list'],
        `${what}: focus is on the visible control that opens the key form`,
      );
    const onRecordControl = (at, what) =>
      assert.deepEqual(
        [at.action, at.visible, at.pane],
        ['new-record', true, 'list'],
        `${what}: focus is on the visible control that opens the record form`,
      );
    // Create a key, show it, and only then let the reload after it draw the access list again,
    // so the control that opened the form is replaced while the key's dialog is open. The page
    // does not set that order: the browser fires the form's close event, which shows the key, a
    // few milliseconds after the form closes, and the reload's answers can arrive first. Then
    // the key's dialog opens over a list already drawn again, and the case would pass without
    // the situation it names. So the reload's first request is held until the key is showing.
    const keyShownOverRedrawnList = async (name, phone = false) => {
      if (phone) {
        // on a phone the source list gives way to the list pane once a source is chosen
        await race.locator('#src-nav .src[data-src="access"]').click();
        await race.getByRole('button', { name: '+ Create access key', exact: true }).click();
        await race.getByLabel('Person or agent name', { exact: true }).fill(name);
      } else await openKeyForm(name);
      const opener = await race.locator('#list-right [data-action="new-key"]').elementHandle();
      const reload = await hold(race, '/api/businesses', { method: 'GET' });
      await inDialog('Create key');
      await race.locator('#dialog-title', { hasText: 'Save this access key' }).waitFor();
      await reload.requested;
      assert.equal(await rowIn(name).count(), 0, 'the key is shown before the list is drawn again');
      assert(
        await opener.evaluate((el) => el.isConnected),
        'the form’s opener is still on the page',
      );
      await reload.release();
      await rowIn(name).waitFor();
      assert(
        !(await opener.evaluate((el) => el.isConnected)),
        'the list behind the key was drawn again, and the form’s opener with it',
      );
      await opener.dispose();
    };
    lateCase('round 4 finding 1: Escape on a new key, after the list was redrawn', async () => {
      await onBusiness(bid, testName);
      await keyShownOverRedrawnList('Browser key closed with Escape');
      await race.keyboard.press('Escape');
      onKeyControl(await focusAfterClose(), 'Escape');
    });
    lateCase('round 4 finding 1: Close on a new key, after the list was redrawn', async () => {
      await onBusiness(bid, testName);
      await keyShownOverRedrawnList('Browser key closed with its Close button');
      await race.getByRole('button', { name: 'Close dialog', exact: true }).click();
      onKeyControl(await focusAfterClose(), 'Close');
    });
    lateCase('round 4 finding 1: closing “Access key not confirmed”', async () => {
      await onBusiness(bid, testName);
      const name = 'Browser key not confirmed, then closed';
      await openKeyForm(name);
      const held = await hold(race, '/api/principals', { method: 'POST', lose: true });
      await inDialog('Create key');
      await held.requested;
      await held.release();
      await race.locator('#dialog-title', { hasText: 'Access key not confirmed' }).waitFor();
      // the notice's own reload draws the list again behind it
      await rowIn(name).waitFor();
      await quiet(race);
      await inDialog('Close');
      onKeyControl(await focusAfterClose(), 'Close');
    });
    lateCase('round 4 finding 1: a key that waited behind a form, then closed', async () => {
      await onBusiness(bid, testName);
      const held = await holdKey();
      const name = 'Browser key that waited behind a record form';
      await openKeyForm(name);
      await inDialog('Create key');
      const secret = await keyFrom(held);
      await race.keyboard.press('Escape');
      await raceSource('records');
      await openRecordForm('Browser record cancelled while a key waited');
      // the key's answer arrives, waits for the form, and its reload redraws the list behind it
      await held.release();
      await race.locator('#toast', { hasText: 'Saved to the workspace.' }).waitFor();
      assert.equal(await dialogTitle(), 'Add business record', 'the key waits for the form');
      await inDialog('Cancel');
      await race.locator('#dialog-title', { hasText: 'Save this access key' }).waitFor();
      assert(await shownKeyIs(secret), 'the key shown is the one the server made');
      await race.keyboard.press('Escape');
      onRecordControl(await focusAfterClose(), 'after the waiting key');
    });
    // a record form open while an earlier save's answer arrives and reloads the list behind it
    const formOverReloadedList = async (title) => {
      await onBusiness(bid, testName);
      await raceSource('records');
      const held = await hold(race, `/api/businesses/${bid}/records`, { method: 'POST' });
      await openRecordForm('Browser record whose answer arrives later');
      await inDialog('Save record');
      await held.requested;
      await race.keyboard.press('Escape');
      await openRecordForm(title);
      await held.release();
      await race.locator('#toast', { hasText: 'Saved to the workspace.' }).waitFor();
      await rowIn('Browser record whose answer arrives later').waitFor();
      await quiet(race);
      assert.equal(await dialogTitle(), 'Add business record', 'the second form is still open');
    };
    lateCase('round 4 finding 1: cancelling a form whose list was reloaded', async () => {
      await formOverReloadedList('Browser record cancelled after a reload');
      await inDialog('Cancel');
      onRecordControl(await focusAfterClose(), 'Cancel');
    });
    lateCase('round 4 finding 1: saving a form whose list was reloaded', async () => {
      const title = 'Browser record saved after a reload';
      const before = await recordsIn(bid, title);
      await formOverReloadedList(title);
      const saved = race.waitForResponse(
        (r) =>
          r.request().method() === 'GET' && new URL(r.url()).pathname === `/api/businesses/${bid}`,
      );
      await inDialog('Save record');
      await saved;
      await rowIn(title).waitFor();
      onRecordControl(await focusAfterClose(), 'Save');
      assert.equal(await recordsIn(bid, title), before + 1, 'the form saved once');
    });
    lateCase('round 4 finding 1: on a phone, focus stays in the pane on screen', async () => {
      await onBusiness(bid, testName);
      await race.setViewportSize({ width: 390, height: 844 });
      if (await race.locator('#back-list').isVisible()) await race.locator('#back-list').click();
      if (await race.locator('#back-src').isVisible()) await race.locator('#back-src').click();
      await keyShownOverRedrawnList('Browser key closed on a phone', true);
      assert.equal(await race.locator('#insp').isVisible(), false, 'the list pane is on screen');
      await race.keyboard.press('Escape');
      onKeyControl(await focusAfterClose(), 'phone');
    });

    lateCase(
      'round 4 finding 2: a sign-out answer held past a 401 and a new sign-in ends nothing',
      async () => {
        await onBusiness(bid, testName);
        const held = await hold(race, '/api/logout', { method: 'POST' });
        await race.locator('.toolbar [data-action="logout"]').click();
        // the server has ended the session; the next thing the page asks for is refused
        await held.requested;
        await race.locator('#src-nav .src[data-src="history"]').click();
        await race.getByLabel('Access key', { exact: true }).waitFor();
        await signInAgain();
        const session = async () =>
          (await context.cookies(origin)).find((c) => c.name === 'foundry_session')?.value;
        const signedIn = await session();
        assert(signedIn, 'the new sign-in has a session cookie');
        await held.release();
        // compared, never printed
        assert((await session()) === signedIn, 'the new session keeps its cookie');
        await raceSource('history');
        await race.locator('#list .grp', { hasText: 'Events' }).waitFor();
        await quiet(race);
        assert(await race.locator('#app').isVisible(), 'the workspace stays on screen');
        assert(await race.locator('#login').isHidden(), 'sign-in is not asked for');
        assert.equal(await race.locator('#toast').isVisible(), false, 'no message appears');
        assert.equal(await dialogOpen(), false, 'nothing opens');
        assert(held.cancelled(), 'the page cancelled the sign-out when its session ended');
      },
    );

    // the test business's records as the server sends them, changed on their way to the page
    const serveChanged = (change) =>
      race.route(
        (url) => url.pathname === `/api/businesses/${bid}`,
        async (route) => {
          if (route.request().method() !== 'GET') return route.fallback();
          const response = await route.fetch();
          const body = await response.json();
          change(body);
          await route.fulfill({ response, json: body });
        },
      );
    // Round 8, A. What a piece of evidence is used by follows the server's rule for withdrawn
    // evidence. The rule's own code, from the server, gives the expected steps for each piece.
    const SERVER_RULE = `
import json, sys
from foundry.service import COMPLETED, evidence_basis
snapshot = json.load(sys.stdin)
objects = snapshot["objects"]
def relies(task):
    refs = list(task["data"].get("evidence_ids", []))
    if task["state"] in COMPLETED:
        refs += list(evidence_basis(task["data"].get("history", [])))
    return refs
print(json.dumps({
    e["id"]: sorted(
        t["id"] for t in objects
        if t["kind"] == "task" and t["state"] != "superseded" and e["id"] in relies(t)
    )
    for e in objects if e["kind"] == "evidence"
}))
`;
    const serverRule = (snapshot) => {
      const run = spawnSync(python, ['-c', SERVER_RULE], {
        input: JSON.stringify(snapshot),
        encoding: 'utf8',
      });
      assert.equal(run.status, 0, run.stderr);
      return JSON.parse(run.stdout);
    };
    // the steps the page counts as using each piece of evidence, from the records it holds
    const pageRule = () =>
      race.evaluate(() =>
        Object.fromEntries(
          objs('evidence').map((e) => [
            e.id,
            usedBy(e.id)
              .filter((o) => o.kind === 'task' && !replaced(o))
              .map((o) => o.id)
              .sort(),
          ]),
        ),
      );
    const sameAsServer = async (what) =>
      assert.deepEqual(
        await pageRule(),
        serverRule(await race.evaluate(() => state.snapshot)),
        `${what}: the page counts what the server's rule gives`,
      );
    // A business of its own for these checks, so that each has steps of its own to decide. Its
    // records let four steps with no prerequisite be approved.
    const usageName = 'Browser usage checks';
    let usageBid = null;
    const usageBusiness = async () => {
      if (!usageBid) {
        usageBid = (
          await call('/businesses', 'POST', {
            name: usageName,
            concept: 'Synthetic business for checking which evidence each step relies on.',
          })
        ).id;
        for (const kind of ['customer', 'offer', 'decision', 'asset'])
          await call(`/businesses/${usageBid}/records`, 'POST', {
            kind,
            title: `Browser usage ${kind}`,
            details: 'Synthetic record for the usage checks.',
          });
      }
      return usageBid;
    };
    // the page on the usage business, after changes made through the API
    const showUsage = async () => {
      await race.reload();
      await race.locator(OPENED).waitFor();
      await onBusiness(usageBid, usageName);
    };
    let usageRun = 0;
    const reviewedPair = async (where = bid) => {
      usageRun += 1;
      const pair = [];
      for (const which of ['A', 'B']) {
        const e = await call(`/businesses/${where}/evidence`, 'POST', {
          title: `Browser usage source ${which}${usageRun}`,
          content: `Synthetic source ${which} for the usage checks.`,
          source_type: 'document',
        });
        pair.push(
          await call(`/businesses/${where}/evidence/${e.id}/review`, 'POST', {
            note: 'Synthetic review for the usage checks.',
            expected_version: e.version,
          }),
        );
      }
      return pair;
    };
    const freeStep = async () => {
      const t = (await snapshotOf(usageBid)).objects.find(
        (o) =>
          o.kind === 'task' &&
          o.state === 'pending' &&
          !o.data.deal_id &&
          !o.data.external &&
          !o.blockers?.length,
      );
      assert(t, 'an internal step with nothing blocking it');
      return t;
    };
    const moveStep = async (id, to, evidence) => {
      const t = (await snapshotOf(usageBid)).objects.find((o) => o.id === id);
      return call(`/businesses/${usageBid}/tasks/${id}/transition`, 'POST', {
        state: to,
        note: 'Synthetic decision for the usage checks.',
        evidence_ids: evidence,
        expected_version: t.version,
      });
    };
    const evSub = (id) => race.locator(`#list .row[data-key="ev:${id}"] .r-sub`).innerText();
    const usedBySection = () =>
      race.locator('#insp .i-sec').filter({ has: race.locator('h3', { hasText: /^Used by/ }) });
    const openEvidence = async (e) => {
      await raceSource('evidence');
      await race.locator(`#list .row[data-key="ev:${e.id}"]`).click();
      await race.locator('#insp-title', { hasText: e.title }).waitFor();
      await quiet(race);
    };
    const usedRows = async () => usedBySection().locator('.att-row .sub').allInnerTexts();
    const EARLIER = ' · relied on from an earlier decision, not attached now';
    lateCase('round 8 A: evidence a completed step relied on earlier is used by it', async () => {
      const [a, b] = await reviewedPair(await usageBusiness());
      const t = await freeStep();
      await moveStep(t.id, 'prepared', [a.id]);
      await moveStep(t.id, 'approved', [a.id]);
      await moveStep(t.id, 'verified', [b.id]);
      await showUsage();
      await raceSource('evidence');
      assert.match(await evSub(a.id), / · used by 1$/, 'A’s row counts the step');
      assert.match(await evSub(b.id), / · used by 1$/);
      await openEvidence(a);
      assert.deepEqual(await usedRows(), [`Task · Verified${EARLIER}`], 'A’s view lists the step');
      assert((await usedBySection().innerText()).includes(t.title));
      await openEvidence(b);
      assert.deepEqual(await usedRows(), ['Task · Verified'], 'B’s view lists it as attached');
      await sameAsServer('a step verified on other evidence');
      // withdrawn through the page, A reopens the step, and the step's new cycle no longer uses it
      await openEvidence(a);
      await race.locator('#insp').getByRole('button', { name: 'Withdraw verification' }).click();
      await race
        .getByLabel('Review note', { exact: true })
        .fill('Synthetic withdrawal for the usage checks.');
      await inDialog('Confirm withdrawal');
      await race.locator('#toast', { hasText: 'Saved to the workspace.' }).waitFor();
      await quiet(race);
      const reopened = (await snapshotOf(usageBid)).objects.find((o) => o.id === t.id);
      assert.equal(reopened.state, 'needs_review', 'the server reopened the step');
      assert.equal(reopened.data.history.at(-1).note, `Evidence withdrawn: ${a.title}`);
      await raceSource('evidence');
      assert.match(await evSub(a.id), / · not used yet$/, 'A is no longer used by it');
      await openEvidence(a);
      assert.deepEqual(await usedRows(), []);
      await sameAsServer('after the withdrawal');
    });
    lateCase('round 8 A: only the latest preparation onward counts', async () => {
      const [a, b] = await reviewedPair(await usageBusiness());
      const t = await freeStep();
      await moveStep(t.id, 'prepared', [a.id]);
      await moveStep(t.id, 'in_progress', []);
      await moveStep(t.id, 'prepared', [b.id]);
      await moveStep(t.id, 'approved', [b.id]);
      await showUsage();
      await raceSource('evidence');
      assert.match(await evSub(a.id), / · not used yet$/, 'A came before the latest preparation');
      assert.match(await evSub(b.id), / · used by 1$/);
      await sameAsServer('a step prepared again on other evidence');
    });
    lateCase('round 8 A: a step not completed counts only what it attaches now', async () => {
      // the API records no change of evidence that keeps a step prepared; a prepared step is
      // sent back to work with only B attached
      const [a, b] = await reviewedPair(await usageBusiness());
      const t = await freeStep();
      await moveStep(t.id, 'prepared', [a.id]);
      await showUsage();
      await raceSource('evidence');
      assert.match(await evSub(a.id), / · used by 1$/, 'a prepared step uses what it attaches');
      await moveStep(t.id, 'in_progress', [b.id]);
      await showUsage();
      await raceSource('evidence');
      assert.match(await evSub(a.id), / · not used yet$/, 'A is only in its history');
      assert.match(await evSub(b.id), / · used by 1$/);
      await sameAsServer('a step sent back to work');
    });
    lateCase('round 8 A: a step reopened and decided again counts only its new cycle', async () => {
      const [a, b] = await reviewedPair(await usageBusiness());
      const t = await freeStep();
      await moveStep(t.id, 'prepared', [a.id]);
      await moveStep(t.id, 'approved', [a.id]);
      await moveStep(t.id, 'needs_review', []);
      await moveStep(t.id, 'not_applicable', [b.id]);
      await showUsage();
      await raceSource('evidence');
      assert.match(await evSub(a.id), / · not used yet$/, 'A came before the step was reopened');
      assert.match(await evSub(b.id), / · used by 1$/);
      await sameAsServer('a step reopened and ruled out');
    });
    lateCase('round 8 A: unreadable histories and replaced steps, served by the test', async () => {
      const [e] = await reviewedPair();
      const pending = (await snapshotOf(bid)).objects.filter(
        (o) => o.kind === 'task' && o.state === 'pending' && !o.data.deal_id,
      );
      assert(pending.length >= 4, 'four steps to serve changed');
      const [notList, strings, gone, earlier] = pending;
      const at = new Date().toISOString();
      await serveChanged((body) => {
        const set = (id, changes) => {
          const t = body.objects.find((o) => o.id === id);
          Object.assign(t, { state: changes.state });
          Object.assign(t.data, changes.data);
        };
        set(notList.id, { state: 'verified', data: { history: 'not a list', evidence_ids: [] } });
        set(strings.id, {
          state: 'verified',
          data: {
            history: ['an entry that is a string', { to: 'prepared', evidence_ids: [42, null] }, 7],
            evidence_ids: [],
          },
        });
        set(gone.id, {
          state: 'superseded',
          data: {
            history: [
              { from: 'pending', to: 'prepared', evidence_ids: [e.id], at },
              { from: 'prepared', to: 'approved', evidence_ids: [e.id], at },
              { from: 'approved', to: 'superseded', note: 'Synthetic replacement.', at },
            ],
            evidence_ids: [],
          },
        });
        set(earlier.id, {
          state: 'approved',
          data: {
            history: [
              { from: 'pending', to: 'prepared', evidence_ids: [e.id], at },
              { from: 'prepared', to: 'approved', evidence_ids: [], at },
            ],
            evidence_ids: [],
          },
        });
      });
      await race.reload();
      await race.locator(OPENED).waitFor();
      await onBusiness(bid, testName);
      await raceSource('evidence');
      assert.match(await evSub(e.id), / · used by 1$/, 'only the approved step counts');
      await openEvidence(e);
      assert.deepEqual(await usedRows(), [`Task · Approved${EARLIER}`]);
      assert((await usedBySection().innerText()).includes(earlier.title));
      assert.equal(
        await race.locator('#insp h3', { hasText: 'Cited by replaced steps' }).count(),
        0,
        'a replaced step is not listed through its history',
      );
      assert.doesNotMatch(await race.locator('#insp').innerText(), new RegExp(gone.title));
      await sameAsServer('served histories');
      for (const t of [notList, strings]) {
        await showStep(t);
        await quiet(race);
        assert.doesNotMatch(
          await race.locator('#insp').innerText(),
          /could not be shown|TypeError|Cannot read/,
          `${t.title}: its view is drawn`,
        );
      }
    });
    // Round 8, B. A document whose snapshot gives the server's reasons, and one whose does not.
    const docRow = (id) => race.locator(`#list .row[data-key="doc:${id}"]`);
    const newDraft = async () => {
      const doc = await call(`/businesses/${bid}/documents`, 'POST', { template: 'business_plan' });
      await race.reload();
      await race.locator(OPENED).waitFor();
      await onBusiness(bid, testName);
      return doc;
    };
    const readDraft = async (doc) => {
      await raceSource('documents');
      await docRow(doc.id).click();
      await race.locator('#insp-title', { hasText: doc.title }).waitFor();
      await race.locator('#insp').getByRole('button', { name: 'Read draft', exact: true }).click();
      await race.locator('#dialog pre.document').waitFor();
    };
    const viewFlags = () => race.locator('#dialog-content .flag').allInnerTexts();
    // what a draft's view says when it is out of date and the server lists no reason
    const DRAFT_CHANGED = 'This draft is out of date. Generate a new version.';
    // the server's own reason when a document's records changed, word for word
    const SOURCE_CHANGED =
      'The business or its records have changed since this document was generated. Generate a new version before relying on it.';
    lateCase('round 8 B: a document with the server’s reasons, from both servers', async () => {
      const doc = await newDraft();
      const reasons = [
        {
          code: 'synthetic_markup',
          message: 'Synthetic reason <b>one</b> & “two”, said "plainly".',
        },
        { code: 'obligations_overdue', message: 'The number of overdue obligations changed.' },
      ];
      // every draft is served stale: with these reasons, or with none, as today's server does
      let mode = 'reasons';
      await serveChanged((body) => {
        for (const d of body.objects.filter((o) => o.kind === 'document')) {
          delete d.stale_reasons;
          d.stale = true;
          if (mode === 'reasons') d.stale_reasons = reasons;
        }
      });
      const related = await call(`/businesses/${bid}/records`, 'POST', {
        kind: 'risk',
        title: 'Browser record related to a draft with reasons',
        details: 'Synthetic record related to a draft.',
        related_ids: [doc.id],
      });
      await race.reload();
      await race.locator(OPENED).waitFor();
      const messages = reasons.map((r) => r.message);
      const pageWords = /made from records|Source records changed|have changed since/;
      const everywhere = async (what) => {
        await raceSource('documents');
        await docRow(doc.id).click();
        await race.locator('#insp-title', { hasText: doc.title }).waitFor();
        await quiet(race);
        const shown = {
          row: await docRow(doc.id).locator('.r-sub').innerText(),
          title: await race.locator('#src-nav .src[data-src="documents"]').getAttribute('title'),
          state: await race.locator('#insp .i-state .st').innerText(),
          flags: await race.locator('#insp .flag').allInnerTexts(),
        };
        await race
          .locator('#insp')
          .getByRole('button', { name: 'Read draft', exact: true })
          .click();
        await race.locator('#dialog pre.document').waitFor();
        shown.view = await viewFlags();
        await race.getByRole('button', { name: 'Close dialog', exact: true }).click();
        await raceSource('today');
        shown.today = await race.locator(`#list .row[data-key="doc:${doc.id}"] .r-sub`).innerText();
        await raceSource('records');
        await rowIn(related.title).click();
        await race.locator('#insp-title', { hasText: related.title }).waitFor();
        shown.related = await race
          .locator('#insp .att-row', { hasText: doc.title })
          .locator('.sub')
          .innerText();
        return shown;
      };
      const withReasons = await everywhere('with reasons');
      assert.deepEqual(withReasons, {
        row: 'Needs review',
        title: 'Some drafts need review',
        state: 'Needs review',
        flags: messages,
        view: messages,
        today: 'Draft · needs review',
        related: 'Document · Needs review',
      });
      assert.equal(await race.locator('#insp .flag b').count(), 0, 'a reason is shown as text');
      for (const [where, text] of Object.entries(withReasons))
        assert.doesNotMatch(String(text), pageWords, `${where}: no reason of the page’s own`);
      // today's server: stale, and no reasons
      mode = 'plain';
      await race.reload();
      await race.locator(OPENED).waitFor();
      const withoutReasons = await everywhere('without reasons');
      assert.deepEqual(withoutReasons, {
        row: 'Out of date',
        title: 'Some drafts are out of date',
        state: 'Out of date',
        flags: [
          'This draft is out of date. This copy stays as it was generated. Generate a new version.',
        ],
        view: [DRAFT_CHANGED],
        today: 'Draft · out of date',
        related: 'Document · Out of date',
      });
      for (const [where, text] of Object.entries(withoutReasons))
        assert.doesNotMatch(String(text), pageWords, `${where}: no reason of the page’s own`);
    });
    // Round 9, D. Whether a document is out of date is decided once, and every place that speaks
    // about it says the same: the server's reasons when it lists them, and none of the page's own.
    // Each case has a business of its own, so that only its draft is in the Documents source.
    const OLD_DOC_WORDS = [
      'made from records that changed',
      'Source records changed',
      'have changed since',
      'Inputs have changed',
    ];
    const docBusiness = async (name) => {
      const id = (
        await call('/businesses', 'POST', {
          name,
          concept: 'Synthetic business for the out-of-date document checks.',
        })
      ).id;
      const doc = await call(`/businesses/${id}/documents`, 'POST', { template: 'business_plan' });
      return { id, doc };
    };
    const showBusiness = async (id, name) => {
      await race.reload();
      await race.locator(OPENED).waitFor();
      await onBusiness(id, name);
    };
    // a business's records as the server sends them, changed on their way to the page
    const serveBusiness = (id, change) =>
      race.route(
        (url) => url.pathname === `/api/businesses/${id}`,
        async (route) => {
          if (route.request().method() !== 'GET') return route.fallback();
          const response = await route.fetch();
          const body = await response.json();
          change(body);
          await route.fulfill({ response, json: body });
        },
      );
    // Everything the page says about one draft: the Documents source, its row, its inspector, its
    // full view, Today, and the record that relates to it. Each reason must be visible, and in the
    // full view above the draft's text. `texts` is the page's text at each of those places.
    const docPlaces = async (doc, related) => {
      const texts = [];
      await raceSource('documents');
      const docSource = race.locator('#src-nav .src[data-src="documents"]');
      await docRow(doc.id).click();
      await race.locator('#insp-title', { hasText: doc.title }).waitFor();
      await quiet(race);
      const shown = {
        sourceGlyph: (await docSource.locator('svg.g-review').count()) > 0,
        title: await docSource.getAttribute('title'),
        rowGlyph: (await docRow(doc.id).locator('svg.g-review').count()) > 0,
        row: await docRow(doc.id).locator('.r-sub').innerText(),
        inspectorGlyph: (await race.locator('#insp .i-state svg.g-review').count()) > 0,
        state: await race.locator('#insp .i-state .st').innerText(),
        renew: await race
          .locator('#insp')
          .getByRole('button', { name: 'Generate a new version', exact: true })
          .count(),
        flags: await race.locator('#insp .flag').allInnerTexts(),
        // the page's own bold words; a reason's markup is shown as text
        bold: await race.locator('#insp .flag b').count(),
      };
      assert.equal(await race.locator('#insp .flag img').count(), 0, 'no image from a reason');
      for (const f of await race.locator('#insp .flag').all())
        assert(await f.isVisible(), 'each reason in the inspector can be seen');
      texts.push(await race.locator('#app').innerText());
      await race.locator('#insp').getByRole('button', { name: 'Read draft', exact: true }).click();
      await race.locator('#dialog pre.document').waitFor();
      const viewFlagEls = race.locator('#dialog-content .flag');
      shown.view = await viewFlagEls.allInnerTexts();
      assert.equal(await race.locator('#dialog-content .flag :is(img, b)').count(), 0, 'as text');
      const textBox = await race.locator('#dialog pre.document').boundingBox();
      for (const f of await viewFlagEls.all()) {
        assert(await f.isVisible(), 'each reason in the full view can be seen');
        const box = await f.boundingBox();
        assert(box.y + box.height <= textBox.y, 'each reason sits above the draft’s text');
      }
      texts.push(await race.locator('#dialog').innerText());
      await race.getByRole('button', { name: 'Close dialog', exact: true }).click();
      await raceSource('today');
      const todayRow = race.locator(`#list .row[data-key="doc:${doc.id}"]`);
      shown.today = (await todayRow.count())
        ? await todayRow.first().locator('.r-sub').innerText()
        : null;
      texts.push(await race.locator('#list').innerText());
      if (related) {
        await raceSource('records');
        await rowIn(related.title).click();
        await race.locator('#insp-title', { hasText: related.title }).waitFor();
        const relatedSub = race.locator('#insp .att-row', { hasText: doc.title }).locator('.sub');
        shown.related = await relatedSub.innerText();
        // amber when the document needs review, as the line says
        shown.relatedAmber = await relatedSub.evaluate((e) => e.classList.contains('amber'));
        texts.push(await race.locator('#insp').innerText());
      }
      // the row and the full view agree on whether the draft is out of date
      assert.equal(
        shown.row !== 'Matches the current records',
        shown.view.length > 0,
        'the row and the full view agree',
      );
      return { shown, texts };
    };
    // no place states a reason of the page's own; the server's sentences are its own words
    const noPageReason = (texts, messages) => {
      for (let text of texts) {
        for (const m of messages) text = text.split(m).join('');
        for (const w of OLD_DOC_WORDS) assert(!text.includes(w), `the page says “${w}”`);
      }
    };
    const docCurrent = {
      sourceGlyph: false,
      title: null,
      rowGlyph: false,
      row: 'Matches the current records',
      inspectorGlyph: false,
      state: 'Draft',
      renew: 0,
      flags: [],
      bold: 0,
      view: [],
      today: null,
      related: 'Document · Draft',
      relatedAmber: false,
    };
    const docWithReasons = (messages) => ({
      sourceGlyph: true,
      title: 'Some drafts need review',
      rowGlyph: true,
      row: 'Needs review',
      inspectorGlyph: true,
      state: 'Needs review',
      renew: 1,
      flags: messages,
      bold: 0,
      view: messages,
      today: 'Draft · needs review',
      related: 'Document · Needs review',
      relatedAmber: true,
    });
    const docNoReason = {
      sourceGlyph: true,
      title: 'Some drafts are out of date',
      rowGlyph: true,
      row: 'Out of date',
      inspectorGlyph: true,
      state: 'Out of date',
      renew: 1,
      flags: [
        'This draft is out of date. This copy stays as it was generated. Generate a new version.',
      ],
      bold: 1,
      view: [DRAFT_CHANGED],
      today: 'Draft · out of date',
      related: 'Document · Out of date',
      relatedAmber: true,
    };
    const without = (o, ...keys) =>
      Object.fromEntries(Object.entries(o).filter(([k]) => !keys.includes(k)));
    lateCase('round 9 D: a current draft, then a record saved through the page', async () => {
      const name = 'Browser document checks, real server';
      const { id, doc } = await docBusiness(name);
      await showBusiness(id, name);
      const served = async () => (await snapshotOf(id)).objects.find((o) => o.id === doc.id);
      const fresh = await served();
      assert.equal(fresh.stale, false, 'the server says the draft is current');
      assert.deepEqual(fresh.stale_reasons, [], 'and lists no reason');
      const current = await docPlaces(doc, null);
      assert.deepEqual(
        current.shown,
        without(docCurrent, 'related', 'relatedAmber'),
        'a current draft',
      );
      noPageReason(current.texts, []);
      // a record saved through the page makes the draft out of date on the server
      await raceSource('records');
      await openRecordForm('Browser record saved after a draft was generated');
      await inDialog('Save record');
      await race.locator('#toast', { hasText: 'Saved to the workspace.' }).waitFor();
      await quiet(race);
      const marked = await served();
      assert.equal(marked.stale, true);
      assert.deepEqual(
        marked.stale_reasons.map((r) => [r.code, r.message]),
        [['source_changed', SOURCE_CHANGED]],
        'one reason, in the server’s words',
      );
      const related = await call(`/businesses/${id}/records`, 'POST', {
        kind: 'risk',
        title: 'Browser record related to a draft that is out of date',
        details: 'Synthetic record related to a draft.',
        related_ids: [doc.id],
      });
      assert.equal((await served()).stale_reasons.length, 1, 'still the one reason');
      await showBusiness(id, name);
      const changed = await docPlaces(doc, related);
      assert.deepEqual(changed.shown, docWithReasons([SOURCE_CHANGED]), 'a draft the server marks');
      noPageReason(changed.texts, [SOURCE_CHANGED]);
    });
    lateCase('round 9 D: every way a draft can be served, the same in every place', async () => {
      const name = 'Browser document checks, served';
      const { id, doc } = await docBusiness(name);
      const related = await call(`/businesses/${id}/records`, 'POST', {
        kind: 'risk',
        title: 'Browser record related to a served draft',
        details: 'Synthetic record related to a draft.',
        related_ids: [doc.id],
      });
      const first = {
        code: 'source_changed',
        message: 'Synthetic first reason, from the server.',
      };
      const hostile = {
        code: 'overdue_changed',
        as_of: '2030-06-01',
        today: '2030-06-02',
        message:
          'Synthetic second reason <img src=x onerror="window.injectedReason=true"> & “quoted” "plain" \'single\' <b>bold</b>',
      };
      let serve;
      await serveBusiness(id, (body) => {
        for (const d of body.objects.filter((o) => o.kind === 'document')) {
          delete d.stale;
          delete d.stale_reasons;
          Object.assign(d, serve);
        }
      });
      const states = [
        [
          'stale with two reasons',
          { stale: true, stale_reasons: [first, hostile] },
          docWithReasons([first.message, hostile.message]),
        ],
        ['stale with the list missing', { stale: true }, docNoReason],
        ['stale with an empty list', { stale: true, stale_reasons: [] }, docNoReason],
        [
          'stale with a list that is not a list',
          { stale: true, stale_reasons: 'not a list' },
          docNoReason,
        ],
        ['not stale with an empty list', { stale: false, stale_reasons: [] }, docCurrent],
        [
          'not stale with one reason',
          { stale: false, stale_reasons: [first] },
          docWithReasons([first.message]),
        ],
        [
          'stale with reasons that cannot be read',
          {
            stale: true,
            stale_reasons: [null, 'a string', 7, { code: 'no_message' }, { message: 5 }],
          },
          docNoReason,
        ],
        [
          'stale with one readable reason among unreadable ones',
          { stale: true, stale_reasons: [null, first, { code: 'no_message' }] },
          docWithReasons([first.message]),
        ],
      ];
      for (const [what, value, expected] of states) {
        serve = value;
        await showBusiness(id, name);
        const { shown, texts } = await docPlaces(doc, related);
        assert.deepEqual(shown, expected, what);
        noPageReason(texts, [first.message, hostile.message]);
      }
      assert.equal(
        await race.evaluate(() => Boolean(window.injectedReason)),
        false,
        'no reason ran a script',
      );
    });
    lateCase(
      'round 9 D: an open draft follows a reload that lists a reason and does not mark it stale',
      async () => {
        const doc = await newDraft();
        const reason = {
          code: 'source_changed',
          message: 'Synthetic reason, served with the draft not marked stale.',
        };
        let reasons = [];
        await serveChanged((body) => {
          Object.assign(
            body.objects.find((o) => o.id === doc.id),
            { stale: false, stale_reasons: reasons },
          );
        });
        await race.reload();
        await race.locator(OPENED).waitFor();
        await readDraft(doc);
        assert.deepEqual(await viewFlags(), [], 'no reason yet');
        reasons = [reason];
        await race.evaluate(() => refresh());
        await quiet(race);
        assert.deepEqual(await viewFlags(), [reason.message], 'the open view gives the reason');
        assert.equal(await docRow(doc.id).locator('.r-sub').innerText(), 'Needs review');
      },
    );
    // Round 10, F2. A deal analysis's or a forecast's reasons follow the documents' rule: a reason
    // the page cannot read is left out, and the others are shown in the server's order, as text.
    // The test business's deal analysis and forecasts are served with the reasons changed.
    const analysisPlaces = async () => {
      const snap = await snapshotOf(bid);
      const deal = snap.objects.find((o) => o.kind === 'deal');
      const plan = snap.objects.filter((o) => o.kind === 'financial_plan').at(-1);
      assert(deal && plan, 'the test business has a deal analysis and a forecast');
      const thrown = [];
      const onError = (e) => thrown.push(e.message);
      race.on('pageerror', onError);
      try {
        await race.reload();
        await race.locator(OPENED).waitFor();
        await onBusiness(bid, testName);
        const shown = {};
        // the page's own notes are neutral flags; the reasons are the others
        const flags = '#insp .flag:not(.neutral)';
        for (const [where, source, key, title] of [
          ['deal', 'deals', 'deal:' + deal.id, deal.title],
          ['forecast', 'money', 'plan:' + plan.id, plan.title],
          ['money', 'money', 'money:ending', 'Money position'],
        ]) {
          await raceSource(source);
          await race.locator(`#list .row[data-key="${key}"]`).click();
          await race
            .locator('#insp-title', { hasText: title })
            .waitFor()
            .catch(() => {
              throw new Error(`the ${where} view did not open`);
            });
          await quiet(race);
          shown[where] = {
            flags: await race.locator(flags).allInnerTexts(),
            markup: await race.locator(`${flags} :is(b, img)`).count(),
          };
        }
        shown.thrown = thrown;
        return shown;
      } finally {
        race.off('pageerror', onError);
      }
    };
    const inEachView = (messages) => ({
      deal: { flags: messages, markup: 0 },
      forecast: { flags: messages, markup: 0 },
      money: { flags: messages, markup: 0 },
      thrown: [],
    });
    lateCase(
      'round 10 F2: a deal analysis and a forecast served with reasons the page cannot read',
      async () => {
        const first = {
          code: 'evidence_changed',
          message: 'Synthetic first reason, from the server.',
        };
        const second = {
          code: 'jurisdiction_changed',
          message:
            'Synthetic second reason <b>bold</b> <img src=x onerror="window.injectedAnalysisReason=true"> & “quoted” "plain" \'single\'',
        };
        let served;
        await serveChanged((body) => {
          for (const o of body.objects.filter((x) => ['deal', 'financial_plan'].includes(x.kind)))
            Object.assign(o, served);
        });
        const unreadable = [
          null,
          'a string',
          7,
          { code: 'no_message' },
          { code: 'not_text', message: 5 },
        ];
        served = { stale: true, stale_reasons: [null, first, ...unreadable.slice(1), second] };
        assert.deepEqual(
          await analysisPlaces(),
          inEachView([
            'Synthetic first reason, from the server.',
            'Synthetic second reason <b>bold</b> <img src=x onerror="window.injectedAnalysisReason=true"> & “quoted” "plain" \'single\'',
          ]),
          'two reasons the page can read among five it cannot',
        );
        // marked for review, and no reason the page can read: one sentence, and no reason of its own
        served = { stale: true, stale_reasons: unreadable };
        assert.deepEqual(
          await analysisPlaces(),
          {
            deal: {
              flags: ['This deal analysis needs review. Check it before relying on its figures.'],
              markup: 0,
            },
            forecast: {
              flags: ['This forecast needs review. Check it before relying on its figures.'],
              markup: 0,
            },
            money: {
              flags: ['This forecast needs review. Check it before relying on its figures.'],
              markup: 0,
            },
            thrown: [],
          },
          'no reason the page can read',
        );
        // not marked, and no reason the page can read: nothing
        served = { stale: false, stale_reasons: unreadable };
        assert.deepEqual(await analysisPlaces(), inEachView([]), 'not marked for review');
        assert.equal(
          await race.evaluate(() => Boolean(window.injectedAnalysisReason)),
          false,
          'no reason ran a script',
        );
      },
    );
    lateCase(
      'round 10 F2: a deal analysis served with the two reasons the server can give together',
      async () => {
        const evidence = {
          code: 'evidence_changed',
          message:
            'Supporting evidence has changed since this deal analysis was saved. Run a revised analysis before relying on these figures.',
        };
        const jurisdiction = {
          code: 'jurisdiction_changed',
          saved_jurisdiction: 'undecided',
          current_jurisdiction: 'GB',
          message:
            'This deal analysis was saved while the planning jurisdiction was undecided. It is now GB. Check its assumptions, the tax rate among them, and run a revised analysis before relying on these figures.',
        };
        await serveChanged((body) => {
          for (const o of body.objects.filter((x) => x.kind === 'deal'))
            Object.assign(o, { stale: true, stale_reasons: [evidence, jurisdiction] });
        });
        const { deal, thrown } = await analysisPlaces();
        assert.deepEqual(
          { deal, thrown },
          {
            deal: {
              flags: [
                'Supporting evidence has changed since this deal analysis was saved. Run a revised analysis before relying on these figures.',
                'This deal analysis was saved while the planning jurisdiction was undecided. It is now GB. Check its assumptions, the tax rate among them, and run a revised analysis before relying on these figures.',
              ],
              markup: 0,
            },
            thrown: [],
          },
        );
      },
    );
    // Round 9, B. The backup command a desktop owner is shown. Only the desktop window's server adds
    // `desktop` to an owner's /api/me, and this test's server is `foundry serve`, so the field is
    // added on its way to the page. Without it, both places say what `foundry serve` always shows.
    lateCase('round 9 B: the backup command, in the panel and in the integrity view', async () => {
      const panel = race
        .locator('#insp details')
        .filter({ has: race.locator('summary', { hasText: 'Backup and restore' }) });
      const backups = race
        .locator('#insp .i-sec')
        .filter({ has: race.locator('h3', { hasText: /^Backups$/ }) });
      const paras = async (where) =>
        (await where.locator('p').allTextContents()).map((t) => t.trim());
      let field,
        withField = false;
      await race.route('**/api/me', async (route) => {
        const response = await route.fetch();
        const body = await response.json();
        await route.fulfill({ response, json: withField ? { ...body, desktop: field } : body });
      });
      // both places, as the page shows them after loading again with the field as it is now
      const shownWith = async (value, on = true) => {
        field = value;
        withField = on;
        await race.reload();
        await race.locator(OPENED).waitFor();
        await onBusiness(bid, testName);
        await raceSource('access');
        await race.locator('#list .row[data-key="ai:overview"]').click();
        await panel.waitFor({ state: 'attached' });
        const shown = {
          panel: await paras(panel),
          panelHtml: await panel.innerHTML(),
          panelCode: await panel.locator('code').allTextContents(),
          panelMarkup: await panel.locator('img, b').count(),
        };
        await raceSource('history');
        await race.locator('#list .row[data-key="integrity"]').click();
        await backups.waitFor();
        shown.integrity = await paras(backups);
        shown.integrityCode = await backups.locator('code').allTextContents();
        shown.integrityMarkup = await backups.locator('img, b').count();
        assert(
          ![...shown.panel, ...shown.integrity].some((t) => /\bundefined\b|\bnull\b/.test(t)),
          'neither place prints undefined or null',
        );
        return shown;
      };
      const PLAIN_PANEL =
        '<summary>Backup and restore</summary><p class="fine">Run <code>uv run foundry backup /private/path/backup.db</code>. Restore into a new database path with <code>uv run foundry --db /private/path/restored.db restore /private/path/backup.db</code>. Backups contain credential hashes and all businesses; protect them.</p>';
      const PLAIN_INTEGRITY = [
        'Run uv run foundry backup /private/path/backup.db. Backups hold every business and the credential hashes; keep them private.',
      ];
      const plain = async (what, value, on) => {
        const shown = await shownWith(value, on);
        assert.equal(shown.panelHtml, PLAIN_PANEL, `${what}: the panel, to the letter`);
        assert.deepEqual(shown.integrity, PLAIN_INTEGRITY, `${what}: the integrity view`);
      };
      // the ordinary server
      await plain('no field', undefined, false);
      const restoreText =
        'To restore, put the backup in a new folder: uv run foundry --db /private/path/restored/foundry.db restore /private/path/backup.db. Then open that folder in the app: uv run foundry-desktop --data-dir /private/path/restored.';
      const protectText = 'Backups contain credential hashes and all businesses; protect them.';
      const holdText = 'Backups hold every business and the credential hashes; keep them private.';
      const database = '/Users/founder/Library/Application Support/Business Foundry/foundry.db';
      const command = `uv run foundry --db '${database}' backup /private/path/backup.db`;
      const runText = (c) =>
        `To back them up, open a terminal in the folder you start Business Foundry from. Then run ${c}. Change /private/path/backup.db to where you want the backup saved.`;
      let shown = await shownWith({ database, backup_command: command });
      assert.deepEqual(shown.panel, [
        `Business Foundry keeps your records in ${database}.`,
        runText(command),
        restoreText,
        protectText,
      ]);
      assert.equal(shown.panelCode[1], command, 'the second code is the command');
      assert.deepEqual(shown.integrity, [
        `Business Foundry keeps your records in ${database}.`,
        runText(command),
        holdText,
      ]);
      assert.equal(shown.integrityCode[1], command, 'the integrity view gives the same command');
      // a path and a command that hold markup and both kinds of quote are shown as text
      const hostile =
        '/Users/a&b/"q" \'x\'/<img src=x onerror="window.injectedBackup=true"><b>bold</b>/foundry.db';
      const hostileCommand = `uv run foundry --db ${hostile} backup &amp; done`;
      shown = await shownWith({ database: hostile, backup_command: hostileCommand });
      assert.deepEqual(shown.panelCode.slice(0, 2), [hostile, hostileCommand]);
      assert.deepEqual(shown.integrityCode.slice(0, 2), [hostile, hostileCommand]);
      assert.equal(shown.panelMarkup, 0, 'no img or b element in the panel');
      assert.equal(shown.integrityMarkup, 0, 'none in the integrity view');
      // no ready command: null, the key missing, an empty string, or not a string
      const noCommand =
        'To back them up, open a terminal in the folder you start Business Foundry from. Then run uv run foundry --db DATABASE_PATH backup /private/path/backup.db. Put the path above, quoted for your shell, in place of DATABASE_PATH. Change /private/path/backup.db to where you want the backup saved.';
      for (const [what, value] of [
        ['a null command', { database, backup_command: null }],
        ['no command key', { database }],
        ['an empty command', { database, backup_command: '' }],
        ['a command that is not a string', { database, backup_command: 7 }],
      ]) {
        shown = await shownWith(value);
        assert.deepEqual(
          shown.panel,
          [
            `Business Foundry keeps your records in ${database}.`,
            noCommand,
            restoreText,
            protectText,
          ],
          `${what}: the panel`,
        );
        assert.deepEqual(
          shown.integrity,
          [`Business Foundry keeps your records in ${database}.`, noCommand, holdText],
          `${what}: the integrity view`,
        );
      }
      // a field that cannot be read is read as no field
      for (const [what, value] of [
        ['a field that is null', null],
        ['a field that is a string', 'a string'],
        ['a field that is a list', [database, command]],
        ['a database that is not a string', { database: 7, backup_command: command }],
        ['no database', { backup_command: command }],
        ['an empty database', { database: '', backup_command: command }],
      ])
        await plain(what, value, true);
      // the route let go: the ordinary panel again
      await race.unroute('**/api/me');
      await plain('the route removed', undefined, false);
      assert.equal(await race.evaluate(() => Boolean(window.injectedBackup)), false);
    });
    // Round 8, N2. A view of one item open across a reload of its own business.
    // that a request a hold() is holding reached the server; a page that never sends it fails here,
    // after the page's usual time limit, rather than leaving the test waiting
    const sent = async (held, what) => {
      let timer;
      try {
        return await Promise.race([
          held.requested,
          new Promise((_, fail) => {
            timer = setTimeout(() => fail(new Error(`${what} was never sent`)), 10000 * slowdown);
          }),
        ]);
      } finally {
        clearTimeout(timer);
      }
    };
    // a record saved, its answer held, and its form put away: releasing it reloads the records
    // while whatever was opened meanwhile stays open
    const saveHeld = async (title) => {
      await raceSource('records');
      const held = await hold(race, `/api/businesses/${bid}/records`, { method: 'POST' });
      await openRecordForm(title);
      await inDialog('Save record');
      await sent(held, 'the held request');
      await race.keyboard.press('Escape');
      return held;
    };
    lateCase('round 8 N2: an open draft learns that a reload made it stale', async () => {
      const doc = await newDraft();
      assert.equal(doc.stale ?? false, false);
      await raceSource('records');
      await openRecordForm('Browser record that makes an open draft stale');
      const reload = await hold(race, `/api/businesses/${bid}`, { method: 'GET' });
      await inDialog('Save record');
      await sent(reload, 'the reload');
      await readDraft(doc);
      assert.deepEqual(await viewFlags(), [], 'the records on the page still match the draft');
      const view = await race.locator('#dialog pre.document').elementHandle();
      const scroll = await race.evaluate(() => {
        const pre = $('#dialog pre.document');
        pre.scrollTop = 120;
        return [pre.scrollTop, $('#dialog').scrollTop, pre.scrollHeight > pre.clientHeight];
      });
      assert(scroll[2] && scroll[0] === 120, 'the draft’s text scrolls');
      await reload.release();
      assert.deepEqual(
        await viewFlags(),
        [SOURCE_CHANGED],
        'the open view says the draft is stale',
      );
      assert(await view.evaluate((el) => el.isConnected && $('#dialog').open), 'never closed');
      assert.deepEqual(
        await race.evaluate(() => [$('#dialog pre.document').scrollTop, $('#dialog').scrollTop]),
        scroll.slice(0, 2),
        'the reading position is where it was',
      );
      assert.equal(
        await race.locator('#dialog a', { hasText: 'Download Markdown' }).getAttribute('href'),
        `/api/businesses/${bid}/documents/${doc.id}/download`,
      );
      assert.equal(await docRow(doc.id).locator('.r-sub').innerText(), 'Needs review');
      await view.dispose();
      // opened again, it says the same
      await race.getByRole('button', { name: 'Close dialog', exact: true }).click();
      await race.locator('#insp').getByRole('button', { name: 'Read draft', exact: true }).click();
      await race.locator('#dialog pre.document').waitFor();
      assert.deepEqual(await viewFlags(), [SOURCE_CHANGED]);
    });
    lateCase(
      'round 8 N2: an open draft loses its warning when a reload says it matches',
      async () => {
        const doc = await newDraft();
        let stale = true;
        await serveChanged((body) => {
          // the save below makes the draft stale on the server; what is served lists no reason
          Object.assign(
            body.objects.find((o) => o.id === doc.id),
            { stale, stale_reasons: [] },
          );
        });
        await race.reload();
        await race.locator(OPENED).waitFor();
        const held = await saveHeld('Browser record saved while a stale draft is open');
        await readDraft(doc);
        assert.deepEqual(await viewFlags(), [DRAFT_CHANGED]);
        const link = race.locator('#dialog a', { hasText: 'Download Markdown' });
        await link.focus();
        const focusedLink = await link.elementHandle();
        stale = false;
        await held.release();
        assert.deepEqual(await viewFlags(), [], 'the warning is gone');
        assert(await race.evaluate(() => $('#dialog').open));
        assert(
          await focusedLink.evaluate((el) => el.isConnected && document.activeElement === el),
          'focus is on the same download link',
        );
        await focusedLink.dispose();
      },
    );
    lateCase(
      'round 8 N2: a view of a draft no longer in the records closes and says so',
      async () => {
        const doc = await newDraft();
        let gone = false;
        await serveChanged((body) => {
          if (gone) body.objects = body.objects.filter((o) => o.id !== doc.id);
        });
        const held = await saveHeld('Browser record saved while a draft leaves the records');
        await readDraft(doc);
        gone = true;
        await held.release();
        assert.equal(await dialogOpen(), false, 'the view closed');
        assert.equal(await race.locator('#dialog-content').innerHTML(), '');
        assert.equal(
          await toastText(),
          `Saved to the workspace. “${doc.title}” is no longer in the records, so its view was closed.`,
        );
      },
    );
    lateCase('round 8 N2: an invoice’s details follow a change made through the API', async () => {
      await onBusiness(bid, testName);
      const unreviewed = await call(`/businesses/${bid}/evidence`, 'POST', {
        title: 'Browser receipt for an invoice viewed while it is issued',
        content: 'Synthetic external receipt; nothing was sent.',
        source_type: 'external_receipt',
      });
      const receipt = await call(`/businesses/${bid}/evidence/${unreviewed.id}/review`, 'POST', {
        note: 'Synthetic review of a synthetic receipt.',
        expected_version: unreviewed.version,
      });
      const inv = await call(`/businesses/${bid}/invoices`, 'POST', {
        customer_id: customer,
        description: 'Browser invoice viewed while it is issued',
        amount: '40',
        due_date: '2027-03-01',
        contract_evidence_id: receipt.id,
        acceptance_evidence_id: receipt.id,
      });
      await race.reload();
      await race.locator(OPENED).waitFor();
      const held = await saveHeld('Browser record saved while invoice details are open');
      await raceSource('sales');
      await race.locator(`#list .row[data-key="inv:${inv.id}"]`).click();
      await race.locator('#insp').getByRole('button', { name: 'Details', exact: true }).click();
      const pre = race.locator('#dialog-content pre');
      assert.equal(JSON.parse(await pre.textContent()).issue_evidence_id, undefined);
      await call(`/businesses/${bid}/invoices/${inv.id}/issue`, 'POST', {
        evidence_id: receipt.id,
        note: 'Synthetic issue recorded while the details were open.',
        expected_version: inv.version,
      });
      await held.release();
      assert(await race.evaluate(() => $('#dialog').open), 'the view stayed open');
      assert.deepEqual(
        JSON.parse(await pre.textContent()),
        (await snapshotOf(bid)).objects.find((o) => o.id === inv.id).data,
        'it shows the invoice as the new records hold it',
      );
    });
    lateCase('round 8 N2: an open draft gains the server’s reasons in a reload', async () => {
      const doc = await newDraft();
      let reasons = null;
      await serveChanged((body) => {
        const d = body.objects.find((o) => o.id === doc.id);
        delete d.stale_reasons;
        d.stale = Boolean(reasons);
        if (reasons) d.stale_reasons = reasons;
      });
      await race.reload();
      await race.locator(OPENED).waitFor();
      const held = await saveHeld('Browser record saved while a draft gains reasons');
      await readDraft(doc);
      assert.deepEqual(await viewFlags(), []);
      reasons = [
        { code: 'synthetic_markup', message: 'Synthetic <i>reason</i> & a "quote".' },
        { code: 'obligations_overdue', message: 'The number of overdue obligations changed.' },
      ];
      await held.release();
      const messages = reasons.map((r) => r.message);
      assert.deepEqual(await viewFlags(), messages, 'the open view prints every reason, in order');
      assert.equal(await race.locator('#dialog-content .flag i').count(), 0, 'as text');
      // the words of a view opened now; the save moved the list to its record meanwhile
      await race.getByRole('button', { name: 'Close dialog', exact: true }).click();
      await readDraft(doc);
      assert.deepEqual(await viewFlags(), messages);
    });
    // Round 8. A change of sign-in in another tab of the same browser. Each case opens its own
    // browser context with two tabs on one session cookie: tab A signed in as the owner, tab B
    // opened on the same sign-in. The viewer used below sees only the workflow test business, so
    // the demo business's name is an owner's record that the viewer never sees.
    let tabsViewer = null;
    const viewerForTabs = async () =>
      (tabsViewer ||= await call('/principals', 'POST', {
        name: 'Browser viewer for other tabs',
        role: 'viewer',
        business_ids: [bid],
      }));
    const twoTabs = async (run, { bare = false } = {}) => {
      const viewer = await viewerForTabs();
      const tabs = await browser.newContext({ viewport: { width: 1440, height: 1050 } });
      try {
        const a = await newPage(tabs),
          b = await newPage(tabs);
        for (const p of [a, b]) p.on('pageerror', (e) => errors.push(e.message));
        // a browser without the signal: tab A's page script finds no BroadcastChannel
        if (bare) await a.addInitScript(() => delete window.BroadcastChannel);
        const me = { a: 0, b: 0 },
          refused = [];
        for (const [name, p] of [
          ['a', a],
          ['b', b],
        ])
          p.on('request', (r) => {
            if (new URL(r.url()).pathname === '/api/me') me[name] += 1;
          });
        a.on('response', (r) => {
          if (r.status() === 401)
            refused.push(`${r.request().method()} ${new URL(r.url()).pathname}`);
        });
        await a.goto(origin);
        await a.getByLabel('Access key', { exact: true }).fill(ownerKey());
        await a.getByRole('button', { name: 'Open workspace' }).click();
        await a.locator(OPENED).waitFor();
        await b.goto(origin);
        await b.locator(OPENED).waitFor();
        await quiet(a);
        await quiet(b);
        assert.equal(
          await a.evaluate(() => typeof BroadcastChannel),
          bare ? 'undefined' : 'function',
        );
        // counted from here: what each tab asks, and what tab A is refused, once both are open
        Object.assign(me, { a: 0, b: 0 });
        refused.length = 0;
        await run({ a, b, me, refused, viewer });
      } finally {
        await tabs.close();
      }
    };
    // a wait for what a tab shows once it has been told, which fails by saying what did not happen
    const told = (shown, what) =>
      shown.waitFor({ timeout: 10000 * slowdown }).catch(() => {
        throw new Error(what);
      });
    const signOutIn = async (p) => {
      await p.locator('.toolbar [data-action="logout"]').click();
      await p.getByLabel('Access key', { exact: true }).waitFor();
    };
    const signInAs = async (p, key) => {
      await p.getByLabel('Access key', { exact: true }).fill(key);
      await p.getByRole('button', { name: 'Open workspace' }).click();
      await p.locator(OPENED).waitFor();
    };
    const keyFormIn = async (p, name) => {
      await p.locator('#src-nav .src[data-src="access"]').click();
      await p.getByRole('button', { name: '+ Create access key', exact: true }).click();
      await p.getByLabel('Person or agent name', { exact: true }).fill(name);
    };
    // whether a secret is anywhere in a tab's page, or waiting in its memory to be shown
    const holdsIn = (p, secret) =>
      p.evaluate(
        (s) =>
          document.documentElement.innerHTML.includes(s) || waiting.some((w) => w.html.includes(s)),
        secret,
      );
    // what a tab holds that only the owner may see: the demo business, by name or id, anywhere in
    // the page or its memory, the list of access keys, and an owner's controls
    const ownerTraces = (p) =>
      p.evaluate(
        ([name, id]) => ({
          inPage: document.documentElement.innerHTML.includes(name),
          inMemory: JSON.stringify([state.businesses, state.snapshot, state.audit]).includes(id),
          principals: state.principals,
          newBusiness: !document.querySelector('[data-action="new-business"]').hidden,
          waiting: waiting.length,
        }),
        [demoName, demoBid],
      );
    const noOwnerTraces = {
      inPage: false,
      inMemory: false,
      principals: null,
      newBusiness: false,
      waiting: 0,
    };
    const CHANGED = 'The sign-in changed in another tab, so this page has started again.';
    lateCase(
      'round 8 N1: another tab signs out and a viewer signs in while a new key is held',
      async () => {
        await twoTabs(async ({ a, b, viewer }) => {
          const held = await hold(a, '/api/principals', { method: 'POST' });
          await keyFormIn(a, 'Browser key held while another tab changes the sign-in');
          await a
            .locator('#dialog')
            .getByRole('button', { name: 'Create key', exact: true })
            .click();
          const secret = (await (await sent(held, 'the held request')).json()).token;
          assert(secret, 'the server made the key');
          await signOutIn(b);
          // tab A was told: it forgot the owner's session, as a sign-out does in it
          await told(
            a.getByLabel('Access key', { exact: true }),
            'tab A was not told of the sign-out in tab B',
          );
          await quiet(a);
          assert.deepEqual(await pageForgotten(a), blankPage, 'tab A forgot the owner’s session');
          assert.equal(
            await a.locator('#login-note').innerText(),
            `${CHANGED} “Create scoped access” was closed after it was sent; check whether it was saved before entering it again.`,
          );
          // a key typed into tab A's sign-in field and never sent
          await a.getByLabel('Access key', { exact: true }).fill('synthetic-key-typed-never-sent');
          await signInAs(b, viewer.token);
          // tab A follows the new sign-in, and holds nothing of the owner's
          await told(
            a.locator('#actor-name', { hasText: viewer.name }),
            'tab A did not follow the sign-in in tab B',
          );
          await quiet(a);
          assert.deepEqual(await ownerTraces(a), noOwnerTraces, 'before the key’s answer');
          assert.equal(await a.locator('#toast').innerText(), CHANGED);
          assert.equal(
            await a.locator('#login-form input').inputValue(),
            '',
            'the key typed into tab A and never sent is not kept behind the workspace',
          );
          await held.release();
          assert.equal(await a.locator('#dialog').evaluate((d) => d.open), false, 'no dialog');
          assert(!(await holdsIn(a, secret)), 'the key is nowhere in tab A');
          assert.equal(await a.evaluate(() => state.actor.id), viewer.id, 'tab A shows the viewer');
          assert.deepEqual(await ownerTraces(a), noOwnerTraces, 'after the key’s answer');
        });
      },
    );
    lateCase('round 8 N1: without the signal, the key check alone keeps the key off', async () => {
      await twoTabs(
        async ({ a, b, me, viewer }) => {
          const held = await hold(a, '/api/principals', { method: 'POST' });
          await keyFormIn(a, 'Browser key held in a tab that hears nothing');
          await a
            .locator('#dialog')
            .getByRole('button', { name: 'Create key', exact: true })
            .click();
          const secret = (await (await sent(held, 'the held request')).json()).token;
          await signOutIn(b);
          await signInAs(b, viewer.token);
          await quiet(a);
          // tab A was not told; it still shows the owner's records
          assert.equal(me.a, 0, 'tab A has not asked who is signed in');
          assert(await a.locator('#app').isVisible());
          await held.release();
          await told(
            a.locator('#actor-name', { hasText: viewer.name }),
            'the key check did not find that someone else is signed in',
          );
          await quiet(a);
          assert.equal(me.a, 2, 'the key check asked, then the page started again');
          assert.equal(await a.locator('#dialog').evaluate((d) => d.open), false, 'no dialog');
          assert(!(await holdsIn(a, secret)), 'the key is nowhere in tab A');
          assert.deepEqual(await ownerTraces(a), noOwnerTraces, 'the owner’s records are gone');
          assert.equal(await a.locator('#toast').innerText(), CHANGED);
        },
        { bare: true },
      );
    });
    lateCase('round 8 N1: someone else with the same role does not get the key', async () => {
      const other = await call('/principals', 'POST', {
        name: 'Browser second owner for other tabs',
        role: 'owner',
        business_ids: [bid],
      });
      await twoTabs(
        async ({ a, b }) => {
          const ownerId = await a.evaluate(() => state.actor.id);
          assert.notEqual(other.id, ownerId, 'two owners, one role');
          const held = await hold(a, '/api/principals', { method: 'POST' });
          await keyFormIn(a, 'Browser key held while another owner signs in');
          await a
            .locator('#dialog')
            .getByRole('button', { name: 'Create key', exact: true })
            .click();
          const secret = (await (await sent(held, 'the key creation')).json()).token;
          await signOutIn(b);
          await signInAs(b, other.token);
          await held.release();
          await told(
            a.locator('#actor-name', { hasText: other.name }),
            'the key check did not find that someone else is signed in',
          );
          await quiet(a);
          assert.equal(await a.locator('#dialog').evaluate((d) => d.open), false, 'no dialog');
          assert(!(await holdsIn(a, secret)), 'the key is nowhere in tab A');
          assert.equal(await a.evaluate(() => state.actor.id), other.id);
        },
        { bare: true },
      );
    });
    lateCase('round 8 N1: another tab signs out and nobody signs in', async () => {
      await twoTabs(async ({ a, b, refused }) => {
        const held = await hold(a, '/api/principals', { method: 'POST' });
        await keyFormIn(a, 'Browser key held while another tab signs out');
        await a.locator('#dialog').getByRole('button', { name: 'Create key', exact: true }).click();
        const secret = (await (await sent(held, 'the held request')).json()).token;
        await signOutIn(b);
        await told(
          a.getByLabel('Access key', { exact: true }),
          'tab A was not told of the sign-out in tab B',
        );
        await quiet(a);
        // the only request of tab A's refused is the question the signal led it to ask
        assert.deepEqual(refused, ['GET /api/me'], 'tab A learned of it from the signal');
        assert.deepEqual(await pageForgotten(a), blankPage);
        await held.release();
        assert.equal(await a.locator('#dialog').evaluate((d) => d.open), false, 'no dialog');
        assert(!(await holdsIn(a, secret)), 'the held key was dropped');
        assert.deepEqual(await pageForgotten(a), blankPage, 'and tab A still holds nothing');
      });
    });
    lateCase(
      'round 8 N1: a form open when another tab signs out is gone, and said so',
      async () => {
        await twoTabs(async ({ a, b }) => {
          const typed = 'Browser record typed in a tab another tab signs out';
          await a.locator('#src-nav .src[data-src="records"]').click();
          await a.getByRole('button', { name: '+ Add record', exact: true }).click();
          await a.getByLabel('Title', { exact: true }).fill(typed);
          await a.getByLabel('Details', { exact: true }).fill('Synthetic details never sent.');
          await signOutIn(b);
          await told(
            a.getByLabel('Access key', { exact: true }),
            'tab A was not told of the sign-out in tab B',
          );
          await quiet(a);
          assert.equal(
            await a.locator('#login-note').innerText(),
            `${CHANGED} “Add business record” was closed without saving.`,
          );
          assert.equal(await a.locator('#dialog').evaluate((d) => d.open), false);
          assert.equal(await a.locator('#dialog-content').innerHTML(), '');
          assert.equal(
            await a.evaluate((t) => document.documentElement.innerHTML.includes(t), typed),
            false,
            'nothing of the form’s content is left',
          );
          assert.equal(await recordsIn(bid, typed), 0, 'and nothing was saved');
        });
      },
    );
    lateCase(
      'round 8 N1: one sign-out sends one question to each other tab, and no more',
      async () => {
        await twoTabs(async ({ a, b, me }) => {
          assert.deepEqual(me, { a: 0, b: 0 }, 'counted from here');
          await signOutIn(b);
          await told(
            a.getByLabel('Access key', { exact: true }),
            'tab A was not told of the sign-out in tab B',
          );
          await quiet(a);
          await quiet(b);
          assert.deepEqual(me, { a: 1, b: 0 }, 'tab A asked once; tab B did not hear itself');
          await quiet(a);
          await quiet(b);
          assert.deepEqual(me, { a: 1, b: 0 }, 'the count has settled');
          // and a sign-in: tab B asks as it starts, tab A once when told
          await signInAs(b, ownerKey());
          await told(a.locator(OPENED), 'tab A did not follow the sign-in in tab B');
          await quiet(a);
          await quiet(b);
          await quiet(a);
          assert.deepEqual(me, { a: 2, b: 1 }, 'after the sign-in, still one question each');
        });
      },
    );
    lateCase(
      'round 8 N1: a message of any other shape on the channel changes nothing',
      async () => {
        await twoTabs(async ({ a, b, me }) => {
          const before = await a.evaluate(() => [state.loaded, state.actor.id, state.bid]);
          // a listener of the test's own in tab A, opened after the page's, hears each message after it
          await a.evaluate(() => {
            window.__heard = [];
            new BroadcastChannel('foundry-session').onmessage = (e) => window.__heard.push(e.data);
          });
          const shapes = [
            {},
            null,
            7,
            'sign-in',
            'SIGN-IN CHANGED',
            ['sign-in changed'],
            { signal: 'sign-in changed', token: 'synthetic' },
          ];
          await b.evaluate((list) => {
            const c = new BroadcastChannel('foundry-session');
            for (const m of list) c.postMessage(m);
            c.close();
          }, shapes);
          await a.waitForFunction((n) => window.__heard.length === n, shapes.length);
          await quiet(a);
          await quiet(b);
          assert.deepEqual(me, { a: 0, b: 0 }, 'no tab asked who is signed in');
          assert.deepEqual(
            await a.evaluate(() => [state.loaded, state.actor.id, state.bid]),
            before,
          );
          assert(await a.locator('#app').isVisible());
          // the same channel, with the page's own word, is heard: the case above was not deaf
          await b.evaluate(() => {
            const c = new BroadcastChannel('foundry-session');
            c.postMessage('sign-in changed');
            c.close();
          });
          await a.waitForFunction((n) => window.__heard.length === n, shapes.length + 1);
          await a.waitForFunction(() => state.loaded > 0);
          await quiet(a);
          assert.equal(me.a, 1, 'the signal itself makes tab A ask again');
        });
      },
    );
    lateCase('round 8 N1: a session the server ends in one tab ends in the other', async () => {
      await twoTabs(async ({ a, b, me }) => {
        const refusedB = [];
        b.on('response', (r) => {
          if (r.status() === 401)
            refusedB.push(`${r.request().method()} ${new URL(r.url()).pathname}`);
        });
        await a.context().clearCookies();
        await a.locator('#src-nav .src[data-src="history"]').click();
        await a.getByLabel('Access key', { exact: true }).waitFor();
        await told(
          b.getByLabel('Access key', { exact: true }),
          'tab B was not told that the session ended in tab A',
        );
        await quiet(a);
        await quiet(b);
        assert.deepEqual(refusedB, ['GET /api/me'], 'tab B learned of it from the signal');
        assert.deepEqual(await pageForgotten(b), blankPage, 'tab B forgot the session');
        assert.equal(await b.locator('#login-note').innerText(), CHANGED);
        assert.deepEqual(me, { a: 0, b: 1 }, 'tab A did not hear its own signal');
      });
    });
    lateCase(
      'round 8 N1: a sign-out held in one tab is cancelled when another signs in',
      async () => {
        await twoTabs(async ({ a, b }) => {
          const held = await hold(b, '/api/logout', { method: 'POST' });
          await b.locator('.toolbar [data-action="logout"]').click();
          // the server has ended the session; a third tab opens on the sign-in page and signs in
          await sent(held, 'the held request');
          const c = await newPage(a.context());
          c.on('pageerror', (e) => errors.push(e.message));
          await c.goto(origin);
          await signInAs(c, ownerKey());
          const session = async () =>
            (await a.context().cookies(origin)).find((x) => x.name === 'foundry_session')?.value;
          const signedIn = await session();
          assert(signedIn, 'the new sign-in has a session cookie');
          // tab B was told, and shows the new sign-in's workspace
          await told(b.locator(OPENED), 'tab B was not told of the sign-in in tab C');
          await held.release();
          assert(held.cancelled(), 'tab B cancelled its sign-out when it was told');
          // compared, never printed
          assert((await session()) === signedIn, 'the new session keeps its cookie');
          await quiet(c);
          assert(await c.locator('#app').isVisible(), 'the tab that signed in stays signed in');
        });
      },
    );
    // R2 on one tab: before a new key is shown, the server is asked who is signed in
    lateCase('round 8 N1: the same person, nothing changed: the key is shown once', async () => {
      await onBusiness(bid, testName);
      const heldMe = await hold(race, '/api/me', { method: 'GET' });
      const heldKey = await holdKey();
      await openKeyForm('Browser key shown after the check');
      await inDialog('Create key');
      const secret = await keyFrom(heldKey);
      await heldKey.release();
      await sent(heldMe, 'the check of who is signed in');
      await quiet(race);
      assert.notEqual(await dialogTitle(), 'Save this access key', 'not before the check answers');
      assert(!(await race.evaluate(() => $('#dialog').open)), 'nothing is open yet');
      await heldMe.release();
      await race.locator('#dialog-title', { hasText: 'Save this access key' }).waitFor();
      assert(await shownKeyIs(secret), 'the key shown is the one the server made');
      await race.getByRole('button', { name: 'Close dialog', exact: true }).click();
      await quiet(race);
      assert.equal(await dialogOpen(), false, 'the key is shown once');
      assert(!(await pageHolds(secret)), 'and left nothing behind');
    });
    lateCase(
      'round 8 N1: a dialog opened during the check: the key waits and is checked again',
      async () => {
        await onBusiness(bid, testName);
        const first = await hold(race, '/api/me', { method: 'GET' });
        const made = race.waitForResponse(
          (r) => r.request().method() === 'POST' && new URL(r.url()).pathname === '/api/principals',
        );
        await openKeyForm('Browser key checked again after a form');
        await inDialog('Create key');
        const secret = (await (await made).json()).token;
        await sent(first, 'the check of who is signed in');
        await raceSource('records');
        await openRecordForm('Browser record opened during a key check');
        await first.release();
        assert.equal(await dialogTitle(), 'Add business record', 'the key waits for the form');
        const second = await hold(race, '/api/me', { method: 'GET' });
        await inDialog('Cancel');
        await sent(second, 'the second check of who is signed in');
        await quiet(race);
        assert.equal(await dialogOpen(), false, 'not shown until it is checked again');
        await second.release();
        await race.locator('#dialog-title', { hasText: 'Save this access key' }).waitFor();
        assert(await shownKeyIs(secret));
        await race.getByRole('button', { name: 'Close dialog', exact: true }).click();
      },
    );
    lateCase(
      'round 8 N1: when the check cannot reach the server, a notice waits its turn',
      async () => {
        await onBusiness(bid, testName);
        const name = 'Browser key whose check could not be made';
        const heldMe = await hold(race, '/api/me', { method: 'GET', lose: true });
        const made = race.waitForResponse(
          (r) => r.request().method() === 'POST' && new URL(r.url()).pathname === '/api/principals',
        );
        await openKeyForm(name);
        await inDialog('Create key');
        const secret = (await (await made).json()).token;
        await sent(heldMe, 'the check of who is signed in');
        // the key form has closed; another form is opened while the check waits
        await raceSource('records');
        const typed = 'Browser record typed while a key check waited';
        await openRecordForm(typed);
        await heldMe.release();
        assert.equal(
          await dialogTitle(),
          'Add business record',
          'the notice does not replace a form',
        );
        assert.equal(await race.getByLabel('Title', { exact: true }).inputValue(), typed);
        assert(!(await pageHolds(secret)), 'the key is not on the page or waiting');
        await inDialog('Cancel');
        await race.locator('#dialog-title', { hasText: 'Access key not shown' }).waitFor();
        assert.equal(
          await race.locator('#dialog-content p').innerText(),
          `This page could not confirm who is signed in, so it cannot show the access key “${name}”. The key was created and cannot be shown again. Look at the access keys under Access & assistants, revoke the key named “${name}”, then create a new one.`,
        );
        assert(!(await pageHolds(secret)), 'the notice holds no key');
        assert.equal((await principalsNamed(name)).length, 1, 'the server made the key');
        await inDialog('Close');
      },
    );
    // Round 9, X1. Two starts in one session of a tab: tab B, on the sign-in page, is told that the
    // sign-in changed and asks who is signed in; before that answer arrives, a person signs in in
    // tab B itself, and it asks again. Only the latest start acts, whatever order the answers
    // arrive in. Each answer is held by holdNext(), so no case waits a fixed time.
    // Hold back only the next answer for a path until release(); later requests pass untouched. With
    // `lose`, release() drops the connection instead. quiet() does not wait for that path meanwhile.
    const holdNext = async (p, path, { lose = false } = {}) => {
      let open, seen, done;
      const gate = new Promise((r) => (open = r)),
        requested = new Promise((r) => (seen = r)),
        handled = new Promise((r) => (done = r));
      let taken = false;
      const entry = { path, method: 'GET' };
      heldOn(p).add(entry);
      await p.route(
        (url) => url.pathname === path,
        async (route) => {
          if (taken || route.request().method() !== 'GET') return route.fallback();
          taken = true;
          const response = await route.fetch();
          seen(response);
          await gate;
          try {
            if (lose) await route.abort('connectionreset');
            else await route.fulfill({ response });
          } catch {
            // the page cancelled the request while it was held
          }
          done();
        },
      );
      return {
        requested,
        async release() {
          heldOn(p).delete(entry);
          open();
          await handled;
          await quiet(p);
        },
      };
    };
    // the signal alone, from another tab, as a tab sends it: the sign-in itself does not change
    const bareSignal = (p) =>
      p.evaluate(() => new BroadcastChannel('foundry-session').postMessage('sign-in changed'));
    // what a tab shows: the sign-in page or a workspace, whose, and anything it says
    const tabShows = async (p) => ({
      signInPage: await p.locator('#login').isVisible(),
      workspace: await p.locator('#app').isVisible(),
      name: await p.locator('#actor-name').innerText(),
      actor: await p.evaluate(() => state.actor?.id ?? null),
      newBusiness: await p.locator('.toolbar [data-action="new-business"]').isVisible(),
      note: await p.locator('#login-note').innerText(),
      error: await p.locator('#login-error').innerText(),
      toast: (await p.locator('#toast').isVisible()) ? await p.locator('#toast').innerText() : null,
    });
    // the owner's workspace, as the server names the person signed in in that tab
    const ownerWorkspace = async (p) => {
      const me = await (await p.context().request.get(origin + '/api/me')).json();
      assert.equal(me.role, 'owner', 'the server says the owner is signed in');
      return {
        signInPage: false,
        workspace: true,
        name: `${me.name} · Owner`,
        actor: me.id,
        newBusiness: true,
        note: '',
        error: '',
        toast: null,
      };
    };
    const signedOutPage = {
      signInPage: true,
      workspace: false,
      name: '',
      actor: null,
      newBusiness: false,
      note: '',
      error: '',
      toast: null,
    };
    // both tabs on the sign-in page, nobody signed in
    const bothSignedOut = async (a, b) => {
      await signOutIn(b);
      await told(a.getByLabel('Access key', { exact: true }), 'tab A was not told of the sign-out');
      await quiet(a);
      await quiet(b);
    };
    const typeKey = async (p, key) => {
      await p.getByLabel('Access key', { exact: true }).fill(key);
      await p.getByRole('button', { name: 'Open workspace' }).click();
    };
    lateCase(
      'round 9 X1 A: an earlier answer naming someone else arrives after this tab’s own sign-in',
      async () => {
        await twoTabs(async ({ a, b, viewer }) => {
          await bothSignedOut(a, b);
          const first = await holdNext(b, '/api/me');
          // tab A's sign-in as the viewer is the signal; tab B's question about it is held
          await signInAs(a, viewer.token);
          const named = await (await sent(first, 'tab B’s question after the signal')).json();
          assert.equal(named.role, 'viewer');
          await signInAs(b, ownerKey());
          await quiet(b);
          await first.release();
          assert.deepEqual(await tabShows(b), await ownerWorkspace(b), 'tab B shows the owner');
        });
      },
    );
    lateCase(
      'round 9 X1 B: an earlier refusal arrives after this tab’s own sign-in has shown the workspace',
      async () => {
        await twoTabs(async ({ a, b, viewer }) => {
          await bothSignedOut(a, b);
          const afterSignIn = await holdNext(b, '/api/me');
          await signInAs(a, viewer.token);
          await sent(afterSignIn, 'tab B’s question after the sign-in in tab A');
          const afterSignOut = await holdNext(b, '/api/me');
          await signOutIn(a);
          const refusal = await sent(afterSignOut, 'tab B’s question after the sign-out in tab A');
          assert.equal(refusal.status(), 401);
          // this one's session ended with the second signal
          await afterSignIn.release();
          assert.deepEqual(await tabShows(b), signedOutPage, 'tab B is still on the sign-in page');
          await signInAs(b, ownerKey());
          await quiet(b);
          await afterSignOut.release();
          assert.deepEqual(await tabShows(b), await ownerWorkspace(b), 'tab B shows the owner');
        });
      },
    );
    lateCase(
      'round 9 X1 C: an earlier refusal arrives between this tab’s accepted sign-in and its answer',
      async () => {
        await twoTabs(async ({ a, b, viewer }) => {
          await bothSignedOut(a, b);
          const afterSignIn = await holdNext(b, '/api/me');
          await signInAs(a, viewer.token);
          await sent(afterSignIn, 'tab B’s question after the sign-in in tab A');
          const afterSignOut = await holdNext(b, '/api/me');
          await signOutIn(a);
          await sent(afterSignOut, 'tab B’s question after the sign-out in tab A');
          await afterSignIn.release();
          const own = await holdNext(b, '/api/me');
          await typeKey(b, ownerKey());
          const named = await (await sent(own, 'tab B’s question after its own sign-in')).json();
          assert.equal(named.role, 'owner');
          // the refusal, asked before the sign-in, arrives first
          await afterSignOut.release();
          await own.release();
          assert.deepEqual(await tabShows(b), await ownerWorkspace(b), 'tab B shows the owner');
        });
      },
    );
    lateCase('round 9 X1: two answers arriving in the order they were asked', async () => {
      await twoTabs(async ({ a, b, viewer }) => {
        await bothSignedOut(a, b);
        const first = await holdNext(b, '/api/me');
        await signInAs(a, viewer.token);
        await sent(first, 'tab B’s question after the signal');
        const own = await holdNext(b, '/api/me');
        await typeKey(b, ownerKey());
        await sent(own, 'tab B’s question after its own sign-in');
        await first.release();
        await own.release();
        const { toast, ...shown } = await tabShows(b);
        const { toast: none, ...owner } = await ownerWorkspace(b);
        assert.deepEqual(shown, owner, 'tab B shows the owner');
      });
    });
    lateCase(
      'round 9 X1: an earlier answer shown while this tab’s sign-in is sent is cleared before the next person’s records',
      async () => {
        await twoTabs(async ({ a, b, viewer }) => {
          await bothSignedOut(a, b);
          const first = await holdNext(b, '/api/me');
          await signInAs(a, viewer.token);
          await sent(first, 'tab B’s question after the signal');
          const login = await hold(b, '/api/login', { method: 'POST' });
          await typeKey(b, ownerKey());
          await sent(login, 'tab B’s sign-in');
          // the earlier answer arrives while the sign-in is on its way: it is still the latest
          await first.release();
          assert.equal(await b.locator('#actor-name').innerText(), `${viewer.name} · Viewer`);
          const records = await holdNext(b, '/api/businesses');
          await login.release();
          // the owner is named; the viewer's records are gone before the owner's arrive
          const between = await b.evaluate(() => ({
            actor: state.actor?.role,
            snapshot: state.snapshot,
            businesses: state.businesses.length,
            name: document.querySelector('#actor-name').textContent,
            list: document.querySelector('#list').innerHTML,
          }));
          assert.deepEqual(between, {
            actor: 'owner',
            snapshot: null,
            businesses: 0,
            name: '',
            list: '',
          });
          await records.release();
          assert.deepEqual(await tabShows(b), await ownerWorkspace(b), 'tab B shows the owner');
        });
      },
    );
    lateCase(
      'round 9 X1: a sentence about another tab survives a second change before it is said',
      async () => {
        await twoTabs(async ({ a, b, viewer }) => {
          await b.locator('#src-nav .src[data-src="records"]').click();
          await b.getByRole('button', { name: '+ Add record', exact: true }).click();
          await b.getByLabel('Title', { exact: true }).fill('Browser record form open in tab B');
          const first = await holdNext(b, '/api/me');
          // the first change closes tab B's form; its answer is held
          await signOutIn(a);
          assert.equal((await sent(first, 'tab B’s first question')).status(), 401);
          // the second is answered at once, and tab B follows it
          await signInAs(a, viewer.token);
          await told(
            b.locator('#actor-name', { hasText: viewer.name }),
            'tab B did not follow the sign-in in tab A',
          );
          await quiet(b);
          const sentence = `${CHANGED} “Add business record” was closed without saving.`;
          assert.equal(await b.locator('#toast').innerText(), sentence, 'the form is still named');
          // the first answer, for a session that has ended, changes nothing
          await first.release();
          assert.equal(await b.locator('#actor-name').innerText(), `${viewer.name} · Viewer`);
          assert(await b.locator('#login').isHidden(), 'tab B still shows the workspace');
        });
      },
    );
    lateCase(
      'round 9 X1: a sign-out in this tab drops the sentence about another tab, even when a signal overtakes it',
      async () => {
        await twoTabs(async ({ a, b }) => {
          const first = await holdNext(b, '/api/me');
          await bareSignal(a);
          await sent(first, 'tab B’s question after the signal');
          // tab B signs itself out; before that answer arrives, another signal reaches it
          const signOut = await hold(b, '/api/logout', { method: 'POST' });
          await b.locator('.toolbar [data-action="logout"]').click();
          await sent(signOut, 'tab B’s sign-out');
          await bareSignal(a);
          await b.getByLabel('Access key', { exact: true }).waitFor();
          await signOut.release();
          await first.release();
          assert.deepEqual(await tabShows(b), signedOutPage, 'no sentence about another tab');
        });
      },
    );
    lateCase(
      'round 9 G8: an answer for a start whose session this tab ended changes nothing',
      async () => {
        await twoTabs(async ({ a, b }) => {
          const answer = await holdNext(b, '/api/me');
          await bareSignal(a);
          await sent(answer, 'tab B’s question after the signal');
          // tab B signs itself out before the answer arrives: a new session, and no new start
          await signOutIn(b);
          await quiet(b);
          const asked = [];
          b.on('request', (r) => asked.push(new URL(r.url()).pathname));
          await answer.release();
          assert.deepEqual(await tabShows(b), signedOutPage, 'tab B stays signed out');
          assert.deepEqual(asked, [], 'and asks nothing more');
        });
      },
    );
    lateCase(
      'round 9 G8: a failed answer for a start whose session this tab ended changes nothing',
      async () => {
        await twoTabs(async ({ a, b }) => {
          const answer = await holdNext(b, '/api/me', { lose: true });
          await bareSignal(a);
          await sent(answer, 'tab B’s question after the signal');
          await signOutIn(b);
          await quiet(b);
          await answer.release();
          const shown = await tabShows(b);
          assert.deepEqual(shown, signedOutPage, `no error and no note: ${JSON.stringify(shown)}`);
        });
      },
    );
    // Round 10, T. Rules of round 9 that no case held.
    lateCase(
      'round 10 T: an earlier answer naming a namesake with the same role is cleared before the owner’s records',
      async () => {
        const ownerMe = await (await context.request.get(`${origin}/api/me`)).json();
        const namesake = await call('/principals', 'POST', {
          name: ownerMe.name,
          role: 'owner',
          business_ids: [bid],
        });
        assert.notEqual(namesake.id, ownerMe.id, 'two owners, one name');
        await twoTabs(async ({ a, b }) => {
          await bothSignedOut(a, b);
          const first = await holdNext(b, '/api/me');
          await signInAs(a, namesake.token);
          const named = await (await sent(first, 'tab B’s question after the signal')).json();
          assert.deepEqual(
            [named.id, named.name, named.role],
            [namesake.id, ownerMe.name, 'owner'],
          );
          const login = await hold(b, '/api/login', { method: 'POST' });
          await typeKey(b, ownerKey());
          await sent(login, 'tab B’s sign-in');
          // the namesake's answer arrives while the sign-in is on its way: it is still the latest
          await first.release();
          assert.equal(await b.locator('#actor-name').innerText(), `${ownerMe.name} · Owner`);
          assert.equal(await b.evaluate(() => state.actor.id), namesake.id);
          const records = await holdNext(b, '/api/businesses');
          await login.release();
          // the same name and role, another person: the namesake's records go first
          const between = await b.evaluate(() => ({
            actor: state.actor?.id,
            snapshot: state.snapshot,
            businesses: state.businesses.length,
            name: document.querySelector('#actor-name').textContent,
            list: document.querySelector('#list').innerHTML,
          }));
          assert.deepEqual(between, {
            actor: ownerMe.id,
            snapshot: null,
            businesses: 0,
            name: '',
            list: '',
          });
          await records.release();
          assert.deepEqual(await tabShows(b), await ownerWorkspace(b), 'tab B shows the owner');
        });
      },
    );
    lateCase(
      'round 10 T: a first load that fails after an earlier answer named someone else shows the sign-in page',
      async () => {
        await twoTabs(async ({ a, b, viewer }) => {
          await bothSignedOut(a, b);
          const first = await holdNext(b, '/api/me');
          await signInAs(a, viewer.token);
          await sent(first, 'tab B’s question after the signal');
          const login = await hold(b, '/api/login', { method: 'POST' });
          await typeKey(b, ownerKey());
          await sent(login, 'tab B’s sign-in');
          await first.release();
          assert.equal(await b.locator('#actor-name').innerText(), `${viewer.name} · Viewer`);
          // the owner is named, the viewer is forgotten, and the owner's first load is lost
          const load = await dropNext(b, '/api/businesses');
          await login.release();
          await load.dropped;
          assert.deepEqual(
            await tabShows(b),
            { ...signedOutPage, error: 'The server could not be reached.' },
            'the sign-in page says why, as when the page starts',
          );
        });
      },
    );
    lateCase(
      'round 10 T: a refusal after two changes still says the sign-in changed, when the tab showed a workspace',
      async () => {
        await twoTabs(async ({ a, b }) => {
          const first = await holdNext(b, '/api/me');
          // the first change: tab B, showing the owner's workspace, asks who is signed in
          await bareSignal(a);
          await sent(first, 'tab B’s first question');
          // the second: tab A signs out, and tab B's question about it is refused
          await signOutIn(a);
          await told(b.getByLabel('Access key', { exact: true }), 'tab B was not told');
          await quiet(b);
          assert.deepEqual(await tabShows(b), { ...signedOutPage, note: CHANGED });
          // the first answer, for a session that has ended, changes nothing
          await first.release();
          assert.deepEqual(await tabShows(b), { ...signedOutPage, note: CHANGED });
        });
      },
    );
    lateCase(
      'round 10 T: a closed form is named once; the next change of sign-in does not name it again',
      async () => {
        await twoTabs(async ({ a, b }) => {
          await b.locator('#src-nav .src[data-src="records"]').click();
          await b.getByRole('button', { name: '+ Add record', exact: true }).click();
          await b.getByLabel('Title', { exact: true }).fill('Browser record form open, named once');
          await bareSignal(a);
          await told(b.locator('#toast', { hasText: CHANGED }), 'tab B did not say so');
          await quiet(b);
          assert.equal(
            await b.locator('#toast').innerText(),
            `${CHANGED} “Add business record” was closed without saving.`,
          );
          await bareSignal(a);
          await b.waitForFunction(() => document.querySelector('#toast').hidden);
          await told(b.locator('#toast', { hasText: CHANGED }), 'tab B did not say so again');
          await quiet(b);
          assert.equal(await b.locator('#toast').innerText(), CHANGED, 'the form is not named');
        });
      },
    );
    lateCase(
      'round 10 T: a load for an earlier start that ends after this tab’s own sign-in says nothing',
      async () => {
        await twoTabs(async ({ a, b }) => {
          await bothSignedOut(a, b);
          const first = await holdNext(b, '/api/me');
          // tab A's sign-in as the owner is the signal; tab B's question about it is held
          await signInAs(a, ownerKey());
          const named = await (await sent(first, 'tab B’s question after the signal')).json();
          assert.equal(named.role, 'owner');
          // the owner signs in in tab B too; that sign-in is sent before the first answer arrives
          const login = await hold(b, '/api/login', { method: 'POST' });
          await typeKey(b, ownerKey());
          await sent(login, 'tab B’s sign-in');
          const firstLoad = await holdNext(b, '/api/businesses');
          await first.release();
          await sent(firstLoad, 'the load for the first answer');
          // tab B's own sign-in is answered, and its question is held
          const own = await holdNext(b, '/api/me');
          await login.release();
          await sent(own, 'tab B’s question after its own sign-in');
          // the first start's load ends: a newer start has begun, so it says nothing
          await firstLoad.release();
          assert.equal(
            await b.locator('#toast').isVisible(),
            false,
            'no sentence about another tab',
          );
          await own.release();
          assert.deepEqual(await tabShows(b), await ownerWorkspace(b), 'tab B shows the owner');
        });
      },
    );
    // Round 10, F1. The toolbar's + New business is for an owner only. While a tab does not know who
    // is signed in, or knows it is someone else, it is not shown, and its form does not open even
    // when its control is clicked from the page's own script.
    const dialogIn = (p) =>
      p.evaluate(() => ({
        open: document.querySelector('#dialog').open,
        title: document.querySelector('#dialog-title').textContent,
      }));
    const noDialog = { open: false, title: '' };
    // a click on a control, sent from the page itself rather than by a person
    const clickFromPage = (p, selector) =>
      p.evaluate((s) => document.querySelector(s).click(), selector);
    // a control for an action, made by the test and clicked from the page, as a page's own script
    // could reach it; the control is taken away again
    const actionFromPage = (p, action, id = '') =>
      p.evaluate(
        ([a, i]) => {
          const control = document.createElement('button');
          control.dataset.action = a;
          if (i) control.dataset.id = i;
          document.body.append(control);
          control.click();
          control.remove();
        },
        [action, id],
      );
    const NEW_BUSINESS = '.toolbar [data-action="new-business"]';
    lateCase(
      'round 10 F1: another tab signs out; while this tab asks who is signed in, no + New business',
      async () => {
        await twoTabs(async ({ a, b }) => {
          assert.equal(await b.locator(NEW_BUSINESS).isVisible(), true, 'the owner has it');
          const question = await holdNext(b, '/api/me');
          await signOutIn(a);
          assert.equal((await sent(question, 'tab B’s question')).status(), 401);
          await quiet(b);
          assert.deepEqual(
            await tabShows(b),
            {
              signInPage: false,
              workspace: true,
              name: '',
              actor: null,
              newBusiness: false,
              note: '',
              error: '',
              toast: null,
            },
            'an empty workspace, with nothing only an owner may use',
          );
          await clickFromPage(b, NEW_BUSINESS);
          await quiet(b);
          assert.deepEqual(await dialogIn(b), noDialog, 'its form does not open');
          await question.release();
          assert.deepEqual(await tabShows(b), { ...signedOutPage, note: CHANGED });
          assert.deepEqual(await dialogIn(b), noDialog);
        });
      },
    );
    lateCase(
      'round 10 F1: a viewer signs in in another tab; while the viewer’s records load, no + New business, then the owner has it',
      async () => {
        await twoTabs(async ({ a, b, viewer }) => {
          await signOutIn(a);
          await told(b.getByLabel('Access key', { exact: true }), 'tab B was not told');
          await quiet(b);
          const records = await holdNext(b, '/api/businesses');
          await signInAs(a, viewer.token);
          await sent(records, 'tab B’s first load as the viewer');
          await quiet(b);
          assert.equal(await b.evaluate(() => state.actor?.role), 'viewer', 'the viewer is named');
          assert.equal(await b.locator(NEW_BUSINESS).isVisible(), false, 'no + New business');
          await clickFromPage(b, NEW_BUSINESS);
          await quiet(b);
          assert.deepEqual(await dialogIn(b), noDialog, 'a click opens no form');
          await records.release();
          assert.deepEqual(await dialogIn(b), noDialog, 'and none is open after the load');
          assert.deepEqual(await tabShows(b), {
            signInPage: false,
            workspace: true,
            name: `${viewer.name} · Viewer`,
            actor: viewer.id,
            newBusiness: false,
            note: '',
            error: '',
            toast: CHANGED,
          });
          // an owner's other forms do not open for the viewer either, however they are reached
          for (const [action, id] of [
            ['new-business', ''],
            ['new-key', ''],
            ['revoke-key', viewer.id],
          ]) {
            await actionFromPage(b, action, id);
            await quiet(b);
            assert.deepEqual(await dialogIn(b), noDialog, `${action}: no form for the viewer`);
          }
          // the owner, signing in after the viewer, has + New business again, and it opens its form
          await signOutIn(a);
          await signInAs(a, ownerKey());
          await told(b.locator(NEW_BUSINESS), 'tab B did not show + New business for the owner');
          await quiet(b);
          assert.deepEqual(await tabShows(b), { ...(await ownerWorkspace(b)), toast: CHANGED });
          await b.locator(NEW_BUSINESS).click();
          await b.locator('#dialog-title', { hasText: 'Create a business' }).waitFor();
          assert.deepEqual(await dialogIn(b), { open: true, title: 'Create a business' });
          await b.getByRole('button', { name: 'Close dialog', exact: true }).click();
          assert.equal((await dialogIn(b)).open, false, 'and closes');
        });
      },
    );
    // Round 11. A person who may write and is not an owner: a reviewer, then an operator. Every
    // place the page decides by isOwner() is held here against each of them: for a viewer, the
    // wider questions canWrite() and canReview() are false too, so a version that asks one of them
    // in place of isOwner() passes every viewer case. The controls each role may use are checked
    // as well, so the case fails if the page hides too much as well as too little.
    const writerKeys = {};
    const writerKey = async (role) =>
      (writerKeys[role] ||= await call('/principals', 'POST', {
        name: `Browser ${role} who may write`,
        role,
        business_ids: [bid],
      }));
    const writerRoles = {
      reviewer: {
        a: 'a reviewer',
        label: 'Reviewer',
        certainty: ['assumption', 'reported', 'verified'],
        moves: ['Approve…', 'Start work…', 'Reopen for review…', 'Not applicable…'],
        move: 'approved',
        other: 'operator',
      },
      operator: {
        a: 'an operator',
        label: 'Operator',
        certainty: ['assumption', 'reported'],
        moves: ['Start work…', 'Reopen for review…'],
        move: 'in_progress',
        other: 'reviewer',
      },
    };
    for (const [role, expected] of Object.entries(writerRoles))
      lateCase(
        `round 11: ${expected.a} may write, and has none of an owner’s controls`,
        async () => {
          const me = await writerKey(role);
          // another key, one that an owner could revoke, for the revoke-key action sent below
          const other = await writerKey(expected.other);
          const prepared = (await snapshotOf(bid)).objects.find(
            (o) => o.kind === 'task' && o.state === 'prepared' && !o.data.deal_id,
          );
          assert(prepared, 'the test business has a prepared step');
          const writer = await browser.newContext({ viewport: { width: 1440, height: 1050 } });
          try {
            const p = await newPage(writer);
            p.on('pageerror', (e) => errors.push(e.message));
            // every request this page makes about access keys, whatever its method
            const keyRequests = [];
            p.on('request', (r) => {
              const path = new URL(r.url()).pathname;
              if (path === '/api/principals' || path.startsWith('/api/principals/'))
                keyRequests.push(`${r.method()} ${path}`);
            });
            await p.goto(origin);
            await signInAs(p, me.token);
            await quiet(p);
            assert.equal(await p.evaluate(() => state.actor?.role), role, `the ${role} is named`);
            assert.equal(
              await p.locator('#business-select').inputValue(),
              bid,
              'the test business is open',
            );
            // the toolbar and the foot of the sources name them and their role, with no + New business
            assert.equal(
              await p.locator('#actor-name').innerText(),
              `${me.name} · ${expected.label}`,
              'the toolbar names them',
            );
            assert.equal(
              await p.locator('#src-foot .account p').innerText(),
              `Signed in as ${me.name} · ${expected.label}`,
              'the account block names them',
            );
            assert.equal(
              await p.locator('[data-action="new-business"]:visible').count(),
              0,
              'no + New business anywhere on the page',
            );
            // the account block at the foot of the sources is shown on a phone only; its
            // controls are checked there, and in the markup at every width
            assert.deepEqual(
              await p.locator('#src-foot .account button').allInnerTexts(),
              ['Sign out'],
              'the account block offers Sign out only',
            );
            await p.setViewportSize({ width: 390, height: 844 });
            await p.locator('#back-src').click();
            await p.locator('#src-foot .account').waitFor();
            await quiet(p);
            assert.equal(
              await p.locator('#src-foot .account p').innerText(),
              `Signed in as ${me.name} · ${expected.label}`,
              'on a phone, the account block names them',
            );
            assert.deepEqual(
              await p.locator('#src-foot .account button:visible').allInnerTexts(),
              ['Sign out'],
              'on a phone, the account block shows Sign out only',
            );
            assert.equal(
              await p.locator('[data-action="new-business"]:visible').count(),
              0,
              'no + New business anywhere on a phone',
            );
            await p.setViewportSize({ width: 1440, height: 1050 });
            // the toolbar's control, clicked from the page, opens no form
            await clickFromPage(p, NEW_BUSINESS);
            await quiet(p);
            assert.deepEqual(
              await dialogIn(p),
              noDialog,
              'a click on + New business opens no form',
            );
            // Access & assistants: no + Create access key, no key list, no owner's section
            await p.locator('#src-nav .src[data-src="access"]').click();
            await p.locator('#list .row[data-key="ai:overview"]').waitFor();
            await quiet(p);
            assert.deepEqual(
              await p.locator('#list-right button').allInnerTexts(),
              [],
              'no + Create access key',
            );
            assert.deepEqual(await p.locator('#list .grp').allInnerTexts(), [], 'no Access keys');
            assert.deepEqual(
              await p.locator('#list .row').evaluateAll((rows) => rows.map((r) => r.dataset.key)),
              ['ai:overview'],
              'the one row is Assistants and sharing',
            );
            await p.locator('#list .row[data-key="ai:overview"]').click();
            await p.locator('#insp-title', { hasText: 'Assistants and sharing' }).waitFor();
            await quiet(p);
            assert.deepEqual(
              await p.locator('#insp .i-sec h3').allInnerTexts(),
              ['Optional assistant review', 'Your business records'],
              'no People and access keys',
            );
            assert.equal(await p.locator('[data-action="revoke-key"]').count(), 0, 'no Revoke');
            // an owner's forms, reached by actions sent from the page, open nothing
            for (const [action, id] of [
              ['new-business', ''],
              ['new-key', ''],
              ['revoke-key', other.id],
            ]) {
              await actionFromPage(p, action, id);
              await quiet(p);
              assert.deepEqual(await dialogIn(p), noDialog, `${action}: no form for the ${role}`);
            }
            // a record form they may use, with the evidence statuses of their role
            await p.locator('#src-nav .src[data-src="records"]').click();
            await p
              .locator('#list-right')
              .getByRole('button', { name: '+ Add record', exact: true })
              .click();
            await p.locator('#dialog-title', { hasText: 'Add business record' }).waitFor();
            assert.deepEqual(
              await dialogIn(p),
              { open: true, title: 'Add business record' },
              'the record form opens',
            );
            assert.deepEqual(
              await p
                .locator('#dialog select[name="certainty"] option')
                .evaluateAll((os) => os.map((o) => o.value)),
              expected.certainty,
              'the evidence statuses of the role',
            );
            await p.getByRole('button', { name: 'Close dialog', exact: true }).click();
            assert.equal((await dialogIn(p)).open, false, 'the record form closes');
            // a decision on a prepared step that their role may make, and only those
            const category = String(prepared.data.category);
            await p
              .locator('#src-nav .src .lbl', {
                hasText: new RegExp(`^${category.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')}$`),
              })
              .click();
            await p.locator(`#list .row[data-key="step:${prepared.id}"]`).click();
            await p.locator('#insp-title', { hasText: prepared.title }).waitFor();
            await quiet(p);
            assert.deepEqual(
              await p.locator('#insp [data-action="tx"]').allInnerTexts(),
              expected.moves,
              'the moves of the role',
            );
            await p.locator(`#insp [data-action="tx"][data-to="${expected.move}"]`).click();
            await p.locator('#tx-form').waitFor();
            assert.equal(
              await p.locator('#tx-form [type=submit]').innerText(),
              'Record decision',
              'the decision form opens',
            );
            await p
              .locator('#tx-form')
              .getByRole('button', { name: 'Not yet', exact: true })
              .click();
            await quiet(p);
            assert.equal(await p.locator('#tx-form').count(), 0, 'and closes unsent');
            // a key the test serves as covering no business: the start page offers no business form
            const businessList = (u) => u.pathname === '/api/businesses';
            await p.route(businessList, (r) =>
              r.request().method() === 'GET' ? r.fulfill({ json: [] }) : r.continue(),
            );
            await p.reload();
            await p.locator('#insp-title', { hasText: 'Start your first business' }).waitFor();
            await quiet(p);
            assert.equal(
              await p.locator('#actor-name').innerText(),
              `${me.name} · ${expected.label}`,
              'the start page names them',
            );
            assert.equal(
              await p.locator('#insp .prose').first().innerText(),
              'This access key does not cover any business yet. Ask the workspace owner to give it access to one.',
              'the start page says what to ask the owner',
            );
            assert.deepEqual(
              await p.locator('#insp button').allInnerTexts(),
              [],
              'no Create a business',
            );
            assert.equal(
              await p.locator('[data-action="new-business"]:visible').count(),
              0,
              'no + New business anywhere on the start page',
            );
            await p.unroute(businessList);
            // the page never asked about access keys, in either load
            assert.deepEqual(keyRequests, [], 'no request about access keys');
          } finally {
            await writer.close();
          }
        },
      );
    // Round 9, H. A step's history served with entries that are not objects among real ones: its
    // view opens and lists the real entries only.
    lateCase('round 9 H: a history holding entries that are not objects', async () => {
      const hStep = (await snapshotOf(bid)).objects.find(
        (o) => o.kind === 'task' && o.state !== 'superseded' && !o.data.deal_id,
      );
      const at = new Date().toISOString();
      const served = {
        [hStep.id]: [
          'approved',
          [
            {
              from: 'pending',
              to: 'prepared',
              evidence_ids: [],
              at,
              note: 'Synthetic preparation for the history check.',
            },
            null,
            'an entry that is a string',
            9,
            {
              from: 'prepared',
              to: 'approved',
              evidence_ids: [],
              at,
              note: 'Synthetic approval for the history check.',
            },
          ],
        ],
      };
      await serveChanged((body) => {
        for (const t of body.objects)
          if (served[t.id]) {
            t.state = served[t.id][0];
            t.data.history = served[t.id][1];
            t.data.evidence_ids = [];
          }
      });
      await race.reload();
      await race.locator(OPENED).waitFor();
      await onBusiness(bid, testName);
      // the step's view opens and shows its two real entries, newest first
      await showStep(hStep);
      await quiet(race);
      const historyRows = await race
        .locator('#insp .i-sec')
        .filter({ has: race.locator('h3', { hasText: /^History/ }) })
        .locator('.att-row .an')
        .evaluateAll((rows) =>
          rows.map((r) => [r.firstChild.textContent, r.querySelector('.sub').textContent]),
        );
      assert.deepEqual(
        historyRows.map(([move, sub]) => [move, sub.split(' · ').at(-1)]),
        [
          ['Prepared → Approved', 'Synthetic approval for the history check.'],
          ['Not started → Prepared', 'Synthetic preparation for the history check.'],
        ],
        'the real entries, and nothing for the others',
      );
      assert.doesNotMatch(
        await race.locator('#insp').innerText(),
        /could not be shown|TypeError|Cannot read/,
        'its view is drawn',
      );
    });
    // Round 9, G1 to G6. Histories served by the test, each telling one sentence of the rule for the
    // evidence a completed step relies on from the code that breaks it. Every step attaches nothing
    // now, so what the page shows comes from the history alone.
    lateCase(
      'round 9 G1 to G6: each sentence of the rule for the evidence a step relies on',
      async () => {
        const [a, b] = await reviewedPair();
        const [c] = await reviewedPair();
        const steps = (await snapshotOf(bid)).objects.filter(
          (o) => o.kind === 'task' && o.state !== 'superseded' && !o.data.deal_id,
        );
        assert(steps.length >= 7, 'seven steps to serve changed');
        const [g1, g2, g3, g4, g5, g6, g6b] = steps;
        const at = new Date().toISOString();
        const to = (state, ids) => ({ to: state, evidence_ids: ids, at });
        const served = {
          // the last entry alone when no entry is prepared: B, not A
          [g1.id]: ['verified', [to('approved', [a.id]), to('verified', [b.id])]],
          // the latest preparation and every entry after it: A and B
          [g2.id]: ['approved', [to('prepared', [a.id]), to('approved', [b.id])]],
          // every entry after the latest needs_review: C alone
          [g3.id]: [
            'not_applicable',
            [
              to('prepared', [a.id]),
              to('approved', []),
              to('needs_review', []),
              to('prepared', [b.id]),
              to('approved', []),
              to('needs_review', []),
              to('not_applicable', [c.id]),
            ],
          ],
          // an empty cycle gives nothing (the state and the history disagree, as only a damaged or
          // foreign snapshot can)
          [g4.id]: [
            'verified',
            [to('prepared', [a.id]), to('approved', []), to('needs_review', [a.id])],
          ],
          // ids are read only from an entry that is an object: A
          [g5.id]: ['approved', [to('prepared', [a.id]), null, to('approved', [])]],
          // a history that is not a list gives nothing
          [g6.id]: ['verified', {}],
          [g6b.id]: ['verified', 7],
        };
        await serveChanged((body) => {
          for (const t of body.objects)
            if (served[t.id]) {
              t.state = served[t.id][0];
              t.data.history = served[t.id][1];
              t.data.evidence_ids = [];
            }
        });
        await race.reload();
        await race.locator(OPENED).waitFor();
        await onBusiness(bid, testName);
        // each evidence item's row and its "Used by" part: the steps, by id, and what each says
        const usedList = () =>
          usedBySection()
            .locator('.att-row')
            .evaluateAll((rows) =>
              rows
                .map((r) => [
                  r.querySelector('[data-go]').dataset.go.split('|step:')[1],
                  r.querySelector('.sub').textContent,
                ])
                .sort(),
            );
        const relied = (step, state) => [step.id, `Task · ${state}${EARLIER}`];
        const expected = [
          [a, 2, [relied(g2, 'Approved'), relied(g5, 'Approved')]],
          [b, 2, [relied(g1, 'Verified'), relied(g2, 'Approved')]],
          [c, 1, [relied(g3, 'Not applicable')]],
        ];
        for (const [e, n, list] of expected) {
          await raceSource('evidence');
          assert.match(
            await evSub(e.id),
            new RegExp(` · used by ${n}$`),
            `${e.title}: used by ${n}`,
          );
          await openEvidence(e);
          assert.deepEqual(
            await usedList(),
            list.sort(),
            `${e.title}: what its Used by part lists`,
          );
        }
        await sameAsServer('round 9 G histories');
        for (const t of [g5, g6, g6b]) {
          await showStep(t);
          await quiet(race);
          assert.doesNotMatch(
            await race.locator('#insp').innerText(),
            /could not be shown|TypeError|Cannot read/,
            `${t.title}: its view is drawn`,
          );
        }
      },
    );
    // Round 9, G7. A new key is shown only to the person who asked for it, by id: a second owner
    // with the same name as the first signs in while the key is held, in a browser without the
    // signal, so the check before the key is shown is all that keeps it back.
    lateCase(
      'round 9 G7: someone else with the same role and the same name does not get the key',
      async () => {
        const ownerName = (await (await context.request.get(`${origin}/api/me`)).json()).name;
        const namesake = await call('/principals', 'POST', {
          name: ownerName,
          role: 'owner',
          business_ids: [bid],
        });
        await twoTabs(
          async ({ a, b }) => {
            const ownerId = await a.evaluate(() => state.actor.id);
            assert.notEqual(namesake.id, ownerId, 'two owners');
            assert.equal(await a.evaluate(() => state.actor.name), namesake.name, 'one name');
            const held = await hold(a, '/api/principals', { method: 'POST' });
            await keyFormIn(a, 'Browser key held while a namesake signs in');
            await a
              .locator('#dialog')
              .getByRole('button', { name: 'Create key', exact: true })
              .click();
            const secret = (await (await sent(held, 'the key creation')).json()).token;
            await signOutIn(b);
            await signInAs(b, namesake.token);
            await held.release();
            await a
              .waitForFunction((id) => state.actor?.id === id, namesake.id, {
                timeout: 10000 * slowdown,
              })
              .catch(() => {
                throw new Error('the key check did not find that someone else is signed in');
              });
            await quiet(a);
            assert.equal(await a.locator('#dialog').evaluate((d) => d.open), false, 'no dialog');
            assert(!(await holdsIn(a, secret)), 'the key is nowhere in tab A');
          },
          { bare: true },
        );
      },
    );
    // Round 9, G9. A reload that changes nothing leaves an open view as it is: the selection in
    // its text, and the elements of its reasons.
    lateCase(
      'round 9 G9: a reload that changes nothing leaves an open draft’s view alone',
      async () => {
        const doc = await newDraft();
        await call(`/businesses/${bid}/records`, 'POST', {
          kind: 'risk',
          title: 'Browser record that makes a draft stale before its view opens',
          details: 'Synthetic record for the unchanged-view check.',
        });
        await race.reload();
        await race.locator(OPENED).waitFor();
        await onBusiness(bid, testName);
        await readDraft(doc);
        assert.deepEqual(await viewFlags(), [SOURCE_CHANGED]);
        const reason = await race.locator('#dialog-content .flag').elementHandle();
        const selected = await race.evaluate(() => {
          const node = $('#dialog pre.document').firstChild;
          const range = document.createRange();
          range.setStart(node, 2);
          range.setEnd(node, 40);
          getSelection().removeAllRanges();
          getSelection().addRange(range);
          return getSelection().toString();
        });
        assert.equal(selected.length, 38, 'part of the text is selected');
        await race.evaluate(() => refresh());
        await quiet(race);
        assert(await race.evaluate(() => $('#dialog').open), 'the view is open');
        assert.equal(
          await race.evaluate(() => getSelection().toString()),
          selected,
          'still selected',
        );
        assert(
          await race.evaluate(
            () => getSelection().anchorNode === $('#dialog pre.document').firstChild,
          ),
          'in the same text',
        );
        assert(await reason.evaluate((el) => el.isConnected), 'the reason is the same element');
        await reason.dispose();
      },
    );
    // Round 9, G10. A viewer's read-only view of a whole record follows a reload of its business.
    lateCase('round 9 G10: an open record view shows a change made through the API', async () => {
      const record = await call(`/businesses/${bid}/records`, 'POST', {
        kind: 'risk',
        title: 'Browser record a viewer reads while it changes',
        details: 'Synthetic details before the change.',
      });
      const viewerKey = (
        await call('/principals', 'POST', {
          name: 'Browser viewer for a record that changes',
          role: 'viewer',
          business_ids: [bid],
        })
      ).token;
      const viewer = await browser.newContext({ viewport: { width: 1440, height: 1050 } });
      try {
        const v = await newPage(viewer);
        v.on('pageerror', (e) => errors.push(e.message));
        await v.goto(origin);
        await v.getByLabel('Access key', { exact: true }).fill(viewerKey);
        await v.getByRole('button', { name: 'Open workspace' }).click();
        await v.locator(OPENED).waitFor();
        await v.locator('#src-nav .src[data-src="records"]').click();
        await v.locator('#list .row', { hasText: record.title }).click();
        await v.locator('#insp').getByRole('button', { name: 'View', exact: true }).click();
        const pre = v.locator('#dialog-content pre');
        assert.equal(
          JSON.parse(await pre.textContent()).data.details,
          'Synthetic details before the change.',
        );
        await call(`/businesses/${bid}/records/${record.id}`, 'PUT', {
          kind: 'risk',
          title: record.title,
          details: 'Synthetic details after the change.',
          expected_version: record.version,
        });
        await v.evaluate(() => refresh());
        await quiet(v);
        assert(await v.locator('#dialog').evaluate((d) => d.open), 'the view stayed open');
        const shown = JSON.parse(await pre.textContent());
        assert.equal(shown.data.details, 'Synthetic details after the change.', 'the new value');
        assert.equal(shown.version, record.version + 1);
        assert(!(await pre.textContent()).includes('before the change'), 'not the old one');
      } finally {
        await viewer.close();
      }
    });
    race.setDefaultTimeout(10000);
    const lateFailures = [];
    // A failure while the page is loaded again for a case names that case, and lists what the
    // cases before it had already recorded, which would otherwise never be printed.
    const settleFor = async (next) => {
      try {
        await settle();
      } catch (error) {
        const before = lateFailures.length
          ? `Failures recorded before it:\n${lateFailures.join('\n')}`
          : 'No failure was recorded before it.';
        throw new Error(`Loading the page again ${next} failed: ${error.message}\n${before}`, {
          cause: error,
        });
      }
    };
    for (const [name, run] of late) {
      await settleFor(`for the case “${name}”`);
      try {
        await run();
      } catch (error) {
        lateFailures.push(`${name}: ${String(error.message).split('\n')[0]}`);
      }
    }
    await settleFor('after the last case');
    race.setDefaultTimeout(30000);
    assert.deepEqual(lateFailures, [], 'late answers act only on their own session and business');

    // A jurisdiction change made through the API: the demo's earlier deal analysis and forecast
    // show the server's own sentence, and the page composes none of its own. It runs on the demo
    // here, after the demo's last use above: the change replaces the demo's guide steps and
    // reopens its completed ones, and no check below uses the demo.
    const demoUrl = `${origin}/api/businesses/${demoBid}`;
    const demoSnap = async () => (await context.request.get(demoUrl)).json();
    const changeTo = async (jurisdiction, key) => {
      const response = await context.request.post(demoUrl + '/jurisdiction', {
        headers: { Origin: origin, 'Idempotency-Key': key },
        data: {
          expected_version: (await demoSnap()).business.version,
          jurisdiction,
          reason: 'Synthetic browser check of earlier analyses after a jurisdiction change.',
          evidence_ids: [],
        },
      });
      assert.equal(response.status(), 200, await response.text());
      return response.json();
    };
    // A record related to the latest forecast, the deal analysis and a draft document. The page's
    // form offers only records to relate; the server accepts any object of the business, so the
    // record is made through the API, before the change.
    const unchanged = await demoSnap();
    const relatedTo = {
      plan: unchanged.objects.filter((o) => o.kind === 'financial_plan').at(-1),
      deal: unchanged.objects.find((o) => o.kind === 'deal'),
      doc: unchanged.objects.find((o) => o.kind === 'document' && o.state === 'draft'),
    };
    for (const [what, o] of Object.entries(relatedTo))
      assert(o, `the demo has a ${what} to relate a record to`);
    const relatedTitle = 'Browser record related to the analyses';
    const recorded = await context.request.post(demoUrl + '/records', {
      headers: { Origin: origin, 'Idempotency-Key': 'browser-record-related-to-analyses' },
      data: {
        kind: 'risk',
        title: relatedTitle,
        details: 'Synthetic record related to a forecast, a deal analysis and a draft document.',
        related_ids: Object.values(relatedTo).map((o) => o.id),
      },
    });
    assert.equal(recorded.status(), 201, await recorded.text());
    const relatedRecord = await recorded.json();
    // The page is opened before the change, to read what each evidence row counts as its uses.
    const jx = await newPage(context);
    jx.on('pageerror', (e) => errors.push(e.message));
    const jxSource = async (id) => {
      await jx.locator(`#src-nav .src[data-src="${id}"]`).click();
      await jx.locator(`#src-nav .src[data-src="${id}"][aria-current="true"]`).waitFor();
    };
    const jxRow = (key) => jx.locator(`#list .row[data-key="${key}"]`);
    const jxOpen = async (source, key, title) => {
      await jxSource(source);
      await jxRow(key).click();
      await jx.locator('#insp-title', { hasText: title }).waitFor();
      await quiet(jx);
    };
    const jxStep = async (t) => {
      await jx.evaluate((id) => go(`journey|step:${id}`), t.id);
      await jx.locator('#insp-title', { hasText: t.title }).waitFor();
      await quiet(jx);
    };
    const onDemo = async () => {
      await jx.locator(OPENED).waitFor();
      if ((await jx.locator('#business-select').inputValue()) !== demoBid) {
        await jx.locator('#business-select').selectOption(demoBid);
        await jx.locator('#list .th-sub', { hasText: demoName }).waitFor();
      }
      await quiet(jx);
    };
    const section = (name) =>
      jx.locator('#insp .i-sec').filter({ has: jx.locator('h3', { hasText: name }) });
    // what an inspector's list of properties says for one name; empty when it has no such row
    const prop = (name) =>
      jx
        .locator('#insp dl.props dt')
        .filter({ hasText: new RegExp(`^${name}$`) })
        .evaluateAll((dts) => dts.map((dt) => dt.nextElementSibling.textContent));
    // one row of an attached list: its glyph, the text before its line of state, and that line
    const attRow = async (list, title, where) => {
      const r = list.locator('.att-row', { hasText: title });
      assert.equal(await r.count(), 1, `${where}: “${title}” is listed once`);
      return r.evaluate((el) => ({
        glyph: el.firstElementChild.getAttribute('data-glyph'),
        cls: el.firstElementChild.getAttribute('class'),
        text: el.querySelector('.an').firstChild.textContent,
        sub: el.querySelector('.sub').textContent,
      }));
    };
    const usesOnPage = async () => {
      await jxSource('evidence');
      await quiet(jx);
      const subs = await jx
        .locator('#list .row[data-key^="ev:"]')
        .evaluateAll((rows) =>
          rows.map((r) => [r.dataset.key.slice(3), r.querySelector('.r-sub').textContent]),
        );
      return new Map(subs.map(([id, sub]) => [id, Number(/used by (\d+)/.exec(sub)?.[1] ?? 0)]));
    };
    await jx.goto(origin);
    await onDemo();
    const usesBefore = await usesOnPage();
    assert(usesBefore.size > 0, 'the demo’s evidence is listed');
    const changed = await changeTo('GB', 'browser-jurisdiction-change');
    const marked = await demoSnap();
    const reasonOf = (kind) => {
      const [reason] = marked.objects.find((o) => o.kind === kind).stale_reasons;
      assert.equal(reason.code, 'jurisdiction_changed');
      return reason.message;
    };
    const dealReason = reasonOf('deal'),
      planReason = reasonOf('financial_plan');
    assert(
      dealReason.startsWith(
        'This deal analysis was saved while the planning jurisdiction was undecided. It is now GB.',
      ),
    );
    assert(
      planReason.startsWith(
        'This forecast was saved while the planning jurisdiction was undecided. It is now GB.',
      ),
    );
    const plans = marked.objects.filter((o) => o.kind === 'financial_plan'),
      deal = marked.objects.find((o) => o.kind === 'deal'),
      gone = marked.objects.filter((o) => o.kind === 'task' && o.state === 'superseded');
    assert(gone.length > 0, 'the change replaced at least one of the demo’s steps');
    assert.equal(marked.stats.superseded, gone.length);
    for (const added of changed.added)
      assert.deepEqual(
        marked.objects.find((o) => o.id === added.id).data.evidence_ids,
        [],
        'a step the change adds cites no evidence',
      );
    const noOldSentence = async (where) => {
      await quiet(jx);
      for (const pane of ['#list', '#insp'])
        assert.doesNotMatch(
          await jx.locator(pane).innerText(),
          /Supporting evidence changed/i,
          `${where}: the page writes no reason of its own`,
        );
    };
    // the page learns of the change when it is reloaded
    await jx.reload();
    await onDemo();
    // the forecast: its full view, the money position that shows the latest one, and its rows
    const latest = plans.at(-1);
    const latestReasons = latest.stale_reasons.map((r) => r.message);
    assert(latestReasons.length > 0, 'the latest forecast needs review');
    await jxOpen('money', 'plan:' + latest.id, latest.title);
    for (const m of latestReasons)
      assert(
        (await jx.locator('#insp .flag').allInnerTexts()).includes(m),
        'the forecast’s own view shows the server’s reason',
      );
    await noOldSentence('a forecast');
    for (const p of plans) {
      assert.equal(
        await jxRow('plan:' + p.id)
          .locator('.r-g svg')
          .getAttribute('data-glyph'),
        'review',
        `${p.title}: its row is marked`,
      );
      assert.match(await jxRow('plan:' + p.id).innerText(), /needs review/);
    }
    assert.equal(
      await jx.locator('#src-nav .src[data-src="money"] .n svg').first().getAttribute('data-glyph'),
      'review',
      'the count beside Money is marked',
    );
    for (const p of plans.slice(0, -1)) {
      await jxRow('plan:' + p.id).click();
      await jx.locator('#insp-title', { hasText: p.title }).waitFor();
      await quiet(jx);
      for (const r of p.stale_reasons)
        assert(
          (await jx.locator('#insp .flag').allInnerTexts()).includes(r.message),
          `an earlier forecast, ${p.title}, shows its own reasons`,
        );
    }
    await jxOpen('money', 'money:ending', 'Money position');
    for (const m of latestReasons)
      assert(
        (await jx.locator('#insp .flag').allInnerTexts()).includes(m),
        'the money position shows the latest forecast’s reason',
      );
    await noOldSentence('the money position');
    // the deal analysis: its full view and its row
    await jxOpen('deals', 'deal:' + deal.id, deal.title);
    for (const r of deal.stale_reasons)
      assert(
        (await jx.locator('#insp .flag').allInnerTexts()).includes(r.message),
        'the deal’s own view shows the server’s reason',
      );
    assert.equal(
      await jxRow('deal:' + deal.id)
        .locator('.r-right')
        .innerText(),
      'Needs review',
    );
    await noOldSentence('a deal analysis and the list of deals');
    // Today's list of what changed holds both, each as needing review, for no named cause
    await jxSource('today');
    await quiet(jx);
    const changedList = jx.getByRole('list', { name: /^Out of date or unreviewed/ });
    for (const [key, sub] of [
      ['deal:' + deal.id, 'Acquisition analysis · needs review'],
      ...plans.map((p) => ['plan:' + p.id, 'Forecast · needs review']),
    ])
      assert.equal(
        await changedList.locator(`.row[data-key="${key}"] .r-sub`).innerText(),
        sub,
        `Today lists ${key} as needing review`,
      );
    await noOldSentence('Today');
    // "Ending cash" is drawn in Money and in Today's Money group. In both it carries the review
    // glyph and says "needs review" while the latest forecast needs review.
    for (const source of ['money', 'today']) {
      await jxSource(source);
      await quiet(jx);
      const ending = jxRow('money:ending');
      assert.equal(
        await ending.locator('.r-g svg').getAttribute('data-glyph'),
        'review',
        `${source}: the Ending cash row has the review glyph`,
      );
      assert.match(
        await ending.locator('.r-sub').innerText(),
        / · needs review$/,
        `${source}: the Ending cash row says it needs review`,
      );
    }
    // The record related to the forecast, the deal analysis and the draft document lists all
    // three, each with the review glyph. None is said to be made from records that changed since:
    // the server gives each one's reasons, in its own view, the document's included.
    const madeFrom = /made from records that changed since/;
    assert.equal(
      marked.objects.find((o) => o.id === relatedTo.doc.id).stale,
      true,
      'the server marks the draft document',
    );
    await jxOpen('records', 'rec:' + relatedRecord.id, relatedTitle);
    const relatedList = section('Related records');
    for (const [o, what] of [
      [relatedTo.plan, 'forecast'],
      [relatedTo.deal, 'deal analysis'],
      [relatedTo.doc, 'draft document'],
    ]) {
      const shown = await attRow(relatedList, o.title, 'Related records');
      assert.equal(shown.glyph, 'review', `a ${what} that needs review has the review glyph`);
      assert.match(shown.sub, / · Needs review/, `the ${what} reads “Needs review”`);
      assert.doesNotMatch(shown.sub, madeFrom, `the ${what} carries no reason the page made up`);
    }
    // the deal also appears under "Used by" on the evidence its adjustment cites
    const adjustmentEvidence = marked.objects.find(
      (o) => o.id === deal.data.inputs.adjustments.find((a) => a.evidence_id).evidence_id,
    );
    await jxOpen('evidence', 'ev:' + adjustmentEvidence.id, adjustmentEvidence.title);
    const dealUse = await attRow(section(/^Used by/), deal.title, 'Used by');
    assert.equal(dealUse.glyph, 'review', 'the deal analysis under Used by has the review glyph');
    assert.match(dealUse.sub, / · Needs review$/, 'the deal analysis under Used by needs review');
    // Each evidence row's count of uses goes down by the replaced steps that cited it; the new
    // steps cite nothing. The count before is the one the page showed before the change.
    const usesAfter = await usesOnPage();
    let uncounted = 0;
    for (const [id, n] of usesBefore) {
      const lost = gone.filter((t) => (t.data.evidence_ids || []).includes(id)).length;
      uncounted += lost;
      assert.equal(
        usesAfter.get(id),
        n - lost,
        `the row for “${marked.objects.find((o) => o.id === id).title}” counts no replaced step among its uses`,
      );
    }
    assert(uncounted > 0, 'a replaced step cited listed evidence');
    // No replaced step is on the map or in any list, and the page counts what the server counts.
    const sameTotals = async (stats, gone) => {
      await jxSource('today');
      await quiet(jx);
      const pageCounts = await jx.evaluate(() => ({
        steps: M.steps.length,
        verified: M.verified,
        na: M.na,
        review: M.steps.filter((x) => x.task.state === 'needs_review').length,
        replacedInModel: M.steps.filter((x) => x.task.state === 'superseded').length,
        lineTotal: M.lines.reduce((n, l) => n + l.steps.length, 0),
        emptyLines: M.lines.filter((l) => !l.steps.length).length,
      }));
      assert.deepEqual(
        pageCounts,
        {
          steps: stats.tasks,
          verified: stats.verified,
          na: stats.not_applicable,
          review: stats.needs_review,
          replacedInModel: 0,
          lineTotal: stats.tasks,
          emptyLines: 0,
        },
        'the page’s totals are the server’s, and no replaced step is among them',
      );
      const todayHead = await jx.locator('#list .today-head').innerText();
      assert(
        todayHead.includes(`${stats.tasks} steps on`),
        `Today counts ${stats.tasks} steps: ${todayHead}`,
      );
      assert(todayHead.includes(`${stats.verified} verified`));
      if (stats.needs_review) assert(todayHead.includes(`${stats.needs_review} need review`));
      if (stats.not_applicable)
        assert(todayHead.includes(`${stats.not_applicable} not applicable`));
      const lineCounts = await jx
        .locator('#src-nav .src .bul')
        .evaluateAll((bs) => bs.map((b) => b.closest('.src').querySelector('.n').textContent));
      assert.equal(
        lineCounts.reduce((n, c) => n + Number(c.split('/')[1]), 0),
        stats.tasks,
        'the lines’ totals add up to the server’s count of steps',
      );
      assert.equal(
        lineCounts.reduce((n, c) => n + Number(c.split('/')[0]), 0),
        stats.verified,
        'and their verified counts to its count of verified steps',
      );
      assert.equal(
        await jx.locator('#src-nav .src[data-src="journey"] .n').innerText(),
        `${stats.tasks - stats.verified - stats.not_applicable} to go`,
      );
      const sources = await jx
        .locator('#src-nav .src')
        .evaluateAll((x) => x.map((el) => el.dataset.src));
      for (const id of sources) {
        await jxSource(id);
        if (id === 'history')
          await jx.locator('#list .grp', { hasText: 'Events' }).waitFor({ state: 'attached' });
        await quiet(jx);
        for (const t of gone) {
          assert.equal(
            await jx.locator(`#list .row[data-key$="${t.id}"]`).count(),
            0,
            `${id}: the replaced step “${t.title}” is in no list`,
          );
          assert.equal(
            await jx.locator(`#insp [data-id="${t.id}"]`).count(),
            0,
            `${id}: nor on the map`,
          );
        }
      }
      await jxSource('journey');
      await jx.locator('#insp .mapbox').waitFor();
      await quiet(jx);
      assert.equal(
        await jx.locator('#insp .mp-tall .stn').count(),
        stats.tasks,
        'one station for each live step',
      );
    };
    await sameTotals(marked.stats, gone);
    // the business shows its new jurisdiction
    assert.match(await jx.locator('#insp').innerText(), /Jurisdiction\s+GB/);
    // A replaced step is reached from the evidence it cites, and opens read-only.
    const citing = gone.find((t) => (t.data.evidence_ids || []).length);
    assert(citing, 'a replaced step cites evidence, so it can be reached from that evidence');
    const cited = marked.objects.find((o) => o.id === citing.data.evidence_ids[0]);
    await jxOpen('evidence', 'ev:' + cited.id, cited.title);
    const historyList = jx
      .locator('#insp .i-sec')
      .filter({ has: jx.locator('h3', { hasText: 'Cited by replaced steps' }) });
    assert.doesNotMatch(
      await jx
        .locator('#insp .i-sec')
        .filter({ has: jx.locator('h3', { hasText: /^Used by/ }) })
        .innerText(),
      new RegExp(citing.title),
      'a replaced step is not counted among the evidence’s uses',
    );
    // in the list of steps that cite it, the replaced step reads "Replaced" and is drawn as a dash
    const citedBy = await attRow(historyList, citing.title, 'Cited by replaced steps');
    assert.match(citedBy.sub, / · Replaced$/, 'a replaced step in an attached list reads Replaced');
    assert.deepEqual(
      [citedBy.cls, citedBy.glyph],
      ['g g-off', null],
      'a replaced step in an attached list is drawn as a dash',
    );
    await historyList.locator('.att-row', { hasText: citing.title }).getByRole('button').click();
    await jx.locator('#insp-title', { hasText: citing.title }).waitFor();
    await quiet(jx);
    // what a replaced step's own view shows: why it was replaced, its state, the crumb, the
    // evidence it cites and its History, with the entry that records the replacement
    const replacedStep = async (t, from, to) => {
      const view = await jx.locator('#insp').innerText();
      assert(
        view.includes(
          `This step was replaced when the planning jurisdiction changed from ${from} to ${to}.`,
        ),
        `${t.title}: it says in plain words that the step was replaced, and why`,
      );
      assert.doesNotMatch(view, /undefined|Cannot read|TypeError/);
      assert.equal(
        await jx.locator('#insp [data-action="tx"], #insp #tx-form').count(),
        0,
        `${t.title}: it offers no decision`,
      );
      assert.equal(
        await jx.locator('#insp .i-state .st').innerText(),
        'Replaced',
        `${t.title}: its state reads Replaced`,
      );
      assert.equal(
        await jx.locator('#crumb').innerText(),
        `Replaced step › ${t.title}`,
        `${t.title}: the crumb says it is a replaced step`,
      );
      const citesList = section('Evidence it cites');
      for (const id of t.data.evidence_ids) {
        const e = marked.objects.find((o) => o.id === id);
        await attRow(citesList, e.title, `${t.title}: Evidence it cites`);
      }
      assert.equal(
        await citesList.locator('.att-row').count(),
        t.data.evidence_ids.length,
        `${t.title}: it lists the evidence it cites`,
      );
      const history = section(/^History$/);
      assert.equal(await history.count(), 1, `${t.title}: its view has a History section`);
      const entry = await attRow(
        history,
        `Jurisdiction changed from ${from} to ${to}`,
        `${t.title}: History`,
      );
      assert(
        entry.sub.includes(` · Jurisdiction changed from ${from} to ${to}. `),
        `${t.title}: the History entry records the replacement`,
      );
      assert.match(entry.text, / → Replaced$/, `${t.title}: its History says Replaced`);
      assert.deepEqual(
        [entry.cls, entry.glyph],
        ['g g-off', null],
        `${t.title}: its History draws the replacement as a dash`,
      );
    };
    await replacedStep(citing, 'undecided', 'GB');
    assert.equal(await jx.locator('#list .row[aria-current="true"]').count(), 0);

    // A second change, GB to SE, replaces the UK step, which depends on the structure decision.
    // The live structure step's "Needed by" then names the Swedish step and not the UK step. The
    // page learns of the change when it is reloaded.
    const toSweden = await changeTo('SE', 'browser-jurisdiction-change-se');
    const remarked = await demoSnap();
    const allGone = remarked.objects.filter((o) => o.kind === 'task' && o.state === 'superseded');
    const ukStep = allGone.find((t) => t.id === toSweden.superseded[0]?.id);
    const seStep = remarked.objects.find((o) => o.id === toSweden.added[0]?.id);
    const structure = remarked.objects.find(
      (o) => o.kind === 'task' && !o.data.deal_id && o.data.key === 'structure',
    );
    assert.equal(toSweden.superseded.length, 1);
    assert.equal(ukStep.title, 'Review UK director, registration and reporting duties');
    assert.equal(seStep.title, 'Review Swedish company and tax setup');
    assert(ukStep.data.depends_on.includes(structure.id), 'the UK step depends on the structure');
    assert(seStep.data.depends_on.includes(structure.id), 'and so does the Swedish step');
    assert.equal(allGone.length, gone.length + 1);
    assert.equal(remarked.stats.superseded, allGone.length);
    await jx.reload();
    await onDemo();
    await sameTotals(remarked.stats, allGone);
    assert.match(await jx.locator('#insp').innerText(), /Jurisdiction\s+SE/);
    await jxStep(structure);
    const neededBy = (await prop('Needed by')).join('\n');
    assert(neededBy.includes(seStep.title), `“Needed by” lists the Swedish step: ${neededBy}`);
    assert(!neededBy.includes(ukStep.title), '“Needed by” lists no replaced step');
    // the evidence rows count the same: the UK step cited nothing
    assert.deepEqual(await usesOnPage(), usesAfter);
    await jxStep(ukStep);
    await replacedStep(ukStep, 'GB', 'SE');
    // the first replaced step still says what replaced it
    await jxStep(citing);
    await replacedStep(citing, 'undecided', 'GB');

    // No live step depends on a replaced step: no base or deal step depends on a step of a
    // planning guide. So the snapshot is altered on its way to the page: one live step also names
    // both replaced steps as prerequisites, and its "Needs these first" must list neither.
    // Once this check is done, the route passes requests on rather than being removed.
    const dependent = remarked.objects.find(
      (o) => o.kind === 'task' && !o.data.deal_id && o.data.key === 'offer',
    );
    const rewired = [...dependent.data.depends_on, ...allGone.map((t) => t.id)];
    let rewiring = true;
    const rewire = async (route) => {
      if (!rewiring) return route.fallback();
      const response = await route.fetch();
      const body = await response.json();
      body.objects.find((o) => o.id === dependent.id).data.depends_on = rewired;
      await route.fulfill({ response, json: body });
    };
    await jx.route((url) => url.pathname === `/api/businesses/${demoBid}`, rewire);
    await jx.reload();
    await onDemo();
    assert.deepEqual(
      await jx.evaluate((id) => find(id).data.depends_on, dependent.id),
      rewired,
      'the page holds the altered snapshot',
    );
    await jxStep(dependent);
    const needsFirst = (await prop('Needs these first')).join('\n');
    for (const t of allGone)
      assert(
        !needsFirst.includes(t.title),
        `“Needs these first” lists no replaced step: ${needsFirst}`,
      );
    rewiring = false;
    // The business form says how the jurisdiction is changed, and that it cannot change it.
    await jxRow('map:all').click();
    await jx.locator('#insp').getByRole('button', { name: 'Edit business', exact: true }).click();
    const intro = await jx.locator('#dialog #active-form > p').first().innerText();
    assert.equal(
      intro,
      'Changes reopen completed work for review. This form cannot change the jurisdiction: an owner or reviewer changes it as a separate, recorded decision with a reason, using the API or the foundry jurisdiction command.',
    );
    await jx.locator('#dialog').getByRole('button', { name: 'Cancel', exact: true }).click();
    // the page remembers the business last opened, for every page on this sign-in; the checks
    // below reload a page and expect the workflow test business, as it was before this block
    await jx.locator('#business-select').selectOption(bid);
    await jx.locator('#list .th-sub', { hasText: testName }).waitFor();
    await jx.close();

    // Stored addresses: only http and https become links. The server refuses anything else, so
    // the snapshot is altered on its way to the page. The currency code is one Intl rejects.
    await onBusiness(bid, testName);
    const snapPath = `/api/businesses/${bid}`;
    // once these checks are done, the route passes requests on rather than being removed
    let altering = true;
    const alter = async (route) => {
      if (!altering) return route.fallback();
      const response = await route.fetch();
      const body = await response.json();
      const tasks = body.objects.filter((o) => o.kind === 'task' && !o.data.deal_id);
      tasks[0].data.source_uri = 'javascript:window.injected=true';
      tasks[1].data.source_uri = 'https://example.org/synthetic-guide';
      body.objects.find((o) => o.kind === 'evidence').data.source_uri =
        'data:text/html,<script>window.injected=true</script>';
      body.business.data.currency = '<img class="inj" src=x>';
      await route.fulfill({ response, json: body });
    };
    await race.route((url) => url.pathname === snapPath, alter);
    await race.reload();
    await race.locator(OPENED).waitFor();
    const altered = await (await context.request.get(origin + snapPath)).json();
    const [t0, t1] = altered.objects.filter((o) => o.kind === 'task' && !o.data.deal_id);
    const openStep = async (t) => {
      await race.evaluate((id) => go(`journey|step:${id}`), t.id);
      await race.locator('#insp-title', { hasText: t.title }).waitFor();
      await quiet(race);
    };
    await openStep(t0);
    assert.equal(await race.locator('#insp a[href^="javascript"]').count(), 0);
    assert.equal(
      await race.locator('#insp .props .uri').innerText(),
      'javascript:window.injected=true',
      'a javascript: address is shown as text',
    );
    await openStep(t1);
    assert.equal(
      await race.locator('#insp a[href="https://example.org/synthetic-guide"]').count(),
      1,
      'an https address is still a link',
    );
    await raceSource('evidence');
    await race.locator('#list .row').filter({ hasText: 'Browser synthetic receipt' }).click();
    await race.locator('#insp pre.original').waitFor();
    await quiet(race);
    assert.equal(await race.locator('#insp a[href^="data:"]').count(), 0);
    assert((await race.locator('#insp .props .uri').innerText()).startsWith('data:text/html,'));
    // money() under a rejected currency code: the amount, as text, and no markup.
    await raceSource('sales');
    await quiet(race);
    assert.equal(await race.locator('.inj').count(), 0, 'no markup from a currency code');
    assert.match(await race.locator('#list').innerText(), /100\.00 imgclassinjsrcx/);
    altering = false;
    const fmt = await race.evaluate(() => ({
      out: [
        money('1<img src=x onerror=alert(1)>', 'U<b>"'),
        money('5', '"><img src=x>'),
        ratio('<b>'),
        percent('<i>'),
        shortMoney(1500),
      ],
      usd: [
        money('1234.5', 'USD'),
        new Intl.NumberFormat(undefined, {
          style: 'currency',
          currency: 'USD',
          minimumFractionDigits: 2,
          maximumFractionDigits: 2,
        }).format('1234.5'),
      ],
    }));
    for (const text of fmt.out) assert(!/[<>&"]/.test(text), `formatter output is text: ${text}`);
    assert.equal(fmt.usd[0], fmt.usd[1], 'a valid currency formats as before');
    // The sign that picks the shortfall glyph is read from the decimal string, not a float.
    const signs = await race.evaluate(() =>
      ['0.00', '-0.00', '12.50', '-3', '0.001', '1E+3', 'abc', null, '', '+0', '-1E-30', ' 7 '].map(
        decimalSign,
      ),
    );
    assert.deepEqual(signs, [0, 0, 1, -1, 1, 1, 0, 0, 0, 0, -1, 1]);

    // Signing out, and a 401, leave nothing of the last person's records in the page or memory.
    const forgotten = () => pageForgotten(race);
    const blank = blankPage;
    await raceSource('history');
    await race.locator('#list .grp', { hasText: 'Events' }).waitFor();
    await race.locator('.toolbar [data-action="logout"]').click();
    await race.getByLabel('Access key', { exact: true }).waitFor();
    await quiet(race);
    assert.deepEqual(await forgotten(), blank, 'signed out');
    await race
      .getByLabel('Access key', { exact: true })
      .fill(readFileSync(join(temp, 'owner.key'), 'utf8').trim());
    await race.getByRole('button', { name: 'Open workspace' }).click();
    await race.locator(OPENED).waitFor();
    await context.clearCookies();
    await race.locator('#src-nav .src[data-src="history"]').click();
    await race.getByLabel('Access key', { exact: true }).waitFor();
    await quiet(race);
    assert.deepEqual(await forgotten(), blank, 'after a 401');
    await race.close();

    // An empty installation (init, no demo) opens on a first-run screen that makes sense.
    mkdirSync(emptyDir);
    const init = spawnSync('uv', ['run', 'foundry', '--db', emptyDb, 'init'], { encoding: 'utf8' });
    assert.equal(init.status, 0, init.stderr);
    let emptyOrigin;
    ({ server: emptyServer, origin: emptyOrigin } = await startServer(python, emptyDb, logs));
    const fresh = await newPage(browser, { viewport: { width: 1440, height: 1000 } });
    fresh.on('pageerror', (e) => errors.push(e.message));
    await signIn(fresh, emptyOrigin, join(emptyDir, 'owner.key'));
    await fresh.getByRole('heading', { name: 'Start your first business' }).waitFor();
    assert(await fresh.getByRole('button', { name: 'Create a business' }).isVisible());
    assert(await fresh.locator('#insp [data-map="empty"]').isVisible());
    await fresh.screenshot({ path: 'test-results/empty-desktop.png' });
    await walkLists(fresh, 'no business yet');
    await fresh.setViewportSize({ width: 390, height: 844 });
    await quiet(fresh);
    assert.equal(await overflow(fresh), false, 'empty workspace: mobile overflow');
    await fresh.close();

    await quiet(page);
    assert.equal(await page.evaluate(() => Boolean(window.injected)), false);
    assert.deepEqual(errors, []);
    console.log(
      'Browser checks passed: login, sources, Start here (a new business opening on it and a business whose path is done on Today, Do this next opening its form or the step’s decision form, a line’s ? by keyboard, tap and as a screen reader meets it, why a step waits, the start-up lines explained on an acquisition, Today naming the same next action with the Acquisition line first, an older analysis whose evidence changed offering to rule its steps out in Today’s Do this next, its line’s heading and ?, the lines explained and the step, each opening the decision form at not applicable with its evidence, the note on ready steps true or absent, starting as a step’s main move, the assistant advice for a served and a desktop owner, at three widths in light and dark), line navigation, search, row and list roles, keyboard focus after re-renders and phone pane changes, line colours as computed in light and dark, Journey map (wide, tall, keyboard), workspace creation, records, evidence review and versions, task decisions, four distinct states, forecast, acquisition (refused without its three required figures, a refusal caused by an example value naming it in the API’s words, empty loans meaning no loan and the page saying no debt was given, figures left empty not sent and listed as not given, a reopened form filling exactly what each kind of saved analysis gave, every figure read, with assumed figures, the rate and term of a loan it did not have and an earlier analysis’s example values empty, and a loan amount entered alone sending no rate or term, which the page then lists as not given, and a business with its building, a property loan, a deferred seller note and investors: empty figures not sent, the balloon, buyer cash and lender coverage shown, and the form reopened with what was given), after two jurisdiction changes, the server’s reasons on earlier analyses and replaced steps off the map, out of every count and open read-only, document, invoice, payment, persistence, integrity, evidence withdrawal reopening work, XSS text handling, late responses (another business, a save, overlapping refreshes), saved changes and accepted decisions whose reload fails (out-of-date notice, its reload control, a pending switch let go, the same key on a failed decision), late answers kept to their own session and business (a new access key after sign-out, after a new sign-in or waiting when a 401 ends the session, discarded; a key waiting for an open form, shown once and kept open across a change of business; forms, the decision form and record views of one business closed when another loads, including one that leaves the list; a form sent during a pending switch saving to its own business; saves, decisions and refusals for one business reported by name on another’s page without marking it out of date; the business form sending the version it was filled from), interrupted commands that say what is true (a lost key creation, a closed form, revoke, sign-out and sign-in), a refused sign-out leaving no message, a sign-out answer held past a 401 and a new sign-in leaving the new session signed in, a sign-in changed in another tab (the previous person forgotten and the change followed, a held access key discarded with and without the signal and when someone with the same role signs in, a session ended by the server in one tab ending in the other, a closed form named, no loop, other messages ignored, a held sign-out cancelled by a sign-in elsewhere), two answers to who is signed in in one tab (an earlier answer naming someone else or refusing, arriving after that tab’s own sign-in, before its answer or in order, and an answer for a session the tab ended: only the latest start in its session acts, a closed form still named after a second change, and the sentence dropped by a sign-out of the tab’s own), an owner’s + New business hidden while a tab does not know an owner is signed in, and an owner’s forms opening for no one else, a reviewer and an operator shown none of an owner’s controls and opening none of an owner’s forms, with the record form and step decisions of their role, the check of who is signed in before a new key is shown (shown once, checked again after a dialog opened during it, or a notice that waits its turn), open document and record-data views kept to each reload (a warning gained and lost, scroll and focus kept, closed when the item leaves), evidence used through earlier decisions by the server’s own rule (unreadable histories and replaced steps included), a step’s view whose history holds entries that are not objects, each sentence of that rule told apart by a served history, a new key kept from a second person with the same role and name, a reload that changes nothing leaving an open view’s selection and reasons in place, a viewer’s open record view following a change, a document’s reasons from either server, stale-document reasons from the server (one rule for whether a draft is out of date, the same words in its source, row, inspector, full view, Today and every list that names it, each reason visible and above the draft’s text, plain words when none is listed, and no reason of the page’s own), the reasons on a deal analysis or forecast (one that cannot be read left out, the others in the server’s order and as text, plain words when none can be read), the backup command a desktop owner is shown (the server’s database path and command in the backup panel and the integrity view, the path form when no command is ready, shown as text, and the plain command without the field or with one that cannot be read), focus after Dismiss on a computer and a phone, focus after a dialog closes over a page redrawn behind it (a new key, a key not confirmed, a key that waited behind a form, a form cancelled or saved after a reload, and on a phone), lists that hold only list items on every source at three widths, link schemes, markup-free formatting, sign-out and 401 clearing, phone and medium layout, and the empty first run.',
    );
  } catch (error) {
    console.error(logs.slice(-8).join(''));
    throw error;
  } finally {
    // Every wait here is bounded: 10 seconds for the browser, and 8 for each server.
    if (browser && !(await within(browser.close(), 10000)))
      console.error(
        'The browser did not close within 10 seconds; it is ended when this test exits.',
      );
    try {
      await stopServer(server);
    } finally {
      try {
        await stopServer(emptyServer);
      } finally {
        rmSync(temp, { recursive: true, force: true });
      }
    }
  }
}
/* The test ends once main() has, passing or failing, after what it printed has been written.
   Playwright kills a browser it launched when this process exits, so no browser is left. */
function end(code) {
  let left = 2;
  const done = () => --left || process.exit(code);
  process.stdout.write('', done);
  process.stderr.write('', done);
}
main().then(
  () => end(0),
  (error) => {
    console.error(error);
    end(1);
  },
);
