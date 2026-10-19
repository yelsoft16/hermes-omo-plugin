import unittest

from omo import tools


class TestCommandUsage(unittest.TestCase):
    def test_help(self):
        self.assertIn("/omo status", tools.handle_command("help"))

    def test_unknown_subcommand(self):
        self.assertIn("Usage:", tools.handle_command("nonsense"))


if __name__ == "__main__":
    unittest.main()
