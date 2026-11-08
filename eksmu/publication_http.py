"""Explicit publication routes; no uploads or submitted-code execution here."""
from urllib.parse import urlsplit

from .store import Problem, exact
from .research_http import query
from .research_render import render_abstract
from .publication_contract import citation
from .time_evidence import sources


def get(publications, raw_path):
    name = urlsplit(raw_path).path.rsplit('/', 1)[-1]
    if name == 'workspace': return publications.workspace(**query(raw_path, {'revision_id'}))
    if name == 'snapshot':
        snap = publications.snapshot(**query(raw_path, {'snapshot_id'}))
        paper = (snap.get('revision') or {}).get('payload', {}).get('paper') or {}
        return {'snapshot': snap, 'citation': citation(snap['revision'], snap['publisher'], snap),
                'rendered_abstract': render_abstract(paper['abstract']) if paper.get('abstract') else None,
                'status': publications.status(snap['exploration']['id']), 'time_sources': sources(snap)}
    if name == 'export': return publications.export(**query(raw_path, {'snapshot_id'}))
    raise Problem('Not found', 404)


def post(publications, research, actor_id, path, payload):
    name = path.rsplit('/', 1)[-1]
    if name == 'snapshot':
        exact(payload, {'revision_id', 'attachment_ids', 'expected_previous_id', 'operation_id'})
        return publications.create_snapshot(research=research, actor_id=actor_id, **payload)
    if name == 'event':
        exact(payload, {'revision_id', 'kind', 'body', 'expected_previous_id', 'operation_id'})
        return publications.event(actor_id=actor_id, **payload)
    raise Problem('Not found', 404)
