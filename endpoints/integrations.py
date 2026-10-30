"""Signing in to outside platforms, and seeing which are connected.

Two endpoints serve every platform in integrations/, each taking the
platform's name, so adding a platform adds nothing here. What a platform can
do once connected is its own endpoint (endpoints/classroom.py).
"""

import integrations
from .registry import MAJOR, MINOR, endpoint, param


@endpoint(
    tier=MINOR,
    description=(
        "Lists the outside platforms you can connect to — what each one gives you "
        "and whether it's connected right now, and as which account."
    ),
    returns="Each platform's name, what it gives, connected or not, and the account.",
)
def integrations_status():
    return integrations.overview()


@endpoint(
    tier=MAJOR,
    description=(
        "Connects you to an outside platform by opening its sign-in page in the "
        "user's browser, then waits (up to a few minutes) while they pick an account "
        "and approve. Only when the user asks to connect, or agrees to after a tool "
        "said the platform isn't connected — tell them a sign-in page is about to "
        "open. With disconnect, signs out and forgets the login instead. Platforms: "
        + "; ".join(f"{p.name} ({p.title})" for p in integrations.PLATFORMS.values())
    ),
    params={
        "platform": param("string", "Which platform.", required=True,
                          enum=list(integrations.PLATFORMS)),
        "disconnect": param("boolean", "Sign out instead of in.", default=False),
    },
    returns="Who it connected as, or why it couldn't.",
)
def integration_connect(platform, disconnect=False):
    p = integrations.get(platform)
    return p.disconnect() if disconnect else p.connect()
