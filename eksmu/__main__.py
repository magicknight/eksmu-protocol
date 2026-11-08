"""Private server, record export/import, and offline instance backup/restore."""
import argparse
import os
import sqlite3
from pathlib import Path

from .server import load_json, make_server
from .store import MAX_BUNDLE_BYTES, Problem, Store, canonical
from .maintenance import InstanceLock


def main():
    parser = argparse.ArgumentParser(description="Eksmu research community server and tools")
    parser.add_argument("--db", default="data/community.sqlite", help="Local SQLite file; never commit it")
    commands = parser.add_subparsers(dest="command", required=True)
    serving = commands.add_parser("serve", help="Listen only on 127.0.0.1")
    serving.add_argument("--port", type=int, default=8765)
    serving.add_argument("--with-verification", action="store_true",
                         help="Opt in to bounded local arithmetic receipt tasks (not AI or Lean)")
    serving.add_argument("--public-origin", help="Prepare one HTTPS proxy origin; this server still binds loopback")
    commands.add_parser("export").add_argument("file")
    commands.add_parser("import").add_argument("file")
    publication = commands.add_parser('verify-publication', help='Offline content-only check of a portable publication package')
    publication.add_argument('file')
    publication.add_argument('--expect-sha256', help='Compare against a separately retained snapshot digest')
    local = commands.add_parser('create-time-evidence', help='Create a private local commitment from a publication package')
    local.add_argument('publication_package')
    local.add_argument('evidence_file')
    check_time = commands.add_parser('verify-time-evidence', help='Offline content commitment check; no external time verification')
    check_time.add_argument('evidence_file')
    check_time.add_argument('--expect-commitment', help='Compare with a separately retained commitment digest')
    backup = commands.add_parser('backup', help='Back up the stopped instance, including sidecars')
    backup.add_argument('directory')
    backup.add_argument('--offline', action='store_true', help='Confirm all instance writers are stopped')
    backup.add_argument('--portable-blobs', action='store_true', help='Copy and verify GCS blob bytes into the offline snapshot')
    backup.add_argument('--secrets-dir', help='New separate directory for the optional service signing key')
    verify = commands.add_parser('verify-backup', help='Check a trusted local instance snapshot')
    verify.add_argument('directory')
    verify.add_argument('--secrets-dir')
    restore = commands.add_parser('restore', help='Restore a trusted snapshot to a NEW directory')
    restore.add_argument('directory')
    restore.add_argument('--into', required=True)
    restore.add_argument('--secrets-dir')
    reconcile = commands.add_parser('reconcile-accounts', help='Reconcile restored credentials against a current OFFLINE database')
    reconcile.add_argument('--from-db', required=True)
    reconcile.add_argument('--offline', action='store_true')
    commands.add_parser('behavior-queue', help='Read private behavior reports with the service stopped')
    review = commands.add_parser('review-behavior', help='Record an offline triage note; does not hide or block content')
    review.add_argument('case_id')
    review.add_argument('--outcome', choices=['no_action', 'follow_up'], required=True)
    review.add_argument('--reason-file', required=True, help='Local UTF-8 file with the review reason')
    review.add_argument('--operator-id', default='local_operator', help='Non-secret local operator label; not verified identity')
    appeal = commands.add_parser('review-behavior-appeal', help='Record an offline appeal review; no content restriction')
    appeal.add_argument('case_id')
    appeal.add_argument('--outcome', choices=['uphold', 'reopen'], required=True)
    appeal.add_argument('--reason-file', required=True)
    appeal.add_argument('--operator-id', required=True, help='Non-secret label distinct from the initial reviewer')
    label_work_cmd = commands.add_parser('label-work', help='Offline maintainer moderation label affecting distribution only')
    label_work_cmd.add_argument('--exploration', required=True, help='Work (exploration) ID')
    label_work_cmd.add_argument('--label', choices=['commercial_promotion', 'pending_review'], required=True)
    label_work_cmd.add_argument('--reason', help='Public reason for the label')
    label_work_cmd.add_argument('--reason-file', help='Alternative UTF-8 file containing the public reason')
    label_work_cmd.add_argument('--operator', '--operator-id', dest='operator', required=True, help='Non-secret operator identifier')
    label_work_cmd.add_argument('--source', choices=['maintainer', 'ai'], default='maintainer')
    retract_label_cmd = commands.add_parser('retract-label', help='Offline retraction of a distribution label')
    retract_label_cmd.add_argument('--label-id', required=True)
    retract_label_cmd.add_argument('--reason', help='Reason for retracting the label')
    retract_label_cmd.add_argument('--reason-file', help='Alternative UTF-8 file containing the reason')
    retract_label_cmd.add_argument('--operator', '--operator-id', dest='operator', required=True)
    commands.add_parser('label-queue', help='List pending_review labels and pending label appeals with the service stopped')
    review_label_cmd = commands.add_parser('review-label-appeal', help='Offline review of an author appeal on a distribution label')
    review_label_cmd.add_argument('--appeal-id', required=True)
    review_label_cmd.add_argument('--decision', choices=['uphold', 'retract'], required=True)
    review_label_cmd.add_argument('--reason', help='Decision reason visible to the author')
    review_label_cmd.add_argument('--reason-file', help='Alternative UTF-8 file containing the decision reason')
    review_label_cmd.add_argument('--operator', '--operator-id', dest='operator', required=True, help='Operator distinct from original labeler')
    trust_author_cmd = commands.add_parser('trust-author', help='Offline maintainer trust exempting an author from new-author cooling-off')
    trust_author_cmd.add_argument('--actor', required=True, help='Existing author (actor) ID')
    trust_author_cmd.add_argument('--reason', help='Reason for trusting the author')
    trust_author_cmd.add_argument('--reason-file', help='Alternative UTF-8 file containing the reason')
    trust_author_cmd.add_argument('--operator', '--operator-id', dest='operator', required=True, help='Non-secret operator identifier')
    untrust_author_cmd = commands.add_parser('untrust-author', help='Offline revocation of manual author trust')
    untrust_author_cmd.add_argument('--actor', required=True, help='Existing author (actor) ID')
    untrust_author_cmd.add_argument('--reason', help='Reason for revoking trust')
    untrust_author_cmd.add_argument('--reason-file', help='Alternative UTF-8 file containing the reason')
    untrust_author_cmd.add_argument('--operator', '--operator-id', dest='operator', required=True, help='Non-secret operator identifier')
    commands.add_parser('list-trusted', help='List currently trusted authors with the service stopped')
    commands.add_parser('verify-chain', help='Recompute every record digest, chain head and anchor binding')
    anchoring = commands.add_parser('anchor', help='One OpenTimestamps pass: upgrade pending proofs, stamp a new head if due')
    anchoring.add_argument('--ots', required=True, help='Path to the pinned ots client (requirements-anchor.txt)')
    anchoring.add_argument('--min-hours', type=float, default=20, help='Minimum hours between new stamps')
    mirroring = commands.add_parser('mirror-export', help='Write a verifiable public mirror of this instance to a NEW directory')
    mirroring.add_argument('directory')
    pulling = commands.add_parser('mirror-pull', help='Copy the public mirror of a site into a NEW directory and verify it')
    pulling.add_argument('origin', help='Site root, e.g. https://eksmu.com')
    pulling.add_argument('directory')
    commands.add_parser('verify-mirror', help='Offline consistency check of a mirror directory').add_argument('directory')
    analysing = commands.add_parser('run-analysis-queue', help='Run queued author-requested AI analyses and AI topic cataloging (needs requirements-analysis.txt)')
    analysing.add_argument('--config', help='Operator config with ANTHROPIC_API / OPENAI_API / DEEPSEEK_API keys')
    analysing.add_argument('--key-file', help='Alternative: file holding one model API key (never logged)')
    analysing.add_argument('--worker-key', help='X25519 key file for author-supplied API keys (created 0600)')
    analysing.add_argument('--budget-usd', type=float, default=10.0, help='Hard monthly spending cap')
    analysing.add_argument('--model', help='Platform model with --key-file (default claude-haiku-5-5)')
    analysing.add_argument('--max-reports', type=int, default=5, help='Reports per run')
    analysing.add_argument('--no-catalog', action='store_true', help='Skip automatic AI topic and keyword cataloging')
    analysing.add_argument('--no-auto-label', action='store_true',
                           help='Record AI promotion triage without automatically applying pending_review labels')
    analysing.add_argument('--promotion-threshold', type=float, default=None,
                           help='Confidence threshold (0-1, default 0.8) for AI promotion triage auto-labeling')
    lean_queue = commands.add_parser('run-lean-queue', help='Run one opted-in private Lean attachment with the service stopped')
    lean_queue.add_argument('--offline', action='store_true', help='Confirm all instance writers are stopped')
    compile_queue = commands.add_parser('run-compile-queue', help='Dispatch and poll isolated LaTeX compilation jobs')
    compile_queue.add_argument('--limit', type=int, default=10, help='Maximum queued jobs to dispatch in one pass')
    compile_queue.add_argument('--local', action='store_true', help='Run local TeX compilation without Cloud Run/GCS')
    migrate = commands.add_parser('migrate-blobs', help='Offline migration of SQLite attachment BLOBs to the configured blobstore')
    migrate.add_argument('--offline', action='store_true', help='Confirm all instance writers are stopped')
    migrate.add_argument('--max-rows', type=int, default=None, help='Optional maximum rows to migrate in one pass')
    reconcile_mat = commands.add_parser('reconcile-materials', help='Verify research attachment bytes and commit material records to record_chain')
    reconcile_mat.add_argument('--offline', action='store_true', help='Acquire exclusive instance lock when writers are stopped')
    redact_att = commands.add_parser('redact-attachment', help='Remove attachment bytes for privacy/compliance while preserving record_chain integrity')
    redact_att.add_argument('--attachment', required=True, help='Attachment ID to redact')
    redact_att.add_argument('--reason-code', choices=['privacy', 'copyright', 'illegal', 'author_request', 'other'], required=True)
    redact_att.add_argument('--reason', help='Public compliance reason')
    redact_att.add_argument('--reason-file', help='Alternative UTF-8 file containing the public compliance reason')
    redact_att.add_argument('--operator', '--operator-id', dest='operator', required=True, help='Non-secret operator identifier')
    redact_att.add_argument('--offline', action='store_true', help='Acquire exclusive instance lock when writers are stopped')
    args = parser.parse_args()
    os.umask(0o077)
    db = Path(args.db)
    try:
        if args.command == 'redact-attachment':
            from .research import Research
            if getattr(args, 'reason', None) is not None:
                reason = args.reason
            elif getattr(args, 'reason_file', None):
                reason = Path(args.reason_file).read_text(encoding='utf-8')
            else:
                raise Problem('Give --reason or --reason-file')
            if not db.is_file():
                raise Problem('Instance database is missing')
            sidecar = Path(str(db) + '.research.sqlite')
            if not sidecar.is_file():
                raise Problem('Research database is missing')
            with InstanceLock(db, exclusive=bool(args.offline)):
                store = Store(db)
                try:
                    research = Research(sidecar, store.rows())
                    try:
                        print(canonical(research.redact_attachment(args.attachment, args.reason_code, reason, args.operator)))
                    finally:
                        research.close()
                finally:
                    store.close()
            return
        if args.command == 'reconcile-materials':
            from .material_chain import reconcile_instance
            print(canonical(reconcile_instance(db, offline=args.offline)))
            return
        if args.command == 'run-compile-queue':
            from .compile_worker import run_compile_queue
            print(canonical(run_compile_queue(db, limit=args.limit, local_mode=args.local)))
            return
        if args.command == 'migrate-blobs':
            from .blobstore import migrate_blobs
            print(canonical(migrate_blobs(db, max_rows=args.max_rows)))
            return
        if args.command == 'verify-publication':
            from .publication_contract import MAX_PACKAGE, verify_package
            with open(args.file, 'rb') as source:
                raw = source.read(MAX_PACKAGE + 1)
            if len(raw) > MAX_PACKAGE:
                raise Problem('Publication package exceeds 128 MiB')
            print(canonical(verify_package(load_json(raw), args.expect_sha256)))
            return
        if args.command in {'create-time-evidence', 'verify-time-evidence'}:
            from .publication_contract import MAX_PACKAGE
            from .time_evidence import create, verify
            source_path = args.publication_package if args.command == 'create-time-evidence' else args.evidence_file
            with open(source_path, 'rb') as source:
                raw = source.read(MAX_PACKAGE + 65536 + 1)
            if len(raw) > MAX_PACKAGE + 65536:
                raise Problem('Time evidence exceeds the local size limit')
            document = load_json(raw)
            if args.command == 'create-time-evidence':
                evidence = create(document)
                with open(args.evidence_file, 'x', encoding='utf-8') as output:
                    output.write(canonical(evidence) + '\n')
                print(canonical(verify(evidence)))
            else:
                print(canonical(verify(document, args.expect_commitment)))
            return
        if args.command in {'mirror-pull', 'verify-mirror'}:
            from .mirror import pull, verify_mirror
            print(canonical(pull(args.origin, args.directory) if args.command == 'mirror-pull' else verify_mirror(args.directory)))
            return
        if args.command == 'run-analysis-queue':
            from .analysis_worker import run_queue
            if not db.is_file():
                raise Problem('Instance database is missing')
            if not args.config and not args.key_file:
                raise Problem('Give --config or --key-file')
            print(canonical(run_queue(db, args.key_file, args.budget_usd, args.max_reports, model=args.model,
                                      config=args.config, worker_key=args.worker_key, catalog=not args.no_catalog,
                                      auto_label=False if args.no_auto_label else None,
                                      promotion_threshold=args.promotion_threshold)))
            return
        if args.command == 'mirror-export':
            from .mirror import write_local
            if not db.is_file():
                raise Problem('Instance database is missing')
            with InstanceLock(db, exclusive=False):
                store = Store(db)
                try:
                    print(canonical(write_local(store, db, args.directory)))
                finally:
                    store.close()
            return
        if args.command in {'verify-chain', 'anchor'}:
            if not db.is_file():
                raise Problem('Instance database is missing')
            if args.command == 'verify-chain':
                from .record_chain import verify
                connection = sqlite3.connect(db.resolve().as_uri() + '?mode=ro', uri=True)
                try:
                    print(canonical(verify(connection)))
                finally:
                    connection.close()
            else:
                from datetime import timedelta
                from .chain_anchor import run
                report = run(db, args.ots, min_interval=timedelta(hours=args.min_hours))
                print(canonical(report))
                if report['errors']:
                    raise SystemExit(2)
            return
        if args.command == 'reconcile-accounts':
            from .account_recovery import reconcile_accounts
            print(canonical(reconcile_accounts(db, args.from_db, offline=args.offline)))
            return
        if args.command in {'backup', 'verify-backup', 'restore'}:
            from .backup import create_backup, verify_backup, restore_backup
            if args.command == 'restore':
                restored = restore_backup(args.directory, args.into, secrets_dir=args.secrets_dir)
                print(canonical({'restored': True, 'database': str(restored), 'service_started': False,
                                 'account_review_required': True}))
            else:
                checked = (create_backup(db, args.directory, offline=args.offline, secrets_dir=args.secrets_dir, portable_blobs=args.portable_blobs)
                           if args.command == 'backup' else verify_backup(args.directory, secrets_dir=args.secrets_dir))
                print(canonical({k: v for k, v in checked.items() if k != 'manifest'} | {
                    'backup_id': checked['manifest']['id'],
                    'components': {k: v is not None for k, v in checked['manifest']['components'].items()}}))
            return
        if args.command in {'behavior-queue', 'review-behavior', 'review-behavior-appeal'}:
            from .community import Community
            with InstanceLock(db, exclusive=True):
                if not db.is_file():
                    raise Problem('Instance database is missing')
                store = Store(db)
                try:
                    sidecar = Path(str(db) + '.community.sqlite')
                    if not sidecar.is_file():
                        raise Problem('Community database is missing')
                    hub = Community(sidecar, store.rows())
                    try:
                        if args.command == 'behavior-queue':
                            rows = hub.db.execute('SELECT id,reporter,target,target_author,category,reason,created_at FROM hub_behavior_cases ORDER BY rowid').fetchall()
                            items = []
                            for row in rows:
                                case = dict(zip(('id','reporter','target','target_author','category','reason','created_at'), row))
                                case['events'] = [dict(zip(('id','actor','kind','body','created_at'), event)) for event in
                                                  hub.db.execute('SELECT id,actor,kind,body,created_at FROM hub_behavior_events WHERE case_id=? ORDER BY rowid', (case['id'],))]
                                items.append(case)
                            print(canonical({'private': True, 'items': items}))
                        elif args.command == 'review-behavior':
                            reason = Path(args.reason_file).read_text(encoding='utf-8')
                            print(canonical(hub.review_behavior(args.case_id, args.outcome, reason, args.operator_id)))
                        else:
                            reason = Path(args.reason_file).read_text(encoding='utf-8')
                            print(canonical(hub.review_behavior_appeal(args.case_id, args.outcome, reason, args.operator_id)))
                    finally:
                        hub.close()
                finally:
                    store.close()
            return
        if args.command in {'label-work', 'retract-label', 'label-queue', 'review-label-appeal',
                            'trust-author', 'untrust-author', 'list-trusted'}:
            from .community import Community
            with InstanceLock(db, exclusive=True):
                if not db.is_file():
                    raise Problem('Instance database is missing')
                store = Store(db)
                try:
                    sidecar = Path(str(db) + '.community.sqlite')
                    if args.command not in {'label-work', 'trust-author'} and not sidecar.is_file():
                        if args.command == 'label-queue':
                            print(canonical({'pending_review': [], 'pending_appeals': [], 'trusted_authors': []}))
                            return
                        if args.command == 'list-trusted':
                            print(canonical({'trusted_authors': []}))
                            return
                        raise Problem('Community database is missing')
                    hub = Community(sidecar, store.rows())
                    try:
                        if args.command == 'label-queue':
                            print(canonical(hub.label_queue()))
                        elif args.command == 'list-trusted':
                            print(canonical(hub.list_trusted()))
                        else:
                            if getattr(args, 'reason', None) is not None:
                                reason = args.reason
                            elif getattr(args, 'reason_file', None):
                                reason = Path(args.reason_file).read_text(encoding='utf-8')
                            else:
                                raise Problem('Give --reason or --reason-file')
                            if args.command == 'label-work':
                                print(canonical(hub.label_work(
                                    args.exploration, args.label, reason, args.operator, source=args.source
                                )))
                            elif args.command == 'retract-label':
                                print(canonical(hub.retract_label(args.label_id, reason, args.operator)))
                            elif args.command == 'trust-author':
                                print(canonical(hub.trust_author(args.actor, reason, args.operator)))
                            elif args.command == 'untrust-author':
                                print(canonical(hub.untrust_author(args.actor, reason, args.operator)))
                            else:
                                print(canonical(hub.review_label_appeal(
                                    args.appeal_id, args.decision, reason, args.operator
                                )))
                    finally:
                        hub.close()
                finally:
                    store.close()
            return
        if args.command == 'run-lean-queue':
            if not args.offline:
                raise Problem('Stop all writers and pass --offline before running a private Lean task')
            from .research import Research
            from .lean_sandbox import run as run_lean
            import tempfile
            with InstanceLock(db, exclusive=True):
                if not db.is_file():
                    raise Problem('Instance database is missing')
                store = Store(db)
                try:
                    sidecar = Path(str(db) + '.research.sqlite')
                    if not sidecar.is_file():
                        raise Problem('Research database is missing')
                    research = Research(sidecar, store.rows())
                    try:
                        job = research.next_lean_job()
                        if job is None:
                            print(canonical({'queued': False, 'executed': False}))
                        else:
                            with tempfile.TemporaryDirectory(prefix='eksmu-lean-queue-') as directory:
                                source = Path(directory) / 'Proof.lean'
                                source.write_bytes(job['source'])
                                report = run_lean(source, job['request']['target'])
                            if report['status'] == 'environment_failed':
                                raise Problem('Lean isolation was unavailable; task remains queued', 503)
                            print(canonical(research.record_lean_run(job['id'], report)))
                    finally:
                        research.close()
                finally:
                    store.close()
            return
        db.parent.mkdir(parents=True, exist_ok=True)
        if args.command == "serve":
            server = make_server(db, args.port, verification=args.with_verification,
                                 public_origin=args.public_origin)
            print(f"Local PRIVATE prototype: http://127.0.0.1:{server.server_port}", flush=True)
            print("No public deployment, AI/Lean execution or financial operations. Local arithmetic requires separate task consent.", flush=True)
            try:
                server.serve_forever()
            except KeyboardInterrupt:
                pass
            finally:
                server.server_close()
        else:
            lease = InstanceLock(db)
            store = None
            try:
                store = Store(db)
                if args.command == "export":
                    with open(args.file, "x", encoding="utf-8") as output:
                        output.write(canonical(store.export()) + "\n")
                    print("Exported records only. Keep identity keys separately and privately.")
                else:
                    with open(args.file, "rb") as source:
                        raw = source.read(MAX_BUNDLE_BYTES + 1)
                    if len(raw) > MAX_BUNDLE_BYTES:
                        raise Problem("Bundle exceeds the 16 MiB prototype limit")
                    store.import_bundle(load_json(raw))
                    print("Imported into empty database. Checksums are not proof of authorship or truth.")
            finally:
                if store is not None:
                    store.close()
                lease.close()
    except sqlite3.Error:
        parser.exit(1, 'Error: SQLite maintenance failed; stop all writers and check the source databases.\n')
    except (Problem, OSError, ValueError) as exc:
        parser.exit(1, f"Error: {exc}\n")


if __name__ == "__main__":
    main()
