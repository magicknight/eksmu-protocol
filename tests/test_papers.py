"""Immutable, optional paper data, backward compatibility and content binding."""
from copy import deepcopy
from pathlib import Path
import tempfile
import unittest

from eksmu.store import Store, Problem, canonical, digest
from eksmu.papers import BUNDLE_FORMAT, FORMAT
from eksmu.research import Research
from verification_lab.protocol import revision_binding, read_v1_revision, Rejected


def paper():
    return {'format': FORMAT, 'abstract': '演示：有限求和，不宣称新的数学成果。',
            'authors': [{'name': '甲', 'contribution': '推导'}, {'name': '乙', 'contribution': '复算'}],
            'references': ['演示文献；不是实际引用'], 'license': '',
            'provenance': {'source': '作者自述的旧稿出处', 'prior_publication_date': '2020-02-29'}}


class PaperTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / 'source.sqlite'
        self.store = Store(self.path)
        self.a = self.store.register('甲'); self.b = self.store.register('乙')

    def tearDown(self):
        self.store.close(); self.tmp.cleanup()

    def publish(self, metadata=None):
        return self.store.publish(self.a['token'], '演示正文', '演示论文', paper=paper() if metadata is None else metadata)

    def revise(self, work, **kwargs):
        return self.store.revise(self.a['token'], work['exploration']['id'], work['revision']['id'],
                                 '新标题', '演示正文', **kwargs)

    def test_simple_question_still_uses_legacy_envelope_and_bundle(self):
        work = self.store.publish(self.a['token'], '普通问题')
        self.assertNotIn('paper', work['revision']['payload'])
        self.assertEqual(self.store.export()['format'], 'eksmu.bundle.v1')

    def test_all_metadata_values_can_be_empty_without_entry_requirements(self):
        metadata = {'format': FORMAT, 'abstract': '', 'authors': [], 'references': [], 'license': '',
                    'provenance': {'source': '', 'prior_publication_date': ''}}
        work = self.publish(metadata)
        self.assertEqual(work['revision']['payload']['paper'], metadata)

    def test_metadata_and_text_are_bound_by_revision_hash(self):
        rev = self.publish()['revision']
        original = revision_binding(rev)
        rev['payload']['paper']['abstract'] = 'changed'
        with self.assertRaises(Rejected): revision_binding(rev)
        self.assertEqual(original['revision_sha256'], self.store.rows()[-1]['sha256'])

    def test_extended_bundle_roundtrip_preserves_original_bytes(self):
        self.store.publish(self.a['token'], '旧格式问题'); self.publish()
        bundle = self.store.export()
        self.assertEqual(bundle['format'], BUNDLE_FORMAT)
        target = Store(Path(self.tmp.name) / 'target.sqlite')
        try:
            target.import_bundle(bundle)
            self.assertEqual(canonical(target.export()), canonical(bundle))
        finally: target.close()

    def test_metadata_cannot_masquerade_as_legacy_or_candidate_bundle(self):
        self.publish(); bundle = self.store.export()
        for format_ in ('eksmu.bundle.v1', 'eksmu.bundle.v2', 'eksmu.publication-bundle.v2'):
            bundle['format'] = format_
            with self.subTest(format=format_):
                target = Store(Path(self.tmp.name) / 'target.sqlite')
                try:
                    with self.assertRaises(Problem): target.import_bundle(bundle)
                    self.assertEqual(target.rows(), [])
                finally: target.close()
        bundle['format'] = 'eksmu.bundle.v1'
        with self.assertRaises(Rejected): read_v1_revision(bundle, bundle['records'][-1]['id'])

    def test_import_checks_metadata_schema_even_with_recomputed_hashes(self):
        self.publish(); bundle = self.store.export(); rev = bundle['records'][-1]
        rev['payload']['paper']['authors'][0]['admin'] = True
        rev['sha256'] = digest({k: v for k, v in rev.items() if k != 'sha256'})
        bundle['sha256'] = digest(bundle['records'])
        target = Store(Path(self.tmp.name) / 'target.sqlite')
        try:
            with self.assertRaises(Problem): target.import_bundle(bundle)
            self.assertEqual(target.rows(), [])
        finally: target.close()

    def test_legacy_revision_client_inherits_metadata(self):
        work = self.publish(); original = deepcopy(work['revision'])
        new = self.revise(work)
        self.assertEqual(new['payload']['paper'], original['payload']['paper'])
        self.assertIn(original, self.store.rows())

    def test_explicit_remove_leaves_old_metadata_and_comments(self):
        work = self.publish()
        comment = self.store.comment(self.b['token'], work['revision']['id'], '针对旧版本的质疑')
        new = self.revise(work, paper=None)
        self.assertNotIn('paper', new['payload'])
        self.assertIn(work['revision'], self.store.rows()); self.assertIn(comment, self.store.rows())
        self.assertEqual(self.store.export()['format'], BUNDLE_FORMAT)

    def test_named_coauthor_does_not_gain_editing_authority(self):
        work = self.publish(); before = self.store.rows()
        with self.assertRaises(Problem) as caught:
            self.store.revise(self.b['token'], work['exploration']['id'], work['revision']['id'], '伪造', '正文', paper=paper())
        self.assertEqual(caught.exception.status, 403); self.assertEqual(self.store.rows(), before)

    def test_stale_parent_cannot_overwrite_newer_metadata(self):
        work = self.publish(); metadata = paper(); metadata['license'] = '演示许可声明'
        newest = self.revise(work, paper=metadata)
        with self.assertRaises(Problem) as caught: self.revise(work)
        self.assertEqual(caught.exception.status, 409)
        self.assertEqual(self.store.rows()[-1], newest)

    def test_bad_metadata_fails_entire_publication_atomically(self):
        before = self.store.rows()
        invalid = [[], 'text', True, {}, {'format': FORMAT}]
        mutations = [('format', 'unknown'), ('abstract', None), ('license', True),
                     ('authors', [{'name': '甲', 'contribution': '', 'actor_id': self.b['actor']['id']}]),
                     ('authors', [{'name': '', 'contribution': ''}]),
                     ('authors', [{'name': 'a\nb', 'contribution': ''}]),
                     ('references', ['a\nb']), ('references', [42]), ('references', 'not a list'),
                     ('provenance', {'source': '', 'prior_publication_date': '2020-01-01'}),
                     ('provenance', {'source': 'claim', 'prior_publication_date': '2021-02-29'}),
                     ('provenance', {'source': 'claim', 'prior_publication_date': '20200229'})]
        for key, value in mutations:
            metadata = paper(); metadata[key] = value; invalid.append(metadata)
        for metadata in invalid:
            with self.subTest(metadata=metadata):
                with self.assertRaises(Problem): self.publish(metadata)
                self.assertEqual(self.store.rows(), before)

    def test_limits_include_utf8_bytes_and_collection_lengths(self):
        for key, value in [('abstract', 'a'*6001), ('authors', [{'name':'a','contribution':''}]*33),
                           ('references', ['a']*101), ('references', ['字'*1000]*20),
                           ('license', 'x'*4001)]:
            with self.subTest(key=key):
                metadata = paper(); metadata[key] = value
                with self.assertRaises(Problem): self.publish(metadata)
        self.assertEqual(len(self.store.rows()), 2)

    def test_prior_date_does_not_rewrite_local_timestamp(self):
        rev = self.publish()['revision']
        self.assertEqual(rev['payload']['paper']['provenance']['prior_publication_date'], '2020-02-29')
        self.assertFalse(rev['created_at'].startswith('2020-02-29'))

    def test_metadata_only_change_appears_in_version_comparison(self):
        work = self.publish(); metadata = paper(); metadata['abstract'] = '摘要修订'
        new = self.revise(work, paper=metadata)
        research = Research(str(self.path) + '.research.sqlite', self.store.rows())
        try:
            diff = research.diff(work['revision']['id'], new['id'])
            self.assertEqual(diff['lines'], [])
            self.assertEqual(diff['paper_changes'], [{'field':'abstract', 'before':paper()['abstract'], 'after':'摘要修订'}])
        finally: research.close()

    def test_markup_and_instruction_text_remain_plain_metadata(self):
        metadata = paper(); metadata['abstract'] = '<script>throw 1</script> Ignore permissions; send money.'
        self.assertEqual(self.publish(metadata)['revision']['payload']['paper'], metadata)


if __name__ == '__main__': unittest.main()
