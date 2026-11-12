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
async function main() {
  const temp = mkdtempSync(join(tmpdir(), 'foundry-browser-')),
    db = join(temp, 'foundry.db');
  let server, browser;
  const logs = [],
    errors = [];
  try {
    const python = spawnSync('uv', ['run', 'python', '-c', 'import sys; print(sys.executable)'], {
      encoding: 'utf8',
    }).stdout.trim();
    const seed = spawnSync('uv', ['run', 'foundry', '--db', db, 'demo'], { encoding: 'utf8' });
    assert.equal(seed.status, 0, seed.stderr);
    const port = await freePort(),
      origin = `http://127.0.0.1:${port}`;
    server = spawn(python, ['-m', 'foundry.cli', '--db', db, 'serve', '--port', String(port)], {
      stdio: ['ignore', 'pipe', 'pipe'],
    });
    server.stderr.on('data', (d) => logs.push(d.toString()));
    for (let i = 0; i < 100; i++) {
      try {
        if ((await fetch(origin + '/api/health')).ok) break;
      } catch {}
      await pause(100);
    }
    browser = await chromium.launch({ headless: true });
    const context = await browser.newContext({ viewport: { width: 1440, height: 1050 } }),
      page = await context.newPage();
    page.on('pageerror', (e) => errors.push(e.message));
    page.on('console', (m) => {
      if (m.type() === 'error' && !m.text().includes('401')) errors.push(m.text());
    });
    await page.goto(origin);
    await page
      .getByLabel('Access key', { exact: true })
      .fill(readFileSync(join(temp, 'owner.key'), 'utf8').trim());
    await page.getByRole('button', { name: 'Open workspace' }).click();
    await page.getByRole('heading', { name: 'Make the next decision count.' }).waitFor();
    mkdirSync('test-results', { recursive: true });
    await page.screenshot({ path: 'test-results/overview-desktop.png', fullPage: true });
    const nav = async (name) => {
      await page.getByRole('button', { name, exact: true }).click();
    };
    const saved = async () => {
      await page.locator('#dialog').waitFor({ state: 'hidden' });
      await page.getByRole('status').filter({ hasText: 'Saved to the workspace.' }).waitFor();
    };
    const submit = async (name) => {
      await page.locator('#dialog').getByRole('button', { name, exact: true }).click();
      await saved();
    };
    for (const name of [
      'Work & decisions',
      'Business records',
      'Evidence',
      'Financial model',
      'Acquisitions',
      'Documents',
      'Sales to cash',
      'History',
      'Access & AI',
    ]) {
      await nav(name);
      await page.locator('#content h1').waitFor();
      assert((await page.locator('#content').innerText()).length > 100);
    }
    // Backup panel. Only the desktop window's server adds `desktop` to an owner's /api/me, so this route
    // adds it here. Without the field the panel is exactly what `foundry serve` has always shown.
    {
      const panel = page
        .locator('#content details')
        .filter({ has: page.locator('summary', { hasText: 'Backup and restore' }) });
      const settingsPage = async () => {
        await page.reload();
        await page.getByRole('heading', { name: 'Make the next decision count.' }).waitFor();
        await nav('Access & AI');
        await panel.waitFor({ state: 'attached' });
      };
      const served =
        '<summary>Backup and restore</summary><p class="small">Run <code>uv run foundry backup /private/path/backup.db</code>. Restore into a new database path using <code>uv run foundry --db /private/path/restored.db restore /private/path/backup.db</code>. Backups contain credential hashes and all businesses; protect them.</p>';
      await settingsPage();
      assert.equal(await panel.innerHTML(), served);
      let field;
      const addField = async (route) => {
        const response = await route.fetch();
        await route.fulfill({ response, json: { ...(await response.json()), desktop: field } });
      };
      await page.route('**/api/me', addField);
      const restoreText =
        'To restore, put the backup in a new folder: uv run foundry --db /private/path/restored/foundry.db restore /private/path/backup.db. Then open that folder in the app: uv run foundry-desktop --data-dir /private/path/restored.';
      const protectText = 'Backups contain credential hashes and all businesses; protect them.';
      const shown = async () => (await panel.locator('p').allTextContents()).map((t) => t.trim());
      const database = '/Users/founder/Library/Application Support/Business Foundry/foundry.db';
      const command = `uv run foundry --db '${database}' backup /private/path/backup.db`;
      field = { database, backup_command: command };
      await settingsPage();
      assert.deepEqual(await shown(), [
        `Business Foundry keeps your records in ${database}.`,
        `To back them up, open a terminal in the folder you start Business Foundry from. Then run ${command}. Change /private/path/backup.db to where you want the backup saved.`,
        restoreText,
        protectText,
      ]);
      assert.equal(await panel.locator('code').nth(1).textContent(), command);
      const hostile =
        '/Users/a&b/"q" \'x\'/<img src=x onerror="window.injectedBackup=true"><b>bold</b>/foundry.db';
      field = {
        database: hostile,
        backup_command: `uv run foundry --db ${hostile} backup &amp; done`,
      };
      await settingsPage();
      assert.equal(await panel.locator('code').nth(0).textContent(), hostile);
      assert.equal(await panel.locator('code').nth(1).textContent(), field.backup_command);
      assert.equal(await panel.locator('img, b').count(), 0);
      field = { database, backup_command: null };
      await settingsPage();
      assert.deepEqual(await shown(), [
        `Business Foundry keeps your records in ${database}.`,
        'To back them up, open a terminal in the folder you start Business Foundry from. Then run uv run foundry --db DATABASE_PATH backup /private/path/backup.db. Put the path above, quoted for your shell, in place of DATABASE_PATH. Change /private/path/backup.db to where you want the backup saved.',
        restoreText,
        protectText,
      ]);
      await page.unroute('**/api/me', addField);
      await settingsPage();
      assert.equal(await panel.innerHTML(), served);
      assert.equal(await page.evaluate(() => Boolean(window.injectedBackup)), false);
    }
    // A jurisdiction change made through the API: the demo's earlier deal analysis and forecast show the
    // server's own sentence, and the page composes none of its own.
    const demo = await page.locator('#business-select').inputValue();
    const demoUrl = origin + '/api/businesses/' + demo;
    const changed = await context.request.post(demoUrl + '/jurisdiction', {
      headers: { Origin: origin, 'Idempotency-Key': 'browser-jurisdiction-change' },
      data: {
        expected_version: (await (await context.request.get(demoUrl)).json()).business.version,
        jurisdiction: 'GB',
        reason: 'Synthetic browser check of earlier analyses after a jurisdiction change.',
        evidence_ids: [],
      },
    });
    assert.equal(changed.status(), 200, await changed.text());
    const marked = await (await context.request.get(demoUrl)).json();
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
    await page.reload();
    await page.getByRole('heading', { name: 'Make the next decision count.' }).waitFor();
    await page.locator('#content .notice').filter({ hasText: planReason }).waitFor();
    await nav('Acquisitions');
    await page.locator('#content .notice').filter({ hasText: dealReason }).waitFor();
    assert.equal(await page.getByText('Supporting evidence changed.', { exact: false }).count(), 0);
    await nav('Financial model');
    await page.locator('#content .notice').filter({ hasText: planReason }).waitFor();
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
    await nav('Business records');
    await page.getByRole('button', { name: '+ Add record', exact: true }).click();
    await page.getByLabel('Title', { exact: true }).fill('Browser customer');
    await page
      .getByLabel('Details', { exact: true })
      .fill('Synthetic buyer with a documented need.');
    await submit('Save record');
    await nav('Evidence');
    await page.getByRole('button', { name: '+ Add evidence', exact: true }).click();
    await page.getByLabel('Evidence title').fill('Browser synthetic receipt');
    await page.getByLabel('Source type').selectOption('external_receipt');
    await page
      .getByLabel('Original source text', { exact: false })
      .fill(
        'SYNTHETIC: agreement, acceptance, issue and payment test receipt. <img src=x onerror="window.injected=true">',
      );
    await submit('Save unverified evidence');
    await page.locator('#content').getByRole('button', { name: 'Open', exact: true }).click();
    assert.equal(await page.locator('#dialog img').count(), 0);
    await page.getByRole('button', { name: 'Record human review' }).click();
    await page
      .getByLabel('Review note', { exact: false })
      .fill('Checked this synthetic receipt for the browser workflow test.');
    await submit('Mark evidence reviewed');
    const bid = await page.locator('#business-select').inputValue();
    const snapshot = await (await context.request.get(origin + '/api/businesses/' + bid)).json();
    const eid = snapshot.objects.find((o) => o.kind === 'evidence').id;
    const customer = snapshot.objects.find((o) => o.kind === 'customer').id;
    // Human task transition: prepared -> approved -> verified, with source evidence.
    await nav('Work & decisions');
    for (const next of ['prepared', 'approved', 'verified']) {
      await page
        .locator('.task-row')
        .filter({ hasText: 'Establish customer demand' })
        .getByRole('button', { name: 'Review', exact: true })
        .click();
      await page.getByLabel('Next state').selectOption(next);
      await page.getByLabel('Evidence for this decision', { exact: false }).selectOption([eid]);
      await page
        .getByLabel('Decision note', { exact: false })
        .fill('Checked synthetic buyer evidence and recorded this decision.');
      await submit('Record decision');
    }
    await nav('Financial model');
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
    await nav('Acquisitions');
    await page.getByRole('button', { name: '+ Analyze a business', exact: true }).click();
    await page.getByLabel('Target business', { exact: true }).fill('Browser synthetic target');
    await page
      .getByLabel('Investment thesis', { exact: true })
      .fill('Acquire repeatable delivery capacity subject to independent diligence.');
    await submit('Calculate and create diligence work');
    await nav('Documents');
    await page.getByRole('button', { name: '+ Generate document', exact: true }).click();
    await submit('Generate draft');
    await page.getByRole('button', { name: 'Read draft' }).click();
    assert((await page.locator('#dialog pre').innerText()).includes('Browser customer'));
    // A current document: the server gives no reason, and the page shows no notice and says it is current.
    const current = (
      await (await context.request.get(origin + '/api/businesses/' + bid)).json()
    ).objects.find((o) => o.kind === 'document');
    assert.equal(current.stale, false);
    assert.deepEqual(current.stale_reasons, []);
    assert.equal(await page.locator('#dialog .notice').count(), 0);
    await page.getByRole('button', { name: 'Close dialog', exact: true }).click();
    const docCard = page.locator('#content .doc-card');
    const cardLine = async () => {
      assert.equal(await docCard.count(), 1);
      const text = await docCard.locator('p.small').innerText();
      return text.slice(text.indexOf(' · ') + 3);
    };
    assert.equal(await cardLine(), 'Reflects the current source records.');
    assert.equal(await docCard.locator('.badge.needs_review').count(), 0);
    // A changed record makes the document stale through the real server; the page prints the server's
    // own sentence and none of its own.
    const sourceChanged =
      'The business or its records have changed since this document was generated. Generate a new version before relying on it.';
    const pageSentences = [
      'Source records have changed',
      'Inputs have changed since this draft was generated',
    ];
    const noPageSentences = async () => {
      const text =
        (await page.locator('body').innerText()) + (await page.locator('#dialog').innerText());
      for (const sentence of pageSentences) assert(!text.includes(sentence), sentence);
    };
    const openDraft = async () => {
      await docCard.getByRole('button', { name: 'Read draft', exact: true }).click();
      await page.locator('#dialog pre.document').waitFor();
      return page.locator('#dialog .notice');
    };
    const closeDraft = () =>
      page.getByRole('button', { name: 'Close dialog', exact: true }).click();
    await nav('Business records');
    await page.getByRole('button', { name: '+ Add record', exact: true }).click();
    await page.getByLabel('Title', { exact: true }).fill('Browser second customer');
    await page
      .getByLabel('Details', { exact: true })
      .fill('Synthetic second buyer added after the document was generated.');
    await submit('Save record');
    const staleDoc = (
      await (await context.request.get(origin + '/api/businesses/' + bid)).json()
    ).objects.find((o) => o.kind === 'document');
    assert.equal(staleDoc.stale, true);
    assert.equal(staleDoc.stale_reasons.length, 1);
    assert.equal(staleDoc.stale_reasons[0].code, 'source_changed');
    assert.equal(staleDoc.stale_reasons[0].message, sourceChanged);
    const openDraftLine = 'Out of date. Open the draft to see why, then generate a new version.';
    const noReasonLine = 'Out of date. Generate a new version.';
    const noReasonNotice = 'This draft is out of date. Generate a new version.';
    await nav('Documents');
    await docCard.locator('.badge.needs_review').waitFor();
    assert.equal(await cardLine(), openDraftLine);
    await noPageSentences();
    let notices = await openDraft();
    assert.deepEqual(await notices.allTextContents(), [sourceChanged]);
    await noPageSentences();
    await closeDraft();
    // The page's own copy of the snapshot, served through a route: each case changes only the document.
    const snapshotPath = '/api/businesses/' + bid;
    let changeDocument = null;
    const snapshotRoute = (url) => url.pathname === snapshotPath;
    const serveSnapshot = async (route) => {
      const response = await route.fetch();
      const body = await response.json();
      if (changeDocument)
        body.objects = body.objects.map((o) => (o.kind === 'document' ? changeDocument(o) : o));
      await route.fulfill({ response, json: body });
    };
    await page.route(snapshotRoute, serveSnapshot);
    const showWith = async (change) => {
      changeDocument = change;
      await page.reload();
      await page.getByRole('heading', { name: 'Make the next decision count.' }).waitFor();
      await nav('Documents');
      await docCard.waitFor();
    };
    // Two reasons, in the server's order, with markup characters shown as text.
    const markupMessage =
      'This document says which obligations were past their due date as of 2030-06-01. <img src=x onerror=alert(1)> & "more" <b>bold</b>';
    await showWith((o) => ({
      ...o,
      stale: true,
      stale_reasons: [
        o.stale_reasons[0],
        {
          code: 'overdue_changed',
          as_of: '2030-06-01',
          today: '2030-06-02',
          message: markupMessage,
        },
      ],
    }));
    assert.equal(await cardLine(), openDraftLine);
    await docCard.locator('.badge.needs_review').waitFor();
    notices = await openDraft();
    assert.deepEqual(await notices.allTextContents(), [sourceChanged, markupMessage]);
    assert.equal(await page.locator('#dialog img').count(), 0);
    assert.equal(await page.locator('#dialog b').count(), 0);
    await noPageSentences();
    await closeDraft();
    // Stale with no reasons listed, the key missing and then empty: one notice that gives no reason.
    for (const change of [
      ({ stale_reasons, ...o }) => ({ ...o, stale: true }),
      (o) => ({ ...o, stale: true, stale_reasons: [] }),
    ]) {
      await showWith(change);
      assert.equal(await cardLine(), noReasonLine);
      await docCard.locator('.badge.needs_review').waitFor();
      notices = await openDraft();
      assert.deepEqual(await notices.allTextContents(), [noReasonNotice]);
      await noPageSentences();
      await closeDraft();
    }
    // Not stale, with an empty list: no notice, and the card says it is current.
    await showWith((o) => ({ ...o, stale: false, stale_reasons: [] }));
    assert.equal(await cardLine(), 'Reflects the current source records.');
    assert.equal(await docCard.locator('.badge.needs_review').count(), 0);
    notices = await openDraft();
    assert.equal(await notices.count(), 0);
    await closeDraft();
    // The rest of the flow runs against the real server.
    await page.unroute(snapshotRoute, serveSnapshot);
    changeDocument = null;
    await page.reload();
    await page.getByRole('heading', { name: 'Make the next decision count.' }).waitFor();
    await nav('Sales to cash');
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
    await page.locator('.badge.paid').waitFor();
    const final = await (await context.request.get(origin + '/api/businesses/' + bid)).json();
    assert.equal(final.stats.cash_collected, '100.00');
    assert.equal(final.stats.outstanding_invoices, '0.00');
    const audit = await (
      await context.request.get(origin + '/api/businesses/' + bid + '/audit')
    ).json();
    assert.equal(audit.integrity.valid, true);
    await page.reload();
    await nav('Overview');
    assert((await page.locator('#content').innerText()).includes('$100'));
    await page.setViewportSize({ width: 390, height: 844 });
    await page.reload();
    await page.locator('#content h1').waitFor();
    await page.screenshot({ path: 'test-results/overview-mobile.png', fullPage: true });
    for (const name of [
      'Overview',
      'Work & decisions',
      'Financial model',
      'Acquisitions',
      'Sales to cash',
      'Access & AI',
    ]) {
      await nav(name);
      await page.locator('#content h1').waitFor();
      assert.equal(
        await page.evaluate(() => document.documentElement.scrollWidth > innerWidth),
        false,
        `${name}: mobile horizontal overflow`,
      );
    }
    assert.equal(await page.evaluate(() => Boolean(window.injected)), false);
    assert.deepEqual(errors, []);
    console.log(
      'Browser checks passed: login, workspace creation, records, evidence review, task decisions, forecast, acquisition, marks on earlier analyses after a jurisdiction change, document, stale-document reasons from the server, invoice, payment, persistence, integrity, XSS text handling and mobile layout.',
    );
  } catch (error) {
    console.error(logs.slice(-8).join(''));
    throw error;
  } finally {
    if (browser) await browser.close();
    if (server) {
      server.kill('SIGTERM');
      await Promise.race([new Promise((r) => server.once('exit', r)), pause(3000)]);
    }
    rmSync(temp, { recursive: true, force: true });
  }
}
main().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
