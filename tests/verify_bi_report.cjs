const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { execFileSync } = require('node:child_process');
const { pathToFileURL } = require('node:url');
const { chromium } = require('/Users/harumi/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright');
const tower = path.resolve(__dirname, '..');
const api = require(path.join(tower, 'dist/bi-report.js'));

function checkAnalysis() {
  const a = { soo_no: 'A', waybill_id: 'SP-MERGED-1', production_date: '2026-10-08', warehouse_name: '仓一', carrier_name: '物流一', soo_qty: 10, is_loaded: true, tms_status: '运输', quality_flags: ['状态与时间冲突'], tms_created_at: '2026-10-09T08:00:00+08:00', load_completed_at: '2026-10-09T10:00:00+08:00', park_entered_at: '2026-10-09T09:00:00+08:00', park_exited_at: '2026-10-09T13:00:00+08:00' };
  const b = { ...a, soo_no: '<img src=x onerror=alert(1)>', soo_qty: 20, tms_status: '确认', quality_flags: [], load_completed_at: '2026-10-09T12:00:00+08:00', park_entered_at: null, park_exited_at: null };
  const c = { ...a, soo_no: 'C', production_date: '2026-10-07', soo_qty: 30 };
  const d = { soo_no: 'D', production_date: '2026-10-08', warehouse_name: '仓二', carrier_name: '物流二', soo_qty: 40, is_loaded: false, tms_status: null };
  const metrics = { rows: [a, b, c, d], planRows: [a, b, d], priorPlanRows: [a, b, d], waitingRows: [b], overdueRows: [b], plan: 70, done: 30, priorPlan: 70, actual: 10, waiting: 20, overdue: 20, cutoff: '2026-10-09' };
  const input = { metrics, dates: ['2026-10-09'], filters: { productionDates: '2026.10.08' }, source: { last_success_import_at: '2026-10-09T07:00:00+08:00' }, generatedAt: '2026-10-09T09:00:00+08:00' };
  const result = api.analyze(input);
  assert.equal(result.daily[0].done, 30);
  assert.equal(result.daily[0].actual, 10);
  assert.equal(result.planSpCount, 2);
  assert.deepEqual(result.statuses.map(item => [item.name, item.value]), [['已装载未提', 1], ['状态缺失', 1]]);
  assert.equal(result.qualityCount, 1);
  assert.equal(result.matrix.get('仓一').get('物流一'), 20);
  assert.equal(result.stages[0].p50, 2);
  assert.equal(result.stages[0].p90, 2);
  assert.equal(result.stages[1].invalid, 1);
  assert.equal(result.ageHours, 2);
  const selfPickup = { ...a, soo_no: 'SELF-PICKUP', waybill_id: 'SP-SELF', carrier_name: '承运商自提', soo_qty: 999, quality_flags: [] };
  const withSelfPickup = { ...metrics, rows: [...metrics.rows, selfPickup], planRows: [...metrics.planRows, selfPickup], priorPlanRows: [...metrics.priorPlanRows, selfPickup], plan: metrics.plan + 999, priorPlan: metrics.priorPlan + 999 };
  const filtered = api.analyze({ ...input, metrics: withSelfPickup });
  assert.equal(filtered.m.plan, 70);
  assert.equal(filtered.m.priorPlan, 70);
  assert.equal(filtered.planSpCount, result.planSpCount);
  assert.equal(filtered.daily[0].plan, result.daily[0].plan);
  assert(!filtered.scope.some(row => row.carrier_name.includes('自提')));
  assert(!api.build({ ...input, metrics: withSelfPickup }).includes('SELF-PICKUP'));
  const html = api.build(input);
  assert(html.includes('<span>生产日期</span><b>2026.10.08</b>'));
  assert(!html.includes('<img src=x onerror'));
  assert(html.includes('&lt;img src=x onerror=alert(1)&gt;'));
  assert(!html.includes('NaN') && !html.includes('Infinity'));
  const empty = { ...metrics, rows: [], planRows: [], priorPlanRows: [], waitingRows: [], overdueRows: [], plan: 0, done: 0, priorPlan: 0, actual: 0, waiting: 0, overdue: 0 };
  assert.equal(api.analyze({ ...input, metrics: empty }).stages[0].p50, null);
  assert(!api.build({ ...input, metrics: empty }).includes('NaN'));
}

async function verifyBrowser() {
  const snapshot = JSON.parse(execFileSync(path.join(tower, '.venv/bin/python'), ['-c', 'import json; from backend.sqlite_app import public_snapshot; print(json.dumps(public_snapshot(), ensure_ascii=False))'], { cwd: path.dirname(tower), env: { ...process.env, PYTHONPATH: tower }, encoding: 'utf8', maxBuffer: 32 * 1024 * 1024 }));
  const files = { '/': ['index.html', 'text/html'], '/bi-report.js': ['bi-report.js', 'application/javascript'], '/warehouse-carriers.json': ['warehouse-carriers.json', 'application/json'], '/outbound-data.json': ['outbound-data.json', 'application/json'] };
  const browser = await chromium.launch({ executablePath: '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome', headless: true });
  try {
    const context = await browser.newContext({ viewport: { width: 1440, height: 1000 }, acceptDownloads: true });
    await context.route('http://report.test/**', async route => {
      const url = new URL(route.request().url());
      if (url.pathname === '/data.json') return route.fulfill({ contentType: 'application/json', body: JSON.stringify(snapshot) });
      if (files[url.pathname]) return route.fulfill({ contentType: files[url.pathname][1], body: fs.readFileSync(path.join(tower, 'dist', files[url.pathname][0])) });
      return route.abort();
    });
    const page = await context.newPage(), errors = [];
    page.on('pageerror', error => errors.push(error.message));
    await page.goto('http://report.test/');
    await page.waitForFunction(() => state.data && state.dates.length && state.outboundSource);
    const spRule = await page.evaluate(() => {
      const rows = Array.from({ length: 120 }, (_, index) => ({ domain_code: 'bbk2b', soo_no: `SOO-${index}`, waybill_id: 'SP26100900060', soo_qty: 1, gross_cbm: index === 119 ? 0.486816 : 180.114368 }));
      const mixedVolumes = [{ waybill_id: 'SP-MIX', gross_cbm: 2 }, { waybill_id: 'SP-MIX', gross_cbm: 2 }, { waybill_id: 'SP-MIX', gross_cbm: 3 }, { waybill_id: 'SP-MIX', gross_cbm: 3 }];
      return { spCount: spCount(rows), pieces: sum(rows, 'soo_qty'), volume: sumVolume(rows), mixedVolume: sumVolume(mixedVolumes) };
    });
    assert.equal(spRule.spCount, 1);
    assert.equal(spRule.pieces, 120);
    assert.equal(spRule.volume, 180.114368);
    assert.equal(spRule.mixedVolume, 2);
    const businessDateCheck = await page.evaluate(() => {
      state.dates = [availableBusinessDates().at(-2) || availableBusinessDates()[0]];
      fillFilters(); render();
      const expectedProductionDate = prevDate(state.dates[0]);
      return { selectedBusinessDate: state.dates[0], expectedProductionDate,
        matches: dateRows().length > 0 && dateRows().every(row => row.production_date === expectedProductionDate || (row.production_date === state.dates[0] && row.production_at && row.production_at.slice(11, 19) <= '12:00:00')),
        displayedBusinessDates: availableBusinessDates() };
    });
    assert(businessDateCheck.matches, JSON.stringify(businessDateCheck));
    await page.evaluate(() => { state.dates = availableBusinessDates().slice(-3); fillFilters(); render(); });
    const expected = await page.evaluate(() => ({ plan: metric().plan, orders: metric().planRows.length, dates: state.dates, overdue: metric().overdueRows.length }));
    const outboundCheck = await page.evaluate(() => {
      const m = metric();
      return { shown: document.querySelector('#outbound-summary').innerText, source: document.querySelector('#outbound-source-note').innerText,
        count: m.outboundOrders, rows: m.outboundRows.length, matched: m.outboundRows.every(row => Boolean(actualShipmentAt(row))),
        qty: m.outboundQty, volume: m.outboundVolume };
    });
    assert(outboundCheck.shown.includes('已出库 SOO'));
    assert(outboundCheck.source.includes('AQ“实际发货时间”非空'));
    assert(outboundCheck.matched);
    assert.equal(outboundCheck.count, new Set((await page.evaluate(() => metric().outboundRows)).map(row => row.soo_no)).size);
    const output = process.env.BI_REPORT_OUTPUT_DIR || path.join(tower, 'outputs/bi-report'); fs.mkdirSync(output, { recursive: true });
    const downloadEvent = page.waitForEvent('download');
    await page.locator('#broadcast-btn').click();
    const download = await downloadEvent, reportPath = path.join(output, download.suggestedFilename());
    await download.saveAs(reportPath);
    assert.equal(errors.length, 0, errors.join('\n'));
    const report = await context.newPage(); report.on('pageerror', error => errors.push(error.message));
    await context.setOffline(true);
    await report.goto(pathToFileURL(reportPath).href);
    await report.locator('#detail-count').waitFor();
    assert.equal(await report.locator('h1').textContent(), '物流运营分析报告');
    assert.equal(await report.locator('svg').count(), 6);
    assert.equal(await report.locator('.kpi').first().locator('strong').textContent(), expected.plan.toLocaleString('zh-CN'));
    await report.screenshot({ path: path.join(output, 'report-desktop.png') });
    await report.locator('#performance').screenshot({ path: path.join(output, 'report-performance.png') });
    await report.locator('#chain').screenshot({ path: path.join(output, 'report-chain.png') });
    await report.locator('#risk').screenshot({ path: path.join(output, 'report-risk.png') });
    await report.locator('#detail-filter').selectOption('overdue');
    assert.equal(await report.locator('#detail-table tr[data-search]:not([hidden])').count(), expected.overdue);
    const csvEvent = report.waitForEvent('download'); await report.locator('#detail-csv').click();
    const csv = await csvEvent, csvPath = path.join(output, '超时明细验证.csv'); await csv.saveAs(csvPath);
    const csvBody = fs.readFileSync(csvPath, 'utf8'); assert(csvBody.startsWith('\ufeff')); assert.equal(csvBody.trim().split('\r\n').length, expected.overdue + 1);
    await report.setViewportSize({ width: 390, height: 844 }); await report.evaluate(() => window.scrollTo(0, 0));
    await report.screenshot({ path: path.join(output, 'report-mobile.png') });
    assert.equal(await report.evaluate(() => document.documentElement.scrollWidth > innerWidth), false, 'mobile page overflow');
    await report.setViewportSize({ width: 1440, height: 1000 }); await report.emulateMedia({ media: 'print' });
    await report.pdf({ path: path.join(output, `物流运营分析报告-${expected.dates[0]}_${expected.dates.at(-1)}.pdf`), format: 'A4', landscape: true, printBackground: true });
    assert.equal(errors.length, 0, errors.join('\n'));
    console.log(JSON.stringify({ reportPath, batch: snapshot.source.published_batch_id, expected, charts: 6, checks: ['analysis denominators', 'status vs quality', 'invalid durations', 'safe escaping', 'empty sample', 'dashboard download', 'offline report', 'CSV filter', 'mobile overflow', 'print PDF'], output }, null, 2));
  } finally { await browser.close(); }
}

checkAnalysis();
if (!process.env.BI_REPORT_ANALYSIS_ONLY) verifyBrowser().catch(error => { console.error(error); process.exitCode = 1; });
