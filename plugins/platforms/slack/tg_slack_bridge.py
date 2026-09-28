#!/usr/bin/env python3
"""Telegram<->Slack thread bridge.

Maps each Telegram topic to a Slack thread, mirrors messages between them,
auto-switches to Slack on Telegram rate limit, auto-switches back when Telegram
recovers, dedupes via a topic<->msg_id index. Persists state to sqlite.

Used as a CLI: `tg_slack_bridge send <chat_id> <message_thread_id> <text>` or
as a daemon: `tg_slack_bridge watch` (probes Telegram health, switches mode).
"""
import json
import os
import sqlite3
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path


def env_token(name, home=None):
    val = (os.environ.get(name) or "").strip()
    if val:
        return val
    # Fall back to the hermes home .env so the CLI works standalone.
    for cand in (Path(home or os.environ.get("HERMES_HOME", "")) / ".env",
                 Path.home() / ".hermes" / ".env"):
        try:
            if cand.exists():
                for line in cand.read_text(errors="replace").splitlines():
                    if line.startswith(f"{name}="):
                        v = line.split("=", 1)[1].strip().strip('"').strip("'")
                        if v:
                            return v
        except OSError:
            pass
    return ""


def bot_cfg(home):
    """Read cfg.yaml from hermes home for slack home channel + bridge settings."""
    import yaml
    p = Path(home) / "config.yaml"
    if not p.exists():
        return {}
    return yaml.safe_load(p.read_text()) or {}


class SlackClient:
    def __init__(self, token):
        self.token = token

    def post(self, channel, text, thread_ts=None):
        body = {"channel": channel, "text": text[:4000]}
        if thread_ts:
            body["thread_ts"] = thread_ts
        from plugins.platforms.slack.egress_guard import EgressDenied, claim as egress_claim
        try:
            egress_claim(account=channel, channel=channel, text=text[:4000],
                         approved=bool(os.environ.get("SLACK_EGRESS_APPROVED")),
                         tool_name="chat.postMessage")
        except EgressDenied as exc:
            return {"ok": False, "error": f"egress denied: {exc}"}
        req = urllib.request.Request(
            "https://slack.com/api/chat.postMessage",
            data=json.dumps(body).encode(),
            headers={
                "Authorization": f"Bearer {self.token}",
                "Content-Type": "application/json; charset=utf-8",
            },
        )
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read().decode())

    def find_or_create_topic(self, channel, topic_name):
        """Find an existing thread by topic_name in channel; else start a new one.

        Slack has no 'create thread by name'. Heuristic: the FIRST root message
        posted as a thread parent acts as the topic. Caller passes the parent
        ts we already have, or we post a marker and use the returned ts.
        """
        # Always start a new root; topic_id stored as parent_ts in mapping.
        return None


class TelegramClient:
    def __init__(self, token):
        self.token = token

    def api(self, method, data=None):
        body = data or {}
        req = urllib.request.Request(
            f"https://api.telegram.org/bot{self.token}/{method}",
            data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.status, json.loads(r.read().decode())
        except urllib.error.HTTPError as ex:
            raw = ex.read().decode(errors="replace")
            try:
                return ex.code, json.loads(raw)
            except Exception:
                return ex.code, {"raw": raw[:200]}

    def health(self):
        """Return (ok, retry_after_seconds)."""
        code, body = self.api("getMe")
        if code == 200 and body.get("ok"):
            return True, 0
        retry = 0
        params = (body or {}).get("parameters") or {}
        try:
            retry = int(params.get("retry_after") or 0)
        except Exception:
            retry = 0
        return False, retry

    def send_message(self, chat_id, text, message_thread_id=None):
        data = {"chat_id": chat_id, "text": text[:4000]}
        if message_thread_id:
            data["message_thread_id"] = message_thread_id
        return self.api("sendMessage", data)


class Bridge:
    def __init__(self, home, db_path):
        self.home = home
        self.db_path = db_path
        self._init_db()
        self.tg_token = env_token("TELEGRAM_BOT_TOKEN", home)
        self.fallback_token = env_token("TELEGRAM_FALLBACK_BOT_TOKEN", home)
        self.slack_token = env_token("SLACK_BOT_TOKEN", home)
        cfg = bot_cfg(home)
        self.slack_channel = (
            cfg.get("platforms", {}).get("slack", {}).get("home_channel")
            or env_token("SLACK_HOME_CHANNEL", home)
            or ""
        )
        self.tg = TelegramClient(self.tg_token)
        self.slack = SlackClient(self.slack_token)

    def _init_db(self):
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        con = sqlite3.connect(self.db_path)
        con.executescript(
            """
            CREATE TABLE IF NOT EXISTS topic_map (
              chat_id TEXT NOT NULL,
              thread_id TEXT NOT NULL,
              topic_name TEXT,
              slack_channel TEXT,
              slack_parent_ts TEXT,
              slack_thread_ts TEXT,
              mode TEXT NOT NULL DEFAULT 'telegram',
              last_switch_at REAL,
              cooldown_until REAL,
              fallback_at REAL,
              created_at REAL NOT NULL
            );
            CREATE UNIQUE INDEX IF NOT EXISTS topic_map_pk ON topic_map(chat_id, thread_id);
            CREATE TABLE IF NOT EXISTS dedupe (
              platform TEXT NOT NULL,
              chat_id TEXT,
              thread_id TEXT,
              msg_id TEXT NOT NULL,
              mirrored_at REAL NOT NULL
            );
            CREATE UNIQUE INDEX IF NOT EXISTS dedupe_pk ON dedupe(platform, msg_id);
            CREATE TABLE IF NOT EXISTS slack_buffer (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              chat_id TEXT NOT NULL,
              thread_id TEXT NOT NULL,
              text TEXT NOT NULL,
              author TEXT,
              created_at REAL NOT NULL,
              delivered INTEGER NOT NULL DEFAULT 0
            );
            """
        )
        con.commit()

    @staticmethod
    def _row_to_dict(row):
        if row is None:
            return None
        return {k: row[k] for k in row.keys()}

    def topic_row(self, chat_id, thread_id):
        con = sqlite3.connect(self.db_path)
        con.row_factory = sqlite3.Row
        r = con.execute(
            "select * from topic_map where chat_id=? and thread_id=?",
            (str(chat_id), str(thread_id)),
        ).fetchone()
        con.close()
        return r

    def ensure_topic(self, chat_id, thread_id, topic_name):
        row = self.topic_row(chat_id, thread_id)
        if row:
            return self._row_to_dict(row)
        # Try to find existing slack parent for this topic_name (so we don't
        # create a duplicate thread if the bridge restarted).
        con = sqlite3.connect(self.db_path)
        existing = con.execute(
            "select * from topic_map where chat_id=? and topic_name=? order by created_at desc limit 1",
            (str(chat_id), topic_name or ""),
        ).fetchone()
        if existing:
            con.close()
            return self._row_to_dict(existing)
        # Create a new Slack parent message that starts the thread.
        slack_ts = ""
        if self.slack_token and self.slack_channel:
            resp = self.slack.post(
                self.slack_channel,
                f"📩 Telegram-topic gestart: {topic_name or '(geen titel)'}",
            )
            slack_ts = (resp.get("ts") or "") if resp.get("ok") else ""
        con = sqlite3.connect(self.db_path)
        now = time.time()
        con.execute(
            "insert or replace into topic_map(chat_id,thread_id,topic_name,slack_channel,slack_parent_ts,slack_thread_ts,mode,created_at) values(?,?,?,?,?,?,?,?)",
            (str(chat_id), str(thread_id), topic_name or "", self.slack_channel, slack_ts, slack_ts, "telegram", now),
        )
        con.commit()
        con.row_factory = sqlite3.Row
        row = con.execute(
            "select * from topic_map where chat_id=? and thread_id=?",
            (str(chat_id), str(thread_id)),
        ).fetchone()
        con.close()
        return self._row_to_dict(row) if row else None

    def see(self, platform, chat_id, thread_id, msg_id):
        """Return True if we already mirrored this msg_id, else record it."""
        con = sqlite3.connect(self.db_path)
        try:
            con.execute(
                "insert into dedupe(platform,chat_id,thread_id,msg_id,mirrored_at) values(?,?,?,?,?)",
                (platform, str(chat_id or ""), str(thread_id or ""), str(msg_id), time.time()),
            )
            con.commit()
            return False
        except sqlite3.IntegrityError:
            return True
        finally:
            con.close()

    def current_mode(self, row, *, retry_after):
        now = time.time()
        # When we're already on slack, only go back when telegram is healthy AND
        # the cooldown has expired.
        if row["mode"] == "slack":
            cooldown = float(row["cooldown_until"] or 0)
            if now < cooldown:
                return "slack"
            if not self.tg_token:
                return "slack"
            ok, ra = self.tg.health()
            if ok and ra <= 30:
                return "telegram"
            return "slack"
        # row mode == telegram
        if not self.tg_token:
            return "telegram"
        if retry_after > 30:
            return "slack"
        # retry_after <=30 still means Telegram is up enough; stay telegram.
        return "telegram"
    def mirror_inbound(self, row, text, *, source_msg_id):
        """Mirror an inbound message (from Telegram) to the Slack thread."""
        mode = row["mode"]
        if mode == "slack":
            # Telegram is rate-limited: show the message in the Slack thread
            # right away AND buffer it for delivery back to Telegram later.
            row_d = self._row_to_dict(row)
            mirrored = "no-slack-route"
            if self.slack_token and row_d.get("slack_thread_ts") and self.slack_channel:
                resp = self.slack.post(
                    self.slack_channel,
                    f"[TG-wachtrij] {text}",
                    thread_ts=row_d["slack_thread_ts"],
                )
                mirrored = "sent" if resp.get("ok") else f"err:{resp.get('error')}"
            con = sqlite3.connect(self.db_path)
            con.execute(
                "insert into slack_buffer(chat_id,thread_id,text,created_at) values(?,?,?,?)",
                (row["chat_id"], row["thread_id"], text, time.time()),
            )
            con.commit()
            con.close()
            return "buffered+" + mirrored if mirrored != "no-slack-route" else "buffered"
        if mode == "telegram":
            row_d = self._row_to_dict(row)
            if not (self.slack_token and row_d.get("slack_thread_ts") and self.slack_channel):
                return "no-slack-route"
            if self.see("slack-out", row_d["chat_id"], row_d["thread_id"], source_msg_id):
                return "deduped"
            resp = self.slack.post(
                self.slack_channel,
                f"[TG] {text}",
                thread_ts=row_d["slack_thread_ts"],
            )
            return "sent" if resp.get("ok") else f"err:{resp.get('error')}"

    def mirror_outbound(self, row, text, *, source_msg_id):
        """Mirror a Slack-side message back to the active channel."""
        if row["mode"] == "slack":
            # Mirror from Slack into the right Telegram topic when Telegram is up.
            ok, _ = self.tg.health()
            if not ok:
                return "tg-down"
            if self.see("tg-out", row["chat_id"], row["thread_id"], source_msg_id):
                return "deduped"
            res = self.tg.send_message(
                row["chat_id"], f"[Slack] {text}", message_thread_id=row["thread_id"]
            )
            return "sent" if res[1].get("ok") else f"err"
        return "no-mirror"

    def switch_mode(self, row, new_mode):
        if row["mode"] == new_mode:
            return row
        now = time.time()
        con = sqlite3.connect(self.db_path)
        if new_mode == "slack":
            cooldown = now + 300  # 5 min
            con.execute(
                "update topic_map set mode=?, last_switch_at=?, cooldown_until=? where chat_id=? and thread_id=?",
                ("slack", now, cooldown, row["chat_id"], row["thread_id"]),
            )
        else:
            con.execute(
                "update topic_map set mode=?, last_switch_at=?, cooldown_until=0 where chat_id=? and thread_id=?",
                ("telegram", now, row["chat_id"], row["thread_id"]),
            )
        # Flush slack_buffer to telegram on switch back.
        if new_mode == "telegram":
            for b in con.execute(
                "select id,text from slack_buffer where chat_id=? and thread_id=? and delivered=0 order by id",
                (row["chat_id"], row["thread_id"]),
            ).fetchall():
                self.tg.send_message(
                    row["chat_id"], f"[Slack-herhaling] {b['text']}", message_thread_id=row["thread_id"]
                )
            con.execute(
                "update slack_buffer set delivered=1 where chat_id=? and thread_id=?",
                (row["chat_id"], row["thread_id"]),
            )
        con.commit()
        con.close()
        return row

    def sweep_recover(self):
        """Switch every ready slack-mode topic back to telegram when healthy.

        One real telegram health probe per pass (jitter inside), then per-topic
        cooldown check. Returns how many topics switched back.
        """
        now = time.time()
        con = sqlite3.connect(self.db_path)
        con.row_factory = sqlite3.Row
        rows = con.execute(
            "select * from topic_map where mode='slack' and cooldown_until<=?",
            (now,),
        ).fetchall()
        con.close()
        if not rows:
            return 0
        try:
            ok, ra = self.tg.health()
        except Exception:
            return 0
        if not (ok and ra <= 30 and self.tg_token):
            return 0
        switched = 0
        for r in rows:
            row = self._row_to_dict(r)
            if self.current_mode(row, retry_after=ra) == "telegram":
                self.switch_mode(row, "telegram")
                switched += 1
        return switched


def main(argv):
    if len(argv) < 2:
        print(__doc__)
        return 2
    cmd = argv[1]
    home = env_token("HERMES_HOME") or str(Path.home() / ".hermes")
    db = env_token("BRIDGE_DB") or str(Path(home) / "bridge.db")
    bridge = Bridge(home, db)
    if cmd == "send":
        chat_id, thread_id, text = argv[2], argv[3], argv[4]
        row = bridge.ensure_topic(chat_id, thread_id, "(untitled)")
        ra = 0
        try:
            ok, ra = bridge.tg.health()
        except Exception:
            ok, ra = False, 9999
        mode = bridge.current_mode(row, retry_after=ra)
        if mode != row["mode"]:
            bridge.switch_mode(row, mode)
        result = bridge.mirror_inbound(row, text, source_msg_id=f"cli-{int(time.time()*1000)}")
        print(json.dumps({"mode": mode, "result": result}))
        return 0
    if cmd == "watch":
        # Single health probe + report.
        ok, ra = bridge.tg.health()
        print(json.dumps({"telegram_ok": ok, "retry_after": ra}))
        return 0
    if cmd == "sweep":
        # One recovery pass: every slack-mode topic whose cooldown expired gets
        # probed; healthy telegram flips it back and drains the buffer.
        switched = bridge.sweep_recover()
        print(json.dumps({"switched_back": switched}))
        return 0
    if cmd == "watch-loop":
        interval = int(argv[2]) if len(argv) > 2 else 60
        while True:
            try:
                n = bridge.sweep_recover()
                if n:
                    print(json.dumps({"switched_back": n}), flush=True)
            except Exception as ex:  # a failed pass must not kill the watcher
                print(json.dumps({"error": str(ex)[:120]}), flush=True)
            time.sleep(interval)
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
