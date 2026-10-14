import { readFile, mkdir, rm, writeFile } from "node:fs/promises";
import { resolve } from "node:path";
import { fileURLToPath } from "node:url";

const appRoot = resolve(fileURLToPath(new URL("..", import.meta.url)));
const repoRoot = resolve(appRoot, "..");
const output = resolve(appRoot, "dist");
const [worker, html, biReport] = await Promise.all([
  readFile(resolve(appRoot, "worker/index.js"), "utf8"),
  readFile(resolve(repoRoot, "dist/index.html"), "utf8"),
  readFile(resolve(repoRoot, "dist/bi-report.js"), "utf8"),
]);
if (!worker.includes('"__TOWER_HTML__"') || !worker.includes('"__TOWER_BI_REPORT__"')) {
  throw new Error("Worker source is missing dashboard placeholders");
}
const siteMode = `function renderSyncStatus() {
      if (state.data?.source?.site_publication_mode === 'private_site_snapshot') {
        const source = state.data.source || {};
        const time = source.last_success_sync_at ? new Date(source.last_success_sync_at).toLocaleString('zh-CN', {timeZone:'Asia/Shanghai',hour12:false}) : '未知';
        const chip = document.querySelector('.sync-chip strong');
        chip.textContent = '私有站点快照'; chip.style.color = 'var(--green)';
        document.querySelector('.sync-chip .status-dot').style.color = 'var(--green)';
        $('collection-status').textContent = '只读数据快照 · 最近本机成功入库：' + time + ' · 页面每 60 秒检查一次站点数据';
        $('sidebar-note').textContent = '当前为私有站点只读快照，来源：' + (source.file_name || '本机最新数据批次') + '。最近成功导入：' + time + '。站点不会连接本机数据库，也不会自动获取本机后续更新。';
        const section = $('import-file')?.closest('.section');
        if (section) { section.querySelector('h4').textContent = '云端数据更新'; section.querySelector('.note').textContent = '当前站点为只读快照；本机文件导入仅可在本机驾驶舱完成。'; $('import-file').hidden = true; $('import-btn').hidden = true; }
        return;
      }`;
const publishedHtml = html
  .replace("function renderSyncStatus() {", siteMode)
  .replace("$('import-btn').addEventListener('click', async () => { const file", "$('import-btn').addEventListener('click', async () => { if (state.data?.source?.site_publication_mode === 'private_site_snapshot') return; const file")
  .replace("</body>", `<script>window.__SITE_MODE__ = 'private-read-only';</script></body>`);
const built = worker.replace('"__TOWER_HTML__"', JSON.stringify(publishedHtml))
  .replace('"__TOWER_BI_REPORT__"', JSON.stringify(biReport));
await rm(output, { recursive: true, force: true });
await mkdir(resolve(output, "server"), { recursive: true });
await writeFile(resolve(output, "server/index.js"), built);
console.log("Built Cloudflare Worker without embedding the local snapshot.");
