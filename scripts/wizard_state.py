#!/usr/bin/env python3
"""Журнал мастера установки: state/wizard.json. Секретов в журнале нет.

  python3 scripts/wizard_state.py show                      — где остановились (читать ПЕРВЫМ делом в любой новой сессии)
  python3 scripts/wizard_state.py step <1-8> start|done|partial ["пояснение"]
  python3 scripts/wizard_state.py position "<что делаю прямо сейчас и чего жду от оператора>"
  python3 scripts/wizard_state.py set <ключ> <значение>     — несекретная настройка
  python3 scripts/wizard_state.py feature <признак> on|off "<основание>"   — gitlab | jira | code_conventions | product_ui
  python3 scripts/wizard_state.py decision <шаг> "<решение оператора своими словами>"
  python3 scripts/wizard_state.py complete                  — мастер пройден, дальше обычная работа

Настройки (ключи set):
  проект:       project_name, product_summary, code_base (путь группы или репозитория), code_host (адрес хостинга кода),
                tracker (что и где: «Jira, проект KEY» / «нет»), tracker_project (ключ проекта в трекере),
                ui_urls (адреса интерфейса продукта через запятую), product_namespaces (пространства стендов через запятую),
                mm_channel, human_reviewer, project_owner, branch_rule, agent_transitions
  методология:  methodology_project, default_branch, poc_branch
  сторож:       watch_interval_minutes, daily_summary_utc_hour
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common as c

KEYS = ['project_name', 'product_summary', 'code_base', 'code_host', 'tracker', 'tracker_project', 'ui_urls', 'product_namespaces',
        'mm_channel', 'human_reviewer', 'project_owner', 'branch_rule', 'agent_transitions',
        'methodology_project', 'default_branch', 'poc_branch', 'watch_interval_minutes', 'daily_summary_utc_hour']
FEATURES = ['gitlab', 'jira', 'code_conventions', 'product_ui']


def show(w):
    print('МАСТЕР УСТАНОВКИ: %s' % ('ПРОЙДЕН — режим обычной работы (см. HANDOFF.md, «После установки»)' if w.get('completed') else 'не завершён'))
    for n, title in c.STEPS.items():
        s = w['steps'].get(n) or {}
        print('  шаг %s (%s): %s%s' % (n, title, {'start': 'идёт', 'done': 'пройден', 'partial': 'пройден частично'}.get(s.get('status'), 'не начат'),
                                     (' — ' + s['note']) if s.get('note') else ''))
    if w.get('position'):
        print('Остановились на: %s' % w['position'])
    if w.get('settings'):
        print('Настройки:'); [print('  %s = %s' % kv) for kv in sorted(w['settings'].items()) if kv[0] != 'features']
        for name, f in sorted((w['settings'].get('features') or {}).items()):
            print('  признак %s: %s — %s' % (name, 'включён' if f.get('on') else 'выключен', f.get('reason', '')))
    if w.get('decisions'):
        print('Решения оператора:'); [print('  [шаг %s, %s] %s' % (d['step'], d['ts'][:16], d['text'])) for d in w['decisions']]
    if w.get('confirmations'):
        print('Согласия на действия (последние 8 из %d):' % len(w['confirmations']))
        [print('  [%s] %s ← «%s»' % (x['ts'][:16], x['action'], x['text'])) for x in w['confirmations'][-8:]]
    r = c.load_state('rollout.json')
    if r:
        print('Накат методологии: python3 scripts/mr_rollout.py status')


def main():
    a = sys.argv[1:]
    w = c.wizard()
    if not a or a[0] == 'show':
        return show(w)
    if a[0] == 'step' and len(a) >= 3 and a[1] in c.STEPS and a[2] in ('start', 'done', 'partial'):
        w['steps'][a[1]] = {'status': a[2], 'ts': c.now(), 'note': a[3] if len(a) > 3 else ''}
        if a[2] != 'start': w['position'] = ''
    elif a[0] == 'position' and len(a) == 2:
        w['position'] = a[1]
    elif a[0] == 'set' and len(a) == 3 and a[1] in KEYS:
        w['settings'][a[1]] = a[2]
    elif a[0] == 'feature' and len(a) == 4 and a[1] in FEATURES and a[2] in ('on', 'off'):
        w['settings'].setdefault('features', {})[a[1]] = {'on': a[2] == 'on', 'reason': a[3], 'ts': c.now()}
    elif a[0] == 'decision' and len(a) == 3:
        w['decisions'].append({'ts': c.now(), 'step': a[1], 'text': a[2]})
    elif a[0] == 'complete':
        w['completed'] = True; w['position'] = ''
    else:
        sys.exit(__doc__)
    c.save_state(c.WIZARD, w); print('записано')


if __name__ == '__main__':
    main()
