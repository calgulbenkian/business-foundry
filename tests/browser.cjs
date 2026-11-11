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
    await signIn(page, origin, join(temp, 'owner.key'));
    await page.locator('#list .today-head').waitFor();
    assert.equal(await page.locator('#list-title').innerText(), 'Today');
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
    // The evidence versions come from the recorded history.
    await page.locator('#insp [data-versions] .att-row').nth(1).waitFor();
    assert((await page.locator('#insp [data-versions]').innerText()).includes('Version 2'));
    const bid = await page.locator('#business-select').inputValue();
    const snapshot = await (await context.request.get(origin + '/api/businesses/' + bid)).json();
    const eid = snapshot.objects.find((o) => o.kind === 'evidence').id;
    const customer = snapshot.objects.find((o) => o.kind === 'customer').id;
    // Human task transition: prepared -> approved -> verified, with source evidence.
    await lineSource('Market');
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
    await page.locator('#list .row').first().click();
    assert.equal(await page.locator('#app').getAttribute('data-level'), 'detail');
    assert(await page.locator('#back-list').isVisible());
    assert.equal(await page.locator('#list').isVisible(), false);
    await page.locator('#back-list').click();
    assert(await page.locator('#list').isVisible());
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
      await page.locator(`#src-nav .src[data-src="${id}"]`).click();
      assert.equal(await overflow(page), false, `${id}: mobile horizontal overflow (list)`);
      if (id !== 'journey') await page.locator('#list .row').first().click();
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
    await fresh.setViewportSize({ width: 390, height: 844 });
    assert.equal(await overflow(fresh), false, 'empty workspace: mobile overflow');
    await fresh.close();

    assert.equal(await page.evaluate(() => Boolean(window.injected)), false);
    assert.deepEqual(errors, []);
    console.log(
      'Browser checks passed: login, sources, line navigation, search, Journey map (wide, tall, keyboard), workspace creation, records, evidence review and versions, task decisions, four distinct states, forecast, acquisition, document, invoice, payment, persistence, integrity, evidence withdrawal reopening work, XSS text handling, phone and medium layout, and the empty first run.',
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
