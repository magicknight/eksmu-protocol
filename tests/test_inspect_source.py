"""Tests for POST /api/research/inspect-source: read-only, side-effect-free LaTeX source expansion."""
from __future__ import annotations

import base64
import http.client
from io import BytesIO
import json
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest
import zipfile

from eksmu.server import make_server
from eksmu.store import Store


class InspectSourceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp.name) / "community.sqlite"
        self.server = make_server(self.db_path, 0)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

        # Create an authenticated actor
        store = Store(self.db_path)
        reg = store.register(alias="探索者")
        self.token = reg["token"]
        self.actor_id = reg["actor"]["id"]
        store.close()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.temp.cleanup()

    def request(self, path, data=None, token=None):
        headers = {"Content-Type": "application/json", "X-Eksmu-Client": "local-prototype"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        body = json.dumps(data).encode("utf-8") if data is not None else None
        conn = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=5)
        try:
            conn.request("POST" if body is not None else "GET", path, body, headers)
            resp = conn.getresponse()
            raw = resp.read()
            parsed = json.loads(raw.decode("utf-8")) if raw else {}
            return resp.status, parsed
        finally:
            conn.close()

    def make_zip(self, files: dict[str, str | bytes]) -> str:
        bio = BytesIO()
        with zipfile.ZipFile(bio, "w", zipfile.ZIP_DEFLATED) as zf:
            for name, content in files.items():
                if isinstance(content, str):
                    zf.writestr(name, content.encode("utf-8"))
                else:
                    zf.writestr(name, content)
        return base64.b64encode(bio.getvalue()).decode("ascii")

    def test_requires_login(self):
        b64 = self.make_zip({"main.tex": "\\documentclass{article}\n\\begin{document}Hello\\end{document}"})
        status, body = self.request("/api/research/inspect-source", {"filename": "source.zip", "data_b64": b64})
        self.assertEqual(status, 401)

    def test_rejects_non_source_archive(self):
        b64 = base64.b64encode(b"plain text").decode("ascii")
        for bad_name in ("paper.txt", "notes.md", "image.png", "doc.pdf"):
            with self.subTest(filename=bad_name):
                status, body = self.request(
                    "/api/research/inspect-source",
                    {"filename": bad_name, "data_b64": b64},
                    token=self.token,
                )
                self.assertEqual(status, 400)
                self.assertIn("只支持 LaTeX 源码包", body.get("error", ""))

    def test_rejects_path_traversal_in_filename(self):
        b64 = self.make_zip({"main.tex": "\\documentclass{article}\n\\begin{document}Hello\\end{document}"})
        for bad in ("../source.zip", "/source.zip", "sub/source.zip", "c:\\source.zip"):
            with self.subTest(filename=bad):
                status, body = self.request(
                    "/api/research/inspect-source",
                    {"filename": bad, "data_b64": b64},
                    token=self.token,
                )
                self.assertEqual(status, 400)
                self.assertIn("文件名不能包含路径", body.get("error", ""))

    def test_rejects_archive_with_path_traversal_entry(self):
        bio = BytesIO()
        with zipfile.ZipFile(bio, "w") as zf:
            zf.writestr("../escape.tex", "bad content")
            zf.writestr("main.tex", "\\documentclass{article}\\begin{document}\\input{../escape}\\end{document}")
        b64 = base64.b64encode(bio.getvalue()).decode("ascii")
        status, body = self.request(
            "/api/research/inspect-source",
            {"filename": "escape.zip", "data_b64": b64},
            token=self.token,
        )
        self.assertEqual(status, 400)

    def test_rejects_empty_archive_or_no_tex(self):
        b64 = self.make_zip({"readme.txt": "No tex file here"})
        status, body = self.request(
            "/api/research/inspect-source",
            {"filename": "empty.zip", "data_b64": b64},
            token=self.token,
        )
        self.assertEqual(status, 400)
        self.assertIn("未找到 .tex 文件", body.get("error", ""))

    def test_rejects_oversized_archive(self):
        # Base64 string exceeding 32 MiB binary limit (~44.74 MiB base64) but within MAX_UPLOAD_REQUEST
        fake_b64 = "A" * 44739248
        status, body = self.request(
            "/api/research/inspect-source",
            {"filename": "huge.zip", "data_b64": fake_b64},
            token=self.token,
        )
        self.assertEqual(status, 413)
        self.assertIn("32 MiB", body.get("error", ""))

    def test_returns_expanded_full_text(self):
        main_tex = (
            "\\documentclass{article}\n"
            "\\title{黎曼猜想探索}\n"
            "\\begin{document}\n"
            "\\begin{abstract}这是论文摘要内容。\\end{abstract}\n"
            "\\section{引言}\n"
            "\\input{chapters/intro}\n"
            "\\end{document}\n"
        )
        intro_tex = "这是引入章节的正文内容，包含公式 $E=mc^2$。"
        b64 = self.make_zip({
            "main.tex": main_tex,
            "chapters/intro.tex": intro_tex,
        })
        status, body = self.request(
            "/api/research/inspect-source",
            {"filename": "paper.zip", "data_b64": b64},
            token=self.token,
        )
        self.assertEqual(status, 200)
        self.assertEqual(body.get("main_file"), "main.tex")
        tex = body.get("tex", "")
        self.assertIn("\\title{黎曼猜想探索}", tex)
        self.assertIn("这是论文摘要内容。", tex)
        self.assertIn("这是引入章节的正文内容，包含公式 $E=mc^2$。", tex)
        self.assertIn("main.tex", body.get("files", []))
        self.assertIn("chapters/intro.tex", body.get("files", []))

    def test_leaves_no_traces_in_database_or_storage(self):
        research_db = Path(str(self.db_path) + ".research.sqlite")
        main_tex = "\\documentclass{article}\n\\title{测试}\n\\begin{document}正文\\end{document}"
        b64 = self.make_zip({"main.tex": main_tex})

        status, body = self.request(
            "/api/research/inspect-source",
            {"filename": "clean.zip", "data_b64": b64},
            token=self.token,
        )
        self.assertEqual(status, 200)

        # Check research sqlite: no research files or compile jobs should exist
        if research_db.exists():
            conn = sqlite3.connect(str(research_db))
            try:
                tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
                if "research_files" in tables:
                    count = conn.execute("SELECT count(*) FROM research_files").fetchone()[0]
                    self.assertEqual(count, 0)
                if "compile_jobs" in tables:
                    count = conn.execute("SELECT count(*) FROM compile_jobs").fetchone()[0]
                    self.assertEqual(count, 0)
            finally:
                conn.close()


if __name__ == "__main__":
    unittest.main()
