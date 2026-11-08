"""Prevent native form submission from putting private fields in the URL."""

from html.parser import HTMLParser
from pathlib import Path
import unittest


PAGE = Path(__file__).resolve().parents[1] / "eksmu/static/index.html"


class Forms(HTMLParser):
    def __init__(self):
        super().__init__()
        self.forms = []
        self.account_sections = {}
        self.current_form = None
        self.inputs_by_form = {}

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        if tag == "form":
            self.forms.append(attributes)
            self.current_form = attributes.get("id")
        if tag == "input" and self.current_form:
            self.inputs_by_form.setdefault(self.current_form, []).append(attributes)
        if attributes.get("id") in {"email-entry", "legacy-account"}:
            self.account_sections[attributes["id"]] = attributes

    def handle_endtag(self, tag):
        if tag == "form":
            self.current_form = None


class FormPrivacyTests(unittest.TestCase):
    def test_native_fallback_never_uses_get(self):
        markup = PAGE.read_text(encoding="utf-8")
        page = Forms()
        page.feed(markup)
        self.assertTrue(page.forms)
        self.assertEqual(
            [(form.get("id"), form.get("method")) for form in page.forms
             if (form.get("method") or "get").lower() != "post"],
            [],
        )
        self.assertEqual(set(page.account_sections), {"email-entry", "legacy-account"})
        self.assertTrue(all("hidden" in section for section in page.account_sections.values()))
        for form_id, field_name in (
            ("email-register", "alias"),
            ("confirm-registration", "alias"),
            ("profile-form", "display_name"),
            ("register", "alias"),
        ):
            matching = [inp for inp in page.inputs_by_form.get(form_id, []) if inp.get("name") == field_name]
            self.assertEqual(len(matching), 1, form_id)
            self.assertEqual(matching[0].get("autocomplete"), "name", form_id)
        self.assertGreaterEqual(markup.count("会永久公开，并写入公开记录，请勿填写邮箱"), 2)


if __name__ == "__main__":
    unittest.main()
