"""keep_going: a tool that does nothing but hand the turn straight back.

The model often ends a message on "Alright, let me build that now." and then
stops — the plan is stated, but a reply with no call in it is the end of the
reply, so nothing ever runs. Calling keep_going at the end of a message like
that gives it a fresh turn to start the work in.

brain.converse() handles the call itself rather than dispatching it: it isn't
shown as a tool use, doesn't use up the tool budget, and comes back with a
nudge to do what was just promised (see brain.KEEP_GOING). It's declared here
so it's listed in every catalog, the assistant's, the coder's and the
subagents' alike.
"""

from .registry import EVERYONE, MINOR, endpoint


@endpoint(
    tier=MINOR, scope=EVERYONE,
    description=(
        "Hands the turn straight back to you so you can carry on. Call it at the "
        "end of any message where you've said what you're about to do but haven't "
        "made the call that does it — \"Alright, let me build that.\" then "
        "keep_going — and you get a new turn to start the work in. Without it, a "
        "message with no tool call ends your reply and nothing you promised "
        "happens. Don't use it in place of a real tool you could call right now."
    ),
    returns="Nothing to report; just carry on.",
)
def keep_going():
    return "Go ahead."
