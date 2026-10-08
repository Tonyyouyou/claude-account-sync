---
name: account-sync
description: Sync the Claude desktop app's Code-tab session list (new, updated, renamed/archived and deleted sessions) between two Claude accounts on the same Mac, so both accounts share one sidebar. Use right after the user switches accounts, or when they say they switched accounts, ask to sync sessions, or run /account-sync — including in Chinese, e.g. 「切号了」「同步会话」「同步索引」. Also use it to check the last sync or roll it back.
---

# Sync Code sessions after switching accounts

The user works with two Claude accounts on one Mac. After every account switch, this skill copies the Code-tab session index from the account they just left to the account they are on now. `sync.py`, in this skill's directory, does all the work. Your job is to run it in the order below and relay the results in the user's language.

## Background

- Each Code session has an index file at `~/Library/Application Support/Claude/claude-code-sessions/<account UUID>/<org UUID>/local_*.json`. The file has no account field, so the directory alone decides which account sees it. The conversation transcripts themselves are not account-specific, and this skill never touches them.
- Direction is always: the other account → the account in `config.json`'s `lastKnownAccountUuid`. This assumes the user syncs on every switch.
- The app must be closed while index files change, or it may write its in-memory copy back over them. So `launch` opens a Terminal window that waits for the user to press Cmd+Q, applies the change, then reopens Claude.
- Rules the script applies:
  - Sessions that are new, or have newer conversation in the source, are copied to the target without `sshReattach` and `sshRemoteProcessId`. That way the app starts a fresh remote process under the current account instead of re-attaching to one that holds the other account's token.
  - If only the title or archived state changed, only those fields are copied.
  - A session both accounts had after the last sync that is now gone from the source is moved out of the target into a backup. This is how deletes propagate.
  - A target copy that is newer, or a session the target deleted, is skipped and reported.
  - "No folder" (scratch) sessions are never synced.
- Every write first backs up both accounts' directories to `~/.claude/account-sync/backups/`. The account config, sync state and log also live in `~/.claude/account-sync/`.

## First-time setup (when `~/.claude/account-sync/accounts.json` does not exist)

1. Run `/usr/bin/python3 -I <skill dir>/sync.py discover`. It lists each `ACCOUNT_UUID/ORG_UUID` directory with a session count and a few session titles, and marks the account that is signed in now.
2. Show the list to the user and agree on which two directories are their two accounts. Ask for a short label for each, such as `work` and `personal` or the email's local part. Use the session titles and the "signed in now" marker to tell the accounts apart.
3. Run `/usr/bin/python3 -I <skill dir>/sync.py setup <ACCT>/<ORG>=<label> <ACCT>/<ORG>=<label>`.

## Sync flow

1. **Run on this Mac.** If the working directory is a remote path (an SSH session, for example `/home/...`), stop. Ask the user to start a local session with no folder and run the skill there.
2. **Pin the current account.** If the working directory is under `…/Claude/scratch-workspaces/<account UUID>/<org UUID>/…`, pass that account UUID as `--expect-target`. Otherwise omit the flag.
3. **Preview.** Run `/usr/bin/python3 -I <skill dir>/sync.py plan --expect-target <UUID>`. Tell the user the direction and how many sessions will be added, updated, renamed or archived, and removed, with titles.
   - If anything is skipped, say so and continue; skipped sessions are left as they are. "Target copy is newer" usually means the previous switch was not synced, so mention that they can roll back afterwards.
   - If the output says "Nothing to write", stop here.
4. **Apply.** Run `/usr/bin/python3 -I <skill dir>/sync.py launch apply --expect-target <UUID>`. Then tell the user that a Terminal window is open, to press Cmd+Q now, that Claude will reopen when the sync is done, and that they can come back to this session and say "check". End your turn with no further tool calls, because quitting Claude interrupts this session.
   - If `open` is blocked by the sandbox, retry once with `dangerouslyDisableSandbox`.
   - If it still fails, give manual steps: press Cmd+Q, then in Terminal.app run `/usr/bin/python3 -I <skill dir>/sync.py apply --relaunch`.
5. **Check.** When the user comes back and says "check", run `/usr/bin/python3 -I <skill dir>/sync.py verify` and relay the result.
   - If a session "re-attached to the source account's old remote process", tell the user it may still be billed to the other account. They should stop the current task in that session, then send a message so the app starts a new process.

## Rollback

- Run `sync.py launch restore` and ask the user to press Cmd+Q. This restores both accounts from the latest backup.
- To use an older backup, add `--backup <dir>`; backups are listed in `~/.claude/account-sync/backups/`.
- The state just before restoring is backed up too, so a restore can itself be undone.

## Do not

- Do not run `apply` or `restore` without `launch` or `--wait-for-quit` while Claude is running. Do not edit or delete index files by hand.
- Do not delete backups. Each one is a few hundred KB.
- Do not touch transcripts on remote servers.
