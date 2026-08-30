#!/usr/bin/env python3
"""클라우드 비서 — 네이버 메일 감시 · 텔레그램 알림 · 10분 미확인 에스컬레이션.

GitHub Actions 가 10분마다 실행한다. 맥이 꺼져 있어도 돈다.

공개 저장소에서도 안전하도록, 저장소에 커밋되는 state.json 에는
**메일 내용을 일절 담지 않는다.** UID 와 시각만 남기고, 재알림 때 필요한
제목은 그때그때 IMAP 에서 다시 가져온다.

비밀정보는 전부 GitHub Secrets → 환경변수로만 들어온다.
"""
import email
import imaplib
import json
import os
import sys
import time
import urllib.parse
import urllib.request
from email.header import decode_header, make_header
from pathlib import Path

BASE = Path(__file__).resolve().parent
STATE = BASE / "state.json"
RULES = BASE / "rules.json"

NAVER_ID = os.environ.get("NAVER_ID", "")
NAVER_PW = os.environ.get("NAVER_PW", "")
TG_TOKEN = os.environ.get("TG_TOKEN", "")
TG_CHAT = os.environ.get("TG_CHAT", "")

ESCALATE_AFTER = 10 * 60
ESCALATE_REPEAT = 10 * 60
MAX_FIRST_RUN = 5      # 첫 실행에서 과거 메일이 쏟아지는 것 방지


def load(path, default):
    try:
        return json.loads(path.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def decode(raw):
    try:
        return str(make_header(decode_header(raw or "")))
    except Exception:
        return raw or ""


# ---------- 텔레그램 ----------

def tg(method, **params):
    url = f"https://api.telegram.org/bot{TG_TOKEN}/{method}"
    data = urllib.parse.urlencode(
        {k: (json.dumps(v, ensure_ascii=False) if isinstance(v, (dict, list)) else v)
         for k, v in params.items()}
    ).encode()
    try:
        with urllib.request.urlopen(url, data=data, timeout=25) as r:
            return json.loads(r.read())
    except Exception as e:
        print(f"[텔레그램 실패] {method}: {e}", file=sys.stderr)
        return None


def notify(item_id, part, sender, subject, preview, escalated=False, waited_min=0):
    head = f"🔔 재알림 — {waited_min}분째 미확인" if escalated else "📬 새 업무"
    tg("sendMessage", chat_id=TG_CHAT,
       text=f"{head}\n\n파트: {part}\n보낸이: {sender}\n제목: {subject}\n\n{preview}",
       reply_markup={"inline_keyboard": [[
           {"text": "✅ 확인", "callback_data": f"ack:{item_id}"}]]})


def collect_acks(state):
    res = tg("getUpdates", offset=state.get("tg_offset", 0), timeout=0)
    if not res or not res.get("ok"):
        return
    for upd in res["result"]:
        state["tg_offset"] = upd["update_id"] + 1
        cb = upd.get("callback_query")
        if not cb or not cb.get("data", "").startswith("ack:"):
            continue
        item_id = cb["data"][4:]
        if item_id in state["pending"]:
            del state["pending"][item_id]
            tg("answerCallbackQuery", callback_query_id=cb["id"], text="확인 처리했어요")
            tg("editMessageReplyMarkup", chat_id=cb["message"]["chat"]["id"],
               message_id=cb["message"]["message_id"], reply_markup={"inline_keyboard": []})
        else:
            tg("answerCallbackQuery", callback_query_id=cb["id"], text="이미 처리된 건이에요")


# ---------- 분류 ----------

def classify(sender, subject, body):
    hay = f"{sender} {subject} {body}".lower()
    for rule in load(RULES, {}).get("parts", []):
        if any(s.lower() in sender.lower() for s in rule.get("senders", [])):
            return rule["name"]
        if any(k.lower() in hay for k in rule.get("keywords", [])):
            return rule["name"]
    return "기타"


# ---------- 메일 ----------

def body_text(msg, limit=300):
    for part in (msg.walk() if msg.is_multipart() else [msg]):
        if part.get_content_type() == "text/plain":
            try:
                cs = part.get_content_charset() or "utf-8"
                return " ".join(part.get_payload(decode=True).decode(cs, "replace").split())[:limit]
            except Exception:
                continue
    return ""


def header_of(M, uid):
    """재알림용으로 제목·보낸이만 다시 가져온다 (저장소에 남기지 않기 위해)."""
    typ, d = M.uid("fetch", uid, "(BODY.PEEK[HEADER.FIELDS (FROM SUBJECT)])")
    if typ != "OK" or not d or not d[0]:
        return "?", "?"
    m = email.message_from_bytes(d[0][1])
    return decode(m.get("From")), decode(m.get("Subject"))


def main():
    missing = [k for k, v in [("NAVER_ID", NAVER_ID), ("NAVER_PW", NAVER_PW),
                              ("TG_TOKEN", TG_TOKEN), ("TG_CHAT", TG_CHAT)] if not v]
    if missing:
        print(f"[중단] 시크릿 누락: {', '.join(missing)}", file=sys.stderr)
        sys.exit(1)

    state = load(STATE, {"last_uid": 0, "tg_offset": 0, "pending": {}})
    state.setdefault("pending", {})
    first_run = state["last_uid"] == 0

    M = imaplib.IMAP4_SSL("imap.naver.com", 993)
    M.login(NAVER_ID, NAVER_PW)
    M.select("INBOX")

    last_uid = state["last_uid"]
    uids = [u for u in M.uid("search", None, f"UID {last_uid + 1}:*")[1][0].split()
            if int(u) > last_uid]
    if first_run:
        uids = uids[-MAX_FIRST_RUN:]

    now = time.time()
    for uid in uids:
        typ, d = M.uid("fetch", uid, "(RFC822)")
        if typ != "OK" or not d or not d[0]:
            continue
        msg = email.message_from_bytes(d[0][1])
        sender, subject = decode(msg.get("From")), decode(msg.get("Subject"))
        preview = body_text(msg)
        part = classify(sender, subject, preview)
        item_id = f"naver-{uid.decode()}"
        notify(item_id, part, sender, subject, preview)
        # 저장소에는 내용을 남기지 않는다 — uid 와 시각만
        state["pending"][item_id] = {"uid": uid.decode(), "at": now, "last_ping": now}
        state["last_uid"] = max(state["last_uid"], int(uid))
        print(f"[새 업무] {part} / {subject}")

    collect_acks(state)

    # 10분 미확인 재알림 — 제목은 여기서 다시 조회
    for item_id, rec in state["pending"].items():
        waited = now - rec["at"]
        if waited >= ESCALATE_AFTER and now - rec.get("last_ping", 0) >= ESCALATE_REPEAT:
            sender, subject = header_of(M, rec["uid"].encode())
            notify(item_id, "미확인", sender, subject, "아직 확인하지 않은 업무입니다.",
                   escalated=True, waited_min=int(waited // 60))
            rec["last_ping"] = now
            print(f"[에스컬레이션] {subject}")

    M.logout()
    STATE.write_text(json.dumps(state, ensure_ascii=False, indent=2))
    print(f"완료 — 미확인 {len(state['pending'])}건")


if __name__ == "__main__":
    main()
