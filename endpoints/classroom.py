"""Google Classroom (integrations/classroom.py), as one endpoint.

One tool taking an action rather than one per action, for the same reason the
prompt and business tools are shaped that way: every tool in the list costs
the model attention on every reply.
"""

from integrations import classroom as gc
from .registry import MINOR, endpoint, param

_ACTIONS = {
    "due": "what's due soon and what's overdue, across every class (the usual one)",
    "courses": "your active classes",
    "work": "a class's assignments, newest first, with your status on each",
    "assignment": "one assignment in full: instructions, attached files, due date, "
                  "your submission and grade",
    "announcements": "a class's recent announcements, or every class's from the last few days",
    "materials": "a class's posted materials",
    "people": "a class's teachers and students",
}


@endpoint(
    tier=MINOR,
    description=(
        "Reads the user's Google Classroom. Actions: "
        + "; ".join(f"{k} — {v}" for k, v in _ACTIONS.items())
        + ". Classes and assignments can be named loosely ('math', 'lab report'). "
        "If it says Classroom isn't connected, offer to connect it with integration_connect."
    ),
    params={
        "action": param("string", "What to read.", required=True, enum=list(_ACTIONS)),
        "course": param("string", "The class, by name or id. Needed for work, assignment, "
                                  "materials and people; optional for announcements."),
        "assignment": param("string", "For action assignment: its title, or part of it, or id."),
        "days": param("integer", "For due: how many days ahead (default 7). For "
                                 "announcements across every class: how many days back."),
    },
    returns=("Plain data: titles, local due dates with how long until or overdue, your "
             "status (not started, turned in, returned, late, grade) and links."),
    result_chars=9000,
)
def classroom(action, course=None, assignment=None, days=None):
    if action == "due":
        return gc.due(days)
    if action == "courses":
        return gc.list_courses()
    if action == "announcements":
        return gc.announcements(course, days)
    if not course:
        raise ValueError(f"Action {action} needs a course.")
    if action == "work":
        return gc.work(course)
    if action == "assignment":
        return gc.assignment(course, assignment)
    if action == "materials":
        return gc.materials(course)
    if action == "people":
        return gc.people(course)
    raise ValueError(f"Unknown action '{action}'. Use one of: {', '.join(_ACTIONS)}")
