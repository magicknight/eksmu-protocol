from copy import deepcopy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from eksmu.store import Store, Problem
from eksmu.community import Community, topic_name, topics_list
from eksmu.research import Research


class CommunityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.store = Store(self.root / 'records.sqlite'); self.addCleanup(self.store.close)
        self.alice = self.store.register('初学者'); self.bob = self.store.register('同行者'); self.cara = self.store.register('第三位')
        self.a = self.alice['actor']['id']; self.b = self.bob['actor']['id']; self.c = self.cara['actor']['id']
        self.work = self.store.publish(self.alice['token'], '探索一个分数问题', '分数与直觉')
        self.wid = self.work['exploration']['id']; self.rev = self.work['revision']['id']
        self.hubs = []; self.addCleanup(lambda: [h.close() for h in self.hubs])

    def hub(self):
        h = Community(self.root / 'social.sqlite', self.store.rows()); self.hubs.append(h); return h

    def detail(self, h=None, **updates):
        values = dict(actor_id=self.a, exploration_id=self.wid, topics=['数学', 'AI'], help='一起找反例', expected_version=0)
        values.update(updates); return (h or self.hub()).set_details(**values)

    def reply(self, who=None, body='我有一个想法', reply_to=None):
        return self.store.comment((who or self.bob)['token'], self.rev, body, reply_to)

    def test_anonymous_reader_sees_unreviewed_works(self):
        x = self.hub().feed()['items'][0]
        self.assertEqual(x['title'], '分数与直觉'); self.assertEqual(x['reports'], 0)
        self.assertEqual(x['topics'], []); self.assertFalse(x['following'])

    def test_feed_excerpt_is_plain_text_not_markdown_source(self):
        self.store.publish(self.bob['token'], '## 背景\n\n设 $x^2$ 为 **正数**，见 [说明](https://example.org)。\n\n```py\nprint(1)\n```\n- 第一项', '格式测试')
        excerpt = self.hub().feed()['items'][0]['excerpt']
        self.assertEqual(excerpt, '背景 设 x^2 为 正数，见 说明。 第一项')

    def test_behavior_feedback_is_private_and_does_not_change_publication(self):
        h = self.hub(); before = self.store.export()
        case = h.submit_behavior(self.b, self.rev, 'privacy', '这里可能包含私人地址，请复核')
        self.assertEqual(case['state'], 'pending')
        self.assertEqual(h.submit_behavior(self.b, self.rev, 'privacy', '重复发送')['id'], case['id'])
        self.assertEqual(len(h.behavior_cases(self.b)['items']), 1)
        self.assertNotIn('reporter', h.behavior_cases(self.a)['items'][0])
        with self.assertRaises(Problem) as denied:
            h.behavior_case(self.c, case['id'])
        self.assertEqual(denied.exception.status, 403)
        h.respond_behavior(self.a, case['id'], '相关内容是测试占位，请查看具体上下文')
        with self.assertRaises(Problem): h.respond_behavior(self.b, case['id'], '冒充作者')
        h.review_behavior(case['id'], 'follow_up', '需要维护者线下核对，尚未限制内容')
        self.assertEqual(h.behavior_case(self.b, case['id'])['state'], 'reviewed')
        self.assertEqual(self.store.export(), before)
        self.assertEqual(h.feed()['total'], 1)

    def test_behavior_report_requires_existing_target_and_has_bounded_quota(self):
        comment = self.reply()
        h = self.hub()
        for target in (self.wid, self.a, '00000000-0000-0000-0000-000000000000'):
            with self.assertRaises(Problem): h.submit_behavior(self.b, target, 'spam', '刷屏')
        with patch('eksmu.community.MAX_BEHAVIOR_CASES_PER_ACTOR', 1):
            first = h.submit_behavior(self.b, self.rev, 'spam', '重复内容')
            self.assertEqual(h.submit_behavior(self.b, self.rev, 'spam', '响应丢失后重试')['id'], first['id'])
            with self.assertRaises(Problem) as full:
                h.submit_behavior(self.b, comment['id'], 'spam', '另一条')
            self.assertEqual(full.exception.status, 413)
        self.store.publish(self.bob['token'], '仍可发表')

    def test_behavior_appeals_preserve_privacy_and_require_distinct_review_label(self):
        h = self.hub(); before = self.store.export()
        case = h.submit_behavior(self.b, self.rev, 'privacy', '合成隐私问题')
        with self.assertRaises(Problem) as early:
            h.appeal_behavior(self.b, case['id'], '请再核对')
        self.assertEqual(early.exception.status, 409)
        h.review_behavior(case['id'], 'no_action', '本轮无处理', 'operator_one')
        reporter_case = h.appeal_behavior(self.b, case['id'], '请复议上下文')
        self.assertEqual(reporter_case['state'], 'appeal_pending')
        author_case = h.behavior_case(self.a, case['id'])
        self.assertNotIn('reporter', author_case)
        self.assertNotIn(self.b, [event['actor'] for event in author_case['events']])
        self.assertEqual(author_case['events'][-1]['actor'], 'reporter')
        with self.assertRaises(Problem) as denied:
            h.appeal_behavior(self.c, case['id'], '冒充报告者')
        self.assertEqual(denied.exception.status, 403)
        with self.assertRaises(Problem) as same:
            h.review_behavior_appeal(case['id'], 'uphold', '维持', 'operator_one')
        self.assertEqual(same.exception.status, 403)
        h.review_behavior_appeal(case['id'], 'reopen', '重新核对', 'operator_two')
        with self.assertRaises(Problem):
            h.review_behavior_appeal(case['id'], 'uphold', '重复结案', 'operator_two')
        with self.assertRaises(Problem):
            h.appeal_behavior(self.b, case['id'], '重复复议')
        h.appeal_behavior(self.a, case['id'], '作者申请复议')
        self.assertEqual(h.behavior_case(self.b, case['id'])['state'], 'appeal_pending')
        h.review_behavior_appeal(case['id'], 'uphold', '维持原记录', 'operator_two')
        self.assertEqual(h.behavior_case(self.a, case['id'])['state'], 'reviewed')
        self.assertEqual(self.store.export(), before)

    def test_behavior_can_target_exact_attachment_and_collaboration_event(self):
        import base64
        research = Research(self.root / 'research.sqlite', self.store.rows())
        self.addCleanup(research.close)
        file = research.upload(self.a, self.rev, 'notes.txt', base64.b64encode(b'synthetic notes').decode())
        task = research.create_task(self.b, self.rev, '复查材料', '合成任务', 'reproduce')
        event_id = task['events'][0]['id']
        h = self.hub()
        file_case = h.submit_behavior(self.b, file['id'], 'privacy', '请检查这一附件', research)
        event_case = h.submit_behavior(self.a, event_id, 'harassment', '请检查这一协作记录', research)
        self.assertEqual(file_case['target_author'], self.a)
        self.assertEqual(event_case['target_author'], self.b)
        self.assertNotIn('reporter', h.behavior_case(self.a, file_case['id']))
        with self.assertRaises(Problem):
            h.submit_behavior(self.b, task['id'], 'spam', '任务容器不是具体事件', research)
        with self.assertRaises(Problem):
            h.submit_behavior(self.b, '00000000-0000-0000-0000-000000000000', 'spam', '不存在', research)

    def test_topics_normalize_unicode_and_deduplicate(self):
        self.assertEqual(topics_list(['ＡＩ', 'ai', ' 数学 ']), ['ai', '数学'])
        self.assertEqual(topic_name('  AI   for Science '), 'ai for science')
        for bad in [['x'] * 9, [''], ['a' * 40], [2], 'math']:
            with self.subTest(bad=bad), self.assertRaises(Problem): topics_list(bad)

    def test_introduction_is_optional_and_does_not_edit_article(self):
        before = self.store.export(); self.detail()
        self.assertEqual(self.store.export(), before)
        self.assertEqual(self.hub().feed(view='collaborate')['total'], 1)

    def test_only_author_edits_own_metadata(self):
        with self.assertRaises(Problem) as err: self.detail(actor_id=self.b)
        self.assertEqual(err.exception.status, 403)
        self.assertEqual(self.hub().details(self.wid)['version'], 0)

    def test_concurrent_metadata_edit_gets_conflict(self):
        h = self.hub(); self.detail(h)
        with self.assertRaises(Problem) as err: self.detail(h, help='stale')
        self.assertEqual(err.exception.status, 409)
        self.detail(h, help='new', expected_version=1)
        self.assertEqual(h.details(self.wid)['version'], 2)
        self.assertEqual(h.db.execute('SELECT COUNT(*) FROM hub_changes').fetchone()[0], 2)

    def test_metadata_clearing_is_supported(self):
        h = self.hub(); self.detail(h)
        self.detail(h, topics=[], help='', expected_version=1)
        self.assertEqual(h.feed(view='collaborate')['total'], 0)

    def test_no_minimum_reputation_for_collaboration(self):
        self.detail(); x = self.hub().feed(topic='ＡＩ', view='collaborate')['items'][0]
        self.assertEqual(x['author'], '初学者'); self.assertEqual(x['help'], '一起找反例')

    def test_search_covers_latest_body_topics_and_help(self):
        self.detail(); h = self.hub()
        for q in ['分数', 'ＡＩ', '反例', '分数 反例']:
            self.assertEqual(h.feed(q=q)['total'], 1)
        self.assertEqual(h.feed(q='不存在')['total'], 0)
        self.assertEqual(h.feed(q="' OR 1=1 --")['total'], 0)

    def test_search_does_not_return_superseded_title(self):
        self.store.revise(self.alice['token'], self.wid, self.rev, '另一个标题', '新的内容')
        h = self.hub()
        self.assertEqual(h.feed(q='分数')['total'], 0)
        self.assertEqual(h.feed(q='新的内容')['total'], 1)

    def test_topic_counts_explorations_not_profile_weight(self):
        self.detail(topics=['数学', '数学'])
        self.assertEqual(self.hub().topics()['items'], [{'name': '数学', 'explorations': 1}])

    def test_author_reply_does_not_remove_waiting_for_others(self):
        self.reply(self.alice)
        self.assertEqual(self.hub().feed(view='unanswered')['total'], 1)
        self.reply(); self.assertEqual(self.hub().feed(view='unanswered')['total'], 0)

    def test_critical_report_is_response_not_publication_gate(self):
        self.store.evidence(self.bob['token'], self.rev, 'human', 's', 'm', '不成立', status='challenged')
        h = self.hub(); self.assertEqual(h.feed()['total'], 1)
        self.assertEqual(h.feed(view='unanswered')['total'], 0)
        self.store.publish(self.alice['token'], '继续探索')

    def test_activity_order_uses_recent_record_not_popularity(self):
        second = self.store.publish(self.bob['token'], '另一个问题')['exploration']['id']
        self.reply()
        h = self.hub(); self.assertEqual(h.feed(order='latest')['items'][0]['id'], second)
        self.assertEqual(h.feed(order='active')['items'][0]['id'], self.wid)

    def test_pagination_has_no_duplicates_on_stable_snapshot(self):
        for i in range(25): self.store.publish(self.alice['token'], str(i))
        h = self.hub(); a, b = h.feed(limit=12), h.feed(limit=12, offset=12)
        self.assertEqual(a['total'], 26); self.assertEqual(a['next_offset'], 12)
        self.assertFalse({x['id'] for x in a['items']} & {x['id'] for x in b['items']})
        self.assertIsNone(h.feed(offset=24, limit=12)['next_offset'])

    def test_invalid_pagination_rejected(self):
        for kw in [{'limit': True}, {'limit': 1000}, {'offset': -1}, {'order': 'richest'}, {'view': 'elite'}]:
            with self.subTest(kw=kw), self.assertRaises(Problem): self.hub().feed(**kw)

    def test_personal_views_require_identity(self):
        for view in ['mine', 'following', 'bookmarks']:
            with self.subTest(view=view), self.assertRaises(Problem) as err: self.hub().feed(view=view)
            self.assertEqual(err.exception.status, 401)

    def test_bookmarks_persist_and_are_personal_not_public_votes(self):
        h = self.hub(); h.mark(self.b, self.wid, 'bookmark', True)
        h.mark(self.b, self.wid, 'bookmark', True)
        new = self.hub()
        self.assertEqual(new.feed(self.b, view='bookmarks')['total'], 1)
        self.assertEqual(new.feed(self.a, view='bookmarks')['total'], 0)
        self.assertFalse(new.feed()['items'][0]['bookmarked'])
        self.assertNotIn('bookmark_count', new.feed()['items'][0])
        new.mark(self.b, self.wid, 'bookmark', False)
        self.assertEqual(new.feed(self.b, view='bookmarks')['total'], 0)

    def test_follow_does_not_notify_for_old_discussion(self):
        self.reply(); h = self.hub(); h.mark(self.c, self.wid, 'follow', True)
        self.assertEqual(h.notifications(self.c)['total'], 0)
        self.reply(); self.assertEqual(self.hub().notifications(self.c)['total'], 1)

    def test_follow_retry_does_not_reset_subscription_start(self):
        h = self.hub(); h.mark(self.c, self.wid, 'follow', True)
        self.reply(); new = self.hub(); new.mark(self.c, self.wid, 'follow', True)
        self.assertEqual(new.notifications(self.c)['total'], 1)

    def test_unfollow_stops_subscription_notifications(self):
        self.hub().mark(self.c, self.wid, 'follow', True); self.reply()
        h = self.hub(); h.mark(self.c, self.wid, 'follow', False)
        self.assertEqual(h.notifications(self.c)['total'], 0)

    def test_bookmark_is_not_follow(self):
        self.hub().mark(self.c, self.wid, 'bookmark', True); self.reply()
        self.assertEqual(self.hub().notifications(self.c)['total'], 0)

    def test_owner_receives_replies_but_not_own_activity(self):
        self.reply(self.alice); other = self.reply()
        notices = self.hub().notifications(self.a)
        self.assertEqual(notices['unread'], 1); self.assertEqual(notices['items'][0]['id'], other['id'])

    def test_reply_to_commenter_notifies_without_following(self):
        comment = self.reply(); self.reply(self.cara, reply_to=comment['id'])
        notices = self.hub().notifications(self.b)['items']
        self.assertEqual(len(notices), 1); self.assertEqual(notices[0]['reason'], 'reply_to_you')

    def test_follower_receives_new_version_and_old_report_stays_bound(self):
        self.hub().mark(self.c, self.wid, 'follow', True)
        new = self.store.revise(self.alice['token'], self.wid, self.rev, 'v2', '修订')
        report = self.store.evidence(self.bob['token'], self.rev, 'human', 'old', 'm', 'r')
        items = self.hub().notifications(self.c)['items']
        self.assertEqual(items[0]['id'], report['id']); self.assertEqual(items[0]['revision_id'], self.rev)
        self.assertEqual(items[1]['revision_id'], new['id'])

    def test_marking_read_does_not_swallow_concurrent_new_response(self):
        old = self.reply(); h = self.hub(); self.reply(self.cara)
        h.mark_read(self.a, [old['id']])
        notices = self.hub().notifications(self.a)
        self.assertEqual(notices['unread'], 1); self.assertEqual(notices['total'], 2)

    def test_cannot_mark_other_persons_notification(self):
        comment = self.reply()
        with self.assertRaises(Problem): self.hub().mark_read(self.c, [comment['id']])

    def test_quota_applies_to_preferences_not_publication(self):
        second = self.store.publish(self.bob['token'], '另一个')['exploration']['id']
        h = self.hub()
        with patch('eksmu.community.MAX_PREFERENCES', 1):
            h.mark(self.c, self.wid, 'follow', True); h.mark(self.c, self.wid, 'follow', True)
            with self.assertRaises(Problem): h.mark(self.c, second, 'follow', True)
        self.store.publish(self.cara['token'], '继续发表')

    def test_preferences_require_real_boolean(self):
        for value in [1, 'true', None]:
            with self.subTest(value=value), self.assertRaises(Problem): self.hub().mark(self.a, self.wid, 'follow', value)

    def test_mine_view_is_author_filter_not_special_role(self):
        self.assertEqual(self.hub().feed(self.a, view='mine')['total'], 1)
        self.assertEqual(self.hub().feed(self.b, view='mine')['total'], 0)

    def test_exports_contain_no_keys_and_no_other_persons_lists(self):
        h = self.hub(); h.mark(self.b, self.wid, 'bookmark', True); self.detail(h)
        bundle = h.personal_export(self.a)
        self.assertEqual(bundle['bookmarks'], []); self.assertEqual(len(bundle['details']), 1)
        self.assertNotIn(self.alice['token'], str(bundle)); self.assertNotIn('key_hash', str(bundle))

    def test_personal_backup_can_restore_on_migrated_records(self):
        h = self.hub(); self.detail(h); h.mark(self.a, self.wid, 'bookmark', True)
        bundle = h.personal_export(self.a)
        second = Store(self.root / 'new-records.sqlite'); self.addCleanup(second.close)
        second.import_bundle(self.store.export())
        restored = Community(self.root / 'new-social.sqlite', second.rows()); self.hubs.append(restored)
        restored.personal_import(self.a, bundle)
        self.assertEqual(restored.personal_export(self.a), bundle)
        second.comment(self.alice['token'], self.rev, '迁移后继续')

    def test_import_cannot_change_other_peoples_introduction(self):
        h = self.hub(); bundle = h.personal_export(self.b)
        bundle['details'] = [{'exploration_id': self.wid, 'topics': ['劫持'], 'help': ''}]
        with self.assertRaises(Problem): h.personal_import(self.b, bundle)
        self.assertEqual(h.details(self.wid)['version'], 0)

    def test_invalid_import_is_atomic(self):
        h = self.hub(); h.mark(self.a, self.wid, 'bookmark', True); before = h.personal_export(self.a)
        invalid = deepcopy(before); invalid['bookmarks'] = []; invalid['details'] = [{'exploration_id': self.wid, 'topics': [], 'help': 'x', 'admin': True}]
        with self.assertRaises(Problem): h.personal_import(self.a, invalid)
        self.assertEqual(h.personal_export(self.a), before)

    def test_reimport_does_not_inflate_metadata_versions(self):
        h = self.hub(); self.detail(h); bundle = h.personal_export(self.a)
        h.personal_import(self.a, bundle); h.personal_import(self.a, bundle)
        self.assertEqual(h.details(self.wid)['version'], 1)

    def test_wrong_identity_import_rejected(self):
        h = self.hub()
        with self.assertRaises(Problem): h.personal_import(self.b, h.personal_export(self.a))

    def test_wrong_database_refused_without_touching_publication(self):
        before = self.store.export()
        with self.assertRaises(Problem): Community(self.root / 'records.sqlite', self.store.rows())
        self.assertEqual(self.store.export(), before)

    def test_auto_catalog_indexes_topics_and_bilingual_keywords_without_overriding_author(self):
        h = self.hub()
        self.assertEqual([r['id'] for r in h.uncataloged_revisions()], [self.rev])
        h.auto_catalog(self.wid, self.rev, ['数学', '数论'], ['rational number', '有理数', 'fraction'])
        self.assertEqual(h.uncataloged_revisions(), [])
        self.assertEqual(h.details(self.wid), {'topics': ['数学', '数论'], 'help': '', 'version': 0})
        self.assertEqual(h.feed(topic='数论')['total'], 1)
        self.assertEqual(h.feed(q='rational number')['total'], 1)
        # Author manual edit at expected_version=0 succeeds, overrides topics, and preserves bilingual keywords
        h.set_details(self.a, self.wid, ['代数'], '欢迎讨论', expected_version=0)
        self.assertEqual(h.details(self.wid), {'topics': ['代数'], 'help': '欢迎讨论', 'version': 1})
        self.assertEqual(h.feed(q='rational')['total'], 1)
        # Subsequent AI catalog on a new revision keeps author-chosen topics while refreshing keywords
        rev2 = self.store.revise(self.alice['token'], self.wid, self.rev, '新版分数', '第二版正文')['id']
        h2 = self.hub()
        self.assertEqual([r['id'] for r in h2.uncataloged_revisions()], [rev2])
        h2.auto_catalog(self.wid, rev2, ['几何'], ['continued fraction', '连分数'])
        self.assertEqual(h2.details(self.wid)['topics'], ['代数'])
        self.assertEqual(h2.feed(q='continued fraction')['total'], 1)

    def test_moderation_labels_feed_views_topics_appeals_and_operator_separation(self):
        h = self.hub()
        self.detail(h, topics=['数学', '数论'], help='需要协助验算')
        h.mark(self.b, self.wid, 'follow', True)
        h.mark(self.c, self.wid, 'bookmark', True)

        # Label as pending_review first
        lbl1 = h.label_work(self.wid, 'pending_review', '疑似含推广链接待复核', 'op_triage_1', source='ai')
        self.assertEqual(lbl1['label'], 'pending_review')
        self.assertEqual(lbl1['state'], 'active')
        queue = h.label_queue()
        self.assertEqual(len(queue['pending_review']), 1)
        self.assertEqual(queue['pending_review'][0]['id'], lbl1['id'])

        # Excluded from default public views and topics, included in labeled and personal views
        for view in ('all', 'unanswered', 'collaborate'):
            self.assertEqual(h.feed(view=view)['total'], 0)
        self.assertEqual(h.topics()['items'], [])
        labeled_feed = h.feed(view='labeled')
        self.assertEqual(labeled_feed['total'], 1)
        self.assertEqual(labeled_feed['items'][0]['moderation_label'], 'pending_review')
        self.assertEqual(labeled_feed['items'][0]['moderation_reason'], '疑似含推广链接待复核')
        self.assertEqual(h.feed(self.a, view='mine')['items'][0]['moderation_label'], 'pending_review')
        self.assertEqual(h.feed(self.b, view='following')['items'][0]['moderation_label'], 'pending_review')
        self.assertEqual(h.feed(self.c, view='bookmarks')['items'][0]['moderation_label'], 'pending_review')

        # Maintainer confirms commercial_promotion (supersedes pending_review)
        lbl2 = h.label_work(self.wid, 'commercial_promotion', '附带商品购买转化链接', 'op_maintainer_1')
        self.assertEqual(h.label_queue()['pending_review'], [])
        self.assertEqual(h.active_label(self.wid)['id'], lbl2['id'])
        self.assertEqual(h.active_label(self.wid)['label'], 'commercial_promotion')

        # Non-author cannot appeal and cannot read private appeal status
        with self.assertRaises(Problem) as err:
            h.appeal_label(self.b, lbl2['id'], '我是旁观者')
        self.assertEqual(err.exception.status, 403)
        with self.assertRaises(Problem) as err:
            h.author_label_status(self.b, self.wid)
        self.assertEqual(err.exception.status, 403)

        # Author submits private appeal (once per label, <= 2000 chars)
        with self.assertRaises(Problem):
            h.appeal_label(self.a, lbl2['id'], 'x' * 2001)
        appeal = h.appeal_label(self.a, lbl2['id'], '本文中的链接指向开源数据集，并非商品推广。')
        self.assertEqual(appeal['state'], 'pending')
        with self.assertRaises(Problem) as err:
            h.appeal_label(self.a, lbl2['id'], '重复申诉')
        self.assertEqual(err.exception.status, 409)

        # Public details vs author details privacy check
        pub_details = h.details(self.wid)
        self.assertNotIn('label_appeal', pub_details)
        other_details = h.details(self.wid, actor_id=self.b)
        self.assertNotIn('label_appeal', other_details)
        author_details = h.details(self.wid, actor_id=self.a)
        self.assertIn('label_appeal', author_details)
        self.assertEqual(author_details['label_appeal']['appeal']['body'], '本文中的链接指向开源数据集，并非商品推广。')

        # Pending appeal appears in offline maintainer label_queue
        queue2 = h.label_queue()
        self.assertEqual(len(queue2['pending_appeals']), 1)
        self.assertEqual(queue2['pending_appeals'][0]['id'], appeal['id'])

        # Same operator as original labeler is rejected on appeal review
        with self.assertRaises(Problem) as err:
            h.review_label_appeal(appeal['id'], 'uphold', '自己复核自己', 'op_maintainer_1')
        self.assertEqual(err.exception.status, 403)

        # Distinct operator reviews with retract -> immediately restores distribution
        reviewed = h.review_label_appeal(appeal['id'], 'retract', '核实确为开源数据集链接，撤回标记', 'op_maintainer_2')
        self.assertEqual(reviewed['decision'], 'retract')
        self.assertEqual(reviewed['state'], 'retracted')
        self.assertIsNone(h.active_label(self.wid))
        self.assertTrue(h.distribution(self.wid)['feed_distribute'])
        self.assertEqual(h.feed(view='all')['total'], 1)
        self.assertEqual(h.feed(view='labeled')['total'], 0)
        self.assertEqual(len(h.topics()['items']), 2)
        author_after = h.details(self.wid, actor_id=self.a)['label_appeal']['appeal']
        self.assertEqual(author_after['decision'], 'retract')
        self.assertEqual(author_after['decision_reason'], '核实确为开源数据集链接，撤回标记')
