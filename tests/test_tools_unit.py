import os
import tempfile
import unittest
from pathlib import Path

from omo import tools


class TestToolHelpers(unittest.TestCase):
    def test_split_model(self):
        self.assertEqual(tools._split_model("omniroute/glm-5.3-flash"),
                         ("omniroute", "glm-5.3-flash"))
        self.assertEqual(tools._split_model("glm-5.3-flash"), (None, "glm-5.3-flash"))
        self.assertEqual(tools._split_model(None), (None, None))

    def test_validate_permission_accepts_known(self):
        for name in ("full-access", "workspace", "accept-edits", "auto", "read-only", "ask"):
            tools._validate_permission(name)

    def test_validate_permission_rejects_unknown(self):
        with self.assertRaises(Exception) as ctx:
            tools._validate_permission("root")
        self.assertEqual(getattr(ctx.exception, "code", ""), "invalid_permission")

    def test_resolve_cwd_requires_absolute(self):
        with self.assertRaises(Exception) as ctx:
            tools._resolve_cwd("relative/path")
        self.assertEqual(getattr(ctx.exception, "code", ""), "invalid_cwd")

    def test_resolve_cwd_rejects_missing(self):
        with self.assertRaises(Exception) as ctx:
            tools._resolve_cwd("/definitely/not/here")
        self.assertEqual(getattr(ctx.exception, "code", ""), "invalid_cwd")

    def test_resolve_cwd_accepts_existing(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(tools._resolve_cwd(tmp), os.path.realpath(tmp))

    def test_resolve_cwd_expands_home(self):
        self.assertEqual(tools._resolve_cwd("~"),
                         os.path.realpath(os.path.expanduser("~")))


if __name__ == "__main__":
    unittest.main()
