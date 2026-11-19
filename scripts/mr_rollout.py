#!/usr/bin/env python3
"""Шаг 2 мастера: накат POC-методологии малыми merge request-ами в ветку POC. Один элемент ROLLOUT.json = один MR.

  python3 scripts/mr_rollout.py plan                  — таблица элементов и состояние
  python3 scripts/mr_rollout.py allow-open --confirmed-by "<ответ оператора>"
                                                      — разовое согласие создавать ветки и MR в ветке POC
  python3 scripts/mr_rollout.py diff <id>             — что именно изменит элемент (показать оператору)
  python3 scripts/mr_rollout.py open <id>             — ветка → коммит → MR → проверка (CI, предпросмотр)
  python3 scripts/mr_rollout.py verify <id>           — повторить проверку / сверку (безопасно запускать сколько угодно раз)
  python3 scripts/mr_rollout.py merge <id> --confirmed-by "<дословный ответ оператора>"
  python3 scripts/mr_rollout.py skip <id> --reason "<почему>"     — пропустить; открытый MR закрывается
  python3 scripts/mr_rollout.py status                — где остановились
  python3 scripts/mr_rollout.py confirmations         — журнал согласий оператора (показать в итоге)
  python3 scripts/mr_rollout.py preview-branch <ветка> — предпросмотр произвольной ветки (например, MR-отката)

Два режима, скрипт определяет сам:
  «подготовка» — инстанс ещё НЕ читает ветку POC. Влитие в ветку на работу агентов не влияет. Предпросмотр проверяет,
                 что ветка разбирается ядром без ошибок.
  «вживую»     — инстанс читает ветку POC. Каждое влитие сразу меняет поведение агентов. Предпросмотр обязан
                 затрагивать только сущность элемента, иначе влитие отклоняется; после влития ждём применения.

Предохранители: merge без --confirmed-by не выполняется; merge отклоняется, если предпросмотр не прошёл, сделан для
другого коммита или (вживую) затрагивает лишнее. Состояние пишется до долгих ожиданий: оборванную команду достаточно
повторить или выполнить verify. Долгие команды (open, verify, merge) запускай с лимитом времени не меньше 10 минут.
"""
import argparse, difflib, json, os, sys, time, urllib.parse
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common as c

MANIFEST = json.load(open(os.path.join(c.PACK, 'methodology', 'ROLLOUT.json'), encoding='utf-8'))
ITEMS = {i['id']: i for i in MANIFEST['items']}
FOOTER = '\n\n---\nPOC-методология, элемент {id} из {n}. Установлено мастером установки по подтверждению оператора.'
OPEN_CONSENT = 'создание веток и merge request-ов в ветке POC'
POLL = float(os.environ.get('GINA_POC_POLL_SECONDS', '5'))   # пауза между опросами; меняется только в испытаниях
q = lambda s: urllib.parse.quote(s, safe='')


# ------------------------------------------------------------------ состояние
def state():
    return c.load_state('rollout.json', {'items': {}})


def put(item_id, **kw):
    s = state()
    s['items'].setdefault(item_id, {}).update(kw, ts=c.now())
    c.save_state('rollout.json', s)
    return s['items'][item_id]


def gitlab():
    g = c.GitLab()
    if not g.branch:
        sys.exit('ветка POC ещё не создана: python3 scripts/branch_setup.py create --name <ветка> --confirmed-by "..."')
    return g


def live():
    import source_switch
    return source_switch.is_live()


# ------------------------------------------------------------------ содержимое элементов
PLACEHOLDERS = (('project_name', 'PROJECT_NAME', 'проект'), ('code_base', 'CODE_BASE', 'не указано'), ('code_host', 'CODE_HOST', 'не указан'),
                ('tracker', 'TRACKER', 'не указан'), ('mm_channel', 'MM_CHANNEL', 'не назначен'), ('human_reviewer', 'HUMAN_REVIEWER', 'не назначен'),
                ('project_owner', 'PROJECT_OWNER', 'не назначен'))


def applicable(item):
    """(применим ли элемент, почему нет)"""
    f = item.get('feature')
    if f and not c.feature(f):
        reason = ((c.settings().get('features') or {}).get(f) or {}).get('reason') or 'признак «%s» не включён в журнале мастера' % f
        return False, reason
    return True, ''


def render(text):
    """Условные строки и несекретные настройки оператора.
    Строка, начинающаяся с [если:признак], остаётся (без метки) только при включённом признаке;
    с [если:!признак] — только при выключенном."""
    out = []
    for line in text.split('\n'):
        m = __import__('re').match(r'^\[если:(!?)([a-z_]+)\]', line)
        if m:
            if c.feature(m.group(2)) == bool(m.group(1)):
                continue
            line = line[m.end():]
        out.append(line)
    text = '\n'.join(out)
    s = c.settings()
    for key, name, default in PLACEHOLDERS:
        text = text.replace('{{%s}}' % name, s.get(key) or default)
    return text


def render_agent(text):
    """Из набора навыков агента убираются навыки, чей признак выключен."""
    meta = json.loads(text); sf = MANIFEST.get('skill_features') or {}
    meta['skills'] = [s for s in meta.get('skills') or [] if s not in sf or c.feature(sf[s])]
    return json.dumps(meta, ensure_ascii=False, indent=2) + '\n'


def merge_role_json(existing, ours):
    """role.json в ветке не затирается: в существующий файл вписываются только наши ключи, остальное сохраняется."""
    try:
        old, new = json.loads(existing), json.loads(ours)
    except ValueError:
        return ours
    keys = {k.get('key'): k for k in old.get('keys') or []}
    for k in new.get('keys') or []:
        keys[k.get('key')] = k
    settings = dict(old.get('settings') or {}); settings.update(new.get('settings') or {})
    if list(keys.values()) == (old.get('keys') or []) and settings == (old.get('settings') or {}):
        return existing            # по содержанию ничего не меняется — оставляем файл как есть
    return json.dumps({'role': new.get('role') or old.get('role'), 'keys': list(keys.values()), 'settings': settings}, ensure_ascii=False, indent=2) + '\n'


def files_of(item):
    """[(путь в репозитории, содержимое из пакета)]"""
    out = []
    if item['kind'] == 'automation':
        auto = c.load_state('automation.json')   # папка регламента называется по id, известному после создания через API
        if not auto:
            sys.exit('регламент ещё не создан на инстансе: сначала python3 scripts/automation_setup.py create --confirmed-by "..."')
        tdir = os.path.join(c.PACK, item['template'])
        for name in sorted(os.listdir(tdir)):
            out.append(('automations/%s/%s' % (auto['folder'], name), open(os.path.join(tdir, name), encoding='utf-8').read()))
        return out
    for f in item['files']:
        full = os.path.join(c.PACK, f)
        if item.get('generated') and not os.path.exists(full):
            sys.exit('элемент %s составляет мастер на шаге 2, а файла %s ещё нет — см. wizard/2-project.md' % (item['id'], f))
        text = open(full, encoding='utf-8').read()
        if item.get('generated') and 'ЗАПОЛНИ' in text:
            sys.exit('в %s остались незаполненные места шаблона (метка «ЗАПОЛНИ») — допиши навык по wizard/2-project.md' % f)
        text = render_agent(text) if f.endswith('/agent.json') else render(text)
        out.append((f[len(MANIFEST['strip_prefix']):], text))
    return out


def compare(g, item, ref=None):
    """[(путь, 'new'|'same'|'differs', старое, новое)] — относительно ветки ref (по умолчанию ветка POC)."""
    res = []
    for path, new in files_of(item):
        old = g.raw(path, ref)
        if old is not None and path.endswith('/role.json'):
            new = merge_role_json(old, new)
        res.append((path, 'new' if old is None else ('same' if old == new else 'differs'), old, new))
    return res


def expected_entity(item):
    return item['name'].split('/')[-1] if item['kind'] == 'role' else item['name']


def missing_skills(g, item):
    """Навыки агента или регламента, которых нет в ветке POC."""
    if item['kind'] == 'agent':
        need = json.loads(dict(files_of(item))['agents/%s/agent.json' % item['name']]).get('skills') or []
    elif item['kind'] == 'automation':
        need = json.load(open(os.path.join(c.PACK, item['template'], 'automation.json'), encoding='utf-8')).get('dispatch_skills') or []
    else:
        return []
    return [s for s in need if g.raw('skills/%s/skill.json' % s) is None]


# ------------------------------------------------------------------ проверки
def pipeline_status(g, iid, sha, wait_seconds=300):
    """'success' | 'failed' | 'canceled' | 'skipped' | 'manual' | 'none' (CI для MR нет) | 'running' (не дождались)."""
    t0 = time.time(); seen = False
    while True:
        st, pl = g.p('GET', '/merge_requests/%d/pipelines' % iid)
        mine = [x for x in (pl if st == 200 and isinstance(pl, list) else []) if x.get('sha') == sha]
        if mine:
            seen = True
            if mine[0]['status'] in ('success', 'failed', 'canceled', 'skipped', 'manual', 'scheduled'):
                return 'manual' if mine[0]['status'] in ('manual', 'scheduled') else mine[0]['status']
        if not seen and time.time() - t0 > 4 * POLL:
            return 'none'
        if time.time() - t0 > wait_seconds:
            return 'running'
        time.sleep(POLL)


def preview(branch):
    gi = c.Gina()
    st, ps = gi.call('GET', '/api/prompt-source')
    if st != 200 or not isinstance(ps, dict):
        return {'ok': False, 'error': 'GET /api/prompt-source → %s (нужен токен владельца)' % st}
    cfg = ps.get('config') or {}
    body = {'instance_id': cfg.get('instance_id'), 'project': c.settings()['methodology_project'].strip('/'), 'branch': branch}
    st, pv = gi.call('POST', '/api/prompt-source/preview', body, timeout=180)
    return pv if isinstance(pv, dict) else {'ok': False, 'error': 'HTTP %s: %s' % (st, str(pv)[:200])}


def check_opened(g, item, s, is_live):
    """Проверка открытого MR: CI и предпросмотр. Возвращает (можно вливать?, причины отказа)."""
    st, mr = g.p('GET', '/merge_requests/%d?include_diverged_commits_count=true' % s['iid'])
    if st != 200:
        return False, ['MR !%s не читается: HTTP %s' % (s['iid'], st)]
    if mr.get('state') == 'merged':
        return True, []
    if mr.get('state') == 'closed':
        return False, ['MR !%s закрыт — открой заново: open %s' % (s['iid'], item['id'])]
    if mr.get('diverged_commits_count'):
        g.p('PUT', '/merge_requests/%d/rebase' % s['iid'], {})
        for _ in range(30):
            time.sleep(POLL)
            st, mr = g.p('GET', '/merge_requests/%d?include_rebase_in_progress=true&include_diverged_commits_count=true' % s['iid'])
            if not mr.get('rebase_in_progress'):
                break
        if mr.get('merge_error') or mr.get('diverged_commits_count'):
            return False, ['ветка MR отстала от ветки POC и не перебазировалась: %s' % (mr.get('merge_error') or 'конфликт')]
        print('  ветка MR перебазирована на свежую ветку POC')
    put(item['id'], sha=mr['sha'])
    ci = pipeline_status(g, s['iid'], mr['sha'])
    pv = preview(s['branch'])
    names = [[x.get('kind'), x.get('name')] for x in pv.get('items') or []]
    unexpected = [n for n in names if n[1] != expected_entity(item)] if is_live else []
    put(item['id'], ci=ci, mode='live' if is_live else 'staging',
        preview={'ok': bool(pv.get('ok')), 'sha': pv.get('sha'), 'items': names, 'changes': pv.get('changes'),
                 'warnings': pv.get('warnings'), 'error': pv.get('error'), 'unexpected': unexpected})
    print('MR !%s  %s' % (s['iid'], s['url']))
    print('CI репозитория: %s' % {'none': 'для merge request-ов в этом репозитории проверки нет', 'running': 'ещё идёт — повтори verify позже',
                                  'manual': 'ждёт ручного запуска'}.get(ci, ci))
    print('Предпросмотр на инстансе (ядро разбирает ветку, ничего не применяя): %s' % ('прошёл' if pv.get('ok') else 'НЕ ПРОШЁЛ'))
    if pv.get('error'):
        print('  ошибка ядра: %s' % pv['error'])
    for w in pv.get('warnings') or []:
        print('  предупреждение ядра: %s' % w)
    if is_live:
        print('  режим «вживую»: влитие сразу изменит на инстансе: %s' % (', '.join('%s %s' % tuple(n) for n in names) or 'ничего (содержимое совпадает с действующим)'))
        if unexpected:
            print('  ЛИШНЕЕ в предпросмотре: %s' % unexpected)
    else:
        print('  режим «подготовка»: инстанс эту ветку пока не читает, влитие на работу агентов не повлияет')
    why = []
    if not pv.get('ok'):
        why.append('предпросмотр не прошёл')
    elif pv.get('sha') != mr['sha']:
        why.append('предпросмотр сделан для другого коммита — повтори verify')
    if ci in ('failed', 'canceled', 'running', 'manual'):
        why.append('CI: %s' % ci)
    if unexpected:
        why.append('предпросмотр затрагивает не только сущность элемента')
    return not why, why


def check_merged(g, item, s, is_live):
    """Сверка после влития: файлы в ветке POC и (вживую) применение инстансом."""
    bad = [p for p, k, _, _ in compare(g, item) if k != 'same']
    print('Файлы в ветке POC «%s»: %s' % (g.branch, 'на месте' if not bad else 'РАСХОЖДЕНИЕ: %s' % bad))
    synced, note = None, 'инстанс эту ветку не читает — подействует после переключения источника'
    if is_live:
        gi = c.Gina()
        st, b = g.p('GET', '/repository/branches/' + q(g.branch))
        head = b['commit']['id'] if st == 200 else None
        for _ in range(36):
            st, ps = gi.call('GET', '/api/prompt-source')
            status = (ps.get('status') or {}) if st == 200 and isinstance(ps, dict) else {}
            if status.get('sha') == head and status.get('state'):
                synced = status.get('state') == 'ok'
                note = 'инстанс применил коммит %s: состояние %s%s' % (head[:8], status.get('state'), ', ошибка: %s' % status.get('error') if status.get('error') else '')
                break
            time.sleep(2 * POLL)
        else:
            synced, note = False, 'инстанс за отведённое время не применил голову ветки POC (%s) — проверь python3 scripts/source_switch.py show' % (head or '?')[:8]
    print('Применение: %s' % note)
    put(item['id'], files_ok=not bad, synced=synced, sync_note=note, verified=c.now())
    return not bad and synced is not False


# ------------------------------------------------------------------ команды
def cmd_plan(_):
    g = gitlab(); s = state()['items']; is_live = live()
    print('Репозиторий %s, ветка POC «%s». Режим: %s\n' % (g.project, g.branch,
          '«вживую» — инстанс читает эту ветку, каждое влитие действует сразу' if is_live else '«подготовка» — инстанс эту ветку пока не читает'))
    names = {'opened': 'открыт MR', 'merged': 'влит', 'already': 'уже был', 'skipped': 'пропущен'}
    for i in MANIFEST['items']:
        st = names.get(s.get(i['id'], {}).get('state'), 'не начат')
        ok_app, why_na = applicable(i)
        if not ok_app:
            print('%s  %-30s %-10s не применимо: %s' % (i['id'], i['slug'], '—', why_na)); continue
        if i.get('generated') and (not os.path.exists(os.path.join(c.PACK, i['files'][-1])) or 'ЗАПОЛНИ' in open(os.path.join(c.PACK, i['files'][-1]), encoding='utf-8').read()):
            print('%s  %-30s %-10s навык ещё не составлен (шаг 2 мастера)' % (i['id'], i['slug'], st)); continue
        if i['kind'] == 'automation' and not c.load_state('automation.json'):
            where = 'создаётся через API после переключения инстанса (scripts/automation_setup.py), затем MR'
        else:
            kinds = {k for _, k, _, _ in compare(g, i)}
            where = 'в ветке уже есть, содержимое другое' if 'differs' in kinds else ('в ветке уже установлено' if kinds == {'same'} else 'новое')
        print('%s  %-30s %-10s %s' % (i['id'], i['slug'], st, where))
        print('      зачем: %s' % i['purpose'])
        print('      что изменится: %s' % i['effect'])


def cmd_allow_open(a):
    if not (a.confirmed_by or '').strip():
        sys.exit('нужен дословный ответ оператора: --confirmed-by "..."')
    c.confirm(OPEN_CONSENT, a.confirmed_by); print('записано: мастер может создавать ветки и merge request-ы в ветке POC; каждое влитие — по отдельному согласию')


def cmd_diff(a):
    g = gitlab(); item = ITEMS[a.id]
    for path, kind, old, new in compare(g, item):
        print('=== %s: %s' % (path, {'new': 'новый файл', 'same': 'без изменений', 'differs': 'изменение существующего в ветке POC'}[kind]))
        if kind == 'new':
            print(new)
        elif kind == 'differs':
            print(''.join(difflib.unified_diff(old.splitlines(1), new.splitlines(1), 'в ветке POC', 'из пакета', n=2)))


def cmd_open(a):
    g = gitlab(); item = ITEMS[a.id]; s = state()['items']; cur = s.get(a.id, {})
    if not any(x['action'] == OPEN_CONSENT for x in c.wizard().get('confirmations', [])):
        sys.exit('нет согласия оператора на создание merge request-ов: сначала allow-open --confirmed-by "..."')
    ok_app, why_na = applicable(item)
    if not ok_app:
        sys.exit('элемент %s к этому проекту не применим: %s' % (a.id, why_na))
    notdone = [d for d in item.get('depends', []) if applicable(ITEMS[d])[0] and s.get(d, {}).get('state') not in ('merged', 'already', 'skipped')]
    if notdone:
        sys.exit('сначала заверши элементы: %s' % ', '.join(notdone))
    if item.get('needs_live') and not live():
        sys.exit('этот элемент ставится только после переключения инстанса на ветку POC: python3 scripts/source_switch.py preview / switch')
    miss = missing_skills(g, item)
    if miss:
        sys.exit('элемент ссылается на навыки, которых нет в ветке POC: %s.\nВарианты для оператора: (1) вернуть пропущенные навыки — open <их номер> и влить; '
                 '(2) пропустить и этот элемент — skip %s --reason "..."; (3) убрать навык из %s в пакете (methodology/) и повторить open.'
                 % (', '.join(miss), a.id, 'agent.json агента' if item['kind'] == 'agent' else 'automation.json регламента'))
    cmp_ = compare(g, item)
    if all(k == 'same' for _, k, _, _ in cmp_):
        put(a.id, state='already'); print('в ветке POC уже установлено — MR не нужен'); return
    foreign = [p for p, k, _, _ in cmp_ if k == 'differs' and not p.endswith('/role.json')]
    if foreign and not a.replace_confirmed_by and cur.get('state') != 'opened':
        sys.exit('в ветке POC уже есть эти файлы с другим содержимым: %s. Покажи оператору `diff %s` и повтори с '
                 '--replace-confirmed-by "<его ответ на ЭТОТ элемент>"' % (', '.join(foreign), a.id))
    if a.replace_confirmed_by:
        c.confirm('замена существующих файлов элемента %s в ветке POC' % a.id, a.replace_confirmed_by)
    branch = MANIFEST['branch_prefix'] + item['id'] + '-' + item['slug']
    st, b = g.p('GET', '/repository/branches/' + q(branch))
    if st != 200:
        st, b = g.p('POST', '/repository/branches?branch=%s&ref=%s' % (q(branch), q(g.branch)))
        if st >= 300:
            sys.exit('не удалось создать ветку %s: HTTP %s %s' % (branch, st, str(b)[:200]))
    actions = [{'action': 'create' if kind == 'new' else 'update', 'file_path': path, 'content': new}
               for path, kind, _, new in compare(g, item, ref=branch) if kind != 'same']
    if actions:
        st, r = g.p('POST', '/repository/commits', {'branch': branch, 'commit_message': item['title'] + '\n\n' + item['purpose'], 'actions': actions})
        if st >= 300:
            sys.exit('коммит не создан: HTTP %s %s' % (st, str(r)[:300]))
    st, mrs = g.p('GET', '/merge_requests?state=opened&source_branch=' + q(branch))
    if st == 200 and mrs:
        mr = mrs[0]
    else:
        desc = ('## Зачем\n%s\n\n## Что изменится\n%s\n\n## Файлы\n%s' % (
            item['purpose'], item['effect'], '\n'.join('- `%s`' % p for p, _ in files_of(item)))) + FOOTER.format(id=item['id'], n=len(ITEMS))
        st, mr = g.p('POST', '/merge_requests', {'source_branch': branch, 'target_branch': g.branch, 'title': item['title'],
                                                 'description': desc, 'remove_source_branch': True})
        if st >= 300:
            sys.exit('MR не создан: HTTP %s %s' % (st, str(mr)[:300]))
    s1 = put(a.id, state='opened', iid=mr['iid'], url=mr['web_url'], branch=branch, sha=mr.get('sha'), ci=None, preview=None)   # до ожиданий
    ok, why = check_opened(g, item, s1, live())
    print('\nМожно предлагать оператору влитие.' if ok else '\nВливать НЕЛЬЗЯ: %s.' % '; '.join(why))
    sys.exit(0 if ok else 1)


def cmd_verify(a):
    g = gitlab(); item = ITEMS[a.id]; s = state()['items'].get(a.id) or {}
    if s.get('state') == 'opened':
        ok, why = check_opened(g, item, s, live())
        print('\nМожно предлагать оператору влитие.' if ok else '\nВливать НЕЛЬЗЯ: %s.' % '; '.join(why)); sys.exit(0 if ok else 1)
    if s.get('state') in ('merged', 'already'):
        sys.exit(0 if check_merged(g, item, s, live()) else 1)
    sys.exit('элемент %s в состоянии «%s» — проверять нечего' % (a.id, s.get('state', 'не начат')))


def cmd_merge(a):
    g = gitlab(); item = ITEMS[a.id]; s = state()['items'].get(a.id) or {}; is_live = live()
    if s.get('state') == 'merged':
        print('элемент уже влит — выполняю сверку'); sys.exit(0 if check_merged(g, item, s, is_live) else 1)
    if s.get('state') != 'opened':
        sys.exit('элемент %s не открыт (сейчас: %s) — сначала open' % (a.id, s.get('state', 'не начат')))
    if not (a.confirmed_by or '').strip():
        sys.exit('нужен дословный ответ оператора на вопрос о влитии ЭТОГО элемента: --confirmed-by "..."')
    st, mr = g.p('GET', '/merge_requests/%d?include_diverged_commits_count=true' % s['iid'])
    if mr.get('state') != 'merged':
        pv = s.get('preview') or {}
        problems = []
        if mr.get('diverged_commits_count'): problems.append('ветка MR отстала от ветки POC')
        if mr['sha'] != s.get('sha'): problems.append('в ветке MR появились коммиты после проверки')
        if not pv.get('ok') or pv.get('sha') != mr['sha']: problems.append('нет успешного предпросмотра для текущего коммита')
        if s.get('ci') not in ('success', 'none', 'skipped'): problems.append('CI: %s' % s.get('ci'))
        if is_live and (s.get('mode') != 'live' or pv.get('unexpected')): problems.append('предпросмотр сделан до переключения инстанса или затрагивает лишнее')
        if problems:
            sys.exit('влитие отклонено: %s. Выполни `verify %s` и повтори.' % ('; '.join(problems), a.id))
        c.confirm('влитие элемента %s (%s), MR !%s%s' % (a.id, item['slug'], s['iid'], ' — ВЖИВУЮ' if is_live else ''), a.confirmed_by)
        st, r = g.p('PUT', '/merge_requests/%d/merge' % s['iid'], {'sha': mr['sha'], 'should_remove_source_branch': True})
        if st >= 300:
            sys.exit('GitLab отклонил влитие: HTTP %s %s. Если у токена нет права влития — попроси оператора нажать Merge в GitLab и повтори эту команду.' % (st, str(r)[:200]))
    else:
        c.confirm('влитие элемента %s выполнено оператором в GitLab, MR !%s' % (a.id, s['iid']), a.confirmed_by)
        print('MR уже влит в GitLab — выполняю сверку')
    s = put(a.id, state='merged', confirmed_by=a.confirmed_by)   # до ожидания применения
    print('Влито: %s' % item['title'])
    sys.exit(0 if check_merged(g, item, s, is_live) else 1)


def cmd_skip(a):
    if not (a.reason or '').strip():
        sys.exit('нужна причина словами оператора: --reason "..."')
    g = gitlab(); s = state()['items'].get(a.id) or {}
    if s.get('state') == 'merged':
        sys.exit('элемент уже влит — пропустить нельзя; откат делается отдельным merge request-ом (Revert в GitLab)')
    if s.get('state') == 'opened':
        st, r = g.p('PUT', '/merge_requests/%d' % s['iid'], {'state_event': 'close'})
        g.p('DELETE', '/repository/branches/' + q(s['branch']))
        print('открытый MR !%s %s' % (s['iid'], 'закрыт, его ветка удалена' if st < 300 else 'закрыть не удалось (HTTP %s) — закрой в GitLab вручную' % st))
    put(a.id, state='skipped', reason=a.reason); c.confirm('пропуск элемента %s' % a.id, a.reason)
    dependents = [i['id'] for i in MANIFEST['items'] if a.id in i.get('depends', [])]
    print('элемент %s пропущен. Вернуть позже: open %s.' % (a.id, a.id))
    if dependents:
        print('От него зависят элементы: %s — при их открытии скрипт скажет, чего не хватает.' % ', '.join(dependents))


def cmd_status(_):
    s = state()['items']
    names = {'opened': 'открыт MR', 'merged': 'влит', 'already': 'уже был', 'skipped': 'пропущен'}
    for i in MANIFEST['items']:
        x = s.get(i['id'], {}); st = x.get('state')
        extra = x.get('url', '')
        if st == 'skipped': extra = 'причина: %s' % x.get('reason', '')
        if st == 'merged' and x.get('files_ok') is None: extra += '  (сверка не завершена — выполни verify %s)' % i['id']
        if st == 'merged' and x.get('synced') is False: extra += '  (инстанс не применил — выполни verify %s)' % i['id']
        ok_app, why_na = applicable(i)
        if not ok_app and not st:
            print('%s  %-30s %-10s %s' % (i['id'], i['slug'], 'не нужно', why_na)); continue
        print('%s  %-30s %-10s %s' % (i['id'], i['slug'], names.get(st, 'не начат'), extra))


def cmd_confirmations(_):
    for x in c.wizard().get('confirmations', []):
        print('[%s] %s ← «%s»' % (x['ts'][:16].replace('T', ' '), x['action'], x['text']))


def cmd_preview_branch(a):
    pv = preview(a.branch)
    print('Предпросмотр ветки %s: %s' % (a.branch, 'прошёл' if pv.get('ok') else 'НЕ ПРОШЁЛ: %s' % pv.get('error')))
    print('  коммит %s; изменится на инстансе: %s' % ((pv.get('sha') or '')[:8], ', '.join('%s %s' % (x.get('kind'), x.get('name')) for x in pv.get('items') or []) or 'ничего'))
    print('  сводка: %s' % json.dumps(pv.get('changes'), ensure_ascii=False))
    sys.exit(0 if pv.get('ok') else 1)


if __name__ == '__main__':
    ap = argparse.ArgumentParser(); sub = ap.add_subparsers(dest='cmd', required=True)
    for n in ('plan', 'status', 'confirmations'):
        sub.add_parser(n)
    sub.add_parser('allow-open').add_argument('--confirmed-by', default='')
    sub.add_parser('preview-branch').add_argument('branch')
    for name in ('diff', 'open', 'verify', 'merge', 'skip'):
        p = sub.add_parser(name); p.add_argument('id', choices=sorted(ITEMS))
        if name == 'open': p.add_argument('--replace-confirmed-by', default='')
        if name == 'merge': p.add_argument('--confirmed-by', default='')
        if name == 'skip': p.add_argument('--reason', default='')
    a = ap.parse_args()
    {'plan': cmd_plan, 'allow-open': cmd_allow_open, 'diff': cmd_diff, 'open': cmd_open, 'verify': cmd_verify, 'merge': cmd_merge,
     'skip': cmd_skip, 'status': cmd_status, 'confirmations': cmd_confirmations, 'preview-branch': cmd_preview_branch}[a.cmd](a)
