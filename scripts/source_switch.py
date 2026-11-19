#!/usr/bin/env python3
"""Переключение инстанса Gina на ветку POC (и обратно).

Инстанс применяет методологию из одной ветки одного репозитория — это его «источник промптов».
Пока источник смотрит на прежнюю ветку, всё, что влито в ветку POC, на работу агентов не влияет.
Переключение — момент, когда POC начинает действовать: каталог инстанса приводится к содержимому ветки POC,
а агенты и навыки, которых в ветке нет, ВЫКЛЮЧАЮТСЯ.

  python3 scripts/source_switch.py show                                  — что инстанс читает сейчас
  python3 scripts/source_switch.py preview                               — что изменится при переключении (ничего не применяет)
  python3 scripts/source_switch.py switch --confirmed-by "<ответ оператора>"
  python3 scripts/source_switch.py rollback --confirmed-by "<ответ оператора>"   — вернуть прежний источник
"""
import argparse, json, os, sys, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common as c


def current(gi):
    st, ps = gi.call('GET', '/api/prompt-source')
    if st != 200 or not isinstance(ps, dict):
        sys.exit('GET /api/prompt-source → %s: нужен токен владельца инстанса (gina.env)' % st)
    return ps.get('config') or {}, ps.get('status') or {}


def target(cfg):
    s = c.settings()
    if not s.get('poc_branch') or not s.get('methodology_project'):
        sys.exit('в журнале нет ветки POC или репозитория — сначала scripts/branch_setup.py create')
    t = dict(cfg); t.update({'mode': 'git', 'project': s['methodology_project'], 'branch': s['poc_branch']})
    return t


def is_live():
    """True, если инстанс уже читает ветку POC."""
    try:
        cfg, _ = current(c.Gina())
    except SystemExit:
        return False
    s = c.settings()
    return cfg.get('mode') == 'git' and (cfg.get('project') or '').strip('/') == (s.get('methodology_project') or '').strip('/') and cfg.get('branch') == s.get('poc_branch')


def cmd_show(_):
    cfg, st = current(c.Gina()); s = c.settings()
    print('Инстанс читает: режим %s, репозиторий %s, ветка %s' % (cfg.get('mode'), cfg.get('project'), cfg.get('branch')))
    print('Состояние: %s, применён коммит %s, синхронизация %s, ошибка: %s' % (st.get('state'), (st.get('sha') or '')[:8], st.get('synced_at'), st.get('error')))
    print('Ветка POC: %s@%s — %s' % (s.get('methodology_project'), s.get('poc_branch'), 'инстанс УЖЕ читает её' if is_live() else 'инстанс её пока НЕ читает'))


def cmd_preview(_):
    gi = c.Gina(); cfg, _ = current(gi); t = target(cfg)
    st, pv = gi.call('POST', '/api/prompt-source/preview', {'instance_id': t.get('instance_id'), 'project': t['project'], 'branch': t['branch']}, timeout=180)
    if not isinstance(pv, dict) or not pv.get('ok'):
        print('Предпросмотр НЕ прошёл: %s' % (pv.get('error') if isinstance(pv, dict) else 'HTTP %s %s' % (st, str(pv)[:200])))
        print('Переключать нельзя, пока ошибка не устранена.'); sys.exit(1)
    c.save_state('switch_preview.json', {'ts': c.now(), 'sha': pv.get('sha'), 'changes': pv.get('changes'), 'items': pv.get('items'), 'warnings': pv.get('warnings')})
    print('Предпросмотр переключения на %s@%s (коммит %s) прошёл. Ничего не применено.' % (t['project'], t['branch'], (pv.get('sha') or '')[:8]))
    print('Что изменится в каталоге инстанса (создано / обновлено / выключено):')
    for kind, v in (pv.get('changes') or {}).items():
        if isinstance(v, dict) and any(v.values()):
            print('  %-14s создано %s, обновлено %s, выключено %s' % (kind, v.get('created', 0), v.get('updated', 0), v.get('disabled', 0)))
    by = {}
    for x in pv.get('items') or []:
        by.setdefault(x.get('kind'), []).append(x.get('name'))
    for kind, names in by.items():
        print('  затронуто (%s): %s' % (kind, ', '.join(str(n) for n in names)))
    for wmsg in pv.get('warnings') or []:
        print('  предупреждение: %s' % wmsg)
    print('ВАЖНО: «выключено» — это агенты и навыки, которые есть на инстансе сейчас, но отсутствуют в ветке POC. '
          'Покажи оператору этот список целиком до вопроса о переключении.')


def put(gi, cfg):
    body = {k: cfg.get(k) for k in ('mode', 'instance_id', 'project', 'branch', 'interval_seconds') if cfg.get(k) is not None}
    return gi.call('PUT', '/api/prompt-source', body, timeout=120)


def head_of(project, branch):
    """Голова ветки в GitLab; None, если узнать не удалось (другой репозиторий, нет доступа)."""
    try:
        g = c.GitLab(need_project=False)
        st, b = g.call('GET', '/projects/%s/repository/branches/%s' % (c.urllib.parse.quote(project.strip('/'), safe=''), c.urllib.parse.quote(branch, safe='')))
        return b['commit']['id'] if st == 200 else None
    except SystemExit:
        return None


def wait(gi, project, branch, before_sha):
    """Ждёт, пока инстанс применит голову указанной ветки. Возвращает статус или None."""
    head = head_of(project, branch)
    for _ in range(36):
        cfg, st = current(gi)
        applied = st.get('sha')
        done = (applied == head) if head else (applied and applied != before_sha)
        if cfg.get('branch') == branch and done and st.get('state'):
            return st
        time.sleep(float(os.environ.get('GINA_POC_POLL_SECONDS', '5')) * 2)
    return None


def cmd_switch(a):
    if not (a.confirmed_by or '').strip():
        sys.exit('нужен дословный ответ оператора: --confirmed-by "..."')
    pv = c.load_state('switch_preview.json')
    if not pv:
        sys.exit('сначала preview: оператор должен увидеть, что изменится')
    gi = c.Gina(); cfg, st0 = current(gi); t = target(cfg)
    if is_live():
        print('инстанс уже читает ветку POC — переключение не требуется'); return
    c.save_state('source_before.json', {'ts': c.now(), 'config': cfg, 'status': {k: st0.get(k) for k in ('state', 'sha')}})
    c.confirm('переключение источника промптов на %s@%s' % (t['project'], t['branch']), a.confirmed_by)
    st, r = put(gi, t)
    if st >= 300:
        sys.exit('переключение отклонено: HTTP %s %s\nПрежний источник не изменён. Оператор может переключить ветку сам в интерфейсе Gina (/ui/engines).' % (st, json.dumps(r, ensure_ascii=False)[:400]))
    s = wait(gi, t['project'], t['branch'], st0.get('sha'))
    if not s:
        print('Источник переключён, но инстанс за отведённое время не сообщил о применении головы ветки POC. Проверь: python3 scripts/source_switch.py show'); sys.exit(1)
    print('Инстанс читает ветку POC: состояние %s, коммит %s, ошибка: %s' % (s.get('state'), (s.get('sha') or '')[:8], s.get('error')))
    if s.get('state') != 'ok':
        print('Ветка не применена. Верни прежний источник: python3 scripts/source_switch.py rollback --confirmed-by "..."'); sys.exit(1)


def cmd_rollback(a):
    if not (a.confirmed_by or '').strip():
        sys.exit('нужен дословный ответ оператора: --confirmed-by "..."')
    before = c.load_state('source_before.json')
    if not before:
        sys.exit('прежний источник не сохранён (переключение этим скриптом не выполнялось)')
    gi = c.Gina(); _, st_now = current(gi)
    c.confirm('возврат источника промптов на %s@%s' % (before['config'].get('project'), before['config'].get('branch')), a.confirmed_by)
    st, r = put(gi, before['config'])
    if st >= 300:
        sys.exit('возврат отклонён: HTTP %s %s' % (st, json.dumps(r, ensure_ascii=False)[:400]))
    s = wait(gi, before['config'].get('project') or '', before['config'].get('branch'), st_now.get('sha'))
    print('Источник возвращён на %s@%s: %s' % (before['config'].get('project'), before['config'].get('branch'), ('инстанс применил её, состояние %s' % s.get('state')) if s else 'применение не подтверждено за отведённое время — проверь show'))


if __name__ == '__main__':
    ap = argparse.ArgumentParser(); sub = ap.add_subparsers(dest='cmd', required=True)
    sub.add_parser('show'); sub.add_parser('preview')
    for n in ('switch', 'rollback'):
        sub.add_parser(n).add_argument('--confirmed-by', default='')
    a = ap.parse_args()
    {'show': cmd_show, 'preview': cmd_preview, 'switch': cmd_switch, 'rollback': cmd_rollback}[a.cmd](a)
