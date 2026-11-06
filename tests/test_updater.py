import importlib.util
import os
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
l2 = sys.modules.get("l2")
if l2 is None:
    spec = importlib.util.spec_from_file_location("l2", os.path.join(HERE, "..", "pve-dash.py"))
    l2 = importlib.util.module_from_spec(spec)
    sys.modules["l2"] = l2
    spec.loader.exec_module(l2)


class UpdaterTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        for fn, body in (("pve-dash.py", 'VERSION = "1.0.0"\n'), ("index.html", "<html>old</html>")):
            with open(os.path.join(self.dir, fn), "w") as f:
                f.write(body)
        self.remote = {
            "https://gh/o/r/main/VERSION": b"1.1.0\n",
            "https://gh/o/r/v1.1.0/pve-dash.py": b'VERSION = "1.1.0"\nprint("hi")\n',
            "https://gh/o/r/v1.1.0/index.html": b"<!doctype html><html>new</html>",
        }

    def up(self):
        def fetch(url):
            if url not in self.remote:
                raise OSError(url)
            return self.remote[url]
        return l2.Updater(fetch=fetch, install_dir=self.dir, repo="o/r", base="https://gh", current="1.0.0")

    def read(self, fn):
        with open(os.path.join(self.dir, fn)) as f:
            return f.read()

    def test_detects_and_applies_update_keeping_backup(self):
        u = self.up()
        self.assertFalse(u.status()["available"])
        u.check()
        self.assertEqual((u.status()["latest"], u.status()["available"]), ("1.1.0", True))
        self.assertEqual(u.apply(), "1.1.0")
        self.assertIn("1.1.0", self.read("pve-dash.py"))
        self.assertIn("new", self.read("index.html"))
        self.assertIn("1.0.0", self.read("pve-dash.py.bak"))

    def test_not_newer_or_unconfigured_is_a_noop(self):
        self.remote["https://gh/o/r/main/VERSION"] = b"1.0.0"
        u = self.up()
        u.check()
        self.assertFalse(u.status()["available"])
        with self.assertRaises(ValueError):
            u.apply()
        self.assertFalse(l2.Updater(repo="OWNER/pve-dash").configured())
        self.assertFalse(l2.Updater(repo="").configured())

    def test_bad_downloads_are_rejected_and_nothing_is_written(self):
        for url, body in (("https://gh/o/r/v1.1.0/pve-dash.py", b"def broken(:\n"),
                          ("https://gh/o/r/v1.1.0/pve-dash.py", b'VERSION = "9.9.9"\n'),
                          ("https://gh/o/r/v1.1.0/index.html", b"not a page")):
            good = dict(self.remote)
            self.remote[url] = body
            with self.assertRaises(Exception):
                self.up().apply()
            self.assertEqual(self.read("pve-dash.py"), 'VERSION = "1.0.0"\n')
            self.assertEqual(self.read("index.html"), "<html>old</html>")
            self.remote = good

    def test_github_down_reports_error(self):
        self.remote.clear()
        u = self.up()
        u.check()
        self.assertIn("Couldn't reach GitHub", u.status()["error"])
        self.assertFalse(u.status()["available"])


if __name__ == "__main__":
    unittest.main()
