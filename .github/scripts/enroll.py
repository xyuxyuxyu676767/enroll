#!/usr/bin/env python3
"""Self-service enrollment for Classroom 50 (see enroll.yaml).

For every open issue in the enroll repository:
  1. parse the issue form (group, last name, first name); the author's GitHub
     login is the student's identity, nothing to type or mistype;
  2. enroll: org invitation carrying the classroom team (a new member), or
     direct team membership (an existing member);
  3. upsert the roster row in classroom50/<classroom>/roster.csv, with section;
  4. answer the issue with the accept link, close and lock it.

Issue bodies are untrusted input: they are parsed as data and never passed
to a shell.
"""

from __future__ import annotations

import base64
import csv
import io
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request

ORG = os.environ["ORG"]
CLASSROOM = os.environ["CLASSROOM"]
ACCEPT_SLUG = os.environ.get("ACCEPT_SLUG", "")
ENROLL_REPO = os.environ["ENROLL_REPO"]
ENROLL_TOKEN = os.environ.get("ENROLL_TOKEN", "")
ISSUES_TOKEN = os.environ.get("GITHUB_TOKEN", "") or ENROLL_TOKEN
DRY_RUN = os.environ.get("DRY_RUN", "false").lower() == "true"
DELETE_ISSUES = os.environ.get("DELETE_ISSUES", "false").lower() == "true"
API = os.environ.get("GITHUB_API_URL", "https://api.github.com").rstrip("/")
TEMPLATE = ".github/ISSUE_TEMPLATE/enroll.yml"
ROSTER_HEADER = ["username", "first_name", "last_name", "email", "section", "github_id", "role"]

SUMMARY: list[str] = []


def log(msg: str) -> None:
    print(msg, flush=True)


def api(token: str, method: str, path: str, body=None, retries: int = 4):
    url = path if path.startswith("http") else f"{API}{path}"
    data = json.dumps(body).encode() if body is not None else None
    for attempt in range(1, retries + 1):
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("Authorization", f"Bearer {token}")
        req.add_header("Accept", "application/vnd.github+json")
        req.add_header("X-GitHub-Api-Version", "2022-11-28")
        if data is not None:
            req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                raw = resp.read()
                return resp.status, (json.loads(raw) if raw else None)
        except urllib.error.HTTPError as e:
            raw = e.read()
            try:
                payload = json.loads(raw) if raw else None
            except ValueError:
                payload = {"message": raw.decode(errors="replace")}
            if e.code in (502, 503, 504) and attempt < retries:
                time.sleep(2 * attempt)
                continue
            return e.code, payload
        except (urllib.error.URLError, OSError):
            if attempt == retries:
                raise
            time.sleep(2 * attempt)
    raise RuntimeError("unreachable")


# ---------------------------------------------------------------- issue form

def allowed_groups() -> list[str]:
    """The dropdown options of the `group` field, read from the template."""
    groups: list[str] = []
    in_group = in_options = False
    for line in open(TEMPLATE, encoding="utf-8"):
        s = line.strip()
        if s.startswith("id:") and s.split(":", 1)[1].strip() == "group":
            in_group = True
            continue
        if in_group and s.startswith("- type:"):
            break
        if in_group and s == "options:":
            in_options = True
            continue
        if in_options:
            if s.startswith("- "):
                groups.append(s[2:].strip().strip('"').strip("'"))
            elif s:
                in_options = False
    return groups


def parse_form(body: str) -> dict[str, str]:
    fields: dict[str, str] = {}
    for m in re.finditer(r"^### (.+?)\s*\n(.*?)(?=^### |\Z)", body or "", re.S | re.M):
        value = m.group(2).strip()
        if value == "_No response_":
            value = ""
        fields[m.group(1).strip()] = value
    return fields


# ---------------------------------------------------------------- classroom

def classroom_team() -> tuple[str, int]:
    status, payload = api(ENROLL_TOKEN, "GET", f"/repos/{ORG}/classroom50/contents/{CLASSROOM}/classroom.json")
    if status != 200:
        sys.exit(f"::error::cannot read {CLASSROOM}/classroom.json: {status} {payload}")
    data = json.loads(base64.b64decode(payload["content"]))
    return data["team"]["slug"], int(data["team"]["id"])


def read_roster() -> tuple[list[dict], str]:
    status, payload = api(ENROLL_TOKEN, "GET", f"/repos/{ORG}/classroom50/contents/{CLASSROOM}/roster.csv")
    if status != 200:
        sys.exit(f"::error::cannot read {CLASSROOM}/roster.csv: {status}")
    text = base64.b64decode(payload["content"]).decode("utf-8")
    rows = list(csv.DictReader(io.StringIO(text)))
    return rows, payload["sha"]


def write_roster(rows: list[dict], sha: str) -> bool:
    out = io.StringIO()
    w = csv.DictWriter(out, fieldnames=ROSTER_HEADER, lineterminator="\n", extrasaction="ignore")
    w.writeheader()
    for r in rows:
        w.writerow({k: r.get(k, "") or "" for k in ROSTER_HEADER})
    body = {
        "message": "[enroll] roster: add students",
        "content": base64.b64encode(out.getvalue().encode("utf-8")).decode(),
        "sha": sha,
    }
    status, payload = api(ENROLL_TOKEN, "PUT", f"/repos/{ORG}/classroom50/contents/{CLASSROOM}/roster.csv", body)
    if status not in (200, 201):
        log(f"::error::roster commit failed: {status} {payload}")
        return False
    return True


# ---------------------------------------------------------------- enrollment

def membership_state(login: str) -> str:
    status, payload = api(ENROLL_TOKEN, "GET", f"/orgs/{ORG}/memberships/{login}")
    if status == 200:
        return payload.get("state", "active")   # active | pending
    return "none"


def enroll(login: str, user_id: int, team_slug: str, team_id: int) -> tuple[bool, str]:
    """Returns (ok, what happened)."""
    state = membership_state(login)
    if state == "active":
        status, payload = api(ENROLL_TOKEN, "PUT", f"/orgs/{ORG}/teams/{team_slug}/memberships/{login}", {"role": "member"})
        if status == 200:
            return True, "уже в организации, добавлен в классрум"
        return False, f"team add failed: {status} {(payload or {}).get('message')}"
    if state == "pending":
        return True, "приглашение уже отправлено раньше, проверь почту"
    status, payload = api(ENROLL_TOKEN, "POST", f"/orgs/{ORG}/invitations",
                          {"invitee_id": user_id, "role": "direct_member", "team_ids": [team_id]})
    if status == 201:
        return True, "приглашение отправлено"
    msg = (payload or {}).get("message", "")
    errs = "; ".join(e.get("message", "") for e in (payload or {}).get("errors", []) if isinstance(e, dict))
    return False, f"invite failed: {status} {msg} {errs}".strip()


# ---------------------------------------------------------------- issues

def open_issues() -> list[dict]:
    issues: list[dict] = []
    page = 1
    while True:
        status, batch = api(ISSUES_TOKEN, "GET", f"/repos/{ENROLL_REPO}/issues?state=open&per_page=100&page={page}")
        if status != 200 or not batch:
            break
        issues.extend(i for i in batch if "pull_request" not in i and i["user"]["type"] == "User")
        if len(batch) < 100:
            break
        page += 1
    return issues


def reply_and_close(issue: dict, text: str, ok: bool) -> None:
    n = issue["number"]
    if DRY_RUN:
        log(f"  dry-run: would reply to #{n}: {text[:80]}...")
        return
    api(ISSUES_TOKEN, "POST", f"/repos/{ENROLL_REPO}/issues/{n}/comments", {"body": text})
    if ok:
        api(ISSUES_TOKEN, "PATCH", f"/repos/{ENROLL_REPO}/issues/{n}", {"state": "closed", "state_reason": "completed"})
        api(ISSUES_TOKEN, "PUT", f"/repos/{ENROLL_REPO}/issues/{n}/lock", {"lock_reason": "resolved"})
        if DELETE_ISSUES:
            status, payload = api(ENROLL_TOKEN, "POST", "/graphql",
                                  {"query": "mutation($id:ID!){deleteIssue(input:{issueId:$id}){clientMutationId}}",
                                   "variables": {"id": issue["node_id"]}})
            if status != 200 or (payload or {}).get("errors"):
                log(f"  could not delete #{n}: {status} {(payload or {}).get('errors')}")
    else:
        api(ISSUES_TOKEN, "POST", f"/repos/{ENROLL_REPO}/issues/{n}/labels", {"labels": ["needs-attention"]})


def accept_link() -> str:
    base = f"https://classroom50.org/{ORG}/{CLASSROOM}"
    return f"{base}/assignments/{ACCEPT_SLUG}/accept" if ACCEPT_SLUG else base


def main() -> None:
    if not ENROLL_TOKEN:
        sys.exit("::error::ENROLL_TOKEN secret is not set")
    groups = allowed_groups()
    if not groups:
        sys.exit(f"::error::no group options found in {TEMPLATE}")
    issues = open_issues()
    log(f"{len(issues)} open issue(s); groups: {', '.join(groups)}")
    if not issues:
        return

    team_slug, team_id = classroom_team()
    rows, sha = read_roster()
    by_user = {(r.get("username") or "").lower(): r for r in rows if r.get("username")}
    link = accept_link()
    changed = False
    SUMMARY.append("| Заявка | Студент | Группа | Результат |")
    SUMMARY.append("|---|---|---|---|")

    for issue in issues:
        login = issue["user"]["login"]
        n = issue["number"]
        form = parse_form(issue.get("body", ""))
        group = form.get("Группа", "")
        last, first = form.get("Фамилия", ""), form.get("Имя", "")
        log(f"#{n} @{login}: group={group!r} name={last!r} {first!r}")

        if group not in groups:
            text = (f"@{login}, не нашёл группу «{group}» в списке. Открой новую заявку и выбери группу "
                    f"из выпадающего списка, а если твоей группы там нет, напиши преподавателю.")
            reply_and_close(issue, text, ok=False)
            SUMMARY.append(f"| #{n} | @{login} | {group} | группа не из списка |")
            continue

        existing = by_user.get(login.lower())
        if existing and existing.get("section") and membership_state(login) == "active":
            text = (f"@{login}, ты уже записан в группу {existing['section']}. "
                    f"Лабы здесь: {link}")
            reply_and_close(issue, text, ok=True)
            SUMMARY.append(f"| #{n} | @{login} | {existing['section']} | уже записан |")
            continue

        status, user = api(ENROLL_TOKEN, "GET", f"/users/{login}")
        if status != 200:
            reply_and_close(issue, f"@{login}, не смог прочитать твой аккаунт ({status}). Попробуй ещё раз позже.", ok=False)
            SUMMARY.append(f"| #{n} | @{login} | {group} | GET /users failed {status} |")
            continue

        if DRY_RUN:
            SUMMARY.append(f"| #{n} | @{login} | {group} | dry-run: would enroll as {last} {first} |")
            continue

        ok, what = enroll(login, user["id"], team_slug, team_id)
        if not ok:
            log(f"::warning::#{n} @{login}: {what}")
            reply_and_close(issue, f"@{login}, не получилось записать автоматически ({what}). Преподаватель посмотрит заявку.", ok=False)
            SUMMARY.append(f"| #{n} | @{login} | {group} | {what} |")
            continue

        row = existing or {"username": login, "email": "", "role": "student"}
        row.update({"username": login, "first_name": first, "last_name": last,
                    "section": group, "github_id": str(user["id"])})
        if not existing:
            rows.append(row)
            by_user[login.lower()] = row
        changed = True

        if what.startswith("уже в организации"):
            steps = (f"1. Открой лабу 0 и нажми **Accept assignment**: {link}\n"
                     f"2. Все лабы курса: https://classroom50.org/{ORG}/{CLASSROOM}\n\n")
        else:
            steps = (f"1. Прими приглашение в организацию `{ORG}` (письмо от GitHub или баннер на github.com).\n"
                     f"2. Открой лабу 0 и нажми **Accept assignment**: {link}\n"
                     f"   Приглашение при этом тоже примется, если ты ещё не успел.\n"
                     f"3. Все лабы курса: https://classroom50.org/{ORG}/{CLASSROOM}\n\n")
        text = (f"@{login}, готово: {what}.\n\n" + steps +
                f"Группа {group} записана в ведомость. Если ошибся группой, напиши преподавателю.")
        reply_and_close(issue, text, ok=True)
        SUMMARY.append(f"| #{n} | @{login} | {group} | {what} |")

    if changed and not DRY_RUN:
        for attempt in range(3):
            if write_roster(rows, sha):
                log("roster.csv committed")
                break
            # someone else committed meanwhile: re-read and re-apply our rows
            fresh, sha = read_roster()
            fresh_by = {(r.get("username") or "").lower(): r for r in fresh if r.get("username")}
            for u, r in by_user.items():
                if r.get("role") == "student" and r.get("section"):
                    if u in fresh_by:
                        fresh_by[u].update({k: r[k] for k in ("first_name", "last_name", "section", "github_id")})
                    else:
                        fresh.append(r)
            rows = fresh
        else:
            log("::error::roster.csv could not be committed after 3 attempts")

    text = "\n".join(SUMMARY) + "\n"
    print("\n" + text)
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if path:
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(f"## Enroll · {CLASSROOM}{' (dry-run)' if DRY_RUN else ''}\n\n" + text)


if __name__ == "__main__":
    main()
