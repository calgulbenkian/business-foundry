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

/* Start an isolated server on a temporary database. Never the repository's own .foundry/. */
async function startServer(python, db, logs) {
  const port = await freePort(),
    origin = `http://127.0.0.1:${port}`;
  const server = spawn(python, ['-m', 'foundry.cli', '--db', db, 'serve', '--port', String(port)], {
    stdio: ['ignore', 'pipe', 'pipe'],
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
async function stopServer(server) {
  if (!server) return;
  server.kill('SIGTERM');
  await Promise.race([new Promise((r) => server.once('exit', r)), pause(3000)]);
}
async function signIn(page, origin, keyFile) {
  await page.goto(origin);
  await page.getByLabel('Access key', { exact: true }).fill(readFileSync(keyFile, 'utf8').trim());
  await page.getByRole('button', { name: 'Open workspace' }).click();
}
const overflow = (page) => page.evaluate(() => document.documentElement.scrollWidth > innerWidth);
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
   the page never hears what it said. */
async function hold(page, path, { method = null, lose = false } = {}) {
  const match = (url) => url.pathname === path;
  const mine = (r) => match(new URL(r.url())) && (!method || r.method() === method);
  let open, seen;
  const gate = new Promise((r) => (open = r)),
    requested = new Promise((r) => (seen = r));
  const handler = async (route) => {
    if (method && route.request().method() !== method) return route.fallback();
    const response = await route.fetch();
    seen(response);
    await gate;
    if (lose) await route.abort('connectionreset');
    else await route.fulfill({ response });
  };
  await page.route(match, handler);
  return {
    requested,
    async release() {
      const finished = page.waitForEvent(lose ? 'requestfailed' : 'requestfinished', mine);
      open();
      await finished;
      await page.unroute(match, handler);
      // let the page's promise chains run on what arrived
      await pause(250);
    },
  };
}
/* Fail the next request for a path as a dropped connection does; later ones go through. A
   dropped command never reaches the server; a dropped read is a reload that fails. */
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
  return {
    // resolves once the failure has reached the page and its promise chains have run on it
    dropped: dropped.then(() => pause(250)),
    off: () => page.unroute(match, handler),
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
    browser = await chromium.launch({ headless: true });
    const context = await browser.newContext({ viewport: { width: 1440, height: 1050 } }),
      page = await context.newPage();
    page.on('pageerror', (e) => errors.push(e.message));
    page.on('console', (m) => {
      if (m.type() === 'error' && !m.text().includes('401')) errors.push(m.text());
    });
    let f; // what has focus, read after each step that re-renders the focused control
    await signIn(page, origin, join(temp, 'owner.key'));
    await page.locator('#list .today-head').waitFor();
    assert.equal(await page.locator('#list-title').innerText(), 'Today');
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
    // A new workspace shows every source's empty state; its lists follow the same rule.
    await walkLists(page, 'new workspace');
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
    assert.deepEqual([f.action, f.to, f.visible], ['tx', 'prepared', true], 'focus after Not yet');
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
    await submit('Calculate and create diligence work');
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
    assert.equal(await page.locator('#app').getAttribute('data-level'), 'list');
    assert.equal(await page.locator('#insp').isVisible(), false);
    await page.screenshot({ path: 'test-results/overview-mobile.png', fullPage: true });
    const inPane = async (pane, what) => {
      const at = await focused(page);
      assert(at.visible && at.pane === pane, `${what}: focus is visible in the ${pane} pane`);
      return at;
    };
    await page.locator('#list .row').first().click();
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
      assert.equal(await overflow(page), false, `${id}: mobile horizontal overflow (list)`);
      await inPane(id === 'journey' ? 'inspector' : 'list', `${id}: source chosen`);
      if (id !== 'journey') {
        await page.locator('#list .row').first().click();
        await inPane('inspector', `${id}: row chosen`);
      }
      await page.locator('#insp #insp-title').waitFor();
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
    const race = await context.newPage();
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
    await race.locator('#list .today-head').waitFor();
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
    await race.locator('#list .today-head').waitFor();
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
      await race.locator('#list .today-head').waitFor();
    };
    const signOut = async () => {
      await race.locator('.toolbar [data-action="logout"]').click();
      await race.getByLabel('Access key', { exact: true }).waitFor();
    };
    const settle = async () => {
      await race.unrouteAll({ behavior: 'ignoreErrors' });
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
      await pause(300);
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
      await pause(300);
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
      await pause(300);
      assert.equal(await dialogTitle(), 'Create a business', 'the key does not replace the form');
      await context.clearCookies();
      await race
        .getByLabel('Concept', { exact: true })
        .fill('Synthetic concept refused because the session has ended.');
      await inDialog('Create workspace');
      await race.getByLabel('Access key', { exact: true }).waitFor();
      await pause(300);
      assert.equal(await dialogOpen(), false, 'nothing opens over the sign-in page');
      assert(!(await pageHolds(secret)), 'the waiting key left with the session');
      await signInAgain();
      await pause(300);
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
        await pause(300);
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
        await pause(300);
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
        await pause(300);
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
      await pause(300);
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
      await pause(300);
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
        const v = await viewer.newPage();
        v.on('pageerror', (e) => errors.push(e.message));
        await v.goto(origin);
        await v.getByLabel('Access key', { exact: true }).fill(viewerKey);
        await v.getByRole('button', { name: 'Open workspace' }).click();
        await v.locator('#list .today-head').waitFor();
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
      // the next list of businesses, built by hand, no longer holds the one on screen
      const listPath = (url) => url.pathname === '/api/businesses';
      const without = async (route) => {
        if (route.request().method() !== 'GET') return route.fallback();
        const response = await route.fetch();
        const json = (await response.json()).filter((x) => x.id !== bid);
        await route.fulfill({ response, json });
      };
      await race.route(listPath, without);
      await held.release();
      await showing(demoName);
      await race.unroute(listPath, without);
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
      await race.locator('#list .today-head').waitFor();
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
      await race.locator('#list .today-head').waitFor();
    });
    lateCase('a sign-out refused because the session ended leaves no message', async () => {
      await context.clearCookies();
      await race.locator('.toolbar [data-action="logout"]').click();
      await race.getByLabel('Access key', { exact: true }).waitFor();
      await pause(300);
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
      assert.equal(await race.locator('#list').isVisible(), false);
      await outdated.getByRole('button', { name: 'Dismiss' }).focus();
      await race.keyboard.press('Enter');
      at = await focused(race);
      assert.deepEqual([at.id, at.visible], ['insp-title', true], 'phone: the inspector heading');
    });

    race.setDefaultTimeout(10000);
    const lateFailures = [];
    for (const [name, run] of late) {
      await settle();
      try {
        await run();
      } catch (error) {
        lateFailures.push(`${name}: ${String(error.message).split('\n')[0]}`);
      }
    }
    await settle();
    race.setDefaultTimeout(30000);
    assert.deepEqual(lateFailures, [], 'late answers act only on their own session and business');

    // Stored addresses: only http and https become links. The server refuses anything else, so
    // the snapshot is altered on its way to the page. The currency code is one Intl rejects.
    await onBusiness(bid, testName);
    const snapPath = `/api/businesses/${bid}`;
    const alter = async (route) => {
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
    await race.locator('#list .today-head').waitFor();
    const altered = await (await context.request.get(origin + snapPath)).json();
    const [t0, t1] = altered.objects.filter((o) => o.kind === 'task' && !o.data.deal_id);
    const openStep = async (t) => {
      await race.evaluate((id) => go(`journey|step:${id}`), t.id);
      await race.locator('#insp-title', { hasText: t.title }).waitFor();
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
    assert.equal(await race.locator('#insp a[href^="data:"]').count(), 0);
    assert((await race.locator('#insp .props .uri').innerText()).startsWith('data:text/html,'));
    // money() under a rejected currency code: the amount, as text, and no markup.
    await raceSource('sales');
    assert.equal(await race.locator('.inj').count(), 0, 'no markup from a currency code');
    assert.match(await race.locator('#list').innerText(), /100\.00 imgclassinjsrcx/);
    await race.unroute((url) => url.pathname === snapPath, alter);
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
    const forgotten = () =>
      race.evaluate(() => ({
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
    const blank = {
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
    await raceSource('history');
    await race.locator('#list .grp', { hasText: 'Events' }).waitFor();
    await race.locator('.toolbar [data-action="logout"]').click();
    await race.getByLabel('Access key', { exact: true }).waitFor();
    assert.deepEqual(await forgotten(), blank, 'signed out');
    await race
      .getByLabel('Access key', { exact: true })
      .fill(readFileSync(join(temp, 'owner.key'), 'utf8').trim());
    await race.getByRole('button', { name: 'Open workspace' }).click();
    await race.locator('#list .today-head').waitFor();
    await context.clearCookies();
    await race.locator('#src-nav .src[data-src="history"]').click();
    await race.getByLabel('Access key', { exact: true }).waitFor();
    assert.deepEqual(await forgotten(), blank, 'after a 401');
    await race.close();

    // An empty installation (init, no demo) opens on a first-run screen that makes sense.
    mkdirSync(emptyDir);
    const init = spawnSync('uv', ['run', 'foundry', '--db', emptyDb, 'init'], { encoding: 'utf8' });
    assert.equal(init.status, 0, init.stderr);
    let emptyOrigin;
    ({ server: emptyServer, origin: emptyOrigin } = await startServer(python, emptyDb, logs));
    const fresh = await browser.newPage({ viewport: { width: 1440, height: 1000 } });
    fresh.on('pageerror', (e) => errors.push(e.message));
    await signIn(fresh, emptyOrigin, join(emptyDir, 'owner.key'));
    await fresh.getByRole('heading', { name: 'Start your first business' }).waitFor();
    assert(await fresh.getByRole('button', { name: 'Create a business' }).isVisible());
    assert(await fresh.locator('#insp [data-map="empty"]').isVisible());
    await fresh.screenshot({ path: 'test-results/empty-desktop.png' });
    await walkLists(fresh, 'no business yet');
    await fresh.setViewportSize({ width: 390, height: 844 });
    assert.equal(await overflow(fresh), false, 'empty workspace: mobile overflow');
    await fresh.close();

    assert.equal(await page.evaluate(() => Boolean(window.injected)), false);
    assert.deepEqual(errors, []);
    console.log(
      'Browser checks passed: login, sources, line navigation, search, row and list roles, keyboard focus after re-renders and phone pane changes, line colours as computed in light and dark, Journey map (wide, tall, keyboard), workspace creation, records, evidence review and versions, task decisions, four distinct states, forecast, acquisition, document, invoice, payment, persistence, integrity, evidence withdrawal reopening work, XSS text handling, late responses (another business, a save, overlapping refreshes), saved changes and accepted decisions whose reload fails (out-of-date notice, its reload control, a pending switch let go, the same key on a failed decision), late answers kept to their own session and business (a new access key after sign-out, after a new sign-in or waiting when a 401 ends the session, discarded; a key waiting for an open form, shown once and kept open across a change of business; forms, the decision form and record views of one business closed when another loads, including one that leaves the list; a form sent during a pending switch saving to its own business; saves, decisions and refusals for one business reported by name on another’s page without marking it out of date; the business form sending the version it was filled from), interrupted commands that say what is true (a lost key creation, a closed form, revoke, sign-out and sign-in), a refused sign-out leaving no message, focus after Dismiss on a computer and a phone, lists that hold only list items on every source at three widths, link schemes, markup-free formatting, sign-out and 401 clearing, phone and medium layout, and the empty first run.',
    );
  } catch (error) {
    console.error(logs.slice(-8).join(''));
    throw error;
  } finally {
    if (browser) await browser.close();
    await stopServer(server);
    await stopServer(emptyServer);
    rmSync(temp, { recursive: true, force: true });
  }
}
main().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
