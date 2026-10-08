# claude-account-sync

A Claude Code skill that keeps the **Claude desktop app's Code-tab session list** in sync between two Claude accounts on the same Mac. Switch accounts, run `/account-sync`, and the sessions you were working in, including SSH sessions, show up in the other account's sidebar, ready to continue.

> Unofficial and not affiliated with Anthropic. It edits the desktop app's undocumented local index files. It was built and tested against Claude desktop 2.26454 and Claude Code 2.1.29x on macOS (October 2026). A future app update may change the format; if so, roll back (see below) and stop using it.

**If you are Claude:** install by cloning this repo into `~/.claude/skills/account-sync`, then follow [SKILL.md](SKILL.md). It covers first-time setup and the sync flow.

## Why

The desktop app keeps one small index file per Code session at:

```
~/Library/Application Support/Claude/claude-code-sessions/<account UUID>/<org UUID>/local_<id>.json
```

The file has no account field; the directory alone decides which account sees the session. When you sign in to another account, the app reads a different directory and the sidebar looks empty. The conversations still exist: SSH transcripts are on the server, and local ones are under `~/.claude/projects/`. Only the index entries are missing.

## What a sync does

On each run, the account you just left is the **source** and the account you are signed in to now is the **target**:

| In the source | In the target |
|---|---|
| New session, or newer conversation | Copied over, without `sshReattach` / `sshRemoteProcessId`, so the app starts a fresh remote process under the current account instead of re-attaching to one that holds the other account's token |
| Only renamed or (un)archived | Just those fields are copied |
| Deleted since the last sync | Moved out of the target into the backup |
| Not touched | Left alone |

Safety rules:

- A target copy that is newer than the source is skipped and reported.
- A session the target deleted is not brought back; the delete carries over on the next sync in the other direction.
- "No folder" (scratch) sessions are never synced, because their working directory belongs to one account.
- Conversation transcripts are never touched.
- The app must be closed while index files change. Otherwise it can write its in-memory copy back over them. So the write happens in a Terminal window that waits for you to press Cmd+Q, then reopens Claude.
- Both accounts' directories are backed up before every write, so any sync can be rolled back.

## Install

Requirements: macOS, the Claude desktop app with the Code tab, two Claude accounts, and the system `/usr/bin/python3`. Only the standard library is used.

```bash
git clone https://github.com/Tonyyouyou/claude-account-sync.git ~/.claude/skills/account-sync
```

The skill is picked up by new sessions.

## Use

1. In the desktop app, switch to the other account.
2. Start a **local session with no folder**; the skill reads local files, so it cannot run inside an SSH session. Type `/account-sync`, or tell Claude you switched accounts.
3. The first time, Claude runs `discover` and asks which two account directories are yours and what to call them.
4. Claude shows what will change and opens a Terminal window. Press **Cmd+Q**. The window applies the sync and reopens Claude.
5. Back in that session, say "check". Claude runs `verify` to confirm the synced sessions are intact and that sessions you opened run under the current account.

To undo the last sync, ask Claude to roll back, or run `sync.py launch restore` and press Cmd+Q.

## Commands

All commands accept `--claude-dir` (default `~/Library/Application Support/Claude`) and `--state-dir` (default `~/.claude/account-sync`).

| Command | What it does |
|---|---|
| `sync.py discover` | List the account/org session directories on this Mac (read-only) |
| `sync.py setup ACCT/ORG=label ACCT/ORG=label` | Save the two accounts to `~/.claude/account-sync/accounts.json` |
| `sync.py plan` | Show what a sync would change (read-only) |
| `sync.py launch apply` | Open a Terminal window that waits for Claude to quit, syncs, and reopens Claude |
| `sync.py apply` | Sync immediately; refuses while Claude is running unless `--wait-for-quit` is given |
| `sync.py verify` | Check the sessions written by the last sync; exits 1 on problems |
| `sync.py launch restore [--backup DIR]` / `restore` | Restore both accounts from a backup (latest by default) |

`--expect-target UUID` makes `plan`, `apply` and `launch apply` abort if the signed-in account is not the expected one.

State lives in `~/.claude/account-sync/`:
- `accounts.json`: the two accounts
- `state.json`: which sessions were in sync after the last run, and what it wrote
- `backups/`
- `sync.log`

## Things to know

- **Sync on every switch.** The deletion rule relies on it. If you skip a sync and then use the same session in both accounts, the copy with the latest activity wins. The other copy's transcript is not lost, but its index entry is only in the backup.
- **The first message after a switch costs more.** Prompt caches are per account, so the first message you send in a session on the other account reprocesses its whole current context. Caches also expire after about an hour of inactivity, so this only matters for sessions you were using right before switching. To keep it small, switch before the old account is completely out of usage: run `/compact` in the active session while its cache is still warm, then switch.
- **Old remote processes stay on the server.** After a switch, the other account's remote processes for sessions you continue are no longer used. They are idle, but you may want to clean them up now and then.
- **The app forks the transcript on resume.** When a synced SSH session is first opened on the other account, the app starts a new transcript file that carries the history, and records the old ID in `priorRemoteCliSessionIds`. The transcript files on the server grow by one per switch for each session you continue.
- Pins, custom sidebar groups and sections are stored per account and are not synced.
- Only two accounts are supported.

## Tests

```bash
cd ~/.claude/skills/account-sync && /usr/bin/python3 -m unittest discover -s tests -v
```

The tests build a synthetic Claude data directory in a temporary folder and never touch the real one.

## 中文简介

在同一台 Mac 上切换两个 Claude 账号后，桌面端 Code 标签页的会话列表会变空。这是因为会话索引按账号分目录存放，对话记录本身并没有丢。

安装：把仓库克隆到 `~/.claude/skills/account-sync`。

每次切号后：
1. 开一个不选文件夹的本机会话，输入 `/account-sync`。
2. Claude 先预览要改的内容，然后打开一个终端窗口。
3. 按 Cmd+Q 退出 Claude，同步完成后 Claude 会自动重新打开。

新增、更新、改名、归档和删除都会同步过去。每次写入前都会自动备份，可以随时回滚。
