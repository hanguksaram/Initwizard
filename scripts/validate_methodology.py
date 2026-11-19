#!/usr/bin/env python3
"""Локальная проверка артефактов методологии до открытия MR: структура, имена, лимиты длины ядра Gina,
ссылки агентов на навыки, соответствие ROLLOUT.json, составленные мастером навыки. Окончательную проверку делает
предпросмотр на инстансе. Элементы, чей признак выключен в журнале мастера, не проверяются на наличие текста.

Запуск: python3 scripts/validate_methodology.py   (код 0 — в порядке)
"""
import json, os, re, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common as c
PACK = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ROOT = os.path.join(PACK, 'methodology', 'files')
NAME = re.compile(r'^[a-z](?:[a-z0-9-]{0,62}[a-z0-9])?$')
ROLE_KEYS = {'feature-lead': {'planner_base_prompt', 'planner_rules', 'planner_prompt_composition'}, 'worker': {'worker_base_prompt', 'worker_rules', 'worker_skill_delivery', 'worker_runtime'},
             'delivery-lead': {'facilitator_prompt', 'facilitator_rules', 'supervisor_rules', 'waiting_reminder_prompt'}, 'dispatcher': {'chooser_prompt', 'chooser_rules', 'router_rules'}}
P = []
def js(path):
    try:
        return json.load(open(path, encoding='utf-8'))
    except Exception as e:
        P.append('%s: не JSON (%s)' % (os.path.relpath(path, ROOT), e)); return None
def ln(s):  # длина как в JS: единицы UTF-16
    return len(s.strip().encode('utf-16-le')) // 2
man = json.load(open(os.path.join(PACK, 'methodology', 'ROLLOUT.json'), encoding='utf-8'))
by_skill = {i['name']: i for i in man['items'] if i['kind'] == 'skill'}
def off(item):
    return bool(item and item.get('feature') and not c.feature(item['feature']))
skills, pending = set(), []
for n in sorted(os.listdir(os.path.join(ROOT, 'skills'))):
    b = os.path.join(ROOT, 'skills', n); m = js(os.path.join(b, 'skill.json'))
    if not m: continue
    it = by_skill.get(n)
    if it and it.get('generated'):
        md = os.path.join(b, 'SKILL.md')
        if off(it):
            continue                                   # признак выключен: навык не ставится
        if not os.path.exists(md) or 'ЗАПОЛНИ' in open(md, encoding='utf-8').read():
            pending.append(n); skills.add(n); continue   # составляется на шаге 2 мастера
    elif off(it):
        continue
    if m.get('name') != n or not NAME.match(n): P.append('skills/%s: имя папки и name должны совпадать и быть вида a-z0-9-' % n)
    if not 0 < ln(m.get('description') or '') <= 1000: P.append('skills/%s: описание пустое или длиннее 1000 символов' % n)
    t = open(os.path.join(b, 'SKILL.md'), encoding='utf-8').read() if os.path.exists(os.path.join(b, 'SKILL.md')) else ''
    if not 0 < ln(t) <= 100000: P.append('skills/%s: SKILL.md пуст или длиннее 100000 символов' % n)
    if m.get('enabled') is not False: skills.add(n)
for n in sorted(os.listdir(os.path.join(ROOT, 'agents'))):
    b = os.path.join(ROOT, 'agents', n); m = js(os.path.join(b, 'agent.json'))
    if not m: continue
    if m.get('name') != n or not NAME.match(n): P.append('agents/%s: имя папки и name должны совпадать' % n)
    if not 0 < ln(m.get('description') or '') <= 4000: P.append('agents/%s: описание пустое или длиннее 4000 символов' % n)
    t = open(os.path.join(b, 'system-prompt.md'), encoding='utf-8').read() if os.path.exists(os.path.join(b, 'system-prompt.md')) else ''
    if not 0 < ln(t) <= 100000: P.append('agents/%s: system-prompt.md пуст или длиннее 100000 символов' % n)
    sf = man.get('skill_features') or {}
    miss = [s for s in m.get('skills') or [] if s not in skills and not (s in sf and not c.feature(sf[s]))]
    if miss: P.append('agents/%s: навыков нет в пакете: %s' % (n, ', '.join(miss)))
for n in sorted(os.listdir(os.path.join(ROOT, 'roles'))):
    b = os.path.join(ROOT, 'roles', n); m = js(os.path.join(b, 'role.json'))
    if not m: continue
    if m.get('role') != n or n not in ROLE_KEYS: P.append('roles/%s: неизвестная роль' % n); continue
    for k in m.get('keys') or []:
        if k.get('key') not in ROLE_KEYS[n]: P.append('roles/%s: неизвестный ключ %s' % (n, k.get('key')))
        if k.get('present') and k.get('file') and not os.path.exists(os.path.join(b, k['file'])): P.append('roles/%s: нет файла %s' % (n, k['file']))
listed = {f for i in man['items'] for f in i['files']}
for i in man['items']:
    if i['kind'] == 'automation' and not off(i):
        tdir = os.path.join(PACK, i['template'])
        for name in ('automation.json', 'mission.md', 'dispatch-instruction.md', 'poll-script.js'):
            if not os.path.exists(os.path.join(tdir, name)): P.append('%s: нет файла %s' % (i['template'], name))
        d = js(os.path.join(tdir, 'automation.json')) or {}
        if d.get('dispatch_agent') and d['dispatch_agent'] not in os.listdir(os.path.join(ROOT, 'agents')): P.append('%s: агента %s нет в пакете' % (i['template'], d.get('dispatch_agent')))
        for s in d.get('dispatch_skills') or []:
            if s not in skills: P.append('%s: навыка %s нет в пакете' % (i['template'], s))
        ps = os.path.join(tdir, 'poll-script.js')
        if os.path.exists(ps) and os.path.getsize(ps) > 65536: P.append('%s: poll-script.js больше 65536 байт' % i['template'])
gen_files = {f for i in man['items'] if i.get('generated') for f in i['files']}
for f in listed:
    if not os.path.exists(os.path.join(PACK, f)) and f not in gen_files: P.append('ROLLOUT.json: нет файла ' + f)
for d, _, fs in os.walk(ROOT):
    for f in fs:
        if f.endswith('.md') or f.endswith('.json'):
            txt = open(os.path.join(d, f), encoding='utf-8').read()
            for mm in re.finditer(r'^\[если:!?([a-z_]+)\]', txt, re.M):
                if mm.group(1) not in (man.get('features') or {}): P.append('%s: неизвестный признак в условной строке: %s' % (os.path.relpath(os.path.join(d, f), PACK), mm.group(1)))
for d, _, fs in os.walk(ROOT):
    for f in fs:
        rel = os.path.relpath(os.path.join(d, f), PACK)
        if rel not in listed: P.append('файл не входит ни в один элемент ROLLOUT.json: ' + rel)
ids = [i['id'] for i in man['items']]
for i in man['items']:
    for dep in i.get('depends', []):
        if dep not in ids or ids.index(dep) >= ids.index(i['id']): P.append('ROLLOUT.json: элемент %s зависит от %s, который идёт не раньше' % (i['id'], dep))
print('\n'.join(P) if P else 'OK: навыков к установке %d, агентов %d, элементов наката %d' % (len(skills), len(os.listdir(os.path.join(ROOT, 'agents'))), len(ids)))
if pending and not P:
    print('Ещё не составлены мастером (шаг 2): %s' % ', '.join(pending))
wanted = [f for f in man.get('features') or {} if c.feature(f)]
if not P:
    print('Включённые признаки: %s' % (', '.join(wanted) or 'нет'))
sys.exit(1 if P else 0)
