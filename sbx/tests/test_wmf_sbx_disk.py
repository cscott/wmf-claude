"""Copy-on-write disks (disk.py, track B2): the chain check, the qcow2
header parse, the Limactl disk methods and the images an overlay keeps in
use. These use the real qemu-img (skipped without it) on small files; no
VM boots."""

import contextlib
import io
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
sys.path.insert(0, os.path.dirname(__file__))

import wmf_sbx.disk as disk  # noqa: E402
import wmf_sbx.image as image  # noqa: E402
import wmf_sbx.lima as lima_mod  # noqa: E402
import wmf_sbx.state as state_mod  # noqa: E402
import wmf_sbx.status as status_mod  # noqa: E402
import settings_isolation  # noqa: E402


def setUpModule():
    settings_isolation.isolate()


from test_wmf_sbx_lifecycle import FakeLima  # noqa: E402

HAVE_QEMU_IMG = shutil.which("qemu-img") is not None
KEY = "0123456789abcdef"


def qemu_img(*args):
    subprocess.run(["qemu-img"] + list(args), check=True, capture_output=True,
                   stdin=subprocess.DEVNULL)


@unittest.skipUnless(HAVE_QEMU_IMG, "needs qemu-img")
class OverlayTests(unittest.TestCase):
    """A cache with one golden image, and a Lima home with one instance
    whose disk is a placeholder copy, as after `limactl create`."""

    def setUp(self):
        self.tmp = os.path.realpath(tempfile.mkdtemp())
        self.addCleanup(self.cleanup)
        self.env = {"XDG_CACHE_HOME": os.path.join(self.tmp, "cache"),
                    "XDG_STATE_HOME": os.path.join(self.tmp, "state"),
                    "LIMA_HOME": os.path.join(self.tmp, "lima"), "HOME": self.tmp}
        self.root = image.cache_root(self.env)
        os.makedirs(image.entry_dir(KEY, self.env))
        self.golden = image.golden_path(KEY, self.env)
        qemu_img("create", "-q", "-f", "qcow2", self.golden, "64M")
        os.chmod(self.golden, 0o444)
        self.lima = lima_mod.Limactl()
        os.makedirs(os.path.join(self.env["LIMA_HOME"], "wmf-sbx-demo"))
        self.disk = self.lima.disk_path("wmf-sbx-demo", self.env)
        shutil.copy(self.lima.placeholder(self.root), self.disk)
        os.chmod(self.disk, 0o644)

    def cleanup(self):
        for d, _dirs, files in os.walk(self.tmp):
            for f in files:
                os.chmod(os.path.join(d, f), 0o644)
        shutil.rmtree(self.tmp)

    def chain(self):
        return self.lima.disk_chain("wmf-sbx-demo", env=self.env)

    def test_the_placeholder_is_small_read_only_and_has_no_backing_file(self):
        path = disk.placeholder_path(self.root)
        self.assertLess(os.path.getsize(path), 1 << 20)
        self.assertFalse(os.stat(path).st_mode & 0o222)
        self.assertIsNone(disk.backing_file(path))
        self.assertEqual(self.lima.placeholder(self.root), path)   # made once

    def test_an_overlay_on_the_golden_image_passes(self):
        self.lima.overlay_disk("wmf-sbx-demo", self.golden, env=self.env)
        self.assertLess(os.path.getsize(self.disk), 1 << 20)
        self.assertEqual(disk.check_chain(self.chain(), self.disk, self.root), [self.golden])
        self.assertEqual(disk.header_chain(self.disk), [self.disk, self.golden])
        self.assertEqual(disk.backing_file(self.golden), None)

    def test_a_disk_with_no_backing_file_is_refused(self):
        with self.assertRaisesRegex(disk.DiskError, "has no backing file"):
            disk.check_chain(self.chain(), self.disk, self.root)

    def test_a_writable_golden_image_is_refused(self):
        self.lima.overlay_disk("wmf-sbx-demo", self.golden, env=self.env)
        os.chmod(self.golden, 0o644)
        with self.assertRaisesRegex(disk.DiskError, "is writable"):
            disk.check_chain(self.chain(), self.disk, self.root)

    def test_a_backing_file_outside_the_cache_is_refused(self):
        stray = os.path.join(self.tmp, "stray.qcow2")
        qemu_img("create", "-q", "-f", "qcow2", stray, "64M")
        os.chmod(stray, 0o444)
        self.lima.overlay_disk("wmf-sbx-demo", stray, env=self.env)
        with self.assertRaisesRegex(disk.DiskError, "not in the image cache"):
            disk.check_chain(self.chain(), self.disk, self.root)

    def test_a_raw_backing_file_is_refused(self):
        raw = os.path.join(image.entry_dir(KEY, self.env), "raw.img")
        qemu_img("create", "-q", "-f", "raw", raw, "64M")
        os.chmod(raw, 0o444)
        os.unlink(self.disk)
        qemu_img("create", "-q", "-f", "qcow2", "-F", "raw", "-b", raw, self.disk)
        with self.assertRaisesRegex(disk.DiskError, "'raw', not qcow2"):
            disk.check_chain(self.chain(), self.disk, self.root)

    def test_a_two_deep_chain_in_the_cache_passes(self):
        # B3's shape: golden <- snapshot <- sandbox.
        snap = os.path.join(image.entry_dir(KEY, self.env), "snapshot.qcow2")
        qemu_img("create", "-q", "-f", "qcow2", "-F", "qcow2", "-b", self.golden, snap)
        os.chmod(snap, 0o444)
        self.lima.overlay_disk("wmf-sbx-demo", snap, env=self.env)
        self.assertEqual(disk.check_chain(self.chain(), self.disk, self.root),
                         [snap, self.golden])

    def test_an_overlay_keeps_its_image_in_use_without_a_state_file(self):
        self.lima.overlay_disk("wmf-sbx-demo", self.golden, env=self.env)
        self.assertEqual(image.images_in_use(self.env), {KEY: ["demo"]})
        with self.assertRaisesRegex(image.ImageError, "in use by: demo"):
            image.remove_image(KEY, self.env)

    def test_status_shows_the_overlay(self):
        self.lima.overlay_disk("wmf-sbx-demo", self.golden, env=self.env)
        with open(os.path.join(image.entry_dir(KEY, self.env), "golden.sha256"), "w") as f:
            f.write(image._sha256_file(self.golden) + "  golden.qcow2\n")
        st = state_mod.new_state("demo", primary_dir="/x", repos=["/x"], image=KEY,
                                 vm_type="qemu")
        st["disk"] = "overlay"
        state_mod.save(st, self.env)

        class Lima(FakeLima):
            disk_chain = lima_mod.Limactl.disk_chain
            _qemu_img = lima_mod.Limactl._qemu_img
            run = staticmethod(subprocess.run)

        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = status_mod.main(["demo"], lima=Lima({"wmf-sbx-demo": "Stopped"}),
                                 env=self.env)
        self.assertEqual(rc, 0, out.getvalue())
        self.assertIn(f"disk      overlay on {self.golden}: ok", out.getvalue())


class HeaderTests(unittest.TestCase):

    def test_not_qcow2_has_no_backing_file(self):
        with tempfile.NamedTemporaryFile() as f:
            f.write(b"\0" * 64)
            f.flush()
            self.assertIsNone(disk.backing_file(f.name))
        self.assertIsNone(disk.backing_file("/nonexistent"))

    def test_a_relative_backing_name_is_relative_to_the_file(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "o.qcow2")
            name = b"base.qcow2"
            head = disk.QCOW2_MAGIC + (3).to_bytes(4, "big") + (100).to_bytes(8, "big") \
                + len(name).to_bytes(4, "big")
            with open(path, "wb") as f:
                f.write(head.ljust(100, b"\0") + name)
            self.assertEqual(disk.backing_file(path), os.path.join(d, "base.qcow2"))

    def test_the_lima_version_warning(self):
        self.assertIsNone(disk.lima_version_warning(disk.TESTED_LIMA))
        self.assertIn("--full-disk", disk.lima_version_warning("2.3.0"))
        self.assertIn("(unknown)", disk.lima_version_warning(None))


class GcTests(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp)
        self.env = {"XDG_CACHE_HOME": os.path.join(self.tmp, "cache"),
                    "XDG_STATE_HOME": os.path.join(self.tmp, "state"),
                    "LIMA_HOME": os.path.join(self.tmp, "lima"), "HOME": self.tmp}

    def entry(self, key, created):
        d = image.entry_dir(key, self.env)
        os.makedirs(d)
        with open(os.path.join(d, "golden.qcow2"), "wb") as f:
            f.write(b"x")
        with open(os.path.join(d, "manifest.json"), "w") as f:
            f.write('{"created": "%s"}' % created)

    def run_cli(self, *argv):
        out = io.StringIO()
        rc = image.main(list(argv) + (["--config", "/nonexistent"] if argv[0] == "gc" else []),
                        env=self.env, out=out)
        return rc, out.getvalue()

    def test_gc_removes_only_old_unused_images_and_prune_removes_all(self):
        import datetime
        now = datetime.datetime.now(datetime.timezone.utc)
        self.entry("1111111111111111", "2026-01-01T00:00:00+00:00")
        self.entry("2222222222222222", (now - datetime.timedelta(days=1)).isoformat())
        rc, out = self.run_cli("gc")
        self.assertEqual(rc, 0)
        self.assertIn("1111111111111111", out)
        self.assertEqual([k for k, _m in image.list_images(self.env)], ["2222222222222222"])
        rc, out = self.run_cli("prune")
        self.assertIn("2222222222222222", out)
        self.assertEqual(image.list_images(self.env), [])

    def test_durations(self):
        import datetime
        self.assertEqual(image.parse_duration("14d"), datetime.timedelta(days=14))
        self.assertEqual(image.parse_duration("12h"), datetime.timedelta(hours=12))
        self.assertEqual(image.parse_duration(3), datetime.timedelta(days=3))
        with self.assertRaisesRegex(image.ImageError, "not a duration"):
            image.gc_age({"imageCache": {"gcAge": "a week"}})


class GoldenAgeTests(unittest.TestCase):
    """imageCache.maxAge (track B3): the golden image is rebuilt, with the
    same inputs and a new epoch, once it is too old."""

    INPUTS = {"arch": "x86_64", "k": 1}

    def setUp(self):
        import datetime
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp)
        self.env = {"XDG_CACHE_HOME": os.path.join(self.tmp, "cache"),
                    "XDG_STATE_HOME": os.path.join(self.tmp, "state"), "HOME": self.tmp}
        self.now = datetime.datetime(2026, 10, 13, 12, 0, tzinfo=datetime.timezone.utc)
        self.day = datetime.timedelta(days=1)

    def entry(self, inputs, age):
        import json
        key = image.cache_key(inputs)
        d = image.entry_dir(key, self.env)
        os.makedirs(d)
        with open(os.path.join(d, "golden.qcow2"), "wb") as f:
            f.write(b"x")
        with open(os.path.join(d, "manifest.json"), "w") as f:
            json.dump({"key": key, "inputs": inputs,
                       "created": (self.now - age).isoformat()}, f)
        return key

    def choose(self, **kw):
        kw.setdefault("now", self.now)
        inputs, note = image.choose_inputs(dict(self.INPUTS), env=self.env, **kw)
        return image.cache_key(inputs), note

    def test_no_image_builds_the_plain_inputs(self):
        self.assertEqual(self.choose(), (image.cache_key(self.INPUTS), None))

    def test_a_young_image_is_used(self):
        key = self.entry(self.INPUTS, 3 * self.day)
        self.assertEqual(self.choose(), (key, None))

    def test_an_old_image_gets_a_new_epoch_once(self):
        old = self.entry(self.INPUTS, 20 * self.day)
        key, note = self.choose()
        self.assertNotEqual(key, old)
        self.assertIn("20 days old", note)
        # Built: the next create, an hour later, uses it (not a third).
        new_inputs = dict(self.INPUTS, epoch=self.now.isoformat(timespec="seconds"))
        self.assertEqual(self.entry(new_inputs, datetime_hours(1)), key)
        self.assertEqual(self.choose(), (key, None))

    def test_no_expire_keeps_it_with_a_warning_unless_renewed(self):
        old = self.entry(self.INPUTS, 20 * self.day)
        for kw in ({"expire": False}, {"config": {"imageCache": {"expire": False}}}):
            key, note = self.choose(**kw)
            self.assertEqual(key, old)
            self.assertIn("--cold", note)
        key, _note = self.choose(expire=False, renew=True)
        self.assertNotEqual(key, old)

    def test_max_age_is_a_setting(self):
        old = self.entry(self.INPUTS, 3 * self.day)
        self.assertNotEqual(self.choose(config={"imageCache": {"maxAge": "2d"}})[0], old)

    def test_min_left_renews_an_image_about_to_expire(self):
        old = self.entry(self.INPUTS, 13 * self.day + datetime_hours(12))
        self.assertEqual(self.choose()[0], old)
        key, note = self.choose(min_left=self.day)
        self.assertNotEqual(key, old)
        self.assertIn("12 h before", note)

    def test_other_inputs_are_another_image(self):
        self.entry(dict(self.INPUTS, k=2), 20 * self.day)
        self.assertEqual(self.choose(), (image.cache_key(self.INPUTS), None))


def datetime_hours(n):
    import datetime
    return datetime.timedelta(hours=n)


if __name__ == "__main__":
    unittest.main()
