# Feishu rich-text (post) alerts

**Date:** 2026-08-14 · **Status:** implemented

## Problem

All Feishu alerts were `msg_type: "text"` one-liners with bare 100-char GitLab
URLs — functional but raw. The `commit:` label also under-specified what the
link is (the MR head at review time).

## Decisions (per CX)

- Format: `msg_type: "post"` rich text — bold title line + labeled clickable
  links. Not interactive cards (keep it simple), not tidied plain text.
- Scope: all alert types (review results, force push, merge-without-MR).
  Plain `notify()` stays for one-liners (sweep-error notice, usermap hint).
- Label: `head commit:` where the link is the branch/MR head (review SHA,
  force-push new tip); `commit:` for individual un-MR'd commits.

## Shape

`Feishu.notify_post(title, lines, at_gitlab_user=None)` sends
`{"zh_cn": {"title": ..., "content": lines}}` (im/v1 takes the locale key
directly — no `"post"` wrapper; that wrapper is webhook-bot-only). Lines are
lists of `{"tag": "text"|"a", ...}` segments; link text is the short SHA or
`!iid`, never a bare URL. A mapped author becomes an `{"tag": "at"}` first
line; unmapped becomes `@username` text, mirroring `notify()`.

Message titles: `✅ Approved: {title}` / `📝 Review: {title} — {n} issue(s)` /
`⚠️ Force push: {path} {branch}` / `⚠️ Merge without MR: {path} {branch}`.
