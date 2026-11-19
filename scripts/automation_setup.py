#!/usr/bin/env python3
"""Последний элемент наката: регламент «СТОРОЖ СБОРКИ» (расписание на инстансе, которое будит дела по окончании сборки).

Регламент нельзя создать из Git: ядро Gina из Git только ОБНОВЛЯЕТ существующий регламент, находя его по
первым 8 знакам id в имени папки `automations/<имя>-<id8>/`. Поэтому порядок такой (после переключения инстанса
на ветку POC — до этого в каталоге нет навыка build-watch, и ядро регламент не примет):

  python3 scripts/automation_setup.py status
  python3 scripts/automation_setup.py create --confirmed-by "<ответ оператора>"   — создать через API ВЫКЛЮЧЕННЫМ
  python3 scripts/mr_rollout.py open 17 / merge 17 ...                            — положить определение в Git (обычный цикл MR)
  python3 scripts/automation_setup.py arm --confirmed-by "<ответ оператора>"      — включить расписание
  python3 scripts/automation_setup.py check                                       — работает ли: состояние, ошибки, журнал странностей
  python3 scripts/automation_setup.py pause --confirmed-by "<ответ оператора>"    — выключить

create идемпотентен: повторный запуск находит регламент по названию и ничего не создаёт.
Если ядро отклонило запрос — скрипт НЕ подбирает другой состав полей: он останавливается и показывает ответ ядра.
"""
import argparse, json, os, sys, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common as c

TPL = os.path.join(c.PACK, 'methodology', 'automation-templates', 'build-watch')
SLUG = 'storozh-sborki'
OFF = ('paused', 'draft')


def tpl(name):
    return open(os.path.join(TPL, name), encoding='utf-8').read()


def rows(gi):
    st, d = gi.call('GET', '/api/automations')
    if st != 200:
        sys.exit('GET /api/automations → HTTP %s %s (нужен токен владельца в gina.env)' % (st, str(d)[:200]))
    return d if isinstance(d, list) else (d.get('items') or d.get('automations') or [])


def find(gi, name):
    live = [a for a in rows(gi) if a.get('name') == name and a.get('state') != 'retired']
    return live[0] if live else None


def remember(a, **extra):
    st = c.load_state('automation.json') or {}
    st.update({'id': a['id'], 'name': a['name'], 'state': a.get('state'), 'folder': '%s-%s' % (SLUG, a['id'][:8]), 'ts': c.now()}); st.update(extra)
    c.save_state('automation.json', st)
    return st


def describe(a):
    word = {'armed': 'ВКЛЮЧЁН (расписание работает)', 'paused': 'выключен', 'draft': 'черновик (выключен)'}.get(a.get('state'), 'состояние «%s»' % a.get('state'))
    print('регламент «%s»: id %s, %s' % (a['name'], a['id'], word))
    for t in a.get('triggers') or []:
        print('  интервал: каждые %s с%s' % (t.get('interval_seconds'), (', следующий запуск %s' % t.get('next_at')) if a.get('state') == 'armed' else ' (пока регламент выключен, запусков нет)'))
    if a.get('last_error'):
        print('  последняя ошибка: %s' % str(a['last_error'])[:300])


def cmd_status(_):
    gi = c.Gina(); d = json.loads(tpl('automation.json')); a = find(gi, d['name'])
    if not a:
        print('регламента «%s» на инстансе нет' % d['name']); return
    st = remember(a); describe(a); print('  папка определения в Git: automations/%s' % st['folder'])


def cmd_create(a):
    if not (a.confirmed_by or '').strip():
        sys.exit('нужен дословный ответ оператора: --confirmed-by "..."')
    if not c.feature('gitlab'):
        sys.exit('регламент побудки по сборке работает только с GitLab через прокси Gina, а признак gitlab в журнале мастера выключен — элемент не применим')
    import source_switch
    if not source_switch.is_live():
        sys.exit('инстанс ещё не читает ветку POC: в его каталоге нет навыка build-watch, регламент создавать рано. Сначала source_switch.py preview / switch.')
    gi = c.Gina(); d = json.loads(tpl('automation.json'))
    have = find(gi, d['name'])
    if have:
        remember(have); print('регламент уже существует — создание пропущено'); describe(have); return
    c.confirm('создание регламента «%s» выключенным' % d['name'], a.confirmed_by)
    body = {
        'name': d['name'], 'mission': tpl('mission.md'), 'poll_script': tpl('poll-script.js'), 'state': 'paused',
        'policy': d.get('policy') or {}, 'min_interval_seconds': d['min_interval_seconds'], 'max_interval_seconds': d['max_interval_seconds'],
        'concurrency_limit': d['concurrency_limit'], 'budget': d.get('budget') or {}, 'escalation': d.get('escalation') or {},
        'dispatch': {'mode': d['dispatch_mode'], 'agent': d['dispatch_agent'], 'skills': d['dispatch_skills'], 'instruction': tpl('dispatch-instruction.md')},
        'triggers': [{'idempotency_key': 'gina-poc:build-watch:interval', 'type': 'fixed_interval', 'interval_seconds': d['min_interval_seconds'],
                      'reason': 'разбор заявок build-watch раз в минуту'}],
        'idempotency_key': 'gina-poc:build-watch:create',
    }
    st, r = gi.call('POST', '/api/automations', body)
    if st not in (200, 201):
        sys.exit('Ядро отклонило создание: HTTP %s %s\nСТОП. Не подбирай другой состав запроса. Покажи этот ответ оператору. Запасной путь: оператор создаёт '
                 'регламент «%s» в интерфейсе Gina выключенным (тексты — в methodology/automation-templates/build-watch/), после чего выполни status.'
                 % (st, json.dumps(r, ensure_ascii=False)[:600], d['name']))
    auto = find(gi, d['name'])
    if not auto:
        sys.exit('ответ получен (HTTP %s), но регламент по названию не найден — СТОП, покажи оператору интерфейс Gina' % st)
    remember(auto, created_confirmed_by=a.confirmed_by)
    if auto.get('state') not in OFF:
        print('ВНИМАНИЕ: ядро создало регламент в состоянии «%s», а не выключенным. Выключаю.' % auto.get('state'))
        st, r = gi.call('POST', '/api/automations/%s/pause' % auto['id'], {'reason': 'мастер установки: регламент должен быть создан выключенным', 'idempotency_key': 'gina-poc:build-watch:pause-after-create'})
        auto = find(gi, d['name']); remember(auto)
        if auto.get('state') not in OFF:
            describe(auto); sys.exit('НЕ УДАЛОСЬ выключить (HTTP %s). Немедленно сообщи оператору: расписание уже работает без его согласия на включение.' % st)
    describe(auto)
    print('Дальше: python3 scripts/mr_rollout.py open 17  — положить определение в Git (папка automations/%s-%s).' % (SLUG, auto['id'][:8]))


def set_state(a, verb):
    if not (a.confirmed_by or '').strip():
        sys.exit('нужен дословный ответ оператора: --confirmed-by "..."')
    gi = c.Gina(); d = json.loads(tpl('automation.json')); auto = find(gi, d['name'])
    if not auto:
        sys.exit('регламента нет — сначала create')
    c.confirm('%s регламента «%s»' % ('включение' if verb == 'arm' else 'выключение', d['name']), a.confirmed_by)
    st, r = gi.call('POST', '/api/automations/%s/%s' % (auto['id'], verb),
                    {'reason': 'мастер установки POC, по согласию оператора', 'idempotency_key': 'gina-poc:build-watch:%s:%d' % (verb, int(time.time()))})
    if st != 200:
        sys.exit('Ядро отклонило: HTTP %s %s\nЕсли оно требует сначала один успешный ручной прогон — попроси оператора нажать «Прогнать сейчас» у регламента в '
                 'интерфейсе Gina (заявок нет, прогон должен завершиться штатно), затем повтори команду.' % (st, json.dumps(r, ensure_ascii=False)[:600]))
    auto = find(gi, d['name']); remember(auto, **{verb + '_confirmed_by': a.confirmed_by}); describe(auto)
    want = 'armed' if verb == 'arm' else 'paused'
    if auto.get('state') != want:
        sys.exit('ожидалось состояние «%s», получено «%s»' % (want, auto.get('state')))


def cmd_check(_):
    gi = c.Gina(); d = json.loads(tpl('automation.json')); auto = find(gi, d['name'])
    if not auto:
        print('регламента на инстансе нет'); sys.exit(1)
    remember(auto); describe(auto)
    ok = auto.get('state') == 'armed' and not auto.get('last_error')
    rc, out, err = c.kubectl(['logs', 'deploy/gina-evd', '--since=15m', '--tail=5000'], timeout=60)
    if rc == 0:
        bad = [l for l in out.splitlines() if auto['id'] in l and ('error' in l.lower() or 'failed' in l.lower())]
        print('  в логах ядра за 15 минут строк с ошибкой этого регламента: %d' % len(bad))
        for l in bad[:3]:
            print('    ' + l[:220])
        ok = ok and not bad
    else:
        print('  логи ядра не читаются (%s) — ошибки скрипта регламента проверить не могу' % (err or out).strip()[:80])
    rc, out, err = c.kubectl(['exec', 'deploy/gina-evd', '--', 'node', '-e',
                              'fetch("http://localhost:8080/api/memory?kind=build-watch-fault").then(async r=>console.log(JSON.stringify({s:r.status,b:await r.json().catch(()=>null)})))'], timeout=60)
    if rc == 0:
        try:
            j = json.loads(out.strip().splitlines()[-1]); items = (j.get('b') or {}).get('items') or []
            if j.get('s') != 200:
                print('  журнал странностей сторожа не читается: HTTP %s' % j.get('s'))
            elif items:
                x = items[0].get('data') or {}
                print('  последняя странность: %s — %s' % (x.get('at'), x.get('reason')))
            else:
                print('  журнал странностей пуст — хорошо')
        except Exception:
            print('  журнал странностей: ответ не разобран')
    else:
        print('  журнал странностей не прочитан: нет права exec в под — этот пункт не проверен')
    print('ИТОГ: %s' % ('регламент включён, ошибок не видно' if ok else 'есть замечания — см. выше'))
    sys.exit(0 if ok else 1)


if __name__ == '__main__':
    ap = argparse.ArgumentParser(); sub = ap.add_subparsers(dest='cmd', required=True)
    sub.add_parser('status'); sub.add_parser('check')
    for n in ('create', 'arm', 'pause'):
        sub.add_parser(n).add_argument('--confirmed-by', default='')
    a = ap.parse_args()
    {'status': cmd_status, 'check': cmd_check, 'create': cmd_create, 'arm': lambda x: set_state(x, 'arm'), 'pause': lambda x: set_state(x, 'pause')}[a.cmd](a)
