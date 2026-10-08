#!/usr/bin/env python3
"""Keep the Claude desktop app's Code-tab session list in sync between two accounts.

The desktop app stores one index file per Code session at
<Claude data dir>/claude-code-sessions/<account UUID>/<org UUID>/local_*.json.
The files carry no account field: which account sees a session depends only on the
directory it sits in, so switching accounts shows a different (often empty) sidebar.
After each switch, this script copies the index from the account you just left (source)
to the account you are on now (target): new and updated sessions are copied, and
sessions deleted in the source are moved out of the target into a backup.

The app must not be running while index files change, or it may write its in-memory
copy back over them. `launch` therefore opens a Terminal window that waits for the app
to quit, applies the change, and reopens the app.

Commands:
  discover        list the account/org directories found on this Mac (read-only)
  setup           save the two accounts, e.g. `setup ACCT/ORG=work ACCT/ORG=personal`
  plan            show what a sync would change (read-only)
  launch apply    in a Terminal window: wait for Claude to quit, sync, reopen Claude
  launch restore  same, but roll back to a backup
  apply           sync now (refuses while Claude is running unless --wait-for-quit)
  verify          check the sessions written by the last sync after Claude reopened
  restore         restore both accounts' index from a backup (latest by default)

macOS only. Standard library only; runs on the system /usr/bin/python3 (3.9).
"""
import argparse
import json
import os
import shlex
import shutil
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

DEFAULT_CLAUDE_DIR = Path.home() / "Library/Application Support/Claude"
DEFAULT_STATE_DIR = Path.home() / ".claude/account-sync"
APP_SUFFIX = "/Claude.app/Contents/MacOS/Claude"

# Point at a remote process started by one account. Dropping them makes the app start a
# fresh remote CLI under the current account and resume from the transcript, instead of
# re-attaching to a process that still holds the other account's token.
STRIP_FIELDS = ("sshReattach", "sshRemoteProcessId")
# Changes that do not count as conversation activity (rename, archive). When both copies
# have the same activity time, only these fields are synced.
META_FIELDS = ("title", "titleSource", "previousTitles", "isArchived")
# "No folder" sessions live in a scratch workspace whose path contains the account UUID,
# so they do not carry over to another account.
SCRATCH_MARK = "/Library/Application Support/Claude/scratch-workspaces/"
QUIT_TIMEOUT_S = 30 * 60
WRITE_KINDS = ("copy", "update", "meta", "remove")
PLAN_KINDS = WRITE_KINDS + ("skip_newer", "skip_deleted", "target_only", "unchanged", "excluded")


class SyncError(Exception):
    pass


def read_json(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def write_json_like_app(path, data):
    """Write the way the app does (compact, non-ASCII kept, no trailing newline), atomically."""
    tmp = path.with_name(".account-sync-" + path.name + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(json.dumps(data, separators=(",", ":"), ensure_ascii=False))
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def now_text():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def format_ms(ms):
    return datetime.fromtimestamp(ms / 1000).strftime("%Y-%m-%d %H:%M") if ms else "unknown"


class Account:
    def __init__(self, conf, claude_dir):
        self.label = conf["label"]
        self.uuid = conf["accountUuid"]
        self.dir = claude_dir / "claude-code-sessions" / self.uuid / conf["orgUuid"]
        if not self.dir.is_dir():
            raise SyncError(f"Session directory for account {self.label} not found: {self.dir}")
        self.sessions = {}  # "local_<id>" -> (path, data)
        unreadable = []
        for path in sorted(self.dir.glob("local_*.json")):
            try:
                self.sessions[path.stem] = (path, read_json(path))
            except (OSError, ValueError) as e:
                unreadable.append(f"{path}: {e}")
        if unreadable:
            raise SyncError("Some index files cannot be read; nothing was changed:\n  " + "\n  ".join(unreadable))
        # deleted_<id> is the tombstone the app leaves when a session is deleted;
        # <id> is the session's cliSessionId at the time of deletion.
        self.tombstones = {p.name[len("deleted_"):] for p in self.dir.glob("deleted_*")}

    def name(self):
        return f"{self.label} ({self.uuid[:8]})"


def lineage_ids(key, data):
    ids = {key[len("local_"):]}
    if data.get("cliSessionId"):
        ids.add(data["cliSessionId"])
    ids.update(data.get("priorRemoteCliSessionIds") or [])
    return ids


def is_scratch(data):
    return any(SCRATCH_MARK in (data.get(k) or "") for k in ("cwd", "originCwd"))


def activity(data):
    return data.get("lastActivityAt") or data.get("createdAt") or 0


def describe(data):
    host = (data.get("sshConfig") or {}).get("sshHost")
    where = f"SSH {host}" if host else f"local {data.get('cwd') or ''}"
    archived = ", archived" if data.get("isArchived") else ""
    return f"{data.get('title') or '(untitled)'}  [{where}{archived}]"


def accounts_path(state_dir):
    return state_dir / "accounts.json"


def load_accounts(state_dir):
    path = accounts_path(state_dir)
    if not path.exists():
        raise SyncError(f"No account config at {path}. Run `discover`, then `setup`.")
    confs = read_json(path)["accounts"]
    if len(confs) != 2:
        raise SyncError(f"{path} must list exactly two accounts")
    return confs


def current_account_uuid(claude_dir):
    try:
        return read_json(claude_dir / "config.json").get("lastKnownAccountUuid")
    except (OSError, ValueError) as e:
        raise SyncError(f"Cannot read the current account from config.json: {e}")


def resolve_direction(args):
    """Source = the other account, target = the account the app is signed in to now."""
    claude_dir = Path(args.claude_dir).expanduser()
    confs = load_accounts(Path(args.state_dir).expanduser())
    current = current_account_uuid(claude_dir)
    if args.expect_target and args.expect_target != current:
        raise SyncError(
            f"Current account mismatch: config.json says {current}, caller expected {args.expect_target}. "
            "Make sure the app is signed in to the account you want to use."
        )
    target_conf = next((c for c in confs if c["accountUuid"] == current), None)
    if target_conf is None:
        raise SyncError(f"The current account {current} is not in the account config")
    source_conf = next(c for c in confs if c is not target_conf)
    return Account(source_conf, claude_dir), Account(target_conf, claude_dir)


def load_state(state_dir):
    path = state_dir / "state.json"
    return read_json(path) if path.exists() else {}


def save_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def log(state_dir, line):
    state_dir.mkdir(parents=True, exist_ok=True)
    with open(state_dir / "sync.log", "a", encoding="utf-8") as f:
        f.write(f"{now_text()} {line}\n")


def build_plan(src, dst, synced):
    """synced: sessions both accounts had after the previous sync. A session that is in
    the target but not in the source counts as deleted in the source only if it is in
    this set (or the source has its tombstone); otherwise the target created it, and it
    is left for the next sync in the other direction."""
    plan = {k: [] for k in PLAN_KINDS}
    for key, (_, s) in src.sessions.items():
        if is_scratch(s):
            plan["excluded"].append(key)
        elif key in dst.sessions:
            t = dst.sessions[key][1]
            if activity(s) > activity(t):
                plan["update"].append(key)
            elif activity(s) < activity(t):
                plan["skip_newer"].append(key)
            elif any(s.get(f) != t.get(f) for f in META_FIELDS):
                plan["meta"].append(key)
            else:
                plan["unchanged"].append(key)
        elif key in synced or lineage_ids(key, s) & dst.tombstones:
            # Deleted in the target: do not bring it back. It stays in `synced`, so the
            # next sync in the other direction deletes it from this side too.
            plan["skip_deleted"].append(key)
        else:
            plan["copy"].append(key)
    for key, (_, t) in dst.sessions.items():
        if key in src.sessions or is_scratch(t):
            continue
        if key in synced or lineage_ids(key, t) & src.tombstones:
            plan["remove"].append(key)
        else:
            plan["target_only"].append(key)
    return plan


def synced_after(plan):
    kinds = ("copy", "update", "meta", "unchanged", "skip_newer", "skip_deleted")
    return sorted(k for kind in kinds for k in plan[kind])


def print_plan(src, dst, plan):
    print(f"Source (account you just left): {src.name()}")
    print(f"Target (current account):       {dst.name()}")
    sections = (
        ("copy", "+", src, "New in source, add to target"),
        ("update", "~", src, "Newer conversation in source, overwrite target"),
        ("meta", "~", src, "Title / archived state changed in source"),
        ("remove", "-", dst, "Deleted in source, move out of target (kept in the backup)"),
        ("skip_newer", "!", dst, "Skipped: target copy is newer (was the previous switch not synced?)"),
        ("skip_deleted", "!", src, "Skipped: deleted in target, not restored (removed from source on the next sync back)"),
        ("target_only", " ", dst, "Only in target, left as is (copied to source on the next sync back)"),
    )
    for kind, mark, side, title in sections:
        items = plan[kind]
        if not items:
            continue
        print(f"\n{title}: {len(items)}")
        for key in sorted(items, key=lambda k: -activity(side.sessions[k][1])):
            print(f"  {mark} {describe(side.sessions[key][1])}")
    print(f"\nUnchanged: {len(plan['unchanged'])}; scratch sessions not synced: {len(plan['excluded'])}.")
    if not any(plan[k] for k in WRITE_KINDS):
        print("Nothing to write.")


def app_running():
    out = subprocess.run(["ps", "-axo", "comm="], capture_output=True, text=True, check=True).stdout
    return any(line.strip().endswith(APP_SUFFIX) for line in out.splitlines())


def wait_for_quit():
    if app_running():
        print("Waiting for Claude to quit... press Cmd+Q in Claude. (Close this window to cancel; nothing will change.)", flush=True)
    deadline = time.time() + QUIT_TIMEOUT_S
    while True:
        while app_running():
            if time.time() > deadline:
                raise SyncError("Claude did not quit within 30 minutes. Gave up; nothing was changed.")
            time.sleep(1)
        time.sleep(3)  # let the app finish writing on its way out
        if not app_running():
            break
    print("Claude has quit.\n", flush=True)


def ensure_app_quit(args):
    if args.wait_for_quit:
        wait_for_quit()
    elif app_running() and not args.allow_running:
        raise SyncError("Claude is running. Quit it before changing the index: use `launch`, or pass --wait-for-quit.")


def backup_accounts(state_dir, tag, accounts, state):
    base = state_dir / "backups" / f"{datetime.now():%Y%m%d-%H%M%S}-{tag}"
    bdir, n = base, 1
    while bdir.exists():
        n += 1
        bdir = base.with_name(f"{base.name}-{n}")
    for acct in accounts:
        shutil.copytree(acct.dir, bdir / acct.label)
    save_json(bdir / "state.json", state)
    return bdir


def relaunch_app():
    print("\nReopening Claude.", flush=True)
    subprocess.run(["open", "-a", "Claude"], check=False)


def cmd_discover(args):
    claude_dir = Path(args.claude_dir).expanduser()
    root = claude_dir / "claude-code-sessions"
    if not root.is_dir():
        raise SyncError(f"{root} does not exist. Is the Claude desktop app installed and has the Code tab been used?")
    try:
        prefs = read_json(claude_dir / "claude_desktop_config.json")["preferences"]["epitaxyPrefs"]
        known = set(prefs.get("dframe-code-sections") or {})
    except (OSError, ValueError, KeyError, TypeError):
        known = set()
    try:
        current = current_account_uuid(claude_dir)
    except SyncError:
        current = None
    found = 0
    for acct_dir in sorted(p for p in root.iterdir() if p.is_dir()):
        for org_dir in sorted(p for p in acct_dir.iterdir() if p.is_dir()):
            key = f"{acct_dir.name}/{org_dir.name}"
            sessions = []
            for path in org_dir.glob("local_*.json"):
                try:
                    sessions.append(read_json(path))
                except (OSError, ValueError):
                    continue
            # Skip leftovers that only hold scheduled-tasks.json
            if not sessions and not any(org_dir.glob("deleted_*")) and key not in known:
                continue
            found += 1
            sessions.sort(key=activity, reverse=True)
            mark = "   <- signed in now" if acct_dir.name == current else ""
            last = f", last active {format_ms(activity(sessions[0]))}" if sessions else ""
            print(f"{key}{mark}\n  {len(sessions)} sessions{last}")
            for data in sessions[:3]:
                print(f"    {describe(data)}")
    if not found:
        raise SyncError(f"No Code session directories found under {root}")
    path = accounts_path(Path(args.state_dir).expanduser())
    if path.exists():
        print(f"\nCurrent config ({path}):")
        for conf in read_json(path)["accounts"]:
            print(f"  {conf['accountUuid']}/{conf['orgUuid']}={conf['label']}")


def cmd_setup(args):
    claude_dir = Path(args.claude_dir).expanduser()
    if len(args.pairs) != 2:
        raise SyncError("setup takes exactly two ACCOUNT_UUID/ORG_UUID=LABEL arguments")
    accounts = []
    for spec in args.pairs:
        key, eq, label = spec.partition("=")
        acct, slash, org = key.partition("/")
        if not (eq and slash and acct and org and label) or "/" in label or label in (".", ".."):
            raise SyncError(f"Bad argument {spec!r}; expected ACCOUNT_UUID/ORG_UUID=LABEL")
        if not (claude_dir / "claude-code-sessions" / acct / org).is_dir():
            raise SyncError(f"No session directory for {key}; run `discover` to see what exists")
        accounts.append({"label": label, "accountUuid": acct, "orgUuid": org})
    if accounts[0]["accountUuid"] == accounts[1]["accountUuid"]:
        raise SyncError("The two entries must be different accounts")
    if accounts[0]["label"] == accounts[1]["label"]:
        raise SyncError("The two labels must differ")
    path = accounts_path(Path(args.state_dir).expanduser())
    save_json(path, {"accounts": accounts})
    print(f"Saved {path}:")
    for conf in accounts:
        print(f"  {conf['label']}: {conf['accountUuid']}/{conf['orgUuid']}")


def cmd_plan(args):
    src, dst = resolve_direction(args)
    state = load_state(Path(args.state_dir).expanduser())
    print_plan(src, dst, build_plan(src, dst, set(state.get("synced", []))))


def cmd_apply(args):
    ensure_app_quit(args)
    state_dir = Path(args.state_dir).expanduser()
    src, dst = resolve_direction(args)
    state = load_state(state_dir)
    plan = build_plan(src, dst, set(state.get("synced", [])))
    print_plan(src, dst, plan)

    written, stripped, removed, bdir = {}, {}, [], None
    if any(plan[k] for k in WRITE_KINDS):
        bdir = backup_accounts(state_dir, f"{src.label}-to-{dst.label}", [src, dst], state)
        for key in plan["copy"] + plan["update"]:
            data = dict(src.sessions[key][1])
            if data.get("sshRemoteProcessId"):
                stripped[key] = data["sshRemoteProcessId"]
            for field in STRIP_FIELDS:
                data.pop(field, None)
            write_json_like_app(dst.dir / f"{key}.json", data)
            written[key] = data.get("cliSessionId")
        for key in plan["meta"]:
            s = src.sessions[key][1]
            data = dict(dst.sessions[key][1])
            for field in META_FIELDS:
                if field in s:
                    data[field] = s[field]
                else:
                    data.pop(field, None)
            write_json_like_app(dst.dir / f"{key}.json", data)
        for key in plan["remove"]:
            removed_dir = bdir / "removed-from-target"
            removed_dir.mkdir(exist_ok=True)
            shutil.move(str(dst.sessions[key][0]), str(removed_dir / f"{key}.json"))
            removed.append(key)

    state["synced"] = synced_after(plan)
    state["lastApply"] = {
        "at": now_text(),
        "source": src.uuid,
        "target": dst.uuid,
        "written": written,
        "metaOnly": plan["meta"],
        "strippedProcessIds": stripped,
        "removed": removed,
        "backup": str(bdir) if bdir else None,
    }
    save_json(state_dir / "state.json", state)
    counts = " ".join(f"{k}={len(plan[k])}" for k in PLAN_KINDS if plan[k])
    log(state_dir, f"apply {src.label}->{dst.label} {counts} backup={bdir}")
    print(f"\nDone: {len(written)} copied, {len(plan['meta'])} renamed/archived, {len(removed)} removed.")
    if bdir:
        print(f"Backup of both accounts before the change: {bdir}")


def cmd_verify(args):
    state_dir = Path(args.state_dir).expanduser()
    last = load_state(state_dir).get("lastApply")
    if not last:
        raise SyncError("No sync has been applied yet.")
    conf = next((c for c in load_accounts(state_dir) if c["accountUuid"] == last["target"]), None)
    if conf is None:
        raise SyncError(f"The last sync's target account {last['target']} is not in the account config")
    dst = Account(conf, Path(args.claude_dir).expanduser())
    print(f"Checking the sync of {last['at']} (written to {dst.name()}):")
    problems = 0
    for key in list(last["written"]) + last.get("metaOnly", []):
        if key not in dst.sessions:
            print(f"  ✗ {key}: index file is gone")
            problems += 1
    for key, cli in last["written"].items():
        if key not in dst.sessions:
            continue
        data = dst.sessions[key][1]
        old_proc = last.get("strippedProcessIds", {}).get(key)
        proc = data.get("sshRemoteProcessId")
        if old_proc and proc == old_proc:
            status, ok = "re-attached to the source account's old remote process (may still bill the source account)", False
        elif data.get("cliSessionId") == cli:
            status, ok = ("remote process started under the current account" if proc else "not opened yet"), True
        elif cli in (data.get("priorRemoteCliSessionIds") or []):
            status, ok = f"opened and continued (new transcript {data['cliSessionId'][:8]})", True
        else:
            status, ok = f"cliSessionId changed to {data.get('cliSessionId')} and is not in its resume history", False
        problems += not ok
        print(f"  {'✓' if ok else '✗'} {describe(data)}: {status}")
    for key in last.get("metaOnly", []):
        if key in dst.sessions:
            print(f"  ✓ {describe(dst.sessions[key][1])}: title / archived state synced")
    for key in last["removed"]:
        if key in dst.sessions:
            print(f"  ✗ {describe(dst.sessions[key][1])}: was removed but is back")
            problems += 1
    if not (last["written"] or last.get("metaOnly") or last["removed"]):
        print("  The last sync did not write anything.")
    print("\nAll good." if problems == 0 else f"\n{problems} problem(s); see the lines marked ✗.")
    return 1 if problems else 0


def latest_backup(state_dir):
    root = state_dir / "backups"
    backups = sorted(p for p in root.iterdir() if p.is_dir()) if root.is_dir() else []
    if not backups:
        raise SyncError("There are no backups yet.")
    return backups[-1]


def cmd_restore(args):
    ensure_app_quit(args)
    state_dir = Path(args.state_dir).expanduser()
    bdir = Path(args.backup).expanduser() if args.backup else latest_backup(state_dir)
    claude_dir = Path(args.claude_dir).expanduser()
    accounts = [Account(c, claude_dir) for c in load_accounts(state_dir) if (bdir / c["label"]).is_dir()]
    if not accounts:
        raise SyncError(f"{bdir} is not a valid backup")
    safety = backup_accounts(state_dir, "before-restore", accounts, load_state(state_dir))
    for acct in accounts:
        saved = bdir / acct.label
        for path in acct.dir.glob("local_*.json"):
            if not (saved / path.name).exists():
                path.unlink()  # just copied into `safety`
        for path in saved.glob("local_*.json"):
            shutil.copy2(path, acct.dir / path.name)
        print(f"Restored the index of {acct.name()}")
    if (bdir / "state.json").exists():
        shutil.copy2(bdir / "state.json", state_dir / "state.json")
    log(state_dir, f"restore from={bdir} safety={safety}")
    print(f"\nRestored from {bdir}. The state before restoring is saved in {safety}")


def cmd_launch(args):
    # Catch what can be caught here, rather than in the Terminal window
    if args.action == "apply":
        resolve_direction(args)
    state_dir = Path(args.state_dir).expanduser()
    cmd = [
        sys.executable, "-I", str(Path(__file__).resolve()), args.action,
        "--wait-for-quit", "--relaunch",
        "--claude-dir", args.claude_dir, "--state-dir", args.state_dir,
    ]
    if args.expect_target:
        cmd += ["--expect-target", args.expect_target]
    if args.action == "restore":
        backup = Path(args.backup).expanduser() if args.backup else latest_backup(state_dir)
        cmd += ["--backup", str(backup)]
    state_dir.mkdir(parents=True, exist_ok=True)
    script = state_dir / "run.command"
    script.write_text(
        "#!/bin/bash\n"
        f"{shlex.join(cmd)}\n"
        "echo\n"
        "read -n 1 -s -r -p 'Press any key to close this window'\n"
        "echo\n",
        encoding="utf-8",
    )
    os.chmod(script, 0o700)
    subprocess.run(["open", "-a", "Terminal", str(script)], check=True)
    print("Opened a Terminal window. It waits for Claude to quit, then applies the change and reopens Claude.")


def main():
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--claude-dir", default=str(DEFAULT_CLAUDE_DIR), help="Claude desktop data directory")
    common.add_argument("--state-dir", default=str(DEFAULT_STATE_DIR), help="where config, state, backups and logs live")
    common.add_argument("--expect-target", help="account UUID the caller believes is signed in; abort if it differs")
    common.add_argument("--backup", help="backup directory for restore (default: latest)")
    common.add_argument("--wait-for-quit", action="store_true", help="wait for Claude to quit before changing anything")
    common.add_argument("--relaunch", action="store_true", help="reopen Claude afterwards")
    common.add_argument("--allow-running", action="store_true", help=argparse.SUPPRESS)  # tests only

    parser = argparse.ArgumentParser(description="Sync the Claude desktop Code-tab session list between two accounts.")
    sub = parser.add_subparsers(dest="cmd", required=True)
    for name in ("discover", "plan", "apply", "verify", "restore"):
        sub.add_parser(name, parents=[common])
    sub.add_parser("setup", parents=[common]).add_argument("pairs", nargs="*", metavar="ACCOUNT_UUID/ORG_UUID=LABEL")
    sub.add_parser("launch", parents=[common]).add_argument("action", choices=("apply", "restore"))
    args = parser.parse_args()

    handlers = {
        "discover": cmd_discover, "setup": cmd_setup, "plan": cmd_plan, "apply": cmd_apply,
        "verify": cmd_verify, "restore": cmd_restore, "launch": cmd_launch,
    }
    code = 0
    try:
        code = handlers[args.cmd](args) or 0
    except SyncError as e:
        print(f"\n✗ {e}", file=sys.stderr)
        code = 1
    finally:
        if args.relaunch and args.cmd in ("apply", "restore"):
            relaunch_app()
    sys.exit(code)


if __name__ == "__main__":
    main()
