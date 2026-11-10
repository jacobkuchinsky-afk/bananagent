"""Google Drive (integrations/drive.py), as one endpoint — see endpoints/classroom.py
for why it's one tool taking an action."""

import config
from integrations import drive as gd
from .registry import MINOR, endpoint, param

_ACTIONS = {
    "search": "finds files by words in their name or contents",
    "recent": "the files you've opened or changed most recently",
    "folder": "what's in a folder (My Drive if none is named)",
    "read": "a file's text — Docs, Sheets, Slides, PDFs, Word and text files",
}


@endpoint(
    tier=MINOR,
    description=(
        "Reads the user's Google Drive, including files shared with them. Actions: "
        + "; ".join(f"{k} — {v}" for k, v in _ACTIONS.items())
        + ". A file can be given by name, id, or its Drive/Docs link (Classroom "
        "attachments included). Long files come a page at a time: when the result "
        "says there's more, call read again with the start it gives. If it says Drive "
        "isn't connected, offer to connect it with integration_connect."
    ),
    params={
        "action": param("string", "What to do.", required=True, enum=list(_ACTIONS)),
        "query": param("string", "For search: the words to look for."),
        "file": param("string", "For read: the file. For folder: the folder. "
                                "A name, an id, or a link."),
        "start": param("integer", "For read: the character to start from, to read on "
                                  "through a long file.", default=0),
    },
    returns=("Files with name, type, owner, last modified, id and link; for read, "
             "the text, how long it is, and where to start for the rest."),
    result_chars=config.DRIVE_READ_CHARS + 1500,
)
def drive(action, query=None, file=None, start=0):
    if action == "search":
        return gd.search(query or file)
    if action == "recent":
        return gd.recent()
    if action == "folder":
        return gd.folder(file or query)
    if action == "read":
        return gd.read(file or query, start)
    raise ValueError(f"Unknown action '{action}'. Use one of: {', '.join(_ACTIONS)}")
