// СТОРОЖ СБОРКИ — poll_script регламента. Разбирает заявки kind=build-watch из памяти анклава без модели:
// посмотреть пайплайн (или джобу) заявки, разбудить дело входящим, снять заявку.
// ВСЕГДА возвращает false: заход модели по расписанию не нужен ни в одном исходе.
// Что не удалось — оседает записью kind=build-watch-fault и повторяется следующим тиком.
// Правила разбора:
//  - заявку пишет модель, поэтому терпим короткий sha и имя привязки вместо id;
//  - пайплайн, заведённый РАНЬШЕ границы not_before заявки, ожидаемым не считается (прогоны прошлых попыток);
//  - поле заявки job — ждём конкретную джобу, а не пайплайн;
//  - в ключ побудки входит признак самой заявки: новая заявка на тот же пайплайн будит заново;
//  - живой пайплайн (created, waiting_for_resource, preparing, pending, running) не повод будить и после
//    срока заявки; дело будится на конечном статусе или по жёсткому пределу HOLD_MAX_MS;
//  - разбор идёт по кругу (ротация по минуте тика), проверки и побудки считаются отдельно.
const KIND = input && input.selftest ? 'build-watch-selftest' : 'build-watch';
const FAULT = KIND + '-fault';
const BUDGET_MS = 900;   // рантайм даёт 2500 мс на прогон; остаток — на начатую заявку
const MAX_CLAIMS = 2;    // побудок за тик: 4–6 операций на каждую; потолок рантайма — 64 операции
const MAX_CHECKS = 6;    // проверок за тик: живая заявка стоит 1–2 операции
const LIVE = ['created', 'waiting_for_resource', 'preparing', 'pending', 'running'];
const HOLD_MAX_MS = 5 * 3600 * 1000;   // жёсткий предел ожидания живого пайплайна — потом будим «не закончилась»
const TERMINAL = ['success', 'failed', 'canceled', 'skipped', 'manual', 'scheduled'];
const JOB_TERMINAL = ['success', 'failed', 'canceled', 'skipped', 'manual'];
const UUID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i;
const t0 = Date.now();

// Единственная запись «последняя странность» — чтобы молчание сторожа было объяснимо.
// ⚠️ Память из песочницы принимает ТОЛЬКО строки и массивы строк: ни null, ни числа.
async function fault(reason, c) {
  await memory.upsert({ kind: FAULT }, {
    kind: FAULT,
    at: new Date().toISOString(),
    reason: String(reason),
    sc_id: typeof c.sc_id === 'string' ? c.sc_id : '',
    sha: typeof c.sha === 'string' ? c.sha : '',
    ref: typeof c.ref === 'string' ? c.ref : '',
  });
}

// Ключи, по которым удаляется РОВНО эта заявка. Пустой набор недопустим:
// deleteByKeys({}) снёс бы весь реестр анклава.
function dropKeys(c) {
  const keys = { kind: KIND };
  let extra = 0;
  for (const field of ['sc_id', 'sha', 'task', 'ref', 'git_binding', 'not_before', 'expires_at', 'note', 'job']) {
    const value = c[field];
    if (typeof value === 'string' && value) { keys[field] = value; extra += 1; }
  }
  return extra ? keys : null;
}

async function drop(c, reason) {
  const keys = dropKeys(c);
  await fault(reason, c);
  if (keys) await memory.deleteByKeys(keys);   // нечем отличить запись от чужих — оставляем человеку
}

function claimTag(c, row) {
  const raw = (typeof c.not_before === 'string' && c.not_before) || (typeof c.expires_at === 'string' && c.expires_at)
    || (row && row.id) || '';
  return String(raw).replace(/[^0-9A-Za-z]/g, '').slice(0, 24) || 'n';
}

const claims = await memory.find({ kind: KIND }, { limit: 20 });
const ordered = claims.slice().reverse();      // find отдаёт свежие первыми, разбираем от старых
const shift = ordered.length ? (Math.floor(t0 / 60000) * MAX_CHECKS) % ordered.length : 0;
const queue = ordered.slice(shift).concat(ordered.slice(0, shift));
let handled = 0;
let checks = 0;

for (const row of queue) {
  if (handled >= MAX_CLAIMS || checks >= MAX_CHECKS || Date.now() - t0 > BUDGET_MS) break;   // остальное — следующим тиком
  const c = (row && row.data) || {};
  const sc = typeof c.sc_id === 'string' ? c.sc_id : '';
  const gb = typeof c.git_binding === 'string' ? c.git_binding : '';
  const sha = typeof c.sha === 'string' ? c.sha.trim() : '';
  const proj = typeof c.project_id === 'number' || typeof c.project_id === 'string' ? c.project_id : '';
  const ref = typeof c.ref === 'string' ? c.ref : '';
  const nb = typeof c.not_before === 'string' ? Date.parse(c.not_before) : NaN;
  if (!sc || !proj || sha.length < 7) {         // будить некого, смотреть нечем или коммит неузнаваем
    await drop(c, 'заявка без обязательных полей — снята');
    continue;
  }
  checks += 1;

  const deadline = typeof c.expires_at === 'string' ? Date.parse(c.expires_at) : NaN;
  const expired = Number.isFinite(deadline) && deadline < Date.now();
  // Маршрут в гит: по умолчанию короткий алиас провайдера. Он резолвится, пока активная
  // привязка ровно одна; на двух ядро отвечает 409 proxy_alias_ambiguous и требует id —
  // тогда его и кладут в заявку, и мы идём сквозным прокси.
  const proxy = !gb ? '/gitlab' : (UUID_RE.test(gb) ? '/proxy/' + gb : '/' + encodeURIComponent(gb));
  const base = proxy + '/api/v4/projects/' + encodeURIComponent(String(proj)) + '/pipelines';
  const refQuery = ref ? '&ref=' + encodeURIComponent(ref) : '';

  let pipe = null;
  let gitError = '';
    const jobId = /^[0-9]+$/.test(String(c.job || '')) ? String(c.job) : '';
  let job = null;
  if (jobId) {
    const jr = await gina.request(proxy + '/api/v4/projects/' + encodeURIComponent(String(proj)) + '/jobs/' + jobId);
    if (jr.ok && jr.body && typeof jr.body === 'object') job = jr.body;
    else gitError = 'джоба ' + jobId + ': HTTP ' + jr.status;
  }
  if (jobId) { /* пайплайн не ищем: решает статус джобы */ }
  else if (Number.isInteger(c.pipeline)) {
    const byId = await gina.request(base + '/' + c.pipeline);
    if (byId.ok && byId.body && typeof byId.body === 'object') pipe = byId.body;
    else if (byId.status !== 404) gitError = 'HTTP ' + byId.status;
  }
  if (!jobId && !pipe && !gitError) {
    // ⚠️ Фильтр ?sha= у GitLab работает только по ПОЛНОМУ хэшу: на коротком он молча
    // отдаёт пустой список, и заявка виснет до срока. Короткий ищем перебором по ветке.
    const full = sha.length >= 40;
    const found = await gina.request(base
      + (full ? '?sha=' + encodeURIComponent(sha) + refQuery + '&per_page=1'
        : '?per_page=10' + refQuery));
    if (found.ok) {
      const list = Array.isArray(found.body) ? found.body : [];
      const match = full
        ? (list[0] || null)
        : (list.filter((p) => p && typeof p.sha === 'string' && p.sha.indexOf(sha) === 0)[0] || null);
      // ⚠️ Пайплайн СТАРШЕ заявки — не её: это прогон прошлой попытки на том же коммите.
      // Считаем, что ожидаемая сборка ещё не завелась, и ждём следующего тика.
      // Нечитаемая дата (NaN) границу не включает: молчаливо ужесточать поведение нельзя.
      const born = match ? Date.parse(match.created_at) : NaN;
      const stale = Number.isFinite(nb) && Number.isFinite(born) && born < nb;
      pipe = stale ? null : match;
    } else gitError = 'HTTP ' + found.status;
  }
  if (gitError) await fault('гит не ответил: ' + gitError, c);

  const status = jobId ? (job && typeof job.status === 'string' ? job.status : '')
    : (pipe && typeof pipe.status === 'string' ? pipe.status : '');
  const done = (jobId ? JOB_TERMINAL : TERMINAL).indexOf(status) >= 0;
  if (jobId && job && job.pipeline && typeof job.pipeline === 'object') pipe = job.pipeline;
  if (!done && !expired) continue;             // идёт, ещё не завёлся или гит молчит — ждём тика
    const since = Number.isFinite(nb) ? nb
    : (row && typeof row.created_at === 'string' && Number.isFinite(Date.parse(row.created_at)) ? Date.parse(row.created_at)
      : (Number.isFinite(deadline) ? deadline : t0));
  const held = !done && LIVE.indexOf(status) >= 0;
  const overHold = Date.now() - since > HOLD_MAX_MS;
  if (held && !overHold) continue;              // стоит в очереди или идёт — молча держим заявку
  handled += 1;

  const ctx = await gina.request('/api/sc/' + sc + '/context?limit=1');
  if (ctx.status === 404) { await drop(c, 'дела нет — заявка снята без побудки'); continue; }
  if (!ctx.ok) { await fault('статус дела не прочитан: HTTP ' + ctx.status, c); continue; }
  const scStatus = ctx.body && ctx.body.sc ? ctx.body.sc.status : '';
  if (scStatus === 'REPORTED' || scStatus === 'CLOSED') {
    await drop(c, 'дело уже сдано (' + scStatus + ') — заявка снята без побудки');
    continue;
  }

  const short = (pipe && typeof pipe.sha === 'string' ? pipe.sha : sha).slice(0, 12);
  const where = ref || '?';
  const pid = pipe && pipe.id ? pipe.id : '?';
  // web_url через прокси приходит с подставленным хостом (http://127.0.0.1/proxy/…) и человеку
  // бесполезен; даём его только когда он похож на настоящий адрес.
  const url = pipe && typeof pipe.web_url === 'string' ? pipe.web_url : '';
  const link = url && url.indexOf('127.0.0.1') < 0 ? ' ' + url : '';
  const coords = (jobId ? 'джоба ' + jobId + (job && job.name ? ' (' + job.name + ')' : '') + ', ' : '')
    + 'пайплайн ' + pid + ' (проект ' + proj + ', ветка ' + where + ')';
  const text = done
    ? 'Сборка коммита ' + short + ': ' + coords + ' — ' + status + '.' + link
    : 'Сборка коммита ' + short + ' не закончилась ' + (held ? 'за ' + Math.round(HOLD_MAX_MS / 3600000) + ' ч ожидания: ' : 'за срок заявки: ')
      + (status ? coords + ' — статус ' + status + '.' + link
        : gitError ? 'статус узнать не удалось (гит: ' + gitError + ').'
          : 'пайплайна на этот коммит в ветке ' + where
            + (Number.isFinite(nb) ? ' новее самой заявки' : '') + ' так и нет.');

  const wake = await gina.request('/api/sc/' + sc + '/incoming', {
    method: 'POST',
    body: {
      text: text,
            idempotency_key: 'build-watch:' + (jobId ? 'job' + jobId : (pipe && pipe.id ? pipe.id : short)) + ':'
        + (done ? status : 'expired') + ':' + claimTag(c, row),
      source: { kind: 'automation', id: 'build-watch' },
    },
  });
  if (wake.status === 409) {                        await fault('побудка совпала с уже отправленной (409) — дело могло не проснуться', c);
  } else if (!wake.ok) {
    await fault('побудка не прошла: HTTP ' + wake.status, c);
    continue;                                   // заявку не снимаем: повторим следующим тиком
  }
  const keys = dropKeys(c);
  if (keys) await memory.deleteByKeys(keys);
}

return false;