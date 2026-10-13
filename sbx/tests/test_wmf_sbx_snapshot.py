"""Warm snapshots' store (snapshot.py, track B3): the key, generations,
what keeps a snapshot or a golden image, gc and prune, and the host
repos' gc borrower entries. Real qemu-img (skipped without it) and real
git repos; no VM boots."""

import contextlib
import datetime
import io
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
sys.path.insert(0, os.path.dirname(__file__))

import wmf_sbx.image as image  # noqa: E402
import wmf_sbx.remotes as remotes  # noqa: E402
import wmf_sbx.rm as rm  # noqa: E402
import wmf_sbx.snapshot as snapshot  # noqa: E402
from test_wmf_sbx_lifecycle import git_repo  # noqa: E402

HAVE_TOOLS = shutil.which("qemu-img") is not None and shutil.which("git") is not None
GOLDEN = "0123456789abcdef"


def qemu_img(*args):
    subprocess.run(["qemu-img"] + list(args), check=True, capture_output=True,
                   stdin=subprocess.DEVNULL)


class KeyTests(unittest.TestCase):

    REPOS = [("gerrit:mediawiki/core", "/h/core", False),
             ("gerrit:mediawiki/extensions/Cite", "/h/Cite", False)]

    def test_the_key_is_the_set_not_the_order(self):
        self.assertEqual(snapshot.snapshot_key(GOLDEN, self.REPOS),
                         snapshot.snapshot_key(GOLDEN, list(reversed(self.REPOS))))
        self.assertRegex(snapshot.snapshot_key(GOLDEN, self.REPOS), r"^[0-9a-f]{16}$")

    def test_golden_nested_path_and_ro_change_the_key(self):
        k = snapshot.snapshot_key(GOLDEN, self.REPOS)
        for other in (snapshot.snapshot_key("fedcba9876543210", self.REPOS),
                      snapshot.snapshot_key(GOLDEN, self.REPOS, nested=True),
                      snapshot.snapshot_key(GOLDEN, self.REPOS[:1] + [
                          ("gerrit:mediawiki/extensions/Cite", "/h/Cite", True)]),
                      snapshot.snapshot_key(GOLDEN, self.REPOS[:1] + [
                          ("gerrit:mediawiki/extensions/Cite", "/other/Cite", False)]),
                      snapshot.snapshot_key(GOLDEN, self.REPOS[:1])):
            self.assertNotEqual(k, other)


@unittest.skipUnless(HAVE_TOOLS, "needs qemu-img and git")
class StoreTests(unittest.TestCase):

    def setUp(self):
        self.tmp = os.path.realpath(tempfile.mkdtemp())
        self.addCleanup(self.cleanup)
        self.env = {"XDG_CACHE_HOME": os.path.join(self.tmp, "cache"),
                    "XDG_STATE_HOME": os.path.join(self.tmp, "state"),
                    "LIMA_HOME": os.path.join(self.tmp, "lima"), "HOME": self.tmp}
        d = image.entry_dir(GOLDEN, self.env)
        os.makedirs(d)
        self.golden = image.golden_path(GOLDEN, self.env)
        qemu_img("create", "-q", "-f", "qcow2", self.golden, "64M")
        os.chmod(self.golden, 0o444)
        with open(os.path.join(d, "manifest.json"), "w") as f:
            f.write('{"created": "2026-01-01T00:00:00+00:00", "inputs": {}}')
        self.repo = os.path.join(self.tmp, "src", "core")
        git_repo(self.repo)
        self.key = snapshot.snapshot_key(GOLDEN, [("gerrit:mediawiki/core", self.repo, False)])
        self.now = datetime.datetime(2026, 10, 13, 12, 0, tzinfo=datetime.timezone.utc)

    def cleanup(self):
        for d, _dirs, files in os.walk(self.tmp):
            for f in files:
                os.chmod(os.path.join(d, f), 0o644)
        shutil.rmtree(self.tmp)

    def generation(self, age, meta_too=True):
        """A frozen generation `age` old, backed by the golden image, as a
        freeze makes it; the host repo lists the key."""
        created = self.now - age
        gen = snapshot.new_generation(created)
        d = snapshot.key_dir(self.key, self.env)
        os.makedirs(d, exist_ok=True)
        path = os.path.join(d, gen + ".qcow2")
        qemu_img("create", "-q", "-f", "qcow2", "-F", "qcow2", "-b", self.golden, path)
        os.chmod(path, 0o444)
        snapshot.register_borrowers(self.key, [self.repo])
        if meta_too:
            snapshot.write_meta(os.path.join(d, gen + ".json"), {
                "key": self.key, "golden": GOLDEN, "created": created.isoformat(),
                "sandbox": "demo",
                "repos": [{"canonical": "gerrit:mediawiki/core", "path": self.repo,
                           "readOnly": False}]})
        return gen, path

    def sandbox_on(self, path, name="demo"):
        d = os.path.join(self.env["LIMA_HOME"], "wmf-sbx-" + name)
        os.makedirs(d)
        qemu_img("create", "-q", "-f", "qcow2", "-F", "qcow2", "-b", path,
                 os.path.join(d, "disk"))

    def test_a_generation_without_metadata_is_not_one(self):
        self.generation(datetime.timedelta(hours=2), meta_too=False)
        self.assertIsNone(snapshot.newest(self.key, self.env))
        gen, path = self.generation(datetime.timedelta(hours=1))
        self.assertEqual(snapshot.newest(self.key, self.env)[:2], (gen, path))

    def test_a_snapshot_keeps_its_golden_image(self):
        self.generation(datetime.timedelta(days=1))
        self.assertEqual(image.images_in_use(self.env),
                         {GOLDEN: [snapshot.borrower_name(self.key)]})
        with self.assertRaisesRegex(image.ImageError, "in use"):
            image.remove_image(GOLDEN, self.env)

    def test_the_host_repo_lists_the_key_and_its_gc_is_off(self):
        self.generation(datetime.timedelta(days=1))
        self.assertIn(snapshot.borrower_name(self.key), remotes.borrowers(self.repo))
        self.assertEqual(snapshot.borrowers_intact(self.key, [self.repo]), [])
        out = subprocess.run(["git", "-C", self.repo, "config", "gc.auto"],
                             capture_output=True, text=True).stdout.strip()
        self.assertEqual(out, "0")

    def test_a_replaced_host_repo_is_found(self):
        self.generation(datetime.timedelta(days=1))
        shutil.rmtree(self.repo)
        git_repo(self.repo)   # cloned again: no borrower entry
        self.assertEqual(snapshot.borrowers_intact(self.key, [self.repo]), [self.repo])

    def test_gc_keeps_the_newest_and_what_is_used(self):
        _g1, old_used = self.generation(datetime.timedelta(days=3))
        g2, _p2 = self.generation(datetime.timedelta(days=2))
        g3, _p3 = self.generation(datetime.timedelta(minutes=10))
        self.sandbox_on(old_used)
        gone = snapshot.prune(self.env, gc_age=datetime.timedelta(days=7), now=self.now)
        # g2 is superseded and unused; g1 is used; g3 is the newest.
        self.assertEqual([g for _k, g, _n in gone], [g2])
        self.assertEqual([g for g, _p, _m in snapshot.list_generations(self.key, self.env)],
                         [_g1, g3])

    def test_gc_of_the_last_old_generation_gives_the_repo_its_gc_back(self):
        self.generation(datetime.timedelta(days=10))
        gone = snapshot.prune(self.env, gc_age=datetime.timedelta(days=7), now=self.now)
        self.assertEqual(len(gone), 1)
        self.assertEqual(snapshot.list_keys(self.env), [])
        self.assertNotIn(snapshot.borrower_name(self.key), remotes.borrowers(self.repo))
        out = subprocess.run(["git", "-C", self.repo, "config", "gc.auto"],
                             capture_output=True, text=True).stdout.strip()
        self.assertEqual(out, "")

    def test_prune_removes_every_unused_one_then_the_image(self):
        self.generation(datetime.timedelta(minutes=5))
        out = io.StringIO()
        self.assertEqual(image.main(["prune"], env=self.env, out=out), 0)
        self.assertIn("snapshot", out.getvalue())
        self.assertIn(GOLDEN, out.getvalue())
        self.assertEqual(snapshot.list_keys(self.env), [])
        self.assertEqual(image.list_images(self.env), [])

    def test_image_ls_lists_the_snapshots(self):
        _gen, path = self.generation(datetime.timedelta(hours=1))
        self.sandbox_on(path)
        out = io.StringIO()
        image.main(["ls"], env=self.env, out=out)
        self.assertIn(f"snapshot {self.key}/", out.getvalue())
        self.assertIn("used by: demo", out.getvalue())

    def test_rm_scan_keeps_a_snapshots_borrower_entry(self):
        self.generation(datetime.timedelta(hours=1))
        remotes.add_borrower(self.repo, "gone-sandbox")
        err = io.StringIO()
        with contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
            rm.main(["--scan", os.path.join(self.tmp, "src")], env=self.env)
        self.assertEqual(remotes.borrowers(self.repo), [snapshot.borrower_name(self.key)])


if __name__ == "__main__":
    unittest.main()
