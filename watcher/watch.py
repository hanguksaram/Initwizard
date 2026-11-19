#!/usr/bin/env python3
"""Сторож инстанса Gina (эталонная минимальная версия).

Что делает за один запуск: собирает состояние инстанса, сравнивает с прошлым запуском, пишет каталог
runs/<UTC-время>/ с файлами digest.md (выжимка для разбора), alerts.txt (ровно то, что ушло в канал
уведомлений; пустой — ничего не уходило) и raw.json, обновляет state.json (открытые тревоги).

Только чтение: get/logs/top/exec с командами чтения. На стенде ничего не меняет.

Запуск: python3 watcher/watch.py [--config watcher/config.json] [--no-notify] [--source cron] [--test-notify]
  --no-notify    ничего не отправлять; неотправленное остаётся в очереди и уйдёт при первом обычном запуске
  --source cron  пометить запуск как запуск по расписанию (итоговая проверка по этой метке видит, что расписание сработало)
  --test-notify  отправить одно тестовое сообщение в канал тревог и выйти
  --init-config  создать watcher/config.json из образца и настроек журнала мастера (название, адреса стендов) и выйти

ВАЖНО ДЛЯ МАСТЕРА УСТАНОВКИ: это эталон, а не готовый продукт. На шаге 5 мастер обязан прогнать каждый
сборщик на живом инстансе, выключить в config.json то, для чего нет доступа, и переписать шаблоны и
запросы под фактический формат логов и схему базы этого инстанса (версия ядра может отличаться).
"""
import argparse, datetime, json, os, re, sys, urllib.parse
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'scripts'))
import common as c

HERE = os.path.dirname(os.path.abspath(__file__))
SEV = {'info': 0, 'warn': 1, 'crit': 2}
ICON = {'info': '🔵', 'warn': '🟠', 'crit': '🔴'}


class Run:
    def __init__(self, cfg):
        self.cfg, self.alerts, self.sections, self.raw, self.failed = cfg, {}, [], {}, []

    def alert(self, key, sev, text):
        """key — устойчивый идентификатор тревоги (по нему считается «новая / всё ещё / снято»)."""
        if key not in self.alerts or SEV[sev] > SEV[self.alerts[key]['sev']]:
            self.alerts[key] = {'sev': sev, 'text': text}

    def section(self, title, lines):
        self.sections.append((title, lines))


def norm(line):
    """Сигнатура строки лога: без времени, идентификаторов и чисел."""
    line = re.sub(r'^\S*\d{4}-\d\d-\d\dT[\d:.]+Z?\s*', '', line)
    line = re.sub(r'\x1b\[[0-9;]*m', '', line)
    line = re.sub(r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}', '<id>', line)
    line = re.sub(r'\b[0-9a-f]{16,}\b', '<hex>', line)
    return re.sub(r'\d+', 'N', line)[:160]


# ---------------------------------------------------------------- сборщики
def collect_pods(run, prev):
    rc, out, err = c.kubectl(['get', 'pods', '-o', 'json'])
    if rc != 0:
        raise RuntimeError((err or out).strip()[:200])
    pods, lines = {}, []
    for p in json.loads(out)['items']:
        name = p['metadata']['name']
        if p['status'].get('phase') == 'Succeeded':   # отработавшие разовые задания — не сбой
            continue
        cs = (p.get('status', {}).get('containerStatuses') or [])
        restarts = sum(x.get('restartCount', 0) for x in cs)
        ready = bool(cs) and all(x.get('ready') for x in cs)
        last = next((x['lastState']['terminated'] for x in cs if x.get('lastState', {}).get('terminated')), {})
        base = re.sub(r'-[0-9a-f]{8,10}-[0-9a-z]{5}$', '', name)  # имя без суффикса реплики деплоймента
        pods[base] = {'pod': name, 'phase': p['status'].get('phase'), 'ready': ready, 'restarts': restarts,
                      'last_reason': last.get('reason'), 'last_finished': last.get('finishedAt'),
                      'images': [x['image'].split('/')[-1][:70] for x in p['spec']['containers']]}
        lines.append('- %s: %s ready=%s рестарты=%s образ=%s' % (name, p['status'].get('phase'), ready, restarts, ', '.join(pods[base]['images'])))
        if p['status'].get('phase') != 'Running' or not ready:
            run.alert('pod-not-ready:' + base, 'crit', 'Под %s не готов (%s)' % (name, p['status'].get('phase')))
        old = (prev.get('pods') or {}).get(base)
        if old:
            if restarts > old.get('restarts', 0) and name == old.get('pod'):
                oom = last.get('reason') == 'OOMKilled'
                run.alert('pod-restart:' + base, 'crit' if oom else 'warn',
                          'Под %s перезапускался (%s → %s), причина: %s' % (name, old['restarts'], restarts, last.get('reason') or 'неизвестна'))
            if old.get('images') != pods[base]['images']:
                run.alert('image-changed:' + base, 'info', 'Образ %s сменился: %s → %s' % (base, old.get('images'), pods[base]['images']))
    for base in set(prev.get('pods') or {}) - set(pods):
        run.alert('pod-gone:' + base, 'warn', 'Под %s исчез из пространства' % base)
    run.raw['pods'] = pods
    run.section('Поды', lines)


def collect_logs(run, prev):
    window = '%dm' % run.cfg.get('window_minutes', 60)
    lines, stats = [], {}
    for target in run.cfg.get('log_targets', []):
        rc, out, err = c.kubectl(['logs', target['resource'], '--all-containers=true', '--since=' + window, '--tail=20000'], timeout=120)
        if rc != 0:
            run.failed.append('логи %s: %s' % (target['resource'], (err or out).strip()[:120]))
            continue
        log = out.splitlines()
        counts = {}
        for pat in target.get('patterns', []):
            n = sum(1 for l in log if re.search(pat['regex'], l))
            counts[pat['key']] = n
            for sev in ('crit', 'warn'):
                if pat.get(sev) is not None and n >= pat[sev]:
                    run.alert('log:%s:%s' % (target['name'], pat['key']), sev, '%s — %d за последний интервал (журнал %s)' % (pat['title'], n, target['name']))
                    break
        sigs = {}
        for l in log:
            if re.search(target.get('error_regex', r'(?i)\[error\]|"level":"error"|\bERROR\b'), l):
                s = norm(l); sigs[s] = sigs.get(s, 0) + 1
        old = ((prev.get('logs') or {}).get(target['name']) or {}).get('sigs') or {}
        new = [s for s in sigs if s not in old]
        stats[target['name']] = {'lines': len(log), 'counts': counts, 'sigs': sigs}
        lines.append('- %s: строк %d; %s' % (target['name'], len(log), json.dumps(counts, ensure_ascii=False)))
        for s, n in sorted(sigs.items(), key=lambda x: -x[1])[:8]:
            lines.append('    x%d%s %s' % (n, ' (новая)' if s in new else '', s))
    run.raw['logs'] = stats
    run.section('Логи за окно %s (счётчики и сигнатуры ошибок)' % window, lines)


def collect_jcode_ops(run, prev):
    """Число записей операций на подах jcode: при 10000 под перестаёт создавать сессии (503 operation_capacity)."""
    cfg = run.cfg.get('jcode_ops') or {}
    rc, out, _ = c.kubectl(['get', 'pods', '-o', 'name'])
    lines, data = [], {}
    for pod in [x.split('/')[-1] for x in out.split() if re.search(cfg.get('pod_regex', r'jcode-agent-\d+$'), x)]:
        rc, o, e = c.kubectl(['exec', pod, '-c', cfg.get('container', 'jcode-agent'), '--', 'sh', '-c',
                              'ls %s 2>/dev/null | wc -l' % cfg.get('path', '/data/api/operations')])
        if rc != 0:
            run.failed.append('счётчик операций %s: %s' % (pod, (e or o).strip()[:100])); continue
        n = int(o.strip() or 0); data[pod] = n
        old = ((prev.get('jcode_ops') or {}).get(pod))
        lines.append('- %s: %d записей%s (предел %d)' % (pod, n, '' if old is None else ' (%+d за запуск)' % (n - old), cfg.get('limit', 10000)))
        if n >= cfg.get('crit', 9500):
            run.alert('jcode-ops:' + pod, 'crit', '%s: %d записей операций из %d — новые сессии скоро перестанут создаваться' % (pod, n, cfg.get('limit', 10000)))
        elif n >= cfg.get('warn', 8500):
            run.alert('jcode-ops:' + pod, 'warn', '%s: %d записей операций из %d' % (pod, n, cfg.get('limit', 10000)))
    run.raw['jcode_ops'] = data
    run.section('Записи операций jcode', lines)


def collect_top(run, prev):
    rc, out, err = c.kubectl(['top', 'pods', '--no-headers'])
    if rc != 0:
        run.failed.append('top pods: %s' % (err or out).strip()[:100]); return
    rc2, lim, _ = c.kubectl(['get', 'pods', '-o', 'json'])
    limits = {}
    for p in json.loads(lim)['items'] if rc2 == 0 else []:
        m = sum(_mem(x.get('resources', {}).get('limits', {}).get('memory')) for x in p['spec']['containers'])
        limits[p['metadata']['name']] = m
    lines = []
    for row in out.splitlines():
        name, cpu, mem = row.split()[:3]
        used, limit = _mem(mem), limits.get(name) or 0
        share = used / limit if limit else 0
        lines.append('- %s: CPU %s, память %s%s' % (name, cpu, mem, (' (%.0f%% лимита)' % (share * 100)) if limit else ''))
        if limit and share >= run.cfg.get('memory_warn_share', 0.85):
            run.alert('mem:' + re.sub(r'-[0-9a-f]{8,10}-[0-9a-z]{5}$', '', name), 'warn', 'Под %s: память %.0f%% лимита' % (name, share * 100))
    run.section('Потребление', lines)


def _mem(s):
    if not s:
        return 0
    m = re.match(r'^(\d+(?:\.\d+)?)(Ki|Mi|Gi|K|M|G)?$', s)
    return float(m.group(1)) * {'Ki': 1 / 1024, 'Mi': 1, 'Gi': 1024, 'K': 1 / 1000, 'M': 1, 'G': 1000, None: 1 / 1048576}[m.group(2)] if m else 0


def collect_api(run, prev):
    env = c.load_env('gina.env')
    if not env.get('GINA_URL'):
        return
    g = c.Gina()
    st, h = g.call('GET', '/api/health', auth=False, timeout=20)
    ok = st == 200 and isinstance(h, dict) and h.get('ok')
    lines = ['- /api/health: %s %s' % (st, json.dumps(h, ensure_ascii=False)[:160])]
    if not ok:
        run.alert('api-health', 'crit', 'API Gina не отвечает здоровьем: HTTP %s' % st)
    if env.get('GINA_OWNER_TOKEN'):
        st, ps = g.call('GET', '/api/prompt-source', timeout=20)
        if st == 200 and isinstance(ps, dict):
            s = ps.get('status') or {}
            lines.append('- источник промптов: %s, sha %s, синхронизация %s, ошибка: %s' % (s.get('state'), (s.get('sha') or '')[:8], s.get('synced_at'), s.get('error')))
            if s.get('state') not in (None, 'ok') or s.get('error'):
                run.alert('prompt-source', 'crit', 'Источник промптов в состоянии %s: %s' % (s.get('state'), str(s.get('error'))[:160]))
            run.raw['prompt_source_sha'] = s.get('sha')
        else:
            run.failed.append('GET /api/prompt-source → %s' % st)
    run.section('API инстанса', lines)


def collect_urls(run, prev):
    lines, data = [], {}
    for u in run.cfg.get('urls', []):
        st, _ = c.http('GET', u['url'], timeout=15, as_text=True)
        data[u['name']] = st
        old = (prev.get('urls') or {}).get(u['name'])
        lines.append('- %s: HTTP %s' % (u['name'], st or 'нет связи'))
        if old is not None and old != st:
            run.alert('url:' + u['name'], 'info', '%s: ответ сменился %s → %s' % (u['name'], old, st))
    run.raw['urls'] = data
    run.section('Адреса (стенды и сервисы)', lines)


def collect_pg(run, prev):
    """Необязательный сборщик: запросы из config.json (раздел pg_queries). Каждый запрос возвращает строки;
    тревога поднимается, если число строк ≥ порога. Запросы мастер обязан сверить со схемой инстанса."""
    dsn = c.load_env('db.env').get('PG_DSN')
    if not dsn or not run.cfg.get('pg_queries'):
        return
    import psycopg2
    try:
        conn = psycopg2.connect(dsn, connect_timeout=10, options='-c default_transaction_read_only=on -c statement_timeout=20000')
    except Exception as e:   # текст ошибки не выводим: в нём может оказаться строка подключения
        raise RuntimeError('база не подключается (%s)' % type(e).__name__)
    lines = []
    try:
        for q in run.cfg['pg_queries']:
            cur = conn.cursor()
            try:
                cur.execute(q['sql']); rows = cur.fetchall()
            except Exception as e:
                conn.rollback(); run.failed.append('запрос %s не выполнен (%s)' % (q['key'], type(e).__name__)); continue
            lines.append('- %s: %d' % (q['title'], len(rows)))
            for r in rows[:5]:
                lines.append('    ' + ' | '.join(str(x)[:80] for x in r))
            for sev in ('crit', 'warn'):
                if q.get(sev) is not None and len(rows) >= q[sev]:
                    run.alert('pg:' + q['key'], sev, '%s: %d' % (q['title'], len(rows))); break
    finally:
        conn.close()
    run.section('База инстанса', lines)


COLLECTORS = [('pods', collect_pods), ('logs', collect_logs), ('jcode_ops', collect_jcode_ops), ('top', collect_top),
              ('api', collect_api), ('urls', collect_urls), ('pg', collect_pg)]


# ---------------------------------------------------------------- уведомления
def notify(text):
    n = c.load_env('notify.env')
    kind = (n.get('NOTIFY_KIND') or 'none').lower()
    if kind == 'telegram':
        st, d = c.http('POST', 'https://api.telegram.org/bot%s/sendMessage' % n['TELEGRAM_BOT_TOKEN'],
                       body={'chat_id': n['TELEGRAM_CHAT_ID'], 'text': text[:4000], 'disable_web_page_preview': True}, timeout=20)
        return st == 200
    if kind == 'mattermost':
        st, d = c.http('POST', n['MATTERMOST_WEBHOOK_URL'], body={'text': text[:4000]}, timeout=20)
        return st == 200
    return None  # канал не задан


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--config', default=os.path.join(HERE, 'config.json'))
    ap.add_argument('--no-notify', action='store_true')
    ap.add_argument('--source', default='manual', choices=['manual', 'cron'])
    ap.add_argument('--test-notify', action='store_true')
    ap.add_argument('--init-config', action='store_true')
    a = ap.parse_args()
    if a.init_config:
        if os.path.exists(a.config):
            print('%s уже существует — не трогаю' % a.config); sys.exit(0)
        cfg = json.load(open(os.path.join(HERE, 'config.example.json'), encoding='utf-8')); s = c.settings()
        ns = c.load_env('k8s.env').get('K8S_NAMESPACE') or ''
        cfg['title'] = 'Инстанс Gina %s%s' % (ns, (' (проект %s)' % s['project_name']) if s.get('project_name') else '')
        cfg['urls'] = [{'name': 'стенд ' + u.split('//')[-1].split('/')[0], 'url': u} for u in [x.strip() for x in (s.get('ui_urls') or '').split(',') if x.strip()]]
        if not cfg['urls']:
            cfg['collectors'] = [x for x in cfg['collectors'] if x != 'urls']
        if s.get('watch_interval_minutes'):
            cfg['window_minutes'] = int(s['watch_interval_minutes'])
        json.dump(cfg, open(a.config, 'w', encoding='utf-8'), ensure_ascii=False, indent=2); print('создан %s: %s; адресов стендов: %d' % (a.config, cfg['title'], len(cfg['urls']))); sys.exit(0)
    cfg = json.load(open(a.config, encoding='utf-8'))
    if a.test_notify:
        r = notify('%s: сторож инстанса подключён, это тестовое сообщение.' % cfg.get('title', 'Стенд Gina'))
        print({True: 'доставлено', False: 'НЕ доставлено — проверь notify.env', None: 'канал тревог не задан (NOTIFY_KIND=none)'}[r]); sys.exit(0 if r is not False else 1)
    runs = os.path.join(HERE, 'runs'); os.makedirs(runs, exist_ok=True)
    state_path = os.path.join(HERE, 'state.json')
    state = json.load(open(state_path)) if os.path.exists(state_path) else {'open': {}, 'raw': {}, 'outbox': []}
    now = datetime.datetime.now(datetime.timezone.utc)
    run = Run(cfg)
    ran = set()
    for name, fn in COLLECTORS:
        if name not in cfg.get('collectors', []):
            continue
        before = len(run.failed)
        try:
            fn(run, state.get('raw') or {})
            if len(run.failed) == before:      # сборщик с частичным сбоем отработавшим не считается: его тревоги не снимаем
                ran.add(name)
        except Exception as e:
            run.failed.append('сборщик %s: %s: %s' % (name, type(e).__name__, str(e)[:160]))
    if run.failed:
        run.alert('collect-failed', 'warn', 'Сбор данных неполный: ' + '; '.join(run.failed)[:300])

    # новая / усилилась / всё ещё / снято. Тревоги сборщика, который в этот раз не отработал, НЕ снимаются.
    prefix_owner = {'pod-': 'pods', 'image-': 'pods', 'log:': 'logs', 'jcode-ops:': 'jcode_ops', 'mem:': 'top',
                    'api-': 'api', 'prompt-source': 'api', 'url:': 'urls', 'pg:': 'pg'}
    def owner(key):
        return next((v for k, v in prefix_owner.items() if key.startswith(k)), None)
    changes, opened, outbox = [], dict(state.get('open') or {}), list(state.get('outbox') or [])
    for key, al in run.alerts.items():
        old = opened.get(key)
        if al['sev'] == 'info':     # события: сообщаются один раз, открытыми не считаются
            line = '%s %s' % (ICON['info'], al['text']); changes.append(line); outbox.append({'key': key, 'sev': 'info', 'kind': 'info', 'text': line}); continue
        if not old or SEV[al['sev']] > SEV[old['sev']]:
            line = '%s %s%s' % (ICON[al['sev']], al['text'], ' (усилилась)' if old else '')
            changes.append(line)
            outbox = [m for m in outbox if not (m['key'] == key and m['kind'] == 'raise')]
            outbox.append({'key': key, 'sev': al['sev'], 'kind': 'raise', 'text': line})
        opened[key] = {'sev': al['sev'], 'text': al['text'], 'since': (old or {}).get('since') or now.isoformat()}
    for key in list(opened):
        if key not in run.alerts and (owner(key) in ran or (key == 'collect-failed' and not run.failed)):
            gone = opened.pop(key); line = '🟢 Снято: %s' % gone['text']; changes.append(line)
            unsent = [m for m in outbox if m['key'] == key and m['kind'] == 'raise']
            if unsent:              # о тревоге ещё не сообщали — не шлём ни её, ни «Снято»
                outbox = [m for m in outbox if m not in unsent]
            else:
                outbox.append({'key': key, 'sev': gone['sev'], 'kind': 'clear', 'text': line})

    # отправка: в тихие часы уходит только критичное, остальное ждёт в очереди; при сбое доставки очередь сохраняется
    quiet = cfg.get('quiet_hours_utc')  # [с, по) в часах UTC, например [19, 5]; null — тихих часов нет
    is_quiet = bool(quiet) and (quiet[0] <= now.hour < quiet[1] if quiet[0] < quiet[1] else (now.hour >= quiet[0] or now.hour < quiet[1]))
    ready = [m for m in outbox if not is_quiet or m['sev'] == 'crit']
    text, delivery = '', 'нечего отправлять'
    if ready and a.no_notify:
        delivery = 'запуск без отправки: в очереди %d' % len(outbox)
    elif ready:
        body = '%s, %s UTC\n%s' % (cfg.get('title', 'Стенд Gina'), now.strftime('%d.%m %H:%M'), '\n'.join(m['text'] for m in ready))
        sent = notify(body)
        if sent is False:
            delivery = 'НЕ доставлено — повторю при следующем запуске'; run.failed.append('уведомление не доставлено')
        else:
            text = body; outbox = [m for m in outbox if m not in ready]
            delivery = 'доставлено в канал тревог' if sent else 'канал тревог не задан — сообщения только в этом файле'
    elif outbox:
        delivery = 'тихие часы: в очереди %d до их окончания' % len(outbox)

    d = os.path.join(runs, now.strftime('%Y%m%dT%H%M%SZ')); os.makedirs(d)
    md = ['# Выжимка сторожа: %s, %s UTC' % (cfg.get('title', ''), now.strftime('%d.%m.%Y %H:%M')), '',
          '## Открытые тревоги'] + (['- [%s] %s (с %s)' % (v['sev'], v['text'], v['since'][:16]) for v in opened.values()] or ['- нет'])
    md += ['', '## Изменения за этот запуск'] + (['- ' + l for l in changes] or ['- нет'])
    md += ['', '## Отправка', '- ' + delivery] + ['- ждёт отправки: ' + m['text'] for m in outbox]
    if run.failed:
        md += ['', '## Не удалось собрать'] + ['- ' + f for f in run.failed]
    for title, lines in run.sections:
        md += ['', '## ' + title] + (lines or ['- нет данных'])
    open(os.path.join(d, 'digest.md'), 'w', encoding='utf-8').write('\n'.join(md) + '\n')
    open(os.path.join(d, 'alerts.txt'), 'w', encoding='utf-8').write(text)
    json.dump(run.raw, open(os.path.join(d, 'raw.json'), 'w', encoding='utf-8'), ensure_ascii=False, indent=1)
    raw = dict(state.get('raw') or {}); raw.update(run.raw)  # данные несработавших сборщиков сохраняются с прошлого раза
    new_state = {'open': opened, 'raw': raw, 'outbox': outbox, 'last_run': now.isoformat(), 'last_cron_run': state.get('last_cron_run')}
    if a.source == 'cron':
        new_state['last_cron_run'] = now.isoformat()
    json.dump(new_state, open(state_path, 'w', encoding='utf-8'), ensure_ascii=False, indent=1)
    keep = cfg.get('keep_runs_days', 14)   # старые каталоги запусков удаляются
    for name in sorted(os.listdir(runs)):
        try:
            age = (now - datetime.datetime.strptime(name, '%Y%m%dT%H%M%SZ').replace(tzinfo=datetime.timezone.utc)).days
        except ValueError:
            continue
        if age > keep:
            import shutil; shutil.rmtree(os.path.join(runs, name), ignore_errors=True)
    print(d)


if __name__ == '__main__':
    main()
