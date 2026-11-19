#!/usr/bin/env python3
"""Шаг 4 мастера: итоговая проверка после установки. Ничего не меняет.

Запуск: python3 scripts/healthcheck.py
Проверяет: доступы (как на шаге 1), поды инстанса, прокси GitLab и Jira изнутри пространства, какую ветку читает
инстанс, каталог инстанса против элементов POC, регламент побудки по сборке, сторожа инстанса, стенды продукта.

Статусы: OK — работает; WARN — работает с оговоркой или не проверено; MISS — не ставилось / не задано по решению
оператора; FAIL — должно работать, но не работает. Код возврата 1, если есть FAIL.
"""
import json, os, subprocess, sys, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common as c
import source_switch

R = []
def add(group, name, status, note=''):
    R.append({'group': group, 'check': name, 'status': status, 'note': note})

# 1. доступы — тем же скриптом, что на шаге 1
subprocess.run([sys.executable, os.path.join(c.PACK, 'scripts', 'access_check.py'), '--json'], capture_output=True)
for r in c.load_state('access_check.json', []):
    add('доступы: ' + r['group'], r['check'], r['status'], r['note'])

# 2. поды
rc, out, err = c.kubectl(['get', 'pods', '-o', 'json'])
if rc == 0:
    for p in json.loads(out)['items']:
        if p['status'].get('phase') == 'Succeeded':
            continue
        cs = p['status'].get('containerStatuses') or []
        ok = p['status'].get('phase') == 'Running' and cs and all(x.get('ready') for x in cs)
        add('поды', p['metadata']['name'], 'OK' if ok else 'FAIL', '%s, рестартов %d' % (p['status'].get('phase'), sum(x.get('restartCount', 0) for x in cs)))
else:
    add('поды', 'чтение', 'FAIL', (err or out).strip()[:160])

# 3. прокси изнутри пространства (так ходят агенты Gina) — нужен exec
probe = r'''
const L="http://localhost:8080";const out={};
(async()=>{for(const [k,p] of [["usage","/api/adapters/binding-usage"],["gitlab","/gitlab/api/v4/user"],["jira","/jira/rest/api/2/myself"],["skills","/api/catalog/skills"],["agents","/api/catalog/agents"]]){
 try{const r=await fetch(L+p);const t=await r.text();let j=null;try{j=JSON.parse(t)}catch{};
  if(k=="usage")out[k]={status:r.status,items:(j&&j.items||[]).map(x=>({name:x.display_name,state:x.state,direction:x.direction}))};
  else if(k=="gitlab")out[k]={status:r.status,user:j&&j.username};
  else if(k=="jira")out[k]={status:r.status,user:j&&j.name};
  else{const a=Array.isArray(j)?j:(j&&(j.items||j.skills||j.agents));out[k]={status:r.status,names:(r.status==200&&Array.isArray(a))?a.filter(x=>x.enabled!==false).map(x=>x.name):null}}
 }catch(e){out[k]={status:0,error:String(e.message)}}}
console.log(JSON.stringify(out))})()'''
rc, out, err = c.kubectl(['exec', 'deploy/gina-evd', '--', 'node', '-e', probe], timeout=90)
inside = {}
if rc == 0:
    try:
        inside = json.loads(out.strip().splitlines()[-1])
    except Exception:
        add('прокси Gina', 'разбор ответа', 'WARN', 'ответ пода не разобран — не проверено')
else:
    add('прокси Gina', 'проверка изнутри пространства', 'WARN', 'нет права exec или под недоступен — прокси к внешним системам и каналы общения НЕ проверены')
if inside:
    g, j = inside.get('gitlab', {}), inside.get('jira', {})
    for key, title, d in (('gitlab', 'GitLab без токена', g), ('jira', 'Jira без токена', j)):
        if not c.feature(key):
            add('прокси Gina', title, 'MISS', 'к этому проекту не применимо: признак %s выключен на шаге 2' % key)
    if c.feature('gitlab'): add('прокси Gina', 'GitLab без токена', 'OK' if g.get('status') == 200 else 'FAIL', 'пользователь %s' % g.get('user') if g.get('status') == 200 else 'HTTP %s' % g.get('status'))
    if c.feature('jira'): add('прокси Gina', 'Jira без токена', 'OK' if j.get('status') == 200 else 'FAIL', 'пользователь %s' % j.get('user') if j.get('status') == 200 else 'HTTP %s' % j.get('status'))
    convo = [x for x in inside.get('usage', {}).get('items', []) if x.get('direction') == 'conversation']
    add('прокси Gina', 'каналы общения', 'OK' if convo else 'WARN', ', '.join(x['name'] for x in convo)[:140] or 'привязок разговоров нет: просьбы из чата до агентов не дойдут')

# 4. какую ветку читает инстанс
is_live = False
try:
    gi = c.Gina(); st, ps = gi.call('GET', '/api/prompt-source')
    cfg, status = ((ps.get('config') or {}), (ps.get('status') or {})) if st == 200 and isinstance(ps, dict) else ({}, {})
    is_live = source_switch.is_live()
    s = c.settings()
    if st != 200:
        add('инстанс', 'источник промптов', 'FAIL', 'не читается: HTTP %s' % st)
    elif is_live:
        add('инстанс', 'читает ветку POC', 'OK' if status.get('state') == 'ok' else 'FAIL', '%s@%s, состояние %s, коммит %s%s' % (
            cfg.get('project'), cfg.get('branch'), status.get('state'), (status.get('sha') or '')[:8], ', ошибка: %s' % status.get('error') if status.get('error') else ''))
    else:
        add('инстанс', 'читает ветку POC', 'MISS', 'НЕТ: инстанс читает %s@%s. Методология POC лежит в ветке «%s» и на работу агентов не влияет, пока источник не переключён'
            % (cfg.get('project'), cfg.get('branch'), s.get('poc_branch')))
except SystemExit as e:
    add('инстанс', 'источник промптов', 'FAIL', str(e)[:160])

# 5. элементы POC: ветка и каталог
manifest = json.load(open(os.path.join(c.PACK, 'methodology', 'ROLLOUT.json'), encoding='utf-8'))
rollout = (c.load_state('rollout.json', {'items': {}}))['items']
skills, agents = (inside.get('skills') or {}).get('names'), (inside.get('agents') or {}).get('names')
if skills is None or agents is None:        # запасной путь — внешний API с токеном владельца
    try:
        gi = c.Gina()
        for kind in ('skills', 'agents'):
            st, d = gi.call('GET', '/api/catalog/' + kind)
            a = d if isinstance(d, list) else ((d.get('items') or d.get(kind)) if isinstance(d, dict) else None)
            names = [x.get('name') for x in a if x.get('enabled') is not False] if st == 200 and isinstance(a, list) else None
            if kind == 'skills' and skills is None: skills = names
            if kind == 'agents' and agents is None: agents = names
    except SystemExit:
        pass
for i in manifest['items']:
    x = rollout.get(i['id'], {}); st = x.get('state'); label = '%s %s' % (i['id'], i['slug'])
    if i.get('feature') and not c.feature(i['feature']) and st is None:
        add('методология', label, 'MISS', 'к этому проекту не применимо: ' + (((c.settings().get('features') or {}).get(i['feature']) or {}).get('reason') or 'признак не включён')); continue
    if st == 'skipped':
        add('методология', label, 'MISS', 'пропущено по решению оператора: ' + x.get('reason', '')); continue
    if st is None:
        add('методология', label, 'MISS', 'не ставилось (накат до этого элемента не дошёл)'); continue
    if st == 'opened':
        add('методология', label, 'WARN', 'merge request открыт, но не влит: %s' % x.get('url', '')); continue
    if i['kind'] == 'automation':
        try:
            gi2 = c.Gina(); st2, d2 = gi2.call('GET', '/api/automations')
            rows = d2 if isinstance(d2, list) else ((d2.get('items') or d2.get('automations') or []) if isinstance(d2, dict) else [])
            live_rows = [a for a in rows if a.get('name') == i['name'] and a.get('state') != 'retired']
        except SystemExit:
            live_rows, st2 = [], 0
        if st2 != 200:
            add('методология', label, 'WARN', 'список регламентов не читается (HTTP %s) — не проверено' % st2)
        elif not live_rows:
            add('методология', label, 'FAIL', 'регламента на инстансе нет: дела, ждущие сборку, никто не разбудит')
        else:
            a0 = live_rows[0]
            add('методология', label, 'OK' if a0.get('state') == 'armed' else 'MISS',
                ('включён' if a0.get('state') == 'armed' else 'создан, но выключен (состояние %s) — дела, ждущие сборку, никто не разбудит' % a0.get('state'))
                + ('; последняя ошибка: ' + str(a0['last_error'])[:120] if a0.get('last_error') else ''))
        continue
    if x.get('files_ok') is False:
        add('методология', label, 'FAIL', 'влито, но файлы в ветке POC не совпадают с пакетом — выполни mr_rollout.py verify %s' % i['id']); continue
    if not is_live:
        add('методология', label, 'OK' if x.get('files_ok') else 'WARN', 'в ветке POC%s; на инстансе не действует (источник не переключён)' % ('' if x.get('files_ok') else ', сверка не завершена — verify %s' % i['id'])); continue
    if x.get('synced') is False:
        add('методология', label, 'FAIL', 'инстанс не применил: %s' % x.get('sync_note', '')); continue
    if i['kind'] in ('skill', 'agent'):
        have = skills if i['kind'] == 'skill' else agents
        if have is None:
            add('методология', label, 'WARN', 'в ветке POC; каталог инстанса не читается — наличие на инстансе не проверено')
        else:
            add('методология', label, 'OK' if i['name'] in have else 'FAIL', 'в каталоге инстанса' if i['name'] in have else 'влито, но в каталоге инстанса нет — см. source_switch.py show')
    else:
        add('методология', label, 'OK', 'правила роли в ветке POC, инстанс её читает')

# 6. сторож инстанса
sp = os.path.join(c.PACK, 'watcher', 'state.json')
plan = c.load_state('watcher.json', {})
def age_min(ts):
    return (time.time() - time.mktime(time.strptime(ts[:19], '%Y-%m-%dT%H:%M:%S')) + time.timezone) / 60
if os.path.exists(sp):
    s = json.load(open(sp)); last, cron = s.get('last_run') or '', s.get('last_cron_run') or ''
    add('сторож инстанса', 'последний запуск', 'OK', '%s UTC (%.0f мин назад), открытых тревог: %d, ждёт отправки: %d' % (last[:16], age_min(last), len(s.get('open') or {}), len(s.get('outbox') or [])))
    limit = (int(plan.get('interval_minutes') or 60)) * 2
    if not plan.get('schedule'):
        add('сторож инстанса', 'расписание', 'MISS', 'расписание ещё не установлено (шаг 7)')
    elif not cron:
        add('сторож инстанса', 'расписание', 'WARN', 'установлено («%s»), но по расписанию сторож ещё ни разу не запускался — проверь после первого срабатывания' % plan['schedule'][:60])
    else:
        add('сторож инстанса', 'расписание', 'OK' if age_min(cron) < limit else 'FAIL', 'последний запуск по расписанию %s UTC (%.0f мин назад)' % (cron[:16], age_min(cron)))
else:
    add('сторож инстанса', 'последний запуск', 'FAIL', 'сторож ещё ни разу не запускался')

# 7. стенды продукта (справочно)
cfgp = os.path.join(c.PACK, 'watcher', 'config.json')
for u in (json.load(open(cfgp, encoding='utf-8')).get('urls', []) if os.path.exists(cfgp) else []):
    st, _ = c.http('GET', u['url'], timeout=15, as_text=True)
    up = st in (200, 301, 302, 401, 403)
    add('стенды продукта', u['name'], 'OK' if up else 'WARN', 'HTTP %s%s' % (st or 'нет связи', ' — отвечает' + (', требует вход' if st in (401, 403) else '') if up else (' — приложение не развёрнуто' if st == 503 else '')))

c.save_state('healthcheck.json', {'ts': c.now(), 'results': R})
for r in R:
    print('%-5s %-24s %-44s %s' % (r['status'], r['group'][:24], r['check'][:44], r['note']))
n = {k: sum(1 for r in R if r['status'] == k) for k in ('OK', 'WARN', 'MISS', 'FAIL')}
print('\nИтог: работает %(OK)d, с оговоркой или не проверено %(WARN)d, не ставилось / не задано %(MISS)d, не работает %(FAIL)d' % n)
sys.exit(1 if n['FAIL'] else 0)
