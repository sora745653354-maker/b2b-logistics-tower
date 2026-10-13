const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

const html = fs.readFileSync(new URL('../dist/index.html', `file://${__filename}`), 'utf8');
const start = html.indexOf('    const rowBusinessDates =');
const end = html.indexOf('    const pickupPendingStatuses', start);
assert(start >= 0 && end > start, 'business-date helpers must remain in the page script');

const nextDate = day => {
  const date = new Date(`${day}T00:00:00Z`);
  date.setUTCDate(date.getUTCDate() + 1);
  return date.toISOString().slice(0, 10);
};
const startWindow = html.indexOf('    const prevDate =');
const endWindow = html.indexOf('    const containsSelfPickup', startWindow);
assert(startWindow >= 0 && endWindow > startWindow, 'recent business-date window helper must remain in the page script');
const windowSource = html.slice(startWindow, endWindow);
const recentDates = vm.runInNewContext(`${windowSource}; recentBusinessDates(['2026-10-07','2026-10-08','2026-10-09','2026-10-10','2026-10-11'], '2026-10-10')`, {});
assert.deepEqual(Array.from(recentDates), ['2026-10-08', '2026-10-09', '2026-10-10']);
const recentDatesAcrossMonth = vm.runInNewContext(`${windowSource}; recentBusinessDates(['2026-09-30','2026-10-01','2026-10-02','2026-10-03'], '2026-10-03')`, {});
assert.deepEqual(Array.from(recentDatesAcrossMonth), ['2026-10-01', '2026-10-02', '2026-10-03']);
const helperSource = html.slice(start, end);
const cases = [
  ['2026-10-10T00:00:00+08:00', ['2026-10-10', '2026-10-11']],
  ['2026-10-10T11:59:59+08:00', ['2026-10-10', '2026-10-11']],
  ['2026-10-10T12:00:00+08:00', ['2026-10-10', '2026-10-11']],
  ['2026-10-10T12:00:01+08:00', ['2026-10-11']],
  ['2026-10-10T23:59:59+08:00', ['2026-10-11']],
];
for (const [production_at, expected] of cases) {
  const result = vm.runInNewContext(`${helperSource}; rowBusinessDates({production_date:'2026-10-10', production_at})`, {
    state: { dates: ['2026-10-10'] }, nextDate, production_at,
  });
  assert.deepEqual(Array.from(result), expected, production_at);
}

const isSelected = vm.runInNewContext(`${helperSource}; rowMatchesSelectedBusinessDates({production_date:'2026-10-10', production_at:'2026-10-10T11:59:59+08:00'})`, {
  state: { dates: ['2026-10-10'] }, nextDate,
});
assert.equal(isSelected, true);
const productionDateMatchesAll = vm.runInNewContext(`${helperSource}; rowMatchesSelectedProductionDates({production_date:'2026-10-10'})`, {
  state: { productionDates: null },
});
assert.equal(productionDateMatchesAll, true, 'production-date filter defaults to no restriction');
const productionDateMatchesSelection = vm.runInNewContext(`${helperSource}; rowMatchesSelectedProductionDates({production_date:'2026-10-10'})`, {
  state: { productionDates: ['2026-10-10'] },
});
assert.equal(productionDateMatchesSelection, true);
const productionDateRejectsOtherDay = vm.runInNewContext(`${helperSource}; rowMatchesSelectedProductionDates({production_date:'2026-10-09'})`, {
  state: { productionDates: ['2026-10-10'] },
});
assert.equal(productionDateRejectsOtherDay, false);
const filterLabels = ['提货日期（可多选）', '生产日期（可多选）', 'for="warehouse-toggle">仓库', 'for="region-toggle">统仓区域', 'for="channel-toggle">渠道', 'for="carrier-toggle">承运商'];
const filterPositions = filterLabels.map(label => html.indexOf(label));
assert(filterPositions.every(position => position >= 0));
assert.deepEqual(filterPositions, [...filterPositions].sort((a, b) => a - b), 'global filters should follow the requested row order');
console.log('business-date UI mapping boundaries OK');
