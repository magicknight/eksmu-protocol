"""Phase 2 performance and caching tests: feed pagination, naive equivalence, write visibility, DB isolation, concurrency."""
from __future__ import annotations

import http.client
import json
from pathlib import Path
import tempfile
import threading
import unicodedata
import unittest

from eksmu import record_chain
from eksmu.analysis import Analysis, build_report
from eksmu.community import Community, VIEWS, topic_name
from eksmu.modern_accounts import ModernAccounts
from eksmu.research_render import excerpt
from eksmu.server import make_server
from eksmu.store import Store


NAV = {"Sec-Fetch-Site": "cross-site", "Sec-Fetch-Mode": "navigate", "Sec-Fetch-Dest": "document", "Sec-Fetch-User": "?1"}


def _naive_feed(hub: Community, actor_id=None, *, q="", topic="", view="all", order="latest", offset=0, limit=20, analysis=None) -> dict:
    """Pre-optimization reference implementation of Community.feed() for exact equivalence testing."""
    if actor_id is not None:
        hub.actor(actor_id)
    follow = hub._marks(actor_id, "follow") if actor_id else {}
    saved = hub._marks(actor_id, "bookmark") if actor_id else {}
    active_labels = hub.active_labels()
    metadata = {r[0]: json.loads(r[1]) for r in hub.db.execute("SELECT id,document FROM hub_details")}
    ai_by_rev = dict(analysis.db.execute("SELECT revision_id, COUNT(*) FROM analysis_reports GROUP BY revision_id")) if analysis is not None else {}
    terms = unicodedata.normalize("NFKC", q).casefold().split()
    selected_topic = topic_name(topic) if topic.strip() else ""
    result = []
    for work_id, work in hub.works.items():
        dist = hub.distribution(work_id, active_labels=active_labels)
        if view in {"all", "unanswered", "collaborate"} and not dist["feed_distribute"]:
            continue
        if view == "labeled" and dist["feed_distribute"]:
            continue
        versions = hub.versions[work_id]
        latest = versions[-1]
        meta = metadata.get(work_id, {"topics": [], "help": "", "version": 0})
        if (view == "unanswered" and hub.responses[work_id]) or (view == "collaborate" and not meta["help"]):
            continue
        if (view == "following" and work_id not in follow) or (view == "bookmarks" and work_id not in saved):
            continue
        if view == "mine" and work["actor_id"] != actor_id:
            continue
        if selected_topic and selected_topic not in meta["topics"]:
            continue
        paper = latest["payload"].get("paper") or {}
        haystack = unicodedata.normalize("NFKC", " ".join([
            latest["payload"]["title"], paper.get("abstract") or "", latest["payload"]["body"], meta["help"],
            *meta["topics"], *meta.get("keywords", [])])).casefold()
        if not all(t in haystack for t in terms):
            continue
        pub_lbl = dist["label"]
        item = {"id": work_id, "revision_id": latest["id"], "author_id": work["actor_id"],
                "author": hub.public_name(work["actor_id"]),
                "affiliation": hub.profiles.get(work["actor_id"], {}).get("affiliation", ""),
                "kind": work["payload"]["kind"],
                "title": latest["payload"]["title"], "excerpt": excerpt(paper.get("abstract") or latest["payload"]["body"], 220),
                "created_at": work["created_at"], "updated_at": hub.rows[hub.activity[work_id]]["created_at"],
                "versions": len(versions), "comments": hub.comments[work_id], "reports": hub.reports[work_id],
                "ai_reports": 0 if pub_lbl else sum(ai_by_rev.get(v["id"], 0) for v in versions),
                "topics": meta["topics"], "help": meta["help"], "details_version": meta["version"],
                "following": work_id in follow, "bookmarked": work_id in saved}
        if pub_lbl is not None:
            item["label"] = pub_lbl
            item["moderation_label"] = pub_lbl["label"]
            item["moderation_reason"] = pub_lbl["reason"]
        if hub.chain_db is not None:
            if pub_lbl is not None:
                item["chain_seq"] = None
                item["bitcoin_height"] = None
            else:
                chain = record_chain.summary(hub.chain_db, latest["id"])
                item["chain_seq"] = chain["seq"] if chain else None
                anchor = chain.get("anchor") if chain else None
                item["bitcoin_height"] = anchor["bitcoin"]["height"] if (anchor and anchor.get("state") == "confirmed" and anchor.get("bitcoin")) else None
        result.append(item)
    result.sort(key=lambda x: hub.positions[x["id"]] if order == "latest" else hub.activity[x["id"]], reverse=True)
    total = len(result)
    return {"items": result[offset:offset + limit], "total": total, "offset": offset, "limit": limit,
            "next_offset": offset + limit if offset + limit < total else None}


class Phase2PerfTests(unittest.TestCase):
    def test_feed_pagination_and_naive_equivalence(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "records.sqlite"
            store = Store(db_path)
            self.addCleanup(store.close)
            authors = [store.register(f"作者{i}") for i in range(4)]
            accounts = ModernAccounts(store)
            with accounts.db:
                accounts.db.execute(
                    "INSERT INTO auth_profiles VALUES(?,?,?,1)",
                    (authors[0]["actor"]["id"], "甲学者", "独立数学研究室"),
                )
            works = []
            kinds = ["question", "conjecture", "finding", "experiment", "computation"]
            for i in range(32):
                author = authors[i % len(authors)]
                tok = author["token"]
                kw = "素数分布与黎曼猜想" if i % 3 == 0 else ("图论谱半径上界" if i % 3 == 1 else "随机矩阵特征值")
                created = store.publish(
                    tok,
                    f"第 {i} 篇正文：{kw}。包含公式 $x^{i} + 1$ 与推论。",
                    f"标题 {i} · {kw}",
                    kind=kinds[i % len(kinds)],
                )
                wid = created["exploration"]["id"]
                rev_id = created["revision"]["id"]
                if i % 2 == 0:
                    rev = store.revise(
                        tok, wid, rev_id,
                        f"标题 {i} · 修订二版 {kw}",
                        f"第 {i} 篇修订正文：{kw}，补充了新引理与数值实验。",
                        note="v2",
                    )
                    rev_id = rev["id"]
                if i % 4 == 0:
                    other_tok = authors[(i + 1) % len(authors)]["token"]
                    store.comment(other_tok, rev_id, f"来自他人的讨论 {i}")
                elif i % 4 == 1:
                    store.comment(tok, rev_id, f"作者自评 {i}")
                if i % 5 == 0:
                    other_tok = authors[(i + 2) % len(authors)]["token"]
                    store.evidence(other_tok, rev_id, "computation", "引理1", "脚本复现", "结果吻合")
                works.append((wid, rev_id, author["actor"]["id"]))

            profiles = accounts.public_profiles()
            rows = store.rows()
            hub = Community(str(db_path) + ".community.sqlite", rows, profiles, chain_db=store.db)
            self.addCleanup(hub.close)
            analysis = Analysis(str(db_path) + ".analysis.sqlite", rows)
            self.addCleanup(analysis.close)

            for idx, (wid, rev_id, aid) in enumerate(works):
                topics = []
                if idx % 2 == 0:
                    topics.append("数论")
                if idx % 3 == 0:
                    topics.append("谱理论")
                help_text = "征集数值反例与协作验证" if idx % 4 == 0 else ""
                if topics or help_text:
                    hub.set_details(aid, wid, topics, help_text, 0)
                if idx % 6 == 0:
                    hub.auto_catalog(wid, rev_id, topics or ["组合数学"], ["ai_keyword_token", f"kw_{idx}"])
                if idx % 3 == 0:
                    hub.mark(authors[0]["actor"]["id"], wid, "follow", True)
                if idx % 5 == 0:
                    hub.mark(authors[0]["actor"]["id"], wid, "bookmark", True)
                if idx % 7 == 0:
                    rev_row = hub.index[rev_id]
                    req = analysis.request(aid, rev_id)
                    raw = {
                        "summary": "测试概要", "claims": [], "strengths": [], "questions": [],
                        "suggestions": [], "overall": "好", "findings": [], "not_checked": [],
                        "limitations": "", "suspicious_instructions": False,
                    }
                    analyzer = {
                        "kind": "model", "name": "gpt-6-luna", "requested_model": "gpt-6-luna",
                        "prompt_template": "t", "operator": "eksmu.com", "cost_borne_by": "Eksmu operator",
                    }
                    rep = build_report(rev_row, analyzer, raw, {"input_tokens": 1, "output_tokens": 1, "cost_usd": 0})
                    analysis.finish(req["id"], "done", "", rep)

            viewer = authors[0]["actor"]["id"]

            # 1. Full pagination without duplicates or missing items
            for order in ("latest", "active"):
                full_ref = _naive_feed(hub, viewer, order=order, offset=0, limit=50, analysis=analysis)
                self.assertEqual(full_ref["total"], 32)
                for page_size in (1, 5, 7, 12, 20):
                    collected = []
                    offset = 0
                    seen_ids = set()
                    while True:
                        page = hub.feed(viewer, order=order, offset=offset, limit=page_size, analysis=analysis)
                        ref_page = _naive_feed(hub, viewer, order=order, offset=offset, limit=page_size, analysis=analysis)
                        self.assertEqual(page, ref_page)
                        for item in page["items"]:
                            self.assertNotIn(item["id"], seen_ids)
                            seen_ids.add(item["id"])
                            collected.append(item)
                        if page["next_offset"] is None:
                            break
                        offset = page["next_offset"]
                    self.assertEqual(len(collected), full_ref["total"])
                    self.assertEqual(collected, full_ref["items"])

            # 2. Equivalence across all views, orders, queries, topics, and offsets
            queries = ["", "素数", "谱半径", "修订二版 素数", "协作验证", "ai_keyword_token", "kw_6", "不存在的词xyz"]
            topics_filter = ["", "数论", "谱理论", "组合数学", "未知主题"]
            for view in VIEWS:
                actor_arg = viewer if view in {"following", "bookmarks", "mine"} else None
                for order in ("latest", "active"):
                    for q in queries:
                        for topic in topics_filter:
                            for offset, limit in ((0, 12), (4, 7), (25, 10)):
                                got = hub.feed(
                                    actor_arg, q=q, topic=topic, view=view,
                                    order=order, offset=offset, limit=limit, analysis=analysis,
                                )
                                expected = _naive_feed(
                                    hub, actor_arg, q=q, topic=topic, view=view,
                                    order=order, offset=offset, limit=limit, analysis=analysis,
                                )
                                self.assertEqual(got, expected)

    def test_write_immediately_visible_in_feed_state_and_work_page(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "live.sqlite"
            server = make_server(db_path, 0)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            self.addCleanup(lambda: (server.shutdown(), server.server_close(), thread.join()))

            def request(path, data=None, token=None, headers=None):
                hdrs = {"Content-Type": "application/json", "X-Eksmu-Client": "local-prototype"}
                if token:
                    hdrs["Authorization"] = "Bearer " + token
                if headers:
                    hdrs.update(headers)
                conn = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=10)
                try:
                    conn.request(
                        "POST" if data is not None else "GET",
                        path,
                        None if data is None else json.dumps(data).encode("utf-8"),
                        hdrs,
                    )
                    resp = conn.getresponse()
                    return resp.status, resp.read()
                finally:
                    conn.close()

            # Initial empty state
            self.assertEqual(json.loads(request("/api/community/feed?limit=12")[1])["total"], 0)
            self.assertEqual(len(json.loads(request("/api/state")[1])["records"]), 0)

            # Register & publish
            actor_doc = json.loads(request("/api/actors", {"alias": "实时作者"})[1])
            token = actor_doc["token"]
            actor_id = actor_doc["actor"]["id"]

            code, raw_pub = request(
                "/api/explorations",
                {"title": "首次发表标题", "body": "首次发表正文内容", "kind": "finding"},
                token=token,
            )
            self.assertEqual(code, 201)
            pub_doc = json.loads(raw_pub)
            work_id = pub_doc["exploration"]["id"]
            rev1_id = pub_doc["revision"]["id"]

            feed1 = json.loads(request("/api/community/feed?limit=12")[1])
            self.assertEqual(feed1["total"], 1)
            self.assertEqual(feed1["items"][0]["id"], work_id)
            self.assertEqual(feed1["items"][0]["revision_id"], rev1_id)
            self.assertEqual(feed1["items"][0]["title"], "首次发表标题")
            state1 = json.loads(request("/api/state")[1])
            self.assertEqual(len(state1["records"]), 3)
            page1 = request(f"/zh/w/{work_id}", headers=NAV)[1].decode("utf-8")
            self.assertIn("首次发表标题", page1)
            self.assertIn("首次发表正文内容", page1)
            author_page1 = request(f"/zh/u/{actor_id}", headers=NAV)[1].decode("utf-8")
            self.assertIn("首次发表标题", author_page1)

            # Revise and immediately read
            code, raw_rev = request(
                "/api/revisions",
                {
                    "exploration_id": work_id,
                    "parent_revision_id": rev1_id,
                    "title": "立即生效的第二版标题",
                    "body": "立即生效的第二版正文",
                    "note": "更新",
                },
                token=token,
            )
            self.assertEqual(code, 201)
            rev2_id = json.loads(raw_rev)["id"]

            feed2 = json.loads(request("/api/community/feed?limit=12")[1])
            self.assertEqual(feed2["items"][0]["revision_id"], rev2_id)
            self.assertEqual(feed2["items"][0]["title"], "立即生效的第二版标题")
            self.assertEqual(feed2["items"][0]["versions"], 2)
            state2 = json.loads(request("/api/state")[1])
            self.assertEqual(len(state2["records"]), 4)
            page2 = request(f"/zh/w/{work_id}", headers=NAV)[1].decode("utf-8")
            self.assertIn("立即生效的第二版标题", page2)
            self.assertIn("立即生效的第二版正文", page2)
            author_page2 = request(f"/zh/u/{actor_id}", headers=NAV)[1].decode("utf-8")
            self.assertIn("立即生效的第二版标题", author_page2)
            self.assertIn("版本 2", author_page2)

            # Comment and immediately read
            code, raw_comment = request(
                "/api/comments",
                {"revision_id": rev2_id, "body": "立即生效的新评论内容"},
                token=token,
            )
            self.assertEqual(code, 201)
            comment_id = json.loads(raw_comment)["id"]

            feed3 = json.loads(request("/api/community/feed?limit=12")[1])
            self.assertEqual(feed3["items"][0]["comments"], 1)
            state3 = json.loads(request("/api/state")[1])
            self.assertEqual(len(state3["records"]), 5)
            self.assertEqual(state3["records"][-1]["id"], comment_id)
            page3 = request(f"/zh/w/{work_id}", headers=NAV)[1].decode("utf-8")
            self.assertIn("立即生效的新评论内容", page3)
            author_page3 = request(f"/zh/u/{actor_id}", headers=NAV)[1].decode("utf-8")
            self.assertIn("参与 1 条讨论", author_page3)

    def test_two_database_paths_cache_isolation_and_file_replacement(self):
        with tempfile.TemporaryDirectory() as tmp:
            path_a = Path(tmp) / "instance_a.sqlite"
            path_b = Path(tmp) / "instance_b.sqlite"
            store_a = Store(path_a)
            store_b = Store(path_b)
            self.addCleanup(store_a.close)
            self.addCleanup(store_b.close)

            tok_a = store_a.register("实例A作者")["token"]
            tok_b = store_b.register("实例B作者")["token"]
            work_a = store_a.publish(tok_a, "实例A专属正文", "实例A标题")["exploration"]["id"]
            work_b1 = store_b.publish(tok_b, "实例B第一篇正文", "实例B标题1")["exploration"]["id"]
            work_b2 = store_b.publish(tok_b, "实例B第二篇正文", "实例B标题2")["exploration"]["id"]

            rows_a = store_a.rows()
            rows_b = store_b.rows()
            self.assertEqual(len(rows_a), 3)
            self.assertEqual(len(rows_b), 5)
            self.assertEqual({r["id"] for r in rows_a if r["type"] == "exploration"}, {work_a})
            self.assertEqual({r["id"] for r in rows_b if r["type"] == "exploration"}, {work_b1, work_b2})

            hub_a = Community(str(path_a) + ".community.sqlite", store_a.rows(), ModernAccounts(store_a).public_profiles())
            hub_b = Community(str(path_b) + ".community.sqlite", store_b.rows(), ModernAccounts(store_b).public_profiles())
            self.addCleanup(hub_a.close)
            self.addCleanup(hub_b.close)

            self.assertEqual([x["id"] for x in hub_a.feed()["items"]], [work_a])
            self.assertEqual([x["id"] for x in hub_b.feed()["items"]], [work_b2, work_b1])

            # Replace file at path_a with a fresh database and verify cache invalidates by inode/version
            store_a.close()
            path_a.unlink()
            recreated_a = Store(path_a)
            self.addCleanup(recreated_a.close)
            self.assertEqual(recreated_a.rows(), [])
            tok_a2 = recreated_a.register("重建后作者")["token"]
            work_a2 = recreated_a.publish(tok_a2, "重建后正文", "重建后标题")["exploration"]["id"]
            self.assertEqual([r["id"] for r in recreated_a.rows() if r["type"] == "exploration"], [work_a2])
            self.assertEqual({r["id"] for r in store_b.rows() if r["type"] == "exploration"}, {work_b1, work_b2})

    def test_multithreaded_concurrent_reads_and_writes(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "concurrent.sqlite"
            server = make_server(db_path, 0)
            srv_thread = threading.Thread(target=server.serve_forever, daemon=True)
            srv_thread.start()
            self.addCleanup(lambda: (server.shutdown(), server.server_close(), srv_thread.join()))

            def call(path, data=None, token=None, headers=None):
                hdrs = {"Content-Type": "application/json", "X-Eksmu-Client": "local-prototype"}
                if token:
                    hdrs["Authorization"] = "Bearer " + token
                if headers:
                    hdrs.update(headers)
                conn = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=15)
                try:
                    conn.request(
                        "POST" if data is not None else "GET",
                        path,
                        None if data is None else json.dumps(data).encode("utf-8"),
                        hdrs,
                    )
                    resp = conn.getresponse()
                    return resp.status, resp.read()
                finally:
                    conn.close()

            tokens = []
            actor_ids = []
            for i in range(4):
                code, raw = call("/api/actors", {"alias": f"并发作者{i}"})
                self.assertEqual(code, 201)
                doc = json.loads(raw)
                tokens.append(doc["token"])
                actor_ids.append(doc["actor"]["id"])

            # Seed one work so readers always have a valid work page to fetch
            code, seed_raw = call("/api/explorations", {"title": "种子作品", "body": "种子正文 素数"}, token=tokens[0])
            self.assertEqual(code, 201)
            seed_work = json.loads(seed_raw)["exploration"]["id"]

            errors: list[str] = []
            published_works: list[str] = []
            lock = threading.Lock()
            stop_readers = threading.Event()

            def writer(worker_idx: int):
                tok = tokens[worker_idx]
                try:
                    for step in range(5):
                        code, raw = call(
                            "/api/explorations",
                            {"title": f"并发作品 w{worker_idx}-{step}", "body": f"并发正文 {worker_idx}-{step} 素数"},
                            token=tok,
                        )
                        if code != 201:
                            with lock:
                                errors.append(f"publish status {code}: {raw}")
                            return
                        created = json.loads(raw)
                        wid = created["exploration"]["id"]
                        rev_id = created["revision"]["id"]
                        with lock:
                            published_works.append(wid)
                        code, raw_rev = call(
                            "/api/revisions",
                            {
                                "exploration_id": wid,
                                "parent_revision_id": rev_id,
                                "title": f"并发作品 w{worker_idx}-{step} v2",
                                "body": f"并发修订正文 {worker_idx}-{step} 素数",
                                "note": "v2",
                            },
                            token=tok,
                        )
                        if code != 201:
                            with lock:
                                errors.append(f"revise status {code}: {raw_rev}")
                            return
                        rev2_id = json.loads(raw_rev)["id"]
                        code, raw_com = call(
                            "/api/comments",
                            {"revision_id": rev2_id, "body": f"并发评论 {worker_idx}-{step}"},
                            token=tok,
                        )
                        if code != 201:
                            with lock:
                                errors.append(f"comment status {code}: {raw_com}")
                            return
                except Exception as exc:
                    with lock:
                        errors.append(f"writer {worker_idx} exception: {exc!r}")

            def reader(reader_idx: int):
                paths = [
                    "/api/community/feed?limit=12",
                    "/api/community/feed?limit=12&order=active",
                    "/api/community/feed?limit=12&q=%E7%B4%A0%E6%95%B0",
                    "/api/state",
                    "/api/profiles",
                    f"/zh/w/{seed_work}",
                    f"/zh/u/{actor_ids[0]}",
                ]
                step = 0
                try:
                    while not stop_readers.is_set():
                        path = paths[step % len(paths)]
                        hdrs = NAV if path.startswith("/zh/") else None
                        code, body = call(path, headers=hdrs)
                        if code != 200:
                            with lock:
                                errors.append(f"reader {reader_idx} {path} status {code}")
                            return
                        if path.startswith("/api/"):
                            parsed = json.loads(body)
                            if not isinstance(parsed, dict):
                                with lock:
                                    errors.append(f"reader {reader_idx} invalid json on {path}")
                                return
                        step += 1
                except Exception as exc:
                    with lock:
                        errors.append(f"reader {reader_idx} exception: {exc!r}")

            writer_threads = [threading.Thread(target=writer, args=(i,)) for i in range(4)]
            reader_threads = [threading.Thread(target=reader, args=(i,)) for i in range(4)]
            for t in reader_threads + writer_threads:
                t.start()
            for t in writer_threads:
                t.join(timeout=30)
            stop_readers.set()
            for t in reader_threads:
                t.join(timeout=30)

            self.assertEqual(errors, [])
            self.assertEqual(len(published_works), 20)

            # Final consistency checks
            final_feed = json.loads(call("/api/community/feed?limit=50")[1])
            self.assertEqual(final_feed["total"], 21)
            feed_ids = {item["id"] for item in final_feed["items"]}
            self.assertEqual(feed_ids, set(published_works) | {seed_work})

            final_state = json.loads(call("/api/state")[1])
            # 4 actors + 1 seed (exp+rev) + 20 works * (exp + rev1 + rev2 + comment) = 4 + 2 + 80 = 86 records
            self.assertEqual(len(final_state["records"]), 86)

            verify_store = Store(db_path)
            self.addCleanup(verify_store.close)
            chain_check = record_chain.verify(verify_store.db)
            self.assertTrue(chain_check["consistent"])
            self.assertEqual(chain_check["length"], 86)


if __name__ == "__main__":
    unittest.main()
