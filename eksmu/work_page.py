"""Server-rendered public page for one work: readable, linkable and indexable.

Interface text is translated; author content is inserted afterwards and always
escaped (the rendered body comes from the sanitizing Markdown renderer), so a
work is never machine-translated or reinterpreted by the dictionary.
"""
from __future__ import annotations

import json
import re
from html import escape
from urllib.parse import quote

from . import i18n
from .publication_contract import tex
from .research import is_source_archive_filename
from .research_render import excerpt as _excerpt, render, render_abstract
from .store import Problem, identifier

KINDS = {'question': '问题', 'conjecture': '猜想', 'finding': '发现', 'experiment': '实验',
         'computation': '计算', 'failed_attempt': '失败的尝试', 'other': '其他'}
MODERATION_LABELS = {'commercial_promotion': '商业推广', 'pending_review': '待复核'}
OG_LOCALE = {'zh': 'zh_CN', 'en': 'en_US'}


def _attr(value: str) -> str:
    return escape(value, quote=True)


def format_size(size: int) -> str:
    n = int(size)
    if n < 1024:
        return f'{n} B'
    if n < 1024 * 1024:
        return f'{n / 1024:.1f} KiB'
    if n < 1024 * 1024 * 1024:
        return f'{n / (1024 * 1024):.1f} MiB'
    return f'{n / (1024 * 1024 * 1024):.1f} GiB'


def format_size_title(size: int, T) -> str:
    return T('{size} 字节', size=f'{int(size):,}')



def load(database: str, rows: list[dict], profiles: dict, exploration_id: str, revision_id: str | None,
         chain_db=None, *, viewer_id: str | None = None, now=None) -> dict:
    """Collect everything the page shows; raise Problem(404) for unknown links."""
    identifier(exploration_id)
    from pathlib import Path as _P
    from .community import (
        Community,
        _get_derived_index,
        distribution,
        load_active_labels,
        load_author_label_status,
        load_trusted_authors,
    )
    derived = _get_derived_index(rows)
    work = derived.works.get(exploration_id)
    if work is None:
        raise Problem('找不到这项作品', 404)
    versions = list(derived.versions.get(exploration_id, ()))
    if not versions:
        raise Problem('找不到这项作品', 404)
    if revision_id is None:
        revision = versions[-1]
    else:
        identifier(revision_id)
        revision = next((r for r in versions if r['id'] == revision_id), None)
        if revision is None:
            raise Problem('找不到这项作品', 404)
    comments = list(derived.comments_by_rev.get(revision['id'], ()))
    evidence = derived.evidence_by_rev.get(revision['id'], 0)
    details = {'topics': [], 'help': ''}
    materials: list[dict] = []
    status = {'withdrawn': False, 'events': []}
    actor_rec = derived.index.get(work['actor_id'])
    work_rows = [actor_rec, work, *versions] if actor_rec is not None else rows
    # Optional side stores: a missing or older store must not hide the text.
    try:
        if _P(database + '.community.sqlite').is_file():
            hub = Community(database + '.community.sqlite', rows, profiles)
            try:
                details = hub.details(exploration_id, actor_id=viewer_id)
            finally:
                hub.close()
    except Exception:
        pass
    try:
        if _P(database + '.research.sqlite').is_file():
            from .research import Research
            res = Research(database + '.research.sqlite', work_rows)
            try:
                materials = res.files(revision['id'])['items']
            finally:
                res.close()
    except Exception:
        pass
    try:
        if _P(database + '.publications.sqlite').is_file():
            from .publications import Publications
            pub = Publications(database + '.publications.sqlite', work_rows)
            try:
                status = pub.status(exploration_id)
            finally:
                pub.close()
    except Exception:
        pass
    analysis = None
    try:
        if _P(database + '.analysis.sqlite').is_file():
            from .analysis import Analysis
            store = Analysis(database + '.analysis.sqlite', work_rows)
            try:
                analysis = store.view(revision['id'])
            finally:
                store.close()
    except Exception:
        pass
    chain = None
    if chain_db is not None:
        try:
            from .record_chain import summary
            chain = summary(chain_db, revision['id'])
            if materials:
                from .material_chain import materials_status
                by_aid = materials_status(chain_db, rows, revision['id'])
                materials = [dict(m, chain=by_aid[m['id']]) if m.get('id') in by_aid else m for m in materials]
                if chain is not None:
                    chain = dict(
                        chain,
                        materials_total=len(materials),
                        materials_committed=sum(1 for m in materials if m.get('chain')),
                        materials_anchored=sum(
                            1 for m in materials
                            if (m.get('chain') or {}).get('anchor')
                            and m['chain']['anchor'].get('state') == 'confirmed'
                        ),
                        materials_backfilled=any(
                            (m.get('chain') or {}).get('backfilled') for m in materials if m.get('chain')
                        ),
                    )
        except Exception:
            pass
    active_labels = load_active_labels(database)
    trusted_authors = load_trusted_authors(database)
    withdrawn_set = _withdrawn_ids(database, rows)
    dist = distribution(
        exploration_id,
        derived=derived,
        active_labels=active_labels,
        trusted_authors=trusted_authors,
        withdrawn=bool(status.get('withdrawn')),
        withdrawn_ids=withdrawn_set,
        now=now,
    )
    author_label_status = None
    if viewer_id is not None and viewer_id == work['actor_id']:
        try:
            author_label_status = load_author_label_status(database, exploration_id, viewer_id)
        except Exception:
            author_label_status = None
    return {'work': work, 'work_number': derived.work_numbers.get(exploration_id, 1),
            'versions': versions, 'revision': revision, 'comments': comments,
            'evidence': evidence, 'details': details, 'materials': materials, 'status': status,
            'profiles': profiles, 'latest': revision is versions[-1], 'chain': chain, 'analysis': analysis,
            'distribution': dist, 'author_label_status': author_label_status}


def _time_evidence(chain, work_id: str, revision_id: str, lang: str, origin: str, T) -> str:
    """What the record chain and its anchors can say about this version's time."""
    if not chain:
        return ''
    seq, anchor = chain['seq'], chain['anchor']
    if anchor is None:
        state = T('本版正文是本站记录链的第 {seq} 条记录（记录链按顺序收录账户、作品、版本与讨论等所有公开记录）。每日时间锚定后，可用比特币区块独立核验它不晚于何时存在。', seq=seq)
    elif anchor['state'] != 'confirmed':
        state = T('本版正文是本站记录链的第 {seq} 条记录（记录链按顺序收录账户、作品、版本与讨论等所有公开记录），已于 {date} 提交 OpenTimestamps，正在等待比特币确认（通常需要数小时）。',
                  seq=seq, date=anchor['submitted_at'][:16].replace('T', ' ') + ' UTC')
    else:
        checked = anchor.get('checked') or {}
        when = checked.get('block_time')
        state = (T('本版正文是本站记录链的第 {seq} 条记录（记录链按顺序收录账户、作品、版本与讨论等所有公开记录），已锚定到比特币区块 #{height}（区块时间 {time}）：这证明本版内容不晚于该时间已经存在。',
                   seq=seq, height=anchor['bitcoin']['height'], time=when[:16].replace('T', ' ') + ' UTC')
                 if when else T('本版正文是本站记录链的第 {seq} 条记录（记录链按顺序收录账户、作品、版本与讨论等所有公开记录），已锚定到比特币区块 #{height}：这证明本版内容不晚于该区块形成时已经存在。',
                                seq=seq, height=anchor['bitcoin']['height']))
        state += ' ' + (T('已与 {n} 个公开区块浏览器核对；最严格的核验请用自己的比特币节点。', n=checked['matched'])
                        if checked.get('matched') else T('尚未与公开区块浏览器核对。'))
    links = [f'<a href="/api/chain/proof?record={_attr(revision_id)}">{escape(T("记录链证明（JSON）"))}</a>']
    if anchor:
        links += [f'<a href="{_attr(anchor["proof_url"])}">{escape(T("OpenTimestamps 证明"))}</a>',
                  f'<a href="{_attr(anchor["stamped_file_url"])}">{escape(T("被锚定的链头文件"))}</a>']
    links.append(f'<a href="/{lang}/openness.html">{escape(T("开放保障与离线核验"))}</a>')
    badge_md = f'[![Eksmu Credit]({origin}/api/badge/{work_id}.svg)]({origin}/w/{work_id})'
    cert_actions = (f'<div class="work-actions cert-actions">'
                    f'<a class="btn sm quiet secondary button" href="/{lang}/w/{_attr(work_id)}/card.svg" download="eksmu-credit-{_attr(work_id[:8])}.svg">'
                    f'{escape(T("下载 Credit 存证证书 (SVG)"))}</a>'
                    f'<button type="button" id="copy-credit-badge" class="btn sm quiet secondary" data-badge="{_attr(badge_md)}">'
                    f'{escape(T("复制 Markdown 存证徽章"))}</button></div>'
                    f'<p id="credit-badge-state" class="muted" role="status" hidden></p>')
    committed = int(chain.get('materials_committed') or 0)
    if committed > 0:
        total = int(chain.get('materials_total') or committed)
        anchored = int(chain.get('materials_anchored') or 0)
        if committed == total and anchored == total:
            if chain.get('materials_backfilled'):
                coverage_note = T('本版全部 {n} 份附件的内容摘要已补链入记录链并获比特币区块锚定（补链附件的时间上限以覆盖该附件链位置的锚点为准，详见下方“本版材料”）。', n=total)
            else:
                coverage_note = T('本版全部 {n} 份附件的内容摘要已写入记录链并获比特币区块锚定（详见下方“本版材料”）。', n=total)
        else:
            coverage_note = T('本版 {committed}/{total} 份附件的内容摘要已写入记录链（其中 {anchored} 份已获比特币区块锚定；详见下方“本版材料”）。',
                              committed=committed, total=total, anchored=anchored)
    else:
        coverage_note = T('当前时间证据覆盖本版记录，不覆盖 PDF、源码等附件。')
    return (f'<section class="hp side-panel" id="work-time"><header class="hp-head"><span class="hp-code mono">TIME</span><h2 class="hp-title">{escape(T("时间证据"))}</h2></header>'
            f'<div class="hp-body"><p>{escape(state)}</p>'
            f'<p class="muted">{escape(coverage_note)}</p>'
            f'<p class="muted faint">{escape(T("发表日期来自本站时钟；比特币锚定给出不依赖本站的时间上限。时间证据不证明原创性或正确性。"))}</p>'
            f'{cert_actions}'
            f'<p class="footer-links">{" ".join(links)}</p></div></section>')


SEVERITY = {'blocking': '若成立则主要结论不成立', 'major': '重要', 'minor': '次要', 'note': '建议'}
CATEGORY = {'derivation_gap': '推导缺口', 'unsupported_claim': '缺少支持的论断', 'internal_inconsistency': '前后不一致',
            'missing_definition': '缺少定义', 'known_result': '已有结果', 'citation_issue': '引用问题', 'reproduction': '复现',
            'formal_check': '形式化检查', 'clarity': '表述不清', 'other': '其他'}
STANCE = {'accept': '接受', 'dispute': '反驳', 'revised': '已在新版本修改', 'unclear': '不清楚'}
# Fixed scope notes the system writes into every report, shown in the page language.
SYSTEM_NOTES = {'attachments, data files and code were not read': '未读取附件、数据文件和代码',
                'data files and code were not executed': '未运行数据文件和代码',
                'PDF attachments were not read (no text extraction)': 'PDF 附件未读取（暂不提取 PDF 文字）',
                'title and full body of this revision; no attachments': '本版标题与全文，不含附件'}


def _analysis_read(report, T) -> str:
    """What the model read, from the report's own record (older reports only have a fixed sentence)."""
    inputs = report['scope'].get('inputs')
    if not inputs:
        return T(SYSTEM_NOTES.get(report['scope']['read'], report['scope']['read']))
    items = []
    for i in inputs:
        cut = T('，已截断') if i['truncated'] else ''
        if i['kind'] == 'body':
            items.append(T('本版标题、摘要与正文') + cut)
        elif i['kind'] == 'source' and i['read']:
            items.append(T('LaTeX 源码包 {name}（{n} 字符{cut}）', name=i['filename'], n=f"{i['chars']:,}", cut=cut))
        elif i['kind'] == 'html' and i['read']:
            items.append(T('本站编译的 HTML 版 {name}（{n} 字符{cut}）', name=i['filename'], n=f"{i['chars']:,}", cut=cut))
        elif not i['read']:
            items.append(T('{name} 未读取', name=i['filename']))
    return '；'.join(items)
ASSESSMENT = {'supported': '论证充分', 'partially_supported': '部分成立，仍有缺口', 'not_established': '尚未证成', 'not_assessed': '未评估'}
STATE = {'queued': '已排队，通常 1–2 分钟内完成；本页会自动刷新。', 'running': '分析中，通常 1–2 分钟；本页会自动刷新。',
         'failed': '最近一次分析未能完成：{detail}', 'declined': '模型拒绝分析这份稿件：{detail}'}


def _analysis(view, profiles: dict, versions: list[dict], work_id: str, workspace_url: str, lang: str, T) -> str:
    """Public AI analysis on the work page, with responses and in-place reply forms."""
    if view is None:
        return ''
    req, reports = view.get('request'), view.get('reports', [])
    state = req['state'] if req else 'none'
    parts = [f'<section class="work-section hp" id="work-analysis" data-analysis-state="{_attr(state)}">',
             f'<header class="hp-head"><span class="hp-code mono">AI</span><h2 class="hp-title">{escape(T("AI 公开分析"))}</h2>',
             f'<span class="badge">{len(reports)}</span></header>',
             f'<div class="hp-body"><p class="muted">{escape(T("AI 只列出可定位的具体问题，不打分，不影响发表或排序。作者和任何人都可以逐条接受或反驳。"))}</p>']
    if req and req['state'] in STATE:
        message = STATE[req['state']]
        if req['state'] == 'queued' and not (view.get('service') or {}).get('active'):
            message = '已排队；分析服务目前没有在运行，恢复后会自动处理。本页会自动刷新。'
        parts.append(f'<p class="notice" role="status">{escape(T(message, detail=req.get("detail") or ""))}</p>')
    elif not reports:
        parts.append(f'<p class="muted">{escape(T("还没有 AI 分析。作者可以请求一次免费分析。"))}</p>')
    by_finding: dict[tuple[str, str], list[dict]] = {}
    for r in view.get('responses', []):
        by_finding.setdefault((r['report_id'], r['finding_id']), []).append(r)
    checks = view.get('quote_checks', {})
    options = ''.join(f'<option value="{k}">{escape(T(v))}</option>' for k, v in STANCE.items())
    ver_num = {v['id']: idx for idx, v in enumerate(versions, 1)}
    rev_options = (f'<option value="">{escape(T("不关联特定版本"))}</option>' + ''.join(
        f'<option value="{_attr(v["id"])}">{escape(T("版本 {n} · {time}", n=idx, time=v["created_at"][:10]))}</option>'
        for idx, v in enumerate(versions, 1)))
    rendered_reports = []
    for report in reports:
        start = len(parts)
        paid = report['analyzer']['cost_borne_by'] != 'Eksmu operator'
        label = T('作者自费的深入分析（同一评判标准）') if paid else T('平台提供的基础分析')
        parts.append(f'<article class="card hp analysis-report"><header class="hp-head">'
                     f'<span class="hp-code mono">REPORT</span>'
                     f'<h3 class="hp-title">{escape(label)} <span class="muted">· {escape(report["analyzer"]["name"])}'
                     f' · {escape(report["created_at"][:10])}</span></h3></header><div class="hp-body">')
        parts.append(f'<p class="muted analysis-read">{escape(T("本次分析读取了：{items}", items=_analysis_read(report, T)))}</p>')
        if report.get('suspicious_instructions'):
            parts.append(f'<p class="notice">{escape(T("注意：正文中含有试图指挥 AI 的文字，模型已忽略并标记。"))}</p>')

        def heading(title):
            return f'<h4>{escape(T(title))}</h4>'

        def bullets(items):
            return '<ul>' + ''.join(f'<li>{escape(x)}</li>' for x in items) + '</ul>'

        def finding(f):
            ok = checks.get(report['id'], {}).get(f['id'], f['quote_verified'])
            quote = ''
            if f['location']['quote']:
                quote_status = (
                    f'<span class="badge ok quote-status">{escape(T("引文已核对"))}</span>'
                    if ok else
                    f'<span class="badge danger quote-status">{escape(T("正文中找不到这段引文，请谨慎对待这条发现。"))}</span>'
                )
                quote = f'<blockquote>{escape(f["location"]["quote"])}</blockquote>{quote_status}'
            items = by_finding.get((report['id'], f['id']), [])
            n = len(items)
            replies = f' <span class="muted">· {escape(T("{n} 条回应", n=n))}</span>' if n else ''
            where = f' <span class="muted">· {escape(f["location"]["section"])}</span>' if f['location']['section'] else ''
            resp_html = []
            for r in items:
                who = profiles.get(r['actor_id'], {}).get('display_name') or r['actor_id'][:8]
                role = escape(T('（作者）')) if r.get('is_author') else ''
                stance = escape(T(STANCE.get(r['stance'], r['stance'])))
                ref_id = r.get('revision_ref')
                ref_link = ''
                if ref_id and ref_id in ver_num:
                    href = f"/{lang}/w/{work_id}" if ref_id == versions[-1]['id'] else f"/{lang}/w/{work_id}?revision={ref_id}"
                    ref_link = f' · <a href="{_attr(href)}">{escape(T("关联版本 {n}", n=ver_num[ref_id]))}</a>'
                resp_html.append(
                    f'<div class="finding-response"><p class="muted"><strong>{escape(who)}</strong>{role}'
                    f' · <span class="badge">{stance}</span>{ref_link} · {escape(r["created_at"][:10])}</p>'
                    f'<p class="comment-body">{escape(r["body"])}</p></div>'
                )
            form = (f'<details class="finding-reply" hidden><summary>{escape(T("回应这条发现"))}</summary>'
                    f'<form method="post" class="finding-response-form" data-report="{_attr(report["id"])}" data-finding="{_attr(f["id"])}">'
                    f'<label>{escape(T("立场"))}<select name="stance">{options}</select></label>'
                    f'<label>{escape(T("关联版本（可选）"))}<select name="revision_ref">{rev_options}</select></label>'
                    f'<label>{escape(T("回应说明"))}<textarea name="body" rows="3" maxlength="4000" required '
                    f'placeholder="{_attr(T("说明理由；反驳 AI 和接受 AI 同样欢迎。"))}"></textarea></label>'
                    f'<button type="submit">{escape(T("提交回应"))}</button>'
                    f'<p class="muted finding-reply-state" role="status"></p></form></details>')
            sev = f["severity"]
            sev_class = f'finding-sev-{sev}'
            return (f'<li class="finding-item {sev_class}"><p class="finding-header"><strong class="mono">{escape(f["id"])}</strong> '
                    f'<span class="badge sev-badge">{escape(T(SEVERITY[sev]))}</span> <strong>{escape(T(CATEGORY[f["category"]]))}</strong>'
                    f'{where}{replies}</p>{quote}<p class="finding-statement">{escape(f["statement"])}</p>{"".join(resp_html)}{form}</li>')

        v2 = report['format'] == 'eksmu.analysis-report.v2'
        if v2 and report['summary']:
            parts.append(heading('概要') + f'<p>{escape(report["summary"])}</p>')
        if v2 and report['claims']:
            rows = ''.join(
                f'<li class="claim-item claim-{_attr(c["assessment"])}"><p class="claim-head"><strong class="mono">{escape(c["id"])}</strong> '
                f'<span class="badge claim-chip">{escape(T(ASSESSMENT[c["assessment"]]))}</span>'
                + (f' <span class="muted">· {escape(c["section"])}</span>' if c['section'] else '')
                + f'</p><p class="claim-text">{escape(c["claim"])}</p><p class="claim-reasoning">{escape(c["reasoning"])}</p></li>' for c in report['claims'])
            parts.append(heading('主要论断评估') + f'<p class="muted">{escape(T("这是模型对“正文是否已经证成”的意见，不是平台结论。"))}</p>'
                         f'<ol class="analysis-findings">{rows}</ol>')
        if v2 and report['strengths']:
            parts.append(heading('优点') + bullets(report['strengths']))
        major = [f for f in report['findings'] if f['severity'] in ('blocking', 'major')]
        minor = [f for f in report['findings'] if f['severity'] not in ('blocking', 'major')]
        if major:
            parts.append(heading('主要问题') + f'<ol class="analysis-findings">{"".join(finding(f) for f in major)}</ol>')
        if minor:
            parts.append(heading('次要问题') + f'<ol class="analysis-findings">{"".join(finding(f) for f in minor)}</ol>')
        if not report['findings']:
            parts.append(f'<p class="muted">{escape(T("模型没有列出具体问题。这不等于正文已被证明正确。"))}</p>')
        if v2 and report['questions']:
            parts.append(heading('给作者的问题') + bullets(report['questions']))
        if v2 and report['suggestions']:
            parts.append(heading('改进建议') + bullets(report['suggestions']))
        if v2 and report['overall']:
            parts.append(heading('总体意见') + f'<p>{escape(report["overall"])}</p>')
        if report['scope']['not_checked']:
            items = [T(SYSTEM_NOTES.get(x, x)) for x in report['scope']['not_checked']]
            parts.append(f'<p class="muted">{escape(T("未检查：{items}", items="；".join(items)))}</p>')
        parts.append('</div></article>')
        rendered_reports.append(''.join(parts[start:])); del parts[start:]
    if rendered_reports:
        # Newest first and open; earlier reports stay available but folded.
        parts.append(rendered_reports[-1])
        if len(rendered_reports) > 1:
            parts.append(f'<details class="older-reports"><summary>{escape(T("早先的分析（{n} 份）", n=len(rendered_reports) - 1))}</summary>'
                         + ''.join(reversed(rendered_reports[:-1])) + '</details>')
    if reports:
        parts.append(f'<p id="work-analysis-guest" class="muted" hidden><a href="/{lang}/account">{escape(T("登录后逐条回应这些发现"))}</a>'
                     f' · <span>{escape(T("注册不需要任何学术资格。"))}</span></p>')
        parts.append(f'<p><a class="button secondary" href="{_attr(workspace_url + "&section=analysis")}">{escape(T("逐条回应这些发现"))}</a></p>')
    parts.append('</div></section>')
    return ''.join(parts)


def _comments(items: list[dict], profiles: dict, T) -> str:
    """Render discussion comments as a reply tree (max 4 visual indent levels)."""
    if not items:
        return f'<p class="muted">{escape(T("还没有回应。可以从一句问题开始。"))}</p>'
    ordered = [c for _, c in sorted(enumerate(items), key=lambda pair: (pair[1].get('created_at') or '', pair[0]))]
    by_id = {c['id']: c for c in ordered}
    children: dict[str | None, list[dict]] = {None: []}
    parent_of: dict[str, dict | None] = {}
    for c in ordered:
        cid = c['id']
        pid = c['payload'].get('reply_to')
        seen = {cid}
        cur = pid
        while cur in by_id and cur not in seen:
            seen.add(cur)
            cur = by_id[cur]['payload'].get('reply_to')
        if pid not in by_id or cur in seen:
            pid = None
        parent_of[cid] = by_id[pid] if pid else None
        children.setdefault(pid, []).append(c)

    def who_of(c: dict) -> str:
        return profiles.get(c['actor_id'], {}).get('display_name') or c['actor_id'][:8]

    def render_node(c: dict, depth: int) -> str:
        cid = c['id']
        who = who_of(c)
        indent = min(depth, 4)
        parent = parent_of[cid]
        reply_info = ''
        if parent is not None:
            p_who = who_of(parent)
            reply_info = (f' <span class="comment-reply-meta muted"><span aria-hidden="true">↳ </span>'
                          f'<a class="comment-parent-link" href="#c-{_attr(parent["id"])}">'
                          f'{escape(T("回复 {who}", who=p_who))}</a></span>')
        time_str = escape(c['created_at'][:10])
        card = (f'<article class="card comment-card" id="c-{_attr(cid)}" data-comment-id="{_attr(cid)}" data-depth="{indent}">'
                f'<div class="comment-bar"><h3 class="comment-head">{escape(who)}{reply_info} '
                f'<span class="muted mono">· <a class="comment-time" href="#c-{_attr(cid)}">{time_str}</a></span></h3>'
                f'<button type="button" class="btn sm quiet secondary comment-reply-btn" data-comment-id="{_attr(cid)}" '
                f'data-comment-author="{_attr(who)}" hidden>{escape(T("回复"))}</button></div>'
                f'<p class="comment-body">{escape(c["payload"]["body"])}</p></article>')
        sub = ''
        kids = children.get(cid, [])
        if kids:
            flat = ' comment-children-flat' if depth >= 4 else ''
            sub = f'<ol class="comment-children{flat}">' + ''.join(render_node(k, depth + 1) for k in kids) + '</ol>'
        return f'<li class="comment-node" data-depth="{indent}">{card}{sub}</li>'

    return f'<ol class="comment-tree">{"".join(render_node(c, 0) for c in children[None])}</ol>'


def _moderation_and_appeal(work_id: str, dist: dict, author_status: dict | None, T) -> str:
    lbl = dist.get('label')
    parts = []
    if lbl is not None:
        if lbl['label'] == 'pending_review' and lbl.get('source') == 'ai':
            banner = T(
                '系统初筛认为此作品可能以推广为主要目的，正在等待人工复核；复核前不进入首页与搜索引擎收录。作品页、版本与时间证据不受影响。理由：{reason}',
                reason=lbl['reason'],
            )
        else:
            label_name = T(MODERATION_LABELS.get(lbl['label'], lbl['label']))
            banner = T(
                '此作品被标记为：{label}。不进入首页、站点地图与搜索引擎收录；作品页、版本与时间证据保留。理由：{reason}',
                label=label_name,
                reason=lbl['reason'],
            )
        parts.append(f'<div id="work-moderation-notice" class="notice moderation-notice" role="status"><p>{escape(banner)}</p></div>')
    label_id_attr = _attr(lbl['id']) if lbl else ''
    if author_status is not None and (author_status.get('can_appeal') or author_status.get('appeal')):
        inner = [f'<h3>{escape(T("作者申诉"))}</h3>']
        if author_status.get('can_appeal') and lbl is not None:
            inner.append(f'<p class="muted">{escape(T("如果你认为此标记有误，可以提交一次申诉（不超过 2000 字）。申诉内容私密，仅作者本人与离线维护者可见。"))}</p>')
            inner.append(
                f'<form method="post" id="work-label-appeal-form" data-label-id="{label_id_attr}" data-exploration="{_attr(work_id)}">'
                f'<label>{escape(T("申诉说明（最多 2000 字）"))}'
                f'<textarea name="body" rows="4" maxlength="2000" required '
                f'placeholder="{_attr(T("请说明这篇作品并非商业推广或需要复核的具体理由。"))}"></textarea></label>'
                f'<button type="submit">{escape(T("提交申诉"))}</button>'
                f'<p id="work-label-appeal-state" class="muted" role="status"></p></form>'
            )
        elif author_status.get('appeal'):
            ap = author_status['appeal']
            if ap['state'] == 'pending':
                status_line = T('申诉状态：已提交，待维护者离线复核。')
            elif ap['decision'] == 'retract':
                status_line = T('申诉处理结果：已撤回标记，作品已恢复正常分发。')
            else:
                status_line = T('申诉处理结果：维持原标记。')
            inner.append(f'<p><strong>{escape(status_line)}</strong></p>')
            if ap.get('decision_reason'):
                inner.append(f'<p>{escape(T("复核说明：{reason}", reason=ap["decision_reason"]))}</p>')
            inner.append(f'<p class="muted">{escape(T("你的申诉内容：{body}", body=ap["body"]))}</p>')
        parts.append(
            f'<div id="work-label-appeal" class="notice author-appeal-box" '
            f'data-exploration="{_attr(work_id)}" data-label-id="{label_id_attr}">{"".join(inner)}</div>'
        )
    else:
        parts.append(
            f'<div id="work-label-appeal" class="notice author-appeal-box" '
            f'data-exploration="{_attr(work_id)}" data-label-id="{label_id_attr}" hidden></div>'
        )
    return ''.join(parts)


def page(data: dict, lang: str, origin: str, *, now=None) -> bytes:
    T = lambda message, **params: i18n.t(lang, message, **params)
    work, revision, versions, profiles = data['work'], data['revision'], data['versions'], data['profiles']
    if 'distribution' in data and now is None:
        dist = data['distribution']
    else:
        from .community import distribution
        active_lbl = data.get('details', {}).get('label')
        dist = distribution(
            work['id'],
            rows=[work, *versions],
            active_labels={work['id']: active_lbl} if active_lbl else {},
            withdrawn=bool(data['status'].get('withdrawn')),
            now=now,
        )
    payload = revision['payload']
    number = versions.index(revision) + 1
    author = profiles.get(work['actor_id'], {}).get('display_name') or work['actor_id'][:8]
    affiliation = profiles.get(work['actor_id'], {}).get('affiliation') or ''
    first_date = versions[0]['created_at'][:10]
    version_date = revision['created_at'][:10]
    paper = payload.get('paper') or None
    title = payload.get('title') or T('未命名作品')
    description = _excerpt((paper or {}).get('abstract') or payload['body'])
    stable = f"w/{work['id']}"
    here = stable if data['latest'] else f"{stable}?revision={revision['id']}"
    canonical = f"{origin}/{lang}/{here}"
    workspace = f"/{lang}/workspace#exploration={work['id']}&revision={revision['id']}"

    work_number = data.get('work_number', 1)
    author_href = f"/{lang}/u/{work['actor_id']}"
    author_readout = f'<a class="author-link" href="{_attr(author_href)}">{escape(author)}</a>'
    if affiliation:
        author_readout += f' <span class="muted faint">（{escape(affiliation)} · {escape(T("作者自述"))}）</span>'
    version_label = escape(T('版本 {n}/{total}', n=number, total=len(versions)))
    work_number_label = escape(T('本站第 {k} 篇作品', k=work_number))
    version_date_readout = ''
    if number > 1 or not data['latest']:
        version_date_readout = (
            f'<div class="readout"><span class="rl">{escape(T("本版日期"))}</span>'
            f'<span class="rv mono">{escape(version_date)}</span></div>'
        )

    byline = f'<a class="author-link" href="{_attr(author_href)}">{escape(author)}</a>'
    if affiliation:
        byline += escape(T(' · {affiliation}（作者自述）', affiliation=affiliation))
    byline += ' · ' + escape(T('本站第 {k} 篇作品', k=work_number))
    byline += escape(T(' · 首次发表 {date}', date=first_date))
    if number > 1 or not data['latest']:
        byline += escape(T(' · 本版 {date}', date=version_date))

    moderation_notice = _moderation_and_appeal(work['id'], dist, data.get('author_label_status'), T)

    withdrawn = ''
    if data['status'].get('withdrawn'):
        last = next((e for e in reversed(data['status']['events']) if e.get('kind') == 'withdraw'), None)
        note = f"<br>{escape(last['body'])}" if last and last.get('body') else ''
        withdrawn = f'<p class="notice" role="status">{escape(T("作者已撤回这项作品。正文、旧版本与讨论按规则保留，供核对记录。"))}{note}</p>'

    topics = ''
    if data['details'].get('topics'):
        topics = '<div class="topic-chips">' + ''.join(
            f'<span class="chip"># {escape(topic)}</span>' for topic in data['details']['topics']) + '</div>'
    help_note = ''
    if data['details'].get('help'):
        help_note = f'<p class="collaboration-note">{escape(T("一起推进：{help}", help=data["details"]["help"]))}</p>'

    version_items = []
    for index, version in enumerate(versions, 1):
        label = escape(T('版本 {n} · {time}', n=index, time=version['created_at'][:10]))
        href = f"/{lang}/{stable}" if version is versions[-1] else f"/{lang}/{stable}?revision={version['id']}"
        version_items.append(
            f'<li aria-current="page" class="version-item current"><span class="version-label">{label}</span> <span class="badge ok">{escape(T("当前"))}</span></li>'
            if version is revision
            else f'<li class="version-item"><a class="version-link" href="{_attr(href)}">{label}</a></li>'
        )
    versions_section = (
        f'<section class="hp side-panel" id="work-versions">'
        f'<header class="hp-head"><span class="hp-code mono">REV</span><h2 class="hp-title">{escape(T("版本历史"))}</h2>'
        f'<span class="badge">{len(versions)}</span></header>'
        f'<div class="hp-body"><ol class="version-list">{"".join(version_items)}</ol></div></section>'
    )
    versions_earlier_notice = ''
    if not data['latest']:
        versions_earlier_notice = (
            f'<p class="notice version-notice">{escape(T("你正在阅读较早的版本 {n}。", n=number))} '
            f'<a href="/{lang}/{stable}">{escape(T("阅读最新版本"))}</a></p>'
        )
    version_list = versions_earlier_notice + versions_section

    paper_top = paper_bottom = ''
    if paper:
        if paper.get('abstract'):
            abs_rendered = render_abstract(paper['abstract'])
            paper_top += f'<section class="work-abstract" id="work-abstract"><h2>{escape(T("摘要"))}</h2>{abs_rendered["html"]}</section>'
        if paper.get('authors'):
            people = ''.join(
                f'<li>{escape(a["name"])}' + (f' <span class="muted">— {escape(a["contribution"])}</span>' if a.get('contribution') else '') + '</li>'
                for a in paper['authors'])
            paper_top += (f'<section class="work-authors"><h2>{escape(T("署名作者"))}</h2><ul>{people}</ul>'
                          f'<p class="muted">{escape(T("署名由作者声明，本站不核验。"))}</p></section>')
        if paper.get('references'):
            refs = ''.join(f'<li>{escape(r)}</li>' for r in paper['references'])
            paper_bottom += f'<section class="work-section" id="work-references"><h2>{escape(T("参考文献"))}</h2><ol class="references">{refs}</ol></section>'
        extra = []
        if paper.get('license'):
            extra.append(f'<p><strong>{escape(T("许可"))}</strong> {escape(paper["license"])}</p>')
        source = (paper.get('provenance') or {}).get('source')
        if source:
            date = paper['provenance'].get('prior_publication_date')
            extra.append(f'<p><strong>{escape(T("既往发表"))}</strong> {escape(source)}'
                         + (f' · {escape(date)}' if date else '') + f' <span class="muted">{escape(T("（作者声明）"))}</span></p>')
        if extra:
            paper_bottom += '<section class="work-section">' + ''.join(extra) + '</section>'

    rendered = render(payload['body'])
    notice = rendered.get('notice') or ''

    materials = ''
    if data['materials']:
        from .research import is_source_archive_filename
        compiled_html_by_src: dict[str, dict] = {}
        has_compiled_html = False
        for m in data['materials']:
            if (
                not m.get('redacted')
                and m.get('origin') == 'compiled'
                and m.get('mime') == 'text/html'
                and (m.get('filename') or '').lower().endswith('.html')
            ):
                has_compiled_html = True
                if m.get('derived_from_sha256'):
                    compiled_html_by_src[str(m['derived_from_sha256'])] = m
                src_aid = (m.get('compile_info') or {}).get('source_attachment_id')
                if src_aid:
                    compiled_html_by_src[f'id:{src_aid}'] = m
        rows_html = []
        for m in data['materials']:
            fname = m.get('filename', '')
            is_pdf = fname.lower().endswith('.pdf') and m.get('mime') == 'application/pdf'
            is_compiled_html = (
                m.get('origin') == 'compiled'
                and fname.lower().endswith('.html')
                and m.get('mime') == 'text/html'
            )
            pdf_link = (f' · <a href="/w/{_attr(work["id"])}/pdf/{quote(m["id"])}">{escape(T("在线阅读 PDF"))}</a>'
                        if is_pdf else '')
            sibling_html_link = ''
            if is_pdf and m.get('origin') == 'compiled':
                ci_pdf = m.get('compile_info') or {}
                sibling_html = (
                    compiled_html_by_src.get(str(m.get('derived_from_sha256') or ''))
                    or compiled_html_by_src.get(f"id:{ci_pdf.get('source_attachment_id') or ''}")
                )
                if sibling_html is not None:
                    sibling_html_link = (
                        f' · <a href="/w/{_attr(work["id"])}/html/{quote(sibling_html["id"])}">'
                        f'{escape(T("HTML 版"))}</a>'
                    )
            html_view_link = (
                f' · <a href="/w/{_attr(work["id"])}/html/{quote(m["id"])}">{escape(T("HTML 版"))}</a>'
                if is_compiled_html else ''
            )
            extra_info = ''
            if m.get('origin') == 'compiled':
                ci = m.get('compile_info') or {}
                src_id = ci.get('source_attachment_id')
                src_name = ci.get('source_filename') or T('LaTeX 源码包')
                src_link = (
                    f' · <a href="/api/research/file?attachment_id={quote(str(src_id))}" rel="ugc nofollow noopener noreferrer">'
                    f'{escape(T("源码包：{name}", name=src_name))}</a>'
                    if src_id else ''
                )
                if is_compiled_html:
                    ltxml_ver = str(ci.get('latexml') or '0.8')
                    compiled_tag = escape(T('本站从源码转换 · LaTeXML {version}', version=ltxml_ver))
                else:
                    tl_year = str(ci.get('texlive') or '2026')
                    eng = str(ci.get('engine') or 'pdflatex')
                    compiled_tag = escape(T('本站从源码编译 · TeX Live {year} · {engine}', year=tl_year, engine=eng))
                extra_info = f' · <span class="badge">{compiled_tag}</span>{src_link}'
            elif is_source_archive_filename(fname):
                extra_info = f' · <span class="badge">{escape(T("LaTeX 源码包"))}</span>'
            size_text = escape(format_size(m['size']))
            size_title = _attr(format_size_title(m['size'], T))
            sha_text = escape(T('SHA-256 {sha}', sha=m['sha256'][:16] + '…'))
            mc = m.get('chain')
            chain_info = ''
            if mc:
                m_anchor = mc.get('anchor')
                bf_tag = T('（补链）') if mc.get('backfilled') else ''
                if m_anchor and m_anchor.get('state') == 'confirmed':
                    chain_badge = T('记录链 #{seq}{backfilled} · 比特币区块 #{height}',
                                    seq=mc['seq'], backfilled=bf_tag, height=m_anchor['bitcoin']['height'])
                    badge_cls = 'badge ok material-chain'
                else:
                    chain_badge = T('记录链 #{seq}{backfilled}（待锚定）', seq=mc['seq'], backfilled=bf_tag)
                    badge_cls = 'badge material-chain'
                chain_info = (
                    f' · <span class="{badge_cls}">{escape(chain_badge)}</span>'
                    f' · <a class="material-proof-link" href="/api/chain/proof?record={_attr(mc["record_id"])}">{escape(T("附件链证明"))}</a>'
                )
            if m.get('redacted'):
                redacted_badge = escape(T('已因合规原因移除：{reason}', reason=m['redacted']['reason']))
                rows_html.append(
                    f'<li id="material-{_attr(m["id"])}"><span class="muted">{escape(fname)}</span>'
                    f' · <span class="badge danger">{redacted_badge}</span>{chain_info}'
                    f' <span class="muted"><span title="{size_title}">{size_text}</span> · {sha_text}</span></li>'
                )
            else:
                rows_html.append(
                    f'<li id="material-{_attr(m["id"])}"><a href="/api/research/file?attachment_id={quote(m["id"])}" rel="ugc nofollow noopener noreferrer">{escape(fname)}</a>'
                    f'{pdf_link}{sibling_html_link}{html_view_link}{extra_info}{chain_info} <span class="muted"><span title="{size_title}">{size_text}</span> · {sha_text}</span></li>'
                )
        reproducible = any(m.get('origin') == 'compiled' and m.get('mime') == 'application/pdf'
                           and (m.get('compile_info') or {}).get('source_date_epoch') is not None for m in data['materials'])
        repro_note = (
            f'<p class="muted">{escape(T("编译时固定 SOURCE_DATE_EPOCH 为本版提交时间，同一源码重编可得一致的 PDF。"))}</p>'
            if reproducible else ''
        )
        html_disclaimer = (
            f'<p class="muted">{escape(T("HTML 版由程序自动从 LaTeX 源码转换，公式和排版可能与 PDF 不同，请以 PDF 为准。"))}</p>'
            if has_compiled_html else ''
        )
        # PDF-only works stay publishable; the site just does not read the PDF (decision of 2026-11-01).
        pdf_only = (any(m.get('origin', 'upload') == 'upload' and m.get('mime') == 'application/pdf' for m in data['materials'])
                    and not any(m.get('origin', 'upload') == 'upload' and is_source_archive_filename(m.get('filename') or '')
                                for m in data['materials']))
        pdf_only_note = (f'<p class="muted">{escape(T("本版只有 PDF，没有源码：本站不读取 PDF 内容，AI 分析只看标题、摘要和正文。上传 LaTeX 源码包后可获得本站编译的 PDF 与 HTML 版，AI 分析也会读取全文。"))}</p>'
                         if pdf_only else '')
        materials = (f'<section class="hp side-panel" id="work-materials">'
                     f'<header class="hp-head"><span class="hp-code mono">FILES</span><h2 class="hp-title">{escape(T("本版材料"))}</h2>'
                     f'<span class="badge">{len(data["materials"])}</span></header>'
                     f'<div class="hp-body"><ul class="material-list">{"".join(rows_html)}</ul>'
                     f'{pdf_only_note}{repro_note}{html_disclaimer}'
                     f'<p class="muted faint">{escape(T("附件按数据保存，下载不会在本站运行其中的代码。"))}</p></div></section>')

    authors_meta = [a['name'] for a in paper['authors']] if paper and paper.get('authors') else [author]
    names = ', '.join(authors_meta)
    citation = T('{authors}. {title}. Eksmu，版本 {n}，{date}。{url}', authors=names, title=title, n=number,
                 date=version_date, url=f"{origin}/{here}")
    bib_key = f"eksmu-{work['id'][:8]}-v{number}"
    bib_authors = ' and '.join(tex(a) for a in authors_meta)
    bib_note = f"Version {number}; revision {revision['id']}; SHA-256 {revision['sha256']}"
    bibtex = (
        f"@misc{{{bib_key},\n"
        f"  author = {{{bib_authors}}},\n"
        f"  title = {{{{{tex(title)}}}}},\n"
        f"  year = {{{version_date[:4]}}},\n"
        f"  month = {{{version_date[5:7]}}},\n"
        f"  howpublished = {{Eksmu}},\n"
        f"  url = {{{canonical}}},\n"
        f"  note = {{{tex(bib_note)}}}\n"
        f"}}"
    )
    chain = data.get('chain')
    anchor = chain.get('anchor') if chain else None
    if anchor and anchor.get('state') == 'confirmed':
        share_text = T('【Eksmu 时间存证】《{title}》（作者：{authors}，本站第 {k} 篇作品，版本 {n}，{date}）已写入 SHA-256 记录链（第 {seq} 条记录，摘要 {sha}）并锚定至比特币区块 #{height}，证明该版本不晚于此时间已经存在，可独立核验：{url}',
                       title=title, authors=names, k=work_number, n=number, date=version_date, seq=chain['seq'],
                       sha=revision['sha256'][:16], height=anchor['bitcoin']['height'], url=f"{origin}/{here}")
    else:
        share_text = T('【Eksmu 记录链】《{title}》（作者：{authors}，本站第 {k} 篇作品，版本 {n}，{date}）的版本记录已写入公开 SHA-256 记录链（摘要 {sha}）。附件不在此记录的覆盖范围内：{url}',
                       title=title, authors=names, k=work_number, n=number, date=version_date,
                       sha=revision['sha256'][:16], url=f"{origin}/{here}")

    compiled_pdfs = [
        m for m in data['materials']
        if not m.get('redacted')
        and m.get('origin') == 'compiled'
        and m.get('filename', '').lower().endswith('.pdf')
        and m.get('mime') == 'application/pdf'
    ]
    uploaded_pdfs = [
        m for m in data['materials']
        if not m.get('redacted')
        and m.get('origin', 'upload') != 'compiled'
        and m.get('filename', '').lower().endswith('.pdf')
        and m.get('mime') == 'application/pdf'
    ]
    target_pdf = compiled_pdfs[0] if compiled_pdfs else (uploaded_pdfs[0] if uploaded_pdfs else None)

    compiled_htmls = [
        m for m in data['materials']
        if not m.get('redacted')
        and m.get('origin') == 'compiled'
        and m.get('mime') == 'text/html'
        and (m.get('filename') or '').lower().endswith('.html')
    ]
    target_html = compiled_htmls[0] if compiled_htmls else None

    action_buttons = []
    if target_pdf:
        pdf_url = f"/w/{work['id']}/pdf/{quote(target_pdf['id'])}"
        action_buttons.append(
            f'<a class="btn primary action-item" href="{_attr(pdf_url)}">'
            f'<svg class="act-ico" viewBox="0 0 24 24" aria-hidden="true"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z" fill="none" stroke="currentColor" stroke-width="1.8"/><path d="M14 2v6h6M9 13h6M9 17h4" fill="none" stroke="currentColor" stroke-width="1.8"/></svg>'
            f'<span>{escape(T("在线阅读 PDF"))}</span></a>'
        )
    if target_html:
        html_url = f"/w/{work['id']}/html/{quote(target_html['id'])}"
        action_buttons.append(
            f'<a class="btn action-item" href="{_attr(html_url)}">'
            f'<svg class="act-ico" viewBox="0 0 24 24" aria-hidden="true"><polyline points="16 18 22 12 16 6"/><polyline points="8 6 2 12 8 18"/></svg>'
            f'<span>{escape(T("HTML 版"))}</span></a>'
        )
    action_buttons.append(
        f'<a class="btn sm quiet secondary action-item" href="#work-cite">'
        f'<svg class="act-ico" viewBox="0 0 24 24" aria-hidden="true"><path d="M3 21c3 0 7-1 7-8V5c0-1.25-.75-2-2-2H4c-1.25 0-2 .75-2 2v6c0 1.25.75 2 2 2 0 4-1 6-1 8zm14 0c3 0 7-1 7-8V5c0-1.25-.75-2-2-2h-4c-1.25 0-2 .75-2 2v6c0 1.25.75 2 2 2 0 4-1 6-1 8z" fill="currentColor"/></svg>'
        f'<span>{escape(T("引用"))}</span></a>'
    )
    reports = (data.get('analysis') or {}).get('reports', [])
    ai_count = len(reports)
    ai_badge = f' <span class="mono n badge">{ai_count}</span>' if ai_count else ''
    action_buttons.append(
        f'<a class="btn sm quiet secondary action-item" href="#work-analysis">'
        f'<svg class="act-ico" viewBox="0 0 24 24" aria-hidden="true"><path d="M12 2a4 4 0 0 1 4 4c0 .73-.2 1.41-.54 2H18a3 3 0 0 1 3 3v2a3 3 0 0 1-3 3h-1.1c.06.33.1.66.1 1a5 5 0 0 1-5 5 5 5 0 0 1-5-5c0-.34.04-.67.1-1H7a3 3 0 0 1-3-3v-2a3 3 0 0 1 3-3h2.54c-.34-.59-.54-1.27-.54-2a4 4 0 0 1 4-4z" fill="none" stroke="currentColor" stroke-width="1.8"/><circle cx="9" cy="13" r="1.2" fill="currentColor"/><circle cx="15" cy="13" r="1.2" fill="currentColor"/></svg>'
        f'<span>{escape(T("AI 审阅"))}</span>{ai_badge}</a>'
    )
    comments_count = len(data.get('comments', []))
    comments_badge = f' <span class="mono n badge">{comments_count}</span>' if comments_count else ''
    action_buttons.append(
        f'<a class="btn sm quiet secondary action-item" href="#work-discussion">'
        f'<svg class="act-ico" viewBox="0 0 24 24" aria-hidden="true"><path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z" fill="none" stroke="currentColor" stroke-width="1.8"/></svg>'
        f'<span>{escape(T("讨论"))}</span>{comments_badge}</a>'
    )
    action_buttons.append(
        f'<button type="button" class="btn sm quiet paper-toggle action-item secondary" id="paper-toggle" aria-label="{_attr(T("切换纸面阅读模式"))}" title="{_attr(T("切换纸面阅读模式"))}">'
        f'<svg class="act-ico" viewBox="0 0 24 24" aria-hidden="true"><rect x="4" y="3" width="16" height="18" rx="2" fill="none" stroke="currentColor" stroke-width="1.8"/><line x1="8" y1="8" x2="16" y2="8" stroke="currentColor" stroke-width="1.8"/><line x1="8" y1="12" x2="16" y2="12" stroke="currentColor" stroke-width="1.8"/><line x1="8" y1="16" x2="12" y2="16" stroke="currentColor" stroke-width="1.8"/></svg>'
        f'<span>{escape(T("纸面"))}</span></button>'
    )
    action_buttons.append(
        f'<a class="btn sm quiet secondary button action-item" href="{_attr(workspace)}">{escape(T("评论、回应与协作"))}</a>'
    )
    share_title = T("复制时间存证分享文案（证明此版本不晚于某时存在）")
    action_buttons.append(
        f'<button type="button" id="copy-credit-share" class="btn sm quiet secondary action-item" data-share="{_attr(share_text)}" title="{_attr(share_title)}">'
        f'<svg class="act-ico" viewBox="0 0 24 24" aria-hidden="true"><path d="M4 12v8a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2v-8" fill="none" stroke="currentColor" stroke-width="1.8"/><polyline points="16 6 12 2 8 6" fill="none" stroke="currentColor" stroke-width="1.8"/><line x1="12" y1="2" x2="12" y2="15" stroke="currentColor" stroke-width="1.8"/></svg>'
        f'<span>{escape(T("复制存证分享文字"))}</span></button>'
    )
    actions_bar = (
        f'<div class="work-action-bar work-actions work-cta">{"".join(action_buttons)}</div>'
        f'<p id="credit-share-state" class="muted" role="status" hidden></p>'
    )

    # In-page section nav; plain anchors work without JS, work-page.js highlights the current one.
    toc = []
    if 'id="work-abstract"' in paper_top:
        toc.append(('work-abstract', T('摘要'), ''))
    if rendered['html'].strip():
        toc.append(('work-body', T('正文'), ''))
    if 'id="work-references"' in paper_bottom:
        toc.append(('work-references', T('参考文献'), ''))
    analysis_html = _analysis(data.get('analysis'), profiles, versions, work['id'], workspace, lang, T)
    if analysis_html:
        toc.append(('work-analysis', T('AI 审阅'), str(ai_count) if ai_count else ''))
    toc.append(('work-discussion', T('讨论'), str(comments_count) if comments_count else ''))
    section_nav = (f'<nav class="work-toc" id="work-toc" aria-label="{_attr(T("本页章节"))}">'
                   + ''.join(f'<a href="#{key}" data-target="{key}">{escape(label)}'
                             + (f' <span class="mono n">{n}</span>' if n else '') + '</a>'
                             for key, label, n in toc)
                   + '</nav>')

    comments = _comments(data['comments'], profiles, T)
    if data['evidence']:
        comments += f'<p class="muted">{escape(T("另有 {n} 份参与者提交的证据与分析，在“评论、回应与协作”中查看。", n=data["evidence"]))}</p>'

    card_url = f"{origin}/{lang}/w/{work['id']}/card.svg"
    meta = [
        f'<link rel="canonical" href="{_attr(canonical)}">',
        '<meta property="og:type" content="article">',
        '<meta property="og:site_name" content="Eksmu">',
        f'<meta property="og:title" content="{_attr(title)}">',
        f'<meta property="og:description" content="{_attr(description)}">',
        f'<meta property="og:url" content="{_attr(canonical)}">',
        f'<meta property="og:locale" content="{OG_LOCALE[lang]}">',
        f'<meta property="og:image" content="{_attr(card_url)}">',
        '<meta name="twitter:card" content="summary_large_image">',
        f'<meta name="twitter:image" content="{_attr(card_url)}">',
    ]
    if dist['distribute'] and not data['status'].get('withdrawn'):
        ld_doc = {
            '@context': 'https://schema.org',
            '@type': 'ScholarlyArticle',
            'headline': title,
            'description': description,
            'author': [{'@type': 'Person', 'name': name} for name in authors_meta],
            'datePublished': first_date,
            'dateModified': version_date,
            'version': str(number),
            'url': canonical,
            'image': card_url,
            'identifier': f"sha256:{revision['sha256']}",
            'publisher': {'@type': 'Organization', 'name': 'Eksmu · Open Inquiry Community', 'url': origin},
        }
        if data['details'].get('topics'):
            ld_doc['keywords'] = data['details']['topics']
        ld_json = json.dumps(ld_doc, ensure_ascii=False).replace('<', '\\u003c').replace('>', '\\u003e').replace('&', '\\u0026')
        author_pdfs = [
            m for m in data['materials']
            if not m.get('redacted')
            and m.get('actor_id') == work['actor_id']
            and m.get('filename', '').lower().endswith('.pdf')
            and m.get('mime') == 'application/pdf'
        ]
        author_pdfs.sort(key=lambda pair_m: (0 if pair_m.get('origin', 'upload') != 'compiled' else 1, pair_m.get('created_at') or ''))
        first_pdf = author_pdfs[0] if author_pdfs else None
        if first_pdf:
            pdf_url = f"{origin}/w/{work['id']}/pdf/{quote(first_pdf['id'])}"  # one language-neutral URL per file
            pdf_meta = [f'<meta name="citation_pdf_url" content="{_attr(pdf_url)}">']
        else:
            pdf_meta = []
        meta += [
            f'<meta name="citation_title" content="{_attr(title)}">',
            *[f'<meta name="citation_author" content="{_attr(name)}">' for name in authors_meta],
            f'<meta name="citation_publication_date" content="{first_date.replace("-", "/")}">',
            f'<meta name="citation_online_date" content="{version_date.replace("-", "/")}">',
            f'<meta name="citation_public_url" content="{_attr(canonical)}">',
            *pdf_meta,
            f'<meta name="citation_abstract" content="{_attr(description)}">',
            *([f'<meta name="citation_keywords" content="{_attr("; ".join(data["details"]["topics"]))}">'] if data['details'].get('topics') else []),
            f'<script type="application/ld+json">{ld_json}</script>',
        ]
    if not dist['distribute'] or data['status'].get('withdrawn'):
        meta.append('<meta name="robots" content="noindex">')

    markup = i18n.render('work.html', lang, here).decode('utf-8')
    slots = {
        'page_title': escape(f'{title} · Eksmu'), 'description': _attr(description), 'meta': ''.join(meta),
        'kind': escape(T(KINDS.get(work['payload'].get('kind'), '其他'))),
        'version_label': version_label, 'first_date': escape(first_date),
        'version_date_readout': version_date_readout, 'work_number_label': work_number_label,
        'author_readout': author_readout,
        'title': escape(title), 'byline': byline, 'moderation_notice': moderation_notice,
        'withdrawn': withdrawn, 'topics': topics, 'help': help_note,
        'actions_bar': actions_bar, 'section_nav': section_nav,
        'workspace_url': _attr(workspace), 'versions': version_list, 'versions_section': versions_section,
        'versions_earlier_notice': versions_earlier_notice,
        'paper_top': paper_top,
        'body': rendered['html'], 'render_notice': escape(T(notice)) if notice else '',
        'paper_bottom': paper_bottom, 'materials': materials, 'citation': escape(citation), 'bibtex': escape(bibtex),
        'bibtex_data': _attr(quote(bibtex + '\n', safe='')), 'bib_filename': _attr(f'{bib_key}.bib'), 'share_text': _attr(share_text),
        'revision_id': escape(revision['id']), 'digest': escape(revision['sha256'][:16] + '…'), 'comments': comments,
        'time_evidence': _time_evidence(data.get('chain'), work['id'], revision['id'], lang, origin, T),
        'analysis': analysis_html,
        'author_id': _attr(work['actor_id']), 'work_id_attr': _attr(work['id']), 'revision_attr': _attr(revision['id']),
        'analysis_url': _attr(workspace + '&section=analysis'), 'revise_url': _attr(workspace + '&section=revise'),
        'publication_url': _attr(workspace + '&section=publication'), 'comments_url': _attr(workspace + '&section=comments'),
    }
    # One pass, so author text containing "@@name@@" is never re-expanded.
    markup = re.sub(r'@@(\w+)@@', lambda m: slots.get(m.group(1), ''), markup)
    return markup.encode('utf-8')


def load_pdf(database: str, rows: list[dict], exploration_id: str, attachment_id: str, *, now=None):
    """Open a verified PDF stream for an attachment belonging to one of this work's revisions."""
    identifier(exploration_id)
    identifier(attachment_id)
    from pathlib import Path as _P
    from .community import (
        _get_derived_index,
        distribution,
        load_active_labels,
        load_trusted_authors,
    )
    derived = _get_derived_index(rows)
    work = derived.works.get(exploration_id)
    if work is None:
        raise Problem('找不到这项作品', 404)
    versions = list(derived.versions.get(exploration_id, ()))
    if not versions:
        raise Problem('找不到这项作品', 404)
    res_path = _P(database + '.research.sqlite')
    if not res_path.is_file():
        raise Problem('附件不存在', 404)
    actor_rec = derived.index.get(work['actor_id'])
    work_rows = [actor_rec, work, *versions] if actor_rec is not None else rows
    from .research import Research
    res = Research(str(res_path), work_rows)
    try:
        row = res.db.execute('SELECT * FROM research_files WHERE id=?', (attachment_id,)).fetchone()
        if row is None:
            raise Problem('附件不存在', 404)
        version_ids = {v['id'] for v in versions}
        if row['revision_id'] not in version_ids:
            raise Problem('附件不存在', 404)
        filename = row['filename'] or ''
        if not filename.lower().endswith('.pdf'):
            raise Problem('附件不存在', 404)
        total_size, chunks, _, _ = res.download_stream(attachment_id)
        it = iter(chunks)
        prefix = bytearray()
        while len(prefix) < 5:
            nxt = next(it, b'')
            if not nxt:
                break
            prefix.extend(nxt)
        if not bytes(prefix).startswith(b'%PDF-'):
            for _ in it:
                pass
            raise Problem('附件不存在', 404)

        def _stream():
            try:
                if prefix:
                    yield bytes(prefix)
                for chunk in it:
                    if chunk:
                        yield chunk
            finally:
                res.close()

        active_labels = load_active_labels(database)
        trusted_authors = load_trusted_authors(database)
        withdrawn_set = _withdrawn_ids(database, rows, {exploration_id})
        dist = distribution(
            exploration_id,
            derived=derived,
            active_labels=active_labels,
            trusted_authors=trusted_authors,
            withdrawn=(exploration_id in withdrawn_set),
            withdrawn_ids=withdrawn_set,
            now=now,
        )
        return total_size, _stream(), filename, bool(dist['distribute']) and (exploration_id not in withdrawn_set)
    except BaseException:
        res.close()
        raise


def load_html(database: str, rows: list[dict], exploration_id: str, attachment_id: str, *, now=None):
    """Open a verified compiled HTML stream for an author-owned compiled attachment on this work."""
    identifier(exploration_id)
    identifier(attachment_id)
    from pathlib import Path as _P
    from .community import (
        _get_derived_index,
        distribution,
        load_active_labels,
        load_trusted_authors,
    )
    derived = _get_derived_index(rows)
    work = derived.works.get(exploration_id)
    if work is None:
        raise Problem('找不到这项作品', 404)
    versions = list(derived.versions.get(exploration_id, ()))
    if not versions:
        raise Problem('找不到这项作品', 404)
    res_path = _P(database + '.research.sqlite')
    if not res_path.is_file():
        raise Problem('附件不存在', 404)
    actor_rec = derived.index.get(work['actor_id'])
    work_rows = [actor_rec, work, *versions] if actor_rec is not None else rows
    from .research import Research
    res = Research(str(res_path), work_rows)
    try:
        row = res.db.execute('SELECT * FROM research_files WHERE id=?', (attachment_id,)).fetchone()
        if row is None:
            raise Problem('附件不存在', 404)
        version_ids = {v['id'] for v in versions}
        if row['revision_id'] not in version_ids or row['actor_id'] != work['actor_id']:
            raise Problem('附件不存在', 404)
        keys = row.keys()
        if 'origin' not in keys or row['origin'] != 'compiled' or row['mime'] != 'text/html':
            raise Problem('附件不存在', 404)
        filename = row['filename'] or ''
        if not filename.lower().endswith('.html'):
            raise Problem('附件不存在', 404)
        total_size, chunks, _, _ = res.download_stream(attachment_id)
        it = iter(chunks)

        def _stream():
            try:
                for chunk in it:
                    if chunk:
                        yield chunk
            finally:
                res.close()

        active_labels = load_active_labels(database)
        trusted_authors = load_trusted_authors(database)
        withdrawn_set = _withdrawn_ids(database, rows, {exploration_id})
        dist = distribution(
            exploration_id,
            derived=derived,
            active_labels=active_labels,
            trusted_authors=trusted_authors,
            withdrawn=(exploration_id in withdrawn_set),
            withdrawn_ids=withdrawn_set,
            now=now,
        )
        return total_size, _stream(), filename, bool(dist['distribute']) and (exploration_id not in withdrawn_set)
    except BaseException:
        res.close()
        raise


def not_found(lang: str) -> bytes:
    return i18n.render('notfound.html', lang, None)


def load_author(database: str, rows: list[dict], profiles: dict, actor_id: str, *, now=None) -> dict:
    """Collect public profile and published works for one author; raise Problem(404) if unknown."""
    identifier(actor_id)
    from pathlib import Path as _P
    from .community import Community, _get_derived_index, distribution, load_active_labels, load_trusted_authors
    derived = _get_derived_index(rows)
    actor = derived.index.get(actor_id)
    if actor is None or actor.get('type') != 'actor':
        raise Problem('找不到这位作者', 404)
    profile = profiles.get(actor_id) or {}
    display_name = profile.get('display_name') or actor['payload'].get('alias') or actor_id[:8]
    affiliation = profile.get('affiliation') or ''
    revisions_by_work = derived.versions
    author_works = [
        (idx, r) for idx, r in derived.works_by_actor.get(actor_id, ())
        if revisions_by_work.get(r['id'])
    ]
    author_works.sort(key=lambda pair: (pair[1]['created_at'], pair[0]), reverse=True)
    author_work_ids = [w['id'] for _, w in author_works]
    topics_by_work: dict[str, list[str]] = {}
    try:
        if author_work_ids and _P(database + '.community.sqlite').is_file():
            hub = Community(database + '.community.sqlite', rows, profiles)
            try:
                if len(author_work_ids) <= 500:
                    placeholders = ','.join('?' for _ in author_work_ids)
                    query = f'SELECT id, document FROM hub_details WHERE id IN ({placeholders})'
                    cursor = hub.db.execute(query, author_work_ids)
                else:
                    cursor = hub.db.execute('SELECT id, document FROM hub_details')
                for wid, raw in cursor:
                    topics_by_work[wid] = json.loads(raw).get('topics', [])
            finally:
                hub.close()
    except Exception:
        pass
    active_labels = load_active_labels(database)
    trusted_authors = load_trusted_authors(database)
    withdrawn_all = _withdrawn_ids(database, rows) if author_work_ids else set()
    withdrawn = withdrawn_all & set(author_work_ids)
    works = []
    for _, w in author_works:
        vers = revisions_by_work[w['id']]
        latest = vers[-1]
        payload = latest['payload']
        paper = payload.get('paper') or None
        dist = distribution(
            w['id'],
            derived=derived,
            active_labels=active_labels,
            trusted_authors=trusted_authors,
            withdrawn=(w['id'] in withdrawn),
            withdrawn_ids=withdrawn_all,
            now=now,
        )
        lbl = dist['label']
        works.append({
            'id': w['id'],
            'kind': w['payload'].get('kind', 'other'),
            'title': payload.get('title') or '',
            'excerpt': _excerpt((paper or {}).get('abstract') or payload['body'], 220),
            'first_date': vers[0]['created_at'][:10],
            'latest_date': latest['created_at'][:10],
            'version_count': len(vers),
            'topics': topics_by_work.get(w['id'], []),
            'withdrawn': w['id'] in withdrawn,
            'label': lbl,
            'moderation_label': lbl['label'] if lbl else None,
            'moderation_reason': lbl['reason'] if lbl else '',
            'distribute': dist['distribute'],
        })
    comments_count = derived.comments_by_actor.get(actor_id, 0)
    return {
        'actor_id': actor_id,
        'display_name': display_name,
        'affiliation': affiliation,
        'works': works,
        'comments_count': comments_count,
        'has_distributable_works': any(item['distribute'] for item in works),
    }


def author_page(data: dict, lang: str, origin: str) -> bytes:
    T = lambda message, **params: i18n.t(lang, message, **params)
    actor_id = data['actor_id']
    display_name = data['display_name']
    affiliation = data['affiliation']
    works = data['works']
    here = f"u/{actor_id}"
    canonical = f"{origin}/{lang}/{here}"
    if affiliation:
        description = T('{name}（{affiliation}，作者自述）在 Eksmu 公开发表的探索作品（共 {n} 项）。',
                        name=display_name, affiliation=affiliation, n=len(works))
        affiliation_html = f'<p class="work-byline author-affiliation">{escape(T("{affiliation}（作者自述）", affiliation=affiliation))}</p>'
    else:
        description = T('{name} 在 Eksmu 公开发表的探索作品（共 {n} 项）。',
                        name=display_name, n=len(works))
        affiliation_html = ''
    stats = escape(T('共发表 {works} 项作品 · 参与 {comments} 条讨论', works=len(works), comments=data['comments_count']))
    if works:
        cards = []
        for item in works:
            w_title = item['title'] or T('未命名作品')
            href = f"/{lang}/w/{item['id']}"
            kind_label = escape(T(KINDS.get(item['kind'], '其他')))
            withdrawn_badge = (f'<span class="badge badge-withdrawn">{escape(T("已撤回"))}</span>'
                               if item['withdrawn'] else '')
            mod_lbl = item.get('moderation_label') or (item.get('label') or {}).get('label')
            mod_reason = item.get('moderation_reason') or (item.get('label') or {}).get('reason') or ''
            mod_badge = (f'<span class="badge badge-moderation">{escape(T(MODERATION_LABELS.get(mod_lbl, mod_lbl)))}</span>'
                         if mod_lbl else '')
            meta_line = (f'<div class="entry-meta">'
                         f'<span class="badge">{kind_label}</span>'
                         f'{withdrawn_badge}'
                         f'{mod_badge}'
                         f'<span class="muted">{escape(T("版本 {n}", n=item["version_count"]))}'
                         f'{escape(T(" · 首次发表 {date}", date=item["first_date"]))}</span>'
                         f'</div>')
            title_link = f'<a class="link" href="{_attr(href)}">{escape(w_title)}</a>'
            excerpt_html = f'<p>{escape(item["excerpt"])}</p>' if item['excerpt'] else ''
            reason_html = (f'<p class="muted moderation-reason">{escape(T("不予分发理由：{reason}", reason=mod_reason))}</p>'
                           if mod_lbl and mod_reason else '')
            topics_html = ('<div class="topic-chips">'
                           + ''.join(f'<span class="chip"># {escape(t)}</span>' for t in item['topics'])
                           + '</div>') if item['topics'] else ''
            cards.append(f'<article class="entry discovery-card author-work-card">'
                         f'{meta_line}{title_link}{excerpt_html}{reason_html}{topics_html}</article>')
        works_html = f'<div class="author-work-list">{"".join(cards)}</div>'
    else:
        works_html = f'<div class="card empty-state"><p>{escape(T("这位作者还没有公开发表的作品。"))}</p></div>'
    ld_doc = {
        '@context': 'https://schema.org',
        '@type': 'Person',
        'name': display_name,
        'url': canonical,
    }
    ld_json = json.dumps(ld_doc, ensure_ascii=False).replace('<', '\\u003c').replace('>', '\\u003e').replace('&', '\\u0026')
    meta = [
        f'<link rel="canonical" href="{_attr(canonical)}">',
        '<meta property="og:type" content="profile">',
        '<meta property="og:site_name" content="Eksmu">',
        f'<meta property="og:title" content="{_attr(display_name)}">',
        f'<meta property="og:description" content="{_attr(description)}">',
        f'<meta property="og:url" content="{_attr(canonical)}">',
        f'<meta property="og:locale" content="{OG_LOCALE[lang]}">',
        f'<script type="application/ld+json">{ld_json}</script>',
    ]
    has_distributable = data.get(
        'has_distributable_works',
        any(w.get('distribute', not w.get('withdrawn') and not w.get('moderation_label')) for w in works),
    )
    if not has_distributable:
        meta.append('<meta name="robots" content="noindex">')
    markup = i18n.render('author.html', lang, here).decode('utf-8')
    slots = {
        'page_title': escape(f'{display_name} · Eksmu'),
        'description': _attr(description),
        'meta': ''.join(meta),
        'short_id': escape(actor_id[:8]),
        'display_name': escape(display_name),
        'affiliation': affiliation_html,
        'stats': stats,
        'works': works_html,
    }
    markup = re.sub(r'@@(\w+)@@', lambda m: slots[m.group(1)], markup)
    return markup.encode('utf-8')


def _wrap_line(text: str, width: int) -> list[str]:
    text = ' '.join((text or '').split())
    if not text:
        return ['']
    lines = []
    current = ''
    for ch in text:
        step = 2 if ord(ch) > 127 else 1
        cur_w = sum(2 if ord(c) > 127 else 1 for c in current)
        if cur_w + step > width and current:
            lines.append(current.strip())
            current = ch
        else:
            current += ch
    if current:
        lines.append(current.strip())
    return lines



def _fit(text: str, size: float, max_width: int, mono: bool = False) -> str:
    """SVG has no wrapping: squeeze a line that would overrun the card (estimated width) into max_width."""
    width = sum(size * (1.0 if ord(c) > 0x2E80 else (0.6 if mono else 0.55)) for c in text)
    return f' textLength="{max_width}" lengthAdjust="spacingAndGlyphs"' if width > max_width else ''

def card_svg(data: dict, lang: str, origin: str) -> bytes:
    """1200x630 Priority Credit & Timestamp Certificate SVG (also used as og:image)."""
    T = lambda message, **params: i18n.t(lang, message, **params)
    work, revision, versions, profiles = data['work'], data['revision'], data['versions'], data['profiles']
    work_number = data.get('work_number', 1)
    payload = revision['payload']
    number = versions.index(revision) + 1
    author = profiles.get(work['actor_id'], {}).get('display_name') or work['actor_id'][:8]
    affiliation = profiles.get(work['actor_id'], {}).get('affiliation') or ''
    title = payload.get('title') or T('未命名作品')
    version_date = revision['created_at'][:16].replace('T', ' ') + ' UTC'
    chain = data.get('chain')
    anchor = chain.get('anchor') if chain else None
    if anchor and anchor.get('state') == 'confirmed' and anchor.get('bitcoin'):
        btc_text = T('已锚定至比特币区块 #{height}', height=anchor['bitcoin']['height'])
        badge_color = '#5CE6A0'
    elif chain:
        btc_text = T('已写入 SHA-256 记录链第 {seq} 条记录（待比特币区块确认）', seq=chain['seq'])
        badge_color = '#2DE2E6'
    else:
        btc_text = T('已保存 SHA-256 内容摘要')
        badge_color = '#2DE2E6'
    chain_line = T('本站第 {k} 篇作品 · 记录链第 {seq} 条记录 · 版本 SHA-256：{sha}',
                   k=work_number, seq=chain['seq'] if chain else '-', sha=revision['sha256'])
    title_lines = _wrap_line(title, 54)[:2]
    if len(_wrap_line(title, 54)) > 2:
        title_lines[-1] = title_lines[-1][:-1] + '…'
    t1 = escape(title_lines[0])
    t2 = escape(title_lines[1]) if len(title_lines) > 1 else ''
    who = f"{author} · {affiliation}" if affiliation else author
    topics_str = '  '.join(f'#{t}' for t in data['details'].get('topics', [])[:5])
    card_note = T("在真理面前，我们都是学徒。此存证证明该版本不晚于锚定时间已经存在，不证明原创或正确。")
    url_str = f"{origin}/w/{work['id']}"
    svg = f'''<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1200 630" width="1200" height="630">
  <rect width="1200" height="630" fill="#07080D"/>
  <rect x="28" y="28" width="1144" height="574" rx="8" fill="#0C0F18" stroke="rgba(130, 148, 200, 0.28)" stroke-width="2"/>
  <rect x="28" y="28" width="1144" height="120" rx="8" fill="#10131D"/>
  <line x1="28" y1="148" x2="1172" y2="148" stroke="#2A3350" stroke-width="1.5"/>
  <rect x="28" y="28" width="80" height="3" fill="#2DE2E6"/>
  <path d="M28 44V28H44" stroke="#2DE2E6" stroke-width="2.5" fill="none"/>
  <path d="M1172 586v16h-16" stroke="#2DE2E6" stroke-width="2.5" fill="none"/>
  <text x="68" y="74" font-family="'Chakra Petch',system-ui,-apple-system,sans-serif" font-size="16" font-weight="700" fill="#2DE2E6" letter-spacing="2">EKSMU · OPEN INQUIRY COMMUNITY</text>
  <text x="68" y="112" font-family="system-ui,-apple-system,'Segoe UI',sans-serif" font-size="22" font-weight="600" fill="#E6E9F2">{escape(T("时间戳与记录链存证证书"))}</text>
  <rect x="740" y="58" width="392" height="48" rx="4" fill="#10131D" stroke="{badge_color}" stroke-width="1.5"/>
  <text x="936" y="89" text-anchor="middle" font-family="'JetBrains Mono',ui-monospace,monospace" font-size="16" font-weight="600" fill="{badge_color}"{_fit(btc_text, 16, 370, True)}>{escape(btc_text)}</text>
  <text x="68" y="210" font-family="Georgia,'Noto Serif CJK SC',serif" font-size="38" font-weight="700" fill="#E6E9F2">{t1}</text>
  <text x="68" y="260" font-family="Georgia,'Noto Serif CJK SC',serif" font-size="38" font-weight="700" fill="#E6E9F2">{t2}</text>
  <text x="68" y="315" font-family="system-ui,-apple-system,'Segoe UI',sans-serif" font-size="22" fill="#9AA3B8">{escape(T("作者：{who} · 版本 {n} · {date}", who=who, n=number, date=version_date))}</text>
  <text x="68" y="356" font-family="'JetBrains Mono',ui-monospace,monospace" font-size="18" font-weight="600" fill="#2DE2E6">{escape(topics_str)}</text>
  <rect x="64" y="386" width="1072" height="136" rx="4" fill="#10131D" stroke="rgba(130, 148, 200, 0.22)" stroke-width="1"/>
  <text x="92" y="424" font-family="'JetBrains Mono',ui-monospace,monospace" font-size="16" fill="#E6E9F2"{_fit(chain_line, 16, 1020, True)}>{escape(chain_line)}</text>
  <text x="92" y="462" font-family="'JetBrains Mono',ui-monospace,monospace" font-size="17" font-weight="600" fill="#5CE6A0">{escape(T("独立核验地址：{url}", url=url_str))}</text>
  <text x="92" y="498" font-family="system-ui,-apple-system,'Segoe UI',sans-serif" font-size="16" fill="#7C86A0"{_fit(card_note, 16, 1020)}>{escape(card_note)}</text>
  <text x="68" y="568" font-family="'JetBrains Mono',ui-monospace,monospace" font-size="15" fill="#9AA3B8">&gt; python3 eksmu_verify.py pull https://eksmu.com ./mirror &amp;&amp; python3 eksmu_verify.py verify ./mirror</text>
</svg>'''
    return svg.encode('utf-8')


def badge_svg(data: dict, lang: str) -> bytes:
    """Compact shields-style SVG badge for GitHub READMEs and personal sites."""
    revision, chain = data['revision'], data.get('chain')
    date = revision['created_at'][:10]
    anchor = chain.get('anchor') if chain else None
    if anchor and anchor.get('state') == 'confirmed' and anchor.get('bitcoin'):
        right = f"BTC #{anchor['bitcoin']['height']} · {date}"
        color = '#5CE6A0'
    elif chain:
        right = f"SHA-256 #{chain['seq']} · {date}"
        color = '#2DE2E6'
    else:
        right = f"SHA-256 {revision['sha256'][:8]} · {date}"
        color = '#2DE2E6'
    left = 'Eksmu Credit'
    lw = 96
    rw = max(130, len(right) * 7 + 20)
    total = lw + rw
    svg = f'''<svg xmlns="http://www.w3.org/2000/svg" width="{total}" height="22" role="img" aria-label="{escape(left)}: {escape(right)}">
  <title>{escape(left)}: {escape(right)}</title>
  <rect width="{total}" height="22" rx="4" fill="#0C0F18" stroke="rgba(130, 148, 200, 0.3)" stroke-width="1"/>
  <rect width="{lw}" height="22" rx="4" fill="#0C0F18"/>
  <rect x="{lw}" width="{rw}" height="22" rx="4" fill="#161A27"/>
  <rect x="{lw - 1}" width="1" height="22" fill="#2A3350"/>
  <g text-anchor="middle" font-family="'JetBrains Mono',ui-monospace,monospace" font-size="11" font-weight="600">
    <text x="{lw // 2}" y="15" fill="#2DE2E6">{escape(left)}</text>
    <text x="{lw + rw // 2}" y="15" fill="{color}">{escape(right)}</text>
  </g>
</svg>'''
    return svg.encode('utf-8')


def robots_txt(origin: str) -> bytes:
    return f"User-agent: *\nAllow: /\nSitemap: {origin}/sitemap.xml\n".encode('utf-8')


def _withdrawn_ids(database: str, rows: list[dict], only_ids: set[str] | None = None) -> set[str]:
    try:
        from pathlib import Path as _P
        if not _P(database + '.publications.sqlite').is_file():
            return set()
        from .community import _get_derived_index
        from .publications import Publications
        derived = _get_derived_index(rows)
        pub = Publications(database + '.publications.sqlite', ())
        try:
            candidates = {r[0] for r in pub.db.execute('SELECT DISTINCT exploration_id FROM publication_events')}
            if only_ids is not None:
                candidates &= only_ids
            candidates &= set(derived.works)
            if not candidates:
                return set()
            pub.index = derived.index
            return {wid for wid in candidates if pub.status(wid).get('withdrawn')}
        finally:
            pub.close()
    except Exception:
        return set()


def sitemap_xml(database: str, rows: list[dict], origin: str, *, now=None) -> bytes:
    from .community import _get_derived_index, distribution, load_active_labels, load_trusted_authors
    derived = _get_derived_index(rows)
    withdrawn = _withdrawn_ids(database, rows)
    active_labels = load_active_labels(database)
    trusted_authors = load_trusted_authors(database)
    urls = []
    for page_name in ('', 'founding.html', 'participation.html', 'openness.html'):
        suffix = f'/{page_name}' if page_name else '/'
        zh_url = f'{origin}/zh{suffix}'
        en_url = f'{origin}/en{suffix}'
        for loc in (zh_url, en_url):
            urls.append(
                f'  <url><loc>{escape(loc)}</loc>'
                f'<xhtml:link rel="alternate" hreflang="zh-CN" href="{escape(zh_url)}"/>'
                f'<xhtml:link rel="alternate" hreflang="en" href="{escape(en_url)}"/>'
                f'</url>'
            )
    author_lastmod: dict[str, str] = {}
    for wid, r in derived.works.items():
        vers = derived.versions.get(wid)
        if not vers:
            continue
        dist = distribution(
            wid,
            derived=derived,
            active_labels=active_labels,
            trusted_authors=trusted_authors,
            withdrawn=(wid in withdrawn),
            withdrawn_ids=withdrawn,
            now=now,
        )
        if dist['distribute']:
            lastmod = vers[-1]['created_at'][:10]
            aid = r['actor_id']
            if aid not in author_lastmod or lastmod > author_lastmod[aid]:
                author_lastmod[aid] = lastmod
            zh_url = f'{origin}/zh/w/{wid}'
            en_url = f'{origin}/en/w/{wid}'
            for loc in (zh_url, en_url):
                urls.append(
                    f'  <url><loc>{escape(loc)}</loc><lastmod>{escape(lastmod)}</lastmod>'
                    f'<xhtml:link rel="alternate" hreflang="zh-CN" href="{escape(zh_url)}"/>'
                    f'<xhtml:link rel="alternate" hreflang="en" href="{escape(en_url)}"/>'
                    f'</url>'
                )
    for aid, lastmod in author_lastmod.items():
        zh_url = f'{origin}/zh/u/{aid}'
        en_url = f'{origin}/en/u/{aid}'
        for loc in (zh_url, en_url):
            urls.append(
                f'  <url><loc>{escape(loc)}</loc><lastmod>{escape(lastmod)}</lastmod>'
                f'<xhtml:link rel="alternate" hreflang="zh-CN" href="{escape(zh_url)}"/>'
                f'<xhtml:link rel="alternate" hreflang="en" href="{escape(en_url)}"/>'
                f'</url>'
            )
    xml = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9" '
        'xmlns:xhtml="http://www.w3.org/1999/xhtml">\n'
        + '\n'.join(urls)
        + '\n</urlset>\n'
    )
    return xml.encode('utf-8')


def atom_feed(database: str, rows: list[dict], profiles: dict, origin: str, *, now=None) -> bytes:
    from .community import _get_derived_index, distribution, load_active_labels, load_trusted_authors
    derived = _get_derived_index(rows)
    withdrawn = _withdrawn_ids(database, rows)
    active_labels = load_active_labels(database)
    trusted_authors = load_trusted_authors(database)
    works = [r for r in rows if r['type'] == 'exploration']
    revisions = derived.versions
    items = []
    for w in reversed(works):
        if not revisions.get(w['id']):
            continue
        dist = distribution(
            w['id'],
            derived=derived,
            active_labels=active_labels,
            trusted_authors=trusted_authors,
            withdrawn=(w['id'] in withdrawn),
            withdrawn_ids=withdrawn,
            now=now,
        )
        if dist['distribute']:
            items.append(w)
            if len(items) >= 30:
                break
    updated = revisions[items[0]['id']][-1]['created_at'] if items else '2026-10-01T00:00:00+00:00'
    entries = []
    for w in items:
        rev = revisions[w['id']][-1]
        title = rev['payload'].get('title') or '未命名作品'
        author = profiles.get(w['actor_id'], {}).get('display_name') or w['actor_id'][:8]
        summary = _excerpt((rev['payload'].get('paper') or {}).get('abstract') or rev['payload']['body'])
        link = f"{origin}/w/{w['id']}"
        entries.append(
            f'  <entry>\n'
            f'    <title>{escape(title)}</title>\n'
            f'    <link href="{_attr(link)}"/>\n'
            f'    <id>urn:uuid:{escape(w["id"])}</id>\n'
            f'    <updated>{escape(rev["created_at"])}</updated>\n'
            f'    <published>{escape(w["created_at"])}</published>\n'
            f'    <author><name>{escape(author)}</name></author>\n'
            f'    <summary>{escape(summary)}</summary>\n'
            f'  </entry>'
        )
    xml = (
        '<?xml version="1.0" encoding="utf-8"?>\n'
        '<feed xmlns="http://www.w3.org/2005/Atom">\n'
        '  <title>Eksmu · Open Inquiry Community</title>\n'
        f'  <link href="{_attr(origin)}/"/>\n'
        f'  <link href="{_attr(origin)}/feed.xml" rel="self"/>\n'
        f'  <id>{escape(origin)}/</id>\n'
        f'  <updated>{escape(updated)}</updated>\n'
        + ('\n'.join(entries) + '\n' if entries else '')
        + '</feed>\n'
    )
    return xml.encode('utf-8')
