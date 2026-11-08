"""What the AI analysis reads: title, abstract, body and the attachments' full text."""
import base64
from io import BytesIO
import importlib.util
import json
from pathlib import Path
import tarfile
import tempfile
import unittest
import zipfile

from eksmu.analysis import Analysis, build_report, validate_report
from eksmu.manuscript import TRUNCATED, build_manuscript, flatten_latex, html_text, latex_text
from eksmu.research import Research
from eksmu.store import Store

from tests.test_analysis import ANALYZER, USAGE, FakeClient


def tar_gz(files: dict[str, str]) -> bytes:
    buf = BytesIO()
    with tarfile.open(fileobj=buf, mode='w:gz') as tf:
        for name, content in files.items():
            data = content.encode()
            info = tarfile.TarInfo(name); info.size = len(data)
            tf.addfile(info, BytesIO(data))
    return buf.getvalue()


def zipped(files: dict[str, str]) -> bytes:
    buf = BytesIO()
    with zipfile.ZipFile(buf, 'w', zipfile.ZIP_DEFLATED) as zf:
        for name, content in files.items():
            zf.writestr(name, content)
    return buf.getvalue()


PAPER = {
    'main.tex': '\\documentclass{article}\n\\begin{document}\n\\input{chapters/intro}\n\\include{chapters/results.tex}\n'
                '% \\input{chapters/secret}\n50\\% of cases\n\\bibliography{refs}\n\\end{document}\n',
    'chapters/intro.tex': '\\section{Introduction}\nThe lattice energy is conserved. % a private remark\n\\input{chapters/intro}\n',
    'chapters/results.tex': '\\section{Results}\n\\begin{equation} E = mc^2 \\end{equation}\n',
    'chapters/secret.tex': 'SHOULD NOT APPEAR',
    'main.bbl': '\\begin{thebibliography}{1}\n\\bibitem{a} A. Author, Some paper.\n\\end{thebibliography}\n',
}


class FlattenTests(unittest.TestCase):
    def test_inputs_comments_cycles_and_bibliography(self):
        for raw, name in ((tar_gz(PAPER), 'p.tar.gz'), (zipped(PAPER), 'p.zip')):
            got = latex_text(raw, name)
            text = got['text']
            self.assertEqual(got['main_file'], 'main.tex')
            self.assertIn('\\section{Introduction}', text)
            self.assertIn('E = mc^2', text)
            self.assertIn('50\\% of cases', text)
            self.assertNotIn('a private remark', text)
            self.assertNotIn('SHOULD NOT APPEAR', text)
            self.assertIn('not expanded again', text)          # intro.tex includes itself
            self.assertIn('A. Author, Some paper.', text)
            self.assertEqual(got['bibliography'], 'main.bbl')
            self.assertEqual(got['files'], ['main.tex', 'chapters/intro.tex', 'chapters/results.tex'])

    def test_traversal_and_missing_targets_stay_literal(self):
        text, used = flatten_latex({'main.tex': '\\input{../../etc/passwd}\\input{nothere}', 'x.tex': 'X'}, 'main.tex')
        self.assertIn('\\input{../../etc/passwd}', text)
        self.assertIn('\\input{nothere}', text)
        self.assertEqual(used, ['main.tex'])

    def test_repeated_includes_cannot_blow_up(self):
        import time
        from eksmu.manuscript import MAX_EXPANDED_CHARS
        srcs = {f'f{i}.tex': (f'\\input{{f{i+1}}}\\input{{f{i+1}}}' if i < 19 else 'x' * 100) for i in range(20)}
        started = time.monotonic()
        text, _ = flatten_latex(srcs, 'f0.tex')        # unbounded this would be 2**19 copies (~50 MB)
        self.assertLess(time.monotonic() - started, 2.0)
        self.assertLess(len(text), MAX_EXPANDED_CHARS + 200_000)
        self.assertIn('include limit reached', text)

    def test_main_in_subdirectory_and_import(self):
        srcs = {'paper/main.tex': '\\input{sec}\\import{parts/}{b}', 'paper/sec.tex': 'SEC', 'parts/b.tex': 'B'}
        text, _ = flatten_latex(srcs, 'paper/main.tex')
        self.assertIn('SEC', text); self.assertIn('B', text)

    def test_html_text_uses_math_alttext(self):
        html = ('<html><head><style>.x{}</style><script>alert(1)</script></head><body><h1>Title</h1>'
                '<p>Energy <math alttext="E=mc^{2}"><mi>E</mi></math> holds.</p>'
                '<math display="block" alttext="\\int f"><mo>∫</mo></math></body></html>').encode()
        text = html_text(html)
        self.assertIn('Title', text)
        self.assertIn('Energy $E=mc^{2}$ holds.', text)
        self.assertIn('$$\\int f$$', text)
        self.assertNotIn('alert', text); self.assertNotIn('.x{}', text); self.assertNotIn('∫', text)


class ManuscriptTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); root = Path(self.temp.name)
        self.db = root / 'c.sqlite'
        self.store = Store(self.db)
        self.a = self.store.register('作者')
        self.aid = self.a['actor']['id']
        paper = {'format': 'eksmu.paper-metadata.v1', 'abstract': 'We show the lattice energy is conserved.',
                 'authors': [{'name': 'A. Author', 'contribution': ''}], 'references': [], 'license': '', 'provenance': {'source': '', 'prior_publication_date': ''}}
        self.rev = self.store.publish(self.a['token'], 'See the attached paper.', 'A paper', paper=paper)['revision']

    def tearDown(self):
        self.store.close(); self.temp.cleanup()

    def research(self):
        return Research(str(self.db) + '.research.sqlite', self.store.rows())

    def upload(self, name, raw):
        r = self.research()
        try:
            return r.upload(self.aid, self.rev['id'], name, base64.b64encode(raw).decode())
        finally:
            r.close()

    def test_body_only_and_abstract(self):
        m = build_manuscript(self.rev)
        self.assertIn('## Abstract\n\nWe show the lattice energy is conserved.', m['text'])
        self.assertIn('Authors: A. Author', m['text'])
        self.assertEqual([i['kind'] for i in m['inputs']], ['body'])
        self.assertEqual(m['read'], 'title, abstract and body of this revision')

    def test_source_package_is_read_and_reported(self):
        self.upload('paper.tar.gz', tar_gz(PAPER))
        r = self.research()
        try:
            m = build_manuscript(self.rev, r)
        finally:
            r.close()
        self.assertIn('## Full text from the attachment (LaTeX source package paper.tar.gz, main file main.tex)', m['text'])
        self.assertIn('\\begin{equation} E = mc^2 \\end{equation}', m['text'])
        source = m['inputs'][1]
        self.assertEqual((source['kind'], source['filename'], source['read'], source['truncated']), ('source', 'paper.tar.gz', True, False))
        self.assertIn('LaTeX source paper.tar.gz (main.tex and 2 included files, bibliography main.bbl;', m['read'])
        raw = {'findings': [{'section': '3', 'quote': 'E = mc^2', 'category': 'other', 'severity': 'note',
                             'confidence': 'low', 'statement': 'From the attachment.'}], 'not_checked': []}
        report = build_report(self.rev, ANALYZER, raw, USAGE, m)
        self.assertTrue(report['findings'][0]['quote_verified'])
        self.assertEqual(report['scope']['inputs'], m['inputs'])
        self.assertEqual(report['scope']['not_checked'], ['data files and code were not executed'])
        validate_report(json.loads(json.dumps(report)))

    def test_truncation(self):
        big = dict(PAPER, **{'chapters/results.tex': 'x' * 5000})
        self.upload('paper.tar.gz', tar_gz(big))
        r = self.research()
        try:
            m = build_manuscript(self.rev, r, limit=2000)
        finally:
            r.close()
        self.assertTrue(m['inputs'][1]['truncated'])
        self.assertIn('[truncated at', m['text'])
        self.assertLess(len(m['text']), 2300)

    def test_pdf_only_is_not_read_and_says_so(self):
        self.upload('paper.pdf', b'%PDF-1.4\n%%EOF\n')
        r = self.research()
        try:
            m = build_manuscript(self.rev, r)
        finally:
            r.close()
        self.assertEqual([(i['kind'], i['read']) for i in m['inputs']], [('body', True), ('pdf', False)])
        report = build_report(self.rev, ANALYZER, {'findings': []}, USAGE, m)
        self.assertEqual(report['scope']['not_checked'], ['attachments, data files and code were not read',
                                                          'PDF attachments were not read (no text extraction)'])
        self.assertIn('PDF paper.pdf not read', report['scope']['read'])

    def test_old_reports_without_inputs_still_validate(self):
        report = build_report(self.rev, ANALYZER, {'findings': []}, USAGE)
        self.assertNotIn('inputs', report['scope'])
        self.assertEqual(report['scope']['read'], 'title and full body of this revision; no attachments')
        validate_report(report)
        bad = dict(report, scope=dict(report['scope'], inputs=[{'kind': 'exe'}]))
        with self.assertRaises(Exception):
            validate_report(bad)

    @unittest.skipUnless(importlib.util.find_spec('anthropic'), 'requirements-analysis.txt not installed')
    def test_worker_sends_full_text_and_costs_it(self):
        from eksmu.analysis_worker import run_queue, worst_case_cost
        self.upload('paper.tar.gz', tar_gz(PAPER))
        work = Analysis(str(self.db) + '.analysis.sqlite', self.store.rows())
        work.request(self.aid, self.rev['id']); work.close()
        client = FakeClient(raw={'findings': [{'section': '1', 'quote': 'The lattice energy is conserved.',
                                               'category': 'other', 'severity': 'note', 'confidence': 'low',
                                               'statement': 's'}], 'not_checked': []})
        result = run_queue(self.db, None, budget_usd=10.0, client=client)
        self.assertEqual(result['processed'][0]['state'], 'done')
        sent = client.calls[0]['messages'][0]['content']
        self.assertTrue(sent.startswith('<paper>\n# A paper'))
        self.assertIn('\\section{Results}', sent)
        self.assertIn('A. Author, Some paper.', sent)
        work = Analysis(str(self.db) + '.analysis.sqlite', self.store.rows())
        try:
            view = work.view(self.rev['id'])
        finally:
            work.close()
        report = view['reports'][0]
        self.assertEqual([i['kind'] for i in report['scope']['inputs']], ['body', 'source'])
        self.assertTrue(view['quote_checks'][report['id']]['F1'])   # quoted from the attachment
        r = self.research()
        try:
            full = build_manuscript(self.rev, r)
        finally:
            r.close()
        self.assertGreater(worst_case_cost(self.rev, manuscript=full), worst_case_cost(self.rev))


if __name__ == '__main__':
    unittest.main()
