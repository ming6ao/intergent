"""Unit tests for scope canonicalization and hierarchy."""

import unittest

from intergent.scopes import (
    canonical_key,
    make_scope,
    parse_scope_spec,
    parse_scope_specs,
    scope_chain,
    tokens,
)


class ScopeTests(unittest.TestCase):
    def test_file_normalisation(self):
        self.assertEqual(canonical_key("file", "./src/../src/App.py"), "src/app.py")
        self.assertEqual(canonical_key("file", "src//app.py"), "src/app.py")

    def test_dotfile_not_stripped(self):
        self.assertEqual(canonical_key("file", ".github/workflows/ci.yml"), ".github/workflows/ci.yml")

    def test_dir_trailing_slash(self):
        self.assertEqual(canonical_key("dir", "src/api"), "src/api/")

    def test_symbol(self):
        self.assertEqual(canonical_key("symbol", "src/App.py#Service::run"), "src/app.py#Service::run")

    def test_api(self):
        self.assertEqual(canonical_key("api", "get  /Users/{id}"), "GET /users/{id}")

    def test_infer_kind(self):
        self.assertEqual(parse_scope_spec("src/a.py"), ("file", "src/a.py", None))
        self.assertEqual(parse_scope_spec("src/a.py#Foo=replace"), ("symbol", "src/a.py#Foo", "replace"))

    def test_chain_file_has_no_file_as_dir(self):
        chain = scope_chain("file", "src/app.py")
        self.assertIn("dir:src/", chain)
        self.assertNotIn("dir:src/app.py/", chain)
        self.assertEqual(chain[-1], "file:src/app.py")

    def test_chain_symbol(self):
        chain = scope_chain("symbol", "src/app.py#Cls.run")
        self.assertIn("file:src/app.py", chain)
        self.assertIn("symbol:src/app.py#Cls", chain)
        self.assertEqual(chain[-1], "symbol:src/app.py#Cls.run")

    def test_chain_dir(self):
        chain = scope_chain("dir", "src/api/")
        self.assertEqual(chain[-1], "dir:src/api/")
        self.assertIn("dir:src/", chain)

    def test_parse_scope_specs_default_and_dedupe(self):
        items = parse_scope_specs(["file:a.py", "file:a.py", "file:b.py=add"], "modify")
        self.assertEqual(len(items), 2)
        self.assertEqual(items[0][1], "modify")
        self.assertEqual(items[1][1], "add")

    def test_tokens_split_camel(self):
        toks = tokens("symbol", "src/app.py#PaymentService")
        self.assertIn("payment", toks)
        self.assertIn("service", toks)

    def test_make_scope_node(self):
        scope = make_scope("file", "src/App.py")
        self.assertEqual(scope.node, "file:src/app.py")


if __name__ == "__main__":
    unittest.main()
