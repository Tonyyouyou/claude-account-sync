"""End-to-end tests for sync.py against a synthetic Claude data directory.

Run from the repository root:  python3 -m unittest discover -s tests -v
Nothing outside a temporary directory is read or written.
"""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "sync.py"
ALPHA = ("aaaaaaaa-0000-4000-8000-000000000001", "0a0a0a0a-0000-4000-8000-000000000001")
BETA = ("bbbbbbbb-0000-4000-8000-000000000002", "0b0b0b0b-0000-4000-8000-000000000002")
SCRATCH_CWD = "/Users/me/Library/Application Support/Claude/scratch-workspaces/x/y/scratch-1"


def session(sid, title, at, ssh=True, cwd="/home/me", **extra):
    data = {
        "sessionId": f"local_{sid}",
        "cliSessionId": f"cli-{sid}",
        "cwd": cwd,
        "createdAt": at - 1000,
        "lastActivityAt": at,
        "isArchived": False,
        "title": title,
    }
    if ssh:
        data.update(
            sshConfig={"sshHost": "me@example.com"},
            sshRemoteProcessId=f"proc-{sid}",
            sshReattach={"v": 1, "processId": f"proc-{sid}", "state": "adoptable"},
        )
    data.update(extra)
    return data


class SyncTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self.claude = root / "claude"
        self.state = root / "state"
        self.dirs = {
            "alpha": self.claude / "claude-code-sessions" / ALPHA[0] / ALPHA[1],
            "beta": self.claude / "claude-code-sessions" / BETA[0] / BETA[1],
        }
        for d in self.dirs.values():
            d.mkdir(parents=True)
        # A leftover directory that holds only scheduled-tasks.json; discover must skip it
        leftover = self.claude / "claude-code-sessions" / ALPHA[0] / BETA[1]
        leftover.mkdir()
        (leftover / "scheduled-tasks.json").write_text("{}")
        # Fake `open` so launch/relaunch never touch the real desktop
        self.bin = root / "bin"
        self.bin.mkdir()
        self.open_log = root / "open.log"
        (self.bin / "open").write_text(f'#!/bin/bash\necho "$*" >> "{self.open_log}"\n')
        (self.bin / "open").chmod(0o755)

        self.put("alpha", session("s1", "ssh one", 5000))
        self.put("alpha", session("s2", "ssh two", 4000))
        self.put("alpha", session("s3", "local folder", 3000, ssh=False, cwd="/Users/me/proj"))
        self.put("alpha", session("s4", "scratch", 2000, ssh=False, cwd=SCRATCH_CWD))
        self.put("beta", session("b1", "beta only", 6000))
        self.sign_in("beta")
        self.ok("setup", f"{ALPHA[0]}/{ALPHA[1]}=alpha", f"{BETA[0]}/{BETA[1]}=beta")

    def tearDown(self):
        self._tmp.cleanup()

    # helpers -----------------------------------------------------------------

    def sign_in(self, label):
        uuid = ALPHA[0] if label == "alpha" else BETA[0]
        (self.claude / "config.json").write_text(json.dumps({"lastKnownAccountUuid": uuid}))

    def put(self, label, data):
        path = self.dirs[label] / f"{data['sessionId']}.json"
        path.write_text(json.dumps(data, separators=(",", ":"), ensure_ascii=False), encoding="utf-8")

    def get(self, label, sid):
        path = self.dirs[label] / f"local_{sid}.json"
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None

    def edit(self, label, sid, **changes):
        data = self.get(label, sid)
        data.update(changes)
        self.put(label, data)

    def delete(self, label, sid):
        data = self.get(label, sid)
        (self.dirs[label] / f"local_{sid}.json").unlink()
        (self.dirs[label] / f"deleted_{data['cliSessionId']}").write_text("1")

    def run_cli(self, cmd, *rest):
        env = dict(os.environ, PATH=f"{self.bin}:{os.environ['PATH']}")
        return subprocess.run(
            [sys.executable, "-I", str(SCRIPT), cmd,
             "--claude-dir", str(self.claude), "--state-dir", str(self.state), "--allow-running", *rest],
            capture_output=True, text=True, env=env,
        )

    def ok(self, *args):
        result = self.run_cli(*args)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return result.stdout

    # tests -------------------------------------------------------------------

    def test_discover_lists_session_dirs_only(self):
        out = self.ok("discover")
        self.assertIn(f"{ALPHA[0]}/{ALPHA[1]}", out)
        self.assertIn(f"{BETA[0]}/{BETA[1]}   <- signed in now", out)
        self.assertNotIn(f"{ALPHA[0]}/{BETA[1]}", out)

    def test_setup_rejects_bad_input(self):
        self.assertEqual(self.run_cli("setup", "nope").returncode, 1)
        self.assertEqual(self.run_cli("setup", f"{ALPHA[0]}/{ALPHA[1]}=x", f"{ALPHA[0]}/{ALPHA[1]}=y").returncode, 1)
        self.assertEqual(self.run_cli("setup", f"{ALPHA[0]}/missing=x", f"{BETA[0]}/{BETA[1]}=y").returncode, 1)

    def test_first_sync_copies_strips_and_leaves_source_alone(self):
        before = {p.name: p.read_bytes() for p in self.dirs["alpha"].iterdir()}
        self.ok("apply")
        copied = self.get("beta", "s1")
        self.assertNotIn("sshReattach", copied)
        self.assertNotIn("sshRemoteProcessId", copied)
        original = self.get("alpha", "s1")
        self.assertEqual({k: v for k, v in original.items() if k not in ("sshReattach", "sshRemoteProcessId")}, copied)
        raw = (self.dirs["beta"] / "local_s1.json").read_text(encoding="utf-8")
        self.assertEqual(raw, json.dumps(copied, separators=(",", ":"), ensure_ascii=False))
        self.assertIsNotNone(self.get("beta", "s3"))
        self.assertIsNone(self.get("beta", "s4"), "scratch sessions must not be synced")
        self.assertIsNotNone(self.get("beta", "b1"))
        self.assertEqual(before, {p.name: p.read_bytes() for p in self.dirs["alpha"].iterdir()})
        self.assertIn("Nothing to write.", self.ok("plan"))

    def test_round_trip_update_rename_archive_delete_and_new(self):
        self.ok("apply")  # alpha -> beta
        self.edit("beta", "s1", lastActivityAt=9000, cliSessionId="cli-s1-v2",
                  priorRemoteCliSessionIds=["cli-s1"], sshRemoteProcessId="proc-beta")
        self.edit("beta", "s2", title="renamed", isArchived=True)
        self.delete("beta", "s3")
        self.put("beta", session("b2", "new in beta", 8000))
        self.sign_in("alpha")
        self.ok("apply")  # beta -> alpha
        s1 = self.get("alpha", "s1")
        self.assertEqual(s1["cliSessionId"], "cli-s1-v2")
        self.assertNotIn("sshRemoteProcessId", s1)
        s2 = self.get("alpha", "s2")
        self.assertEqual((s2["title"], s2["isArchived"]), ("renamed", True))
        self.assertIn("sshReattach", s2, "a rename must not drop the target's own reattach info")
        self.assertIsNone(self.get("alpha", "s3"))
        removed = list(self.state.glob("backups/*-beta-to-alpha/removed-from-target/local_s3.json"))
        self.assertEqual(len(removed), 1)
        self.assertIsNotNone(self.get("alpha", "b1"))
        self.assertIsNotNone(self.get("alpha", "b2"))
        self.sign_in("beta")
        self.assertIn("Nothing to write.", self.ok("plan"))

    def test_delete_in_target_before_syncing_is_not_undone(self):
        self.ok("apply")  # alpha -> beta
        (self.dirs["beta"] / "local_s2.json").unlink()  # deleted on beta before the next sync
        out = self.ok("apply")  # alpha -> beta again
        self.assertIn("Skipped: deleted in target", out)
        self.assertIsNone(self.get("beta", "s2"))
        self.sign_in("alpha")
        self.ok("apply")  # beta -> alpha
        self.assertIsNone(self.get("alpha", "s2"))

    def test_first_run_respects_target_tombstone(self):
        (self.dirs["beta"] / "deleted_cli-s2").write_text("1")
        out = self.ok("apply")
        self.assertIn("Skipped: deleted in target", out)
        self.assertIsNone(self.get("beta", "s2"))

    def test_newer_target_copy_is_kept(self):
        self.ok("apply")
        self.edit("beta", "s1", lastActivityAt=99999, title="beta was used")
        out = self.ok("apply")
        self.assertIn("Skipped: target copy is newer", out)
        self.assertEqual(self.get("beta", "s1")["title"], "beta was used")

    def test_verify(self):
        self.ok("apply")
        self.assertIn("not opened yet", self.ok("verify"))
        self.edit("beta", "s1", cliSessionId="cli-s1-v2", priorRemoteCliSessionIds=["cli-s1"], sshRemoteProcessId="proc-new")
        self.assertIn("opened and continued", self.ok("verify"))
        self.edit("beta", "s2", sshRemoteProcessId="proc-s2")  # the alpha process came back
        result = self.run_cli("verify")
        self.assertEqual(result.returncode, 1)
        self.assertIn("re-attached to the source account's old remote process", result.stdout)

    def test_restore_latest_backup(self):
        self.ok("apply")
        self.assertIsNotNone(self.get("beta", "s1"))
        self.ok("restore")
        self.assertIsNone(self.get("beta", "s1"))
        self.assertIsNotNone(self.get("beta", "b1"))
        self.assertEqual(json.loads((self.state / "state.json").read_text()).get("synced", []), [])

    def test_wrong_expected_account_aborts(self):
        result = self.run_cli("plan", "--expect-target", ALPHA[0])
        self.assertEqual(result.returncode, 1)
        self.assertIn("Current account mismatch", result.stderr)

    def test_launch_opens_terminal_with_waiting_command(self):
        self.ok("launch", "apply", "--expect-target", BETA[0])
        self.assertIn("-a Terminal", self.open_log.read_text())
        command = (self.state / "run.command").read_text()
        self.assertIn("apply --wait-for-quit --relaunch", command)
        self.assertIn(f"--expect-target {BETA[0]}", command)
        self.assertTrue(os.access(self.state / "run.command", os.X_OK))


if __name__ == "__main__":
    unittest.main()
