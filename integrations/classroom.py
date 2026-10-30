"""Google Classroom: courses, assignments, announcements and your own work.

Read-only (config.CLASSROOM_SCOPES). Everything goes through the Classroom
REST API as whoever signed in, so it sees exactly what that account sees in
Classroom: as a student, your classes and your own submissions; as a teacher,
your classes and how many students have turned each assignment in.

Writing is left out on purpose, not by oversight: Classroom only lets an app
change coursework and submissions that the same app created, so "turn this
in" can never work on a teacher's real assignment.

Courses and assignments can be named rather than given by id — "math",
"essay 3" — and are matched against titles, so the model never has to look an
id up first.
"""

import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import config
from . import Platform, register
from .google import GoogleLogin

API = "https://classroom.googleapis.com/v1"
TITLE = "Google Classroom"
login = GoogleLogin("classroom", TITLE, config.CLASSROOM_SCOPES)

DONE = {"TURNED_IN", "RETURNED"}
_STATE = {"NEW": "not started", "CREATED": "not turned in", "TURNED_IN": "turned in",
          "RETURNED": "returned", "RECLAIMED_BY_STUDENT": "unsubmitted"}

_courses = {"at": 0.0, "list": None}
_COURSE_TTL = 300


# -- shaping ------------------------------------------------------------------
def _clip(text, n=None):
    text = (text or "").strip()
    n = n or config.CLASSROOM_TEXT_CHARS
    return text if len(text) <= n else text[:n].rstrip() + " …"


def _local(stamp):
    """An API timestamp ('2026-10-27T15:04:05.123Z') in local time."""
    if not stamp:
        return None
    return datetime.fromisoformat(stamp.replace("Z", "+00:00")).astimezone()


def _show(dt):
    return f"{dt:%a %b} {dt.day}, {dt:%I:%M %p}".replace(" 0", " ") if dt else None


def _due(work):
    """When it's due, in local time. Classroom keeps due dates in UTC."""
    d = work.get("dueDate")
    if not d:
        return None
    t = work.get("dueTime") or {}
    return datetime(d["year"], d["month"], d["day"], t.get("hours", 0), t.get("minutes", 0),
                    tzinfo=timezone.utc).astimezone()


def _relative(dt, now):
    days = (dt - now).total_seconds() / 86400
    if days < 0:
        return f"overdue by {_span(-days)}"
    return f"due in {_span(days)}"


def _span(days):
    if days < 1:
        hours = round(days * 24)
        return f"{hours} hour{'s' * (hours != 1)}" if hours else "under an hour"
    n = round(days)
    return f"{n} day{'s' * (n != 1)}"


def _materials(items):
    out = []
    for m in items or []:
        if "driveFile" in m:
            f = m["driveFile"].get("driveFile", {})
            out.append({"kind": "Drive file", "title": f.get("title"), "url": f.get("alternateLink")})
        elif "youtubeVideo" in m:
            v = m["youtubeVideo"]
            out.append({"kind": "YouTube", "title": v.get("title"), "url": v.get("alternateLink")})
        elif "link" in m:
            ln = m["link"]
            out.append({"kind": "link", "title": ln.get("title"), "url": (ln.get("url") or "").strip()})
        elif "form" in m:
            fm = m["form"]
            out.append({"kind": "Google Form", "title": fm.get("title"), "url": (fm.get("formUrl") or "").strip()})
    return out


def _submission(sub):
    if not sub:
        return {"state": "not started"}
    out = {"state": _STATE.get(sub.get("state"), sub.get("state", "").lower())}
    if sub.get("late"):
        out["late"] = True
    if sub.get("assignedGrade") is not None:
        out["grade"] = sub["assignedGrade"]
    attached = _materials((sub.get("assignmentSubmission") or {}).get("attachments"))
    if attached:
        out["attached"] = attached
    return out


# -- looking things up ----------------------------------------------------------
def courses(refresh=False):
    """Active courses, each tagged with whether you teach it or take it."""
    if not refresh and _courses["list"] is not None and time.time() - _courses["at"] < _COURSE_TTL:
        return _courses["list"]
    found = login.pages(f"{API}/courses", "courses", courseStates="ACTIVE", pageSize=100)
    teaching = {c["id"] for c in login.pages(f"{API}/courses", "courses",
                                             teacherId="me", courseStates="ACTIVE",
                                             pageSize=100)}
    out = [{"id": c["id"], "name": c.get("name"),
            "section": c.get("section"), "room": c.get("room"),
            "role": "teacher" if c["id"] in teaching else "student",
            "link": c.get("alternateLink")} for c in found]
    _courses.update(at=time.time(), list=out)
    return out


def _match(things, wanted, label, key):
    """The one thing whose id is `wanted` or whose title contains it."""
    wanted = str(wanted or "").strip()
    if not wanted:
        raise ValueError(f"Say which {label}.")
    for t in things:
        if t["id"] == wanted:
            return t
    low = wanted.lower()
    exact = [t for t in things if (key(t) or "").lower() == low]
    if len(exact) == 1:
        return exact[0]
    near = [t for t in things if low in (key(t) or "").lower()]
    if len(near) == 1:
        return near[0]
    if not near:
        options = "; ".join(key(t) or t["id"] for t in things[:25])
        raise ValueError(f"No {label} matching '{wanted}'. There's: {options}")
    raise ValueError(f"'{wanted}' matches more than one {label}: "
                     + "; ".join(f"{key(t)} (id {t['id']})" for t in near[:10]))


def course(wanted):
    return _match(courses(), wanted, "course",
                  lambda c: " ".join(filter(None, [c["name"], c["section"]])))


def _work(course_id, limit=300):
    return login.pages(f"{API}/courses/{course_id}/courseWork", "courseWork", limit=limit,
                       courseWorkStates="PUBLISHED", pageSize=100)


def _my_submissions(course_id):
    """Your submission for every assignment in the course, by assignment id."""
    subs = login.pages(f"{API}/courses/{course_id}/courseWork/-/studentSubmissions",
                       "studentSubmissions", userId="me", pageSize=100)
    return {s["courseWorkId"]: s for s in subs}


def _parallel(fn, items):
    with ThreadPoolExecutor(max_workers=6) as pool:
        return list(pool.map(fn, items))


def _work_entry(c, w, sub, now):
    due = _due(w)
    entry = {"course": c["name"], "title": (w.get("title") or "").strip(), "id": w["id"],
             "course_id": c["id"]}
    if w.get("workType"):
        entry["type"] = w["workType"].replace("_", " ").lower()
    entry.update(due=_show(due), when=_relative(due, now) if due else "no due date")
    if w.get("maxPoints"):
        entry["points"] = w["maxPoints"]
    if c["role"] == "student":
        entry.update(_submission(sub))
    else:
        entry["you"] = "teach this class"
    entry["link"] = w.get("alternateLink")
    return entry


# -- what the endpoint does ------------------------------------------------------
def list_courses():
    return {"courses": courses(refresh=True)}


def due(days=None):
    """What's due soon and what's overdue, across every active course."""
    days = int(days or config.CLASSROOM_DUE_DAYS)
    now = datetime.now().astimezone()
    until = now + timedelta(days=days)

    def one(c):
        work = _work(c["id"])
        subs = _my_submissions(c["id"]) if c["role"] == "student" else {}
        return c, work, subs

    upcoming, overdue, undated = [], [], []
    for c, work, subs in _parallel(one, courses()):
        for w in work:
            sub = subs.get(w["id"])
            if c["role"] == "student" and sub and sub.get("state") in DONE:
                continue
            due_at = _due(w)
            if due_at is None:
                if c["role"] == "student":
                    undated.append(f"{(w.get('title') or '').strip()} ({c['name']})")
            elif now <= due_at <= until:
                upcoming.append((due_at, _work_entry(c, w, sub, now)))
            elif due_at < now and c["role"] == "student":
                overdue.append((due_at, _work_entry(c, w, sub, now)))

    upcoming.sort(key=lambda p: p[0])
    overdue.sort(key=lambda p: p[0], reverse=True)
    out = {"now": _show(now), "window": f"next {days} days",
           "upcoming": [e for _, e in upcoming],
           "overdue": [e for _, e in overdue[:15]]}
    if len(overdue) > 15:
        out["overdue_more"] = len(overdue) - 15
    if undated:
        out["not_turned_in_no_due_date"] = undated[:10] + (
            [f"…and {len(undated) - 10} more"] if len(undated) > 10 else [])
    return out


def work(wanted_course, count=25):
    c = course(wanted_course)
    now = datetime.now().astimezone()
    items = _work(c["id"], limit=count)
    subs = _my_submissions(c["id"]) if c["role"] == "student" else {}
    return {"course": c["name"], "role": c["role"],
            "assignments": [_work_entry(c, w, subs.get(w["id"]), now) for w in items]}


def assignment(wanted_course, wanted):
    c = course(wanted_course)
    w = _match(_work(c["id"]), wanted, "assignment", lambda w: w.get("title"))
    now = datetime.now().astimezone()
    out = _work_entry(c, w, None, now)
    for k in ("state", "late", "grade", "attached"):
        out.pop(k, None)
    out["description"] = _clip(w.get("description")) or "(none)"
    out["materials"] = _materials(w.get("materials"))
    if w.get("topicId"):
        topics = login.pages(f"{API}/courses/{c['id']}/topics", "topic", pageSize=100)
        out["topic"] = next((t.get("name") for t in topics if t["topicId"] == w["topicId"]), None)
    subs = login.pages(f"{API}/courses/{c['id']}/courseWork/{w['id']}/studentSubmissions",
                       "studentSubmissions", pageSize=100,
                       userId="me" if c["role"] == "student" else None)
    if c["role"] == "student":
        out["mine"] = _submission(subs[0] if subs else None)
    else:
        counts = {}
        for s in subs:
            label = _STATE.get(s.get("state"), s.get("state"))
            counts[label] = counts.get(label, 0) + 1
        out["submissions"] = counts
        out["late"] = sum(1 for s in subs if s.get("late"))
    return out


def announcements(wanted_course=None, days=None):
    """Recent announcements in one course, or in every course over the last few days."""
    if wanted_course:
        chosen, since, cap, chars = [course(wanted_course)], None, 10, None
    else:
        days = int(days or config.CLASSROOM_RECENT_DAYS)
        chosen, cap, chars = courses(), 40, 600
        since = datetime.now().astimezone() - timedelta(days=days)

    def one(c):
        posts = login.pages(f"{API}/courses/{c['id']}/announcements", "announcements",
                            limit=10, announcementStates="PUBLISHED",
                            orderBy="updateTime desc", pageSize=10)
        return [(c, p) for p in posts]

    found = []
    for batch in _parallel(one, chosen):
        for c, p in batch:
            when = _local(p.get("updateTime"))
            if since and when and when < since:
                continue
            found.append((when, {"course": c["name"], "posted": _show(when),
                                 "text": _clip(p.get("text"), chars),
                                 "materials": _materials(p.get("materials")),
                                 "link": p.get("alternateLink")}))
    found.sort(key=lambda p: p[0] or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
    out = {"announcements": [a for _, a in found[:cap]]}
    if not wanted_course:
        out["window"] = f"last {days} days"
    return out


def materials(wanted_course):
    c = course(wanted_course)
    items = login.pages(f"{API}/courses/{c['id']}/courseWorkMaterials", "courseWorkMaterial",
                        limit=25, courseWorkMaterialStates="PUBLISHED", pageSize=25)
    return {"course": c["name"], "materials": [
        {"title": m.get("title"), "description": _clip(m.get("description"), 400) or None,
         "posted": _show(_local(m.get("updateTime"))),
         "files": _materials(m.get("materials")), "link": m.get("alternateLink")}
        for m in items]}


def people(wanted_course):
    c = course(wanted_course)

    def person(p):
        prof = p.get("profile", {})
        return " ".join(filter(None, [(prof.get("name") or {}).get("fullName"),
                                      f"<{prof['emailAddress']}>" if prof.get("emailAddress") else None]))

    teachers = login.pages(f"{API}/courses/{c['id']}/teachers", "teachers", pageSize=100)
    students = login.pages(f"{API}/courses/{c['id']}/students", "students", pageSize=100)
    return {"course": c["name"], "teachers": [person(t) for t in teachers],
            "student_count": len(students),
            "students": [person(s) for s in students[:60]]}


# -- the platform -----------------------------------------------------------------
def _connect():
    missing = login.connect()
    _courses.update(at=0.0, list=None)
    me = login.get(f"{API}/userProfiles/me")
    who = (me.get("name") or {}).get("fullName") or "you"
    email = me.get("emailAddress")
    note = (f" Google left out some permissions ({', '.join(missing)}), so parts may not "
            "work — connect again and tick every box." if missing else "")
    n = len(courses())
    return (f"Connected to {TITLE} as {who}{f' <{email}>' if email else ''}, "
            f"with {n} active course{'s' * (n != 1)}.{note}")


def _disconnect():
    _courses.update(at=0.0, list=None)
    if login.disconnect():
        return f"Disconnected from {TITLE} and revoked its login."
    return f"{TITLE} wasn't connected."


def _status():
    if not login.connected():
        return {"connected": False}
    try:
        me = login.get(f"{API}/userProfiles/me")
    except Exception as e:
        return {"connected": False, "note": str(e)}
    name = (me.get("name") or {}).get("fullName")
    return {"connected": True,
            "account": " ".join(filter(None, [name, f"<{me['emailAddress']}>"
                                              if me.get("emailAddress") else None]))}


register(Platform(
    name="classroom",
    title=TITLE,
    description="read your classes, assignments and due dates, announcements, "
                "class materials, and your own submissions and grades",
    connect=_connect,
    disconnect=_disconnect,
    status=_status,
))
