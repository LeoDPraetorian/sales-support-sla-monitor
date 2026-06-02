#!/usr/bin/env python3
"""
Sales-Support SLA monitor.

Runs on a schedule (GitHub Actions cron). Two jobs per run:

  1. NOTIFY  — for every new message that landed on sales-support@praetorian.com
               since the last run, post a header-only card to Slack
               (Subject / From / Received / thread link). Each notified message
               is tagged with a Gmail label so it is never announced twice.

  2. SLA     — for every active sales-support thread, inspect the LATEST message:
                 * latest sender is EXTERNAL (not @praetorian.com) and older than
                   SLA_HOURS  -> breach: post an escalation (once per arming).
                 * latest sender is INTERNAL (@praetorian.com)      -> satisfied:
                   clear any prior breach tag so the next external reply re-arms.

State is stored entirely as Gmail labels — no database, no committed state file.

Auth (whichever set of env vars is present):
  A) OAuth refresh token (a mailbox that RECEIVES the alias, e.g. a group member):
       GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET, GOOGLE_REFRESH_TOKEN
  B) Service account w/ domain-wide delegation (impersonates GMAIL_USER):
       GOOGLE_SERVICE_ACCOUNT_JSON  (the JSON key, as a string)

Notes on the mailbox:
  sales-support@ is almost certainly a Google Group (it fans out to inboxes), and
  Groups have no Gmail mailbox you can query. So read a MEMBER inbox and filter by
  the alias in headers. Set GMAIL_USER to that member (default 'me' for option A).
"""

import json
import os
import sys
import time
import urllib.request
from email.utils import parseaddr

from google.oauth2.credentials import Credentials as UserCredentials
from google.oauth2.service_account import Credentials as SACredentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build

# ---------------------------------------------------------------- config
SCOPES = ["https://www.googleapis.com/auth/gmail.modify"]

ALIAS = os.environ.get("SALES_SUPPORT_ADDRESS", "sales-support@praetorian.com")
INTERNAL_DOMAINS = [
    d.strip().lower()
    for d in os.environ.get("INTERNAL_DOMAINS", "praetorian.com").split(",")
    if d.strip()
]
SLA_HOURS = float(os.environ.get("SLA_HOURS", "24"))
GMAIL_USER = os.environ.get("GMAIL_USER", "me")
SLACK_WEBHOOK_URL = os.environ.get("SLACK_WEBHOOK_URL", "")
SLACK_BOT_TOKEN = os.environ.get("SLACK_BOT_TOKEN", "")
SLACK_CHANNEL = os.environ.get("SLACK_CHANNEL", "")
DRY_RUN = os.environ.get("DRY_RUN", "").lower() in ("1", "true", "yes")

# how far back to look. NOTIFY only needs a short window; SLA needs enough to
# still catch a thread that has been waiting just over SLA_HOURS.
NOTIFY_WINDOW = os.environ.get("NOTIFY_WINDOW", "2d")
SLA_WINDOW = os.environ.get("SLA_WINDOW", "4d")

LABEL_NOTIFIED = "sla/slack-notified"
LABEL_BREACHED = "sla/breach-posted"

ALIAS_MATCH = f"{{to:{ALIAS} cc:{ALIAS}}}"  # Gmail OR-group: matches To OR Cc


# ---------------------------------------------------------------- auth
def get_service():
    sa_json = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON", "").strip()
    if sa_json:
        info = json.loads(sa_json)
        creds = SACredentials.from_service_account_info(info, scopes=SCOPES)
        if GMAIL_USER and GMAIL_USER != "me":
            creds = creds.with_subject(GMAIL_USER)
        return build("gmail", "v1", credentials=creds, cache_discovery=False)

    cid = os.environ.get("GOOGLE_CLIENT_ID", "").strip()
    csec = os.environ.get("GOOGLE_CLIENT_SECRET", "").strip()
    rtok = os.environ.get("GOOGLE_REFRESH_TOKEN", "").strip()
    if cid and csec and rtok:
        creds = UserCredentials(
            token=None,
            refresh_token=rtok,
            client_id=cid,
            client_secret=csec,
            token_uri="https://oauth2.googleapis.com/token",
            scopes=SCOPES,
        )
        creds.refresh(Request())
        return build("gmail", "v1", credentials=creds, cache_discovery=False)

    sys.exit("ERROR: no Gmail credentials. Set GOOGLE_SERVICE_ACCOUNT_JSON or "
             "GOOGLE_CLIENT_ID/SECRET/REFRESH_TOKEN.")


# ---------------------------------------------------------------- helpers
def ensure_label(svc, name):
    existing = svc.users().labels().list(userId=GMAIL_USER).execute().get("labels", [])
    for lab in existing:
        if lab["name"] == name:
            return lab["id"]
    if DRY_RUN:
        print(f"[dry-run] would create label {name!r}")
        return None
    created = svc.users().labels().create(
        userId=GMAIL_USER,
        body={"name": name, "labelListVisibility": "labelShow",
              "messageListVisibility": "show"},
    ).execute()
    return created["id"]


def header(msg, key):
    for h in msg.get("payload", {}).get("headers", []):
        if h["name"].lower() == key.lower():
            return h["value"]
    return ""


def sender_domain(from_value):
    _, addr = parseaddr(from_value)
    return addr.split("@")[-1].lower() if "@" in addr else ""


def is_internal(from_value):
    return sender_domain(from_value) in INTERNAL_DOMAINS


def thread_link(thread_id):
    return f"https://mail.google.com/mail/u/0/#all/{thread_id}"


def post_slack(text):
    # Prefer bot token (chat.postMessage) — no browser flow, never expires.
    # Fall back to an incoming webhook URL if that's what's configured.
    if DRY_RUN or (not SLACK_BOT_TOKEN and not SLACK_WEBHOOK_URL):
        print(f"[dry-run/no-slack] would post to Slack:\n{text}\n")
        return
    if SLACK_BOT_TOKEN:
        body = json.dumps({
            "channel": SLACK_CHANNEL, "text": text, "unfurl_links": False,
        }).encode("utf-8")
        req = urllib.request.Request(
            "https://slack.com/api/chat.postMessage", data=body, method="POST",
            headers={"Content-Type": "application/json; charset=utf-8",
                     "Authorization": f"Bearer {SLACK_BOT_TOKEN}"})
        with urllib.request.urlopen(req, timeout=20) as resp:
            data = json.loads(resp.read())
        if not data.get("ok"):
            raise RuntimeError(f"Slack chat.postMessage failed: {data.get('error')}")
        return
    body = json.dumps({"text": text}).encode("utf-8")
    req = urllib.request.Request(
        SLACK_WEBHOOK_URL, data=body,
        headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=20) as resp:
        resp.read()


def add_label(svc, msg_id, label_id):
    if DRY_RUN:
        return
    svc.users().messages().modify(
        userId=GMAIL_USER, id=msg_id, body={"addLabelIds": [label_id]}).execute()


def remove_label_from_thread(svc, thread, label_id):
    if DRY_RUN:
        return
    for m in thread.get("messages", []):
        if label_id in m.get("labelIds", []):
            svc.users().messages().modify(
                userId=GMAIL_USER, id=m["id"],
                body={"removeLabelIds": [label_id]}).execute()


def fmt_age(hours):
    return f"{int(hours)}h" if hours < 48 else f"{int(hours / 24)}d"


# ---------------------------------------------------------------- jobs
def notify_new(svc, notified_id):
    q = f"{ALIAS_MATCH} newer_than:{NOTIFY_WINDOW} -label:{LABEL_NOTIFIED}"
    resp = svc.users().messages().list(userId=GMAIL_USER, q=q, maxResults=100).execute()
    msgs = resp.get("messages", [])
    print(f"NOTIFY: {len(msgs)} new message(s) matching {q!r}")
    for ref in msgs:
        msg = svc.users().messages().get(
            userId=GMAIL_USER, id=ref["id"], format="metadata",
            metadataHeaders=["Subject", "From", "Date"]).execute()
        subject = header(msg, "Subject") or "(no subject)"
        frm = header(msg, "From") or "(unknown)"
        date = header(msg, "Date")
        text = (
            ":envelope_with_arrow: *New sales-support email*\n"
            f"*Subject:* {subject}\n"
            f"*From:* {frm}\n"
            f"*Received:* {date}\n"
            f"<{thread_link(msg['threadId'])}|Open thread>"
        )
        post_slack(text)
        add_label(svc, ref["id"], notified_id)


def check_sla(svc, breached_id):
    q = f"{ALIAS_MATCH} newer_than:{SLA_WINDOW}"
    resp = svc.users().messages().list(userId=GMAIL_USER, q=q, maxResults=200).execute()
    thread_ids = {m["threadId"] for m in resp.get("messages", [])}
    print(f"SLA: inspecting {len(thread_ids)} active thread(s)")
    now_ms = time.time() * 1000

    for tid in thread_ids:
        thread = svc.users().threads().get(
            userId=GMAIL_USER, id=tid, format="metadata",
            metadataHeaders=["From", "Subject", "Date"]).execute()
        messages = thread.get("messages", [])
        if not messages:
            continue
        latest = max(messages, key=lambda m: int(m.get("internalDate", "0")))
        already_breached = any(
            breached_id in m.get("labelIds", []) for m in messages)

        latest_from = header(latest, "From")
        if is_internal(latest_from):
            # Praetorian replied last -> satisfied. Re-arm for next external reply.
            if already_breached:
                remove_label_from_thread(svc, thread, breached_id)
                print(f"  thread {tid}: satisfied, breach tag cleared")
            else:
                print(f"  thread {tid}: satisfied (latest internal: {latest_from})")
            continue

        age_h = (now_ms - int(latest.get("internalDate", "0"))) / 3_600_000
        if age_h <= SLA_HOURS:
            print(f"  thread {tid}: external, {age_h:.1f}h — within SLA")
            continue
        if already_breached:
            print(f"  thread {tid}: external, {age_h:.1f}h — already escalated")
            continue

        subject = header(latest, "Subject") or "(no subject)"
        frm = header(latest, "From") or "(unknown)"
        date = header(latest, "Date")
        text = (
            ":warning: *24h SLA breach — awaiting Praetorian response*\n"
            f"*Subject:* {subject}\n"
            f"*Last reply from:* {frm}\n"
            f"*Waiting since:* {date}  (_{fmt_age(age_h)}_)\n"
            f"<{thread_link(tid)}|Open thread>"
        )
        post_slack(text)
        add_label(svc, latest["id"], breached_id)
        print(f"  thread {tid}: BREACH escalated ({age_h:.1f}h)")


def main():
    svc = get_service()
    notified_id = ensure_label(svc, LABEL_NOTIFIED)
    breached_id = ensure_label(svc, LABEL_BREACHED)
    notify_new(svc, notified_id)
    check_sla(svc, breached_id)
    print("done")


if __name__ == "__main__":
    main()
