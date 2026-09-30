#!/usr/bin/env python3
"""Prove @channel / @here / @everyone wake all four bots in #bots.

Slack renders <!channel> as a real broadcast element even from a bot, but the
adapter's is_mentioned check only looks for a literal <@BOTUID>, so a broadcast
woke nobody (measured 2026-09-07 10:10 UTC: 0 replies). Fix is config, not code:
slack.extra.mention_patterns treats the broadcast token as a wake word.

Posts one broadcast per token as Sam himself (a human message also resets the
bot->bot hop budget) and reports which bots answered.
"""
import json
import os
import subprocess
import sys
import time
import urllib.parse

TOK = "/home/ubuntu/.hermes-primary/state/slack-bot-tokens.json"
CH = "C0BUZQJ9CRM"
NAMES = {
    "B0C0373HAHF": "Hermes", "B0C0VLF91RN": "Argo",
    "B0C04UXVB60": "Hypest", "B0C04UY4XRS": "ClaudeCode",
}


def main():
    token = sys.argv[1] if len(sys.argv) > 1 else "channel"
    if token == "everyone":
        print("REFUSED: @everyone werkt in Slack alleen in #general, niet in "
              "#bots. chat.postMessage omzeilt die clientbeperking, dus een "
              "PASS hier bewijst niets over wat Sam kan versturen. "
              "Test @channel of @here.")
        return 2

    # Refuse before any token is read and before any request is built. A
    # missing file or a slow lookup must not be able to run ahead of the deny.
    marker = f"BROADCASTTEST-{token}"
    from plugins.platforms.slack.egress_guard import EgressDenied, claim as egress_claim
    try:
        egress_claim(account=CH, channel=CH, text=marker,
                     approved=bool(os.environ.get("SLACK_EGRESS_APPROVED")),
                     tool_name="chat.postMessage")
    except EgressDenied as exc:
        print("geweigerd:", exc)
        return 1

    toks = json.load(open(TOK, encoding="utf-8-sig"))
    tok = toks["Hermes"]["bot_token"]
    env = subprocess.run(
        ["bash", "-c", ". /home/ubuntu/.cache/shell/env-exports.sh; "
         "echo $SLACK_MCP_XOXC_TOKEN; echo $SLACK_MCP_XOXD_TOKEN"],
        capture_output=True, text=True, check=True).stdout.split()
    xoxc, xoxd = env[0], env[1]
    r = subprocess.run(
        ["curl", "-s", "--max-time", "20", "-X", "POST",
         "https://slack.com/api/chat.postMessage",
         "-H", f"Cookie: d={xoxd}",
         "-H", "Content-Type: application/x-www-form-urlencoded",
         "--data", urllib.parse.urlencode({
             "token": xoxc, "channel": CH, "link_names": "true",
             "text": f"{marker} <!{token}> antwoord met alleen je naam."})],
        capture_output=True, text=True)
    d = json.loads(r.stdout)
    if not d.get("ok"):
        print("post failed:", d.get("error"))
        return 1
    ts = d["ts"]
    print(f"posted <!{token}> at {ts}")

    answered = {}
    for _ in range(14):
        time.sleep(12)
        # Bots reply IN THREAD on a broadcast, so channel history alone shows
        # nothing (that false FAIL cost a debugging round on 2026-09-07).
        # Check both surfaces.
        msgs = []
        for method, qs in (
            ("conversations.replies", f"channel={CH}&ts={ts}&limit=30"),
            ("conversations.history", f"channel={CH}&oldest={ts}&limit=30"),
        ):
            h = subprocess.run(
                ["curl", "-s", "--max-time", "20",
                 f"https://slack.com/api/{method}?{qs}",
                 "-H", f"Authorization: Bearer {tok}"],
                capture_output=True, text=True)
            msgs += json.loads(h.stdout).get("messages", [])
        for m in msgs:
            if m.get("ts") == ts:
                continue
            bid = m.get("bot_id")
            if bid and "Interrupting" not in (m.get("text") or ""):
                answered.setdefault(NAMES.get(bid, bid),
                                    (m.get("text") or "")[:50])
        if len(answered) >= 4:
            break

    for name in ("Hermes", "Argo", "Hypest", "ClaudeCode"):
        print(f"  {name}: {answered.get(name, 'GEEN ANTWOORD')}")
    ok = len(answered) == 4
    print("VERDICT:", "PASS" if ok else f"FAIL ({len(answered)}/4)")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
