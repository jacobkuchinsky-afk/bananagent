"""Google Drive: finding, listing and reading your files.

Read-only (config.DRIVE_SCOPES), through the Drive API as whoever signed in —
My Drive, files shared with you, and shared drives. Reading turns a file into
text the model can use:

  * Google Docs export as markdown, so headings, lists and tables survive;
  * Google Slides as plain text; Google Sheets as CSV — only the first sheet,
    which is all Drive's export gives;
  * PDFs through pypdf, Word files from their XML, and anything text-shaped
    (txt, md, csv, json, code) as it is.

Anything else — images, videos, Excel, drawings — comes back as its details
and link only. Long files are read a page at a time (DRIVE_READ_CHARS), and
the text is kept for the last few files so reading on doesn't download again.

Files and folders can be named rather than given by id, and a Drive or Docs
link works too — which is how an attachment Classroom lists gets read.
"""

import io
import os
import re
import zipfile
from datetime import datetime
from xml.etree import ElementTree

import config
from . import Platform, register
from .google import GoogleLogin

API = "https://www.googleapis.com/drive/v3"
TITLE = "Google Drive"
login = GoogleLogin("drive", TITLE, config.DRIVE_SCOPES)

FOLDER = "application/vnd.google-apps.folder"
DOC = "application/vnd.google-apps.document"
SHEET = "application/vnd.google-apps.spreadsheet"
SLIDES = "application/vnd.google-apps.presentation"
PDF = "application/pdf"
WORD = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
_KINDS = {
    FOLDER: "folder", DOC: "Google Doc", SHEET: "Google Sheet", SLIDES: "Google Slides",
    "application/vnd.google-apps.form": "Google Form",
    "application/vnd.google-apps.drawing": "Google Drawing",
    "application/vnd.google-apps.shortcut": "shortcut",
    PDF: "PDF", WORD: "Word document",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": "Excel sheet",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation": "PowerPoint",
}
_TEXTISH = ("application/json", "application/xml", "application/javascript",
            "application/x-python", "application/x-sh", "application/rtf")
_FIELDS = ("id,name,mimeType,modifiedTime,size,webViewLink,"
           "owners(displayName,me),shortcutDetails")
_ID_IN_LINK = re.compile(r"(?:/d/|/folders/|[?&]id=)([\w-]{20,})")
_BARE_ID = re.compile(r"^[\w-]{25,}$")
# The markdown export carries a Doc's images inline as base64 — thousands of
# characters of noise apiece. Each becomes a placeholder.
_IMAGE_DATA = re.compile(r"^\[[^\]]*\]:\s*<?data:image/[^\s>]*>?\s*$", re.M)
_IMAGE_REF = re.compile(r"!\[[^\]]*\](?:\[[^\]]*\]|\(data:image/[^)]*\))")

_texts = {}  # (id, modifiedTime) -> text, for the last few files read
_KEEP = 5

# Every list asks for files across My Drive, shared-with-me and shared drives.
_EVERYWHERE = dict(corpora="allDrives", includeItemsFromAllDrives="true",
                   supportsAllDrives="true")


# -- shaping ------------------------------------------------------------------
def _show(stamp):
    if not stamp:
        return None
    dt = datetime.fromisoformat(stamp.replace("Z", "+00:00")).astimezone()
    return f"{dt:%a %b} {dt.day} {dt.year}, {dt:%I:%M %p}".replace(" 0", " ")


def _kind(f):
    mime = f.get("mimeType", "")
    return _KINDS.get(mime) or (mime.split("/")[-1] if mime else "file")


def _entry(f):
    owners = f.get("owners") or []
    owner = "you" if any(o.get("me") for o in owners) else \
        (owners[0].get("displayName") if owners else "a shared drive")
    return {"name": (f.get("name") or "").strip(), "type": _kind(f), "id": f["id"],
            "modified": _show(f.get("modifiedTime")), "owner": owner,
            "link": f.get("webViewLink")}


def _quote(text):
    return str(text).replace("\\", "\\\\").replace("'", "\\'")


def _list(q, count, order=None):
    return login.pages(f"{API}/files", "files", limit=count, q=q, orderBy=order,
                       pageSize=min(count, 100), fields=f"nextPageToken,files({_FIELDS})",
                       **_EVERYWHERE)


# -- finding one file ---------------------------------------------------------
def _by_id(file_id):
    return login.get(f"{API}/files/{file_id}", fields=_FIELDS, supportsAllDrives="true")


def find(wanted, folder=False):
    """The one file (or folder) meant by an id, a link, or its name."""
    wanted = str(wanted or "").strip()
    if not wanted:
        raise ValueError("Say which file.")
    match = _ID_IN_LINK.search(wanted)
    if match or _BARE_ID.match(wanted):
        try:
            f = _by_id(match.group(1) if match else wanted)
        except RuntimeError as e:
            if "404" in str(e):
                raise ValueError(f"No file at '{wanted}' that this account can see.") from None
            raise
        return _follow(f)

    only = f" and mimeType {'=' if folder else '!='} '{FOLDER}'"
    label = "folder" if folder else "file"
    found = _list(f"name contains '{_quote(wanted)}' and trashed = false" + only, 25)
    # The same name — ignoring case, spaces and the extension — is a match even
    # when there are several; the newest wins and the rest are mentioned.
    exact = sorted((f for f in found if _stem(f.get("name")) == _stem(wanted)),
                   key=lambda f: f.get("modifiedTime", ""), reverse=True)
    if exact:
        chosen = _follow(exact[0])
        if len(exact) > 1:
            chosen["_also"] = [f"{_listing(f)}" for f in exact[1:6]]
        return chosen
    if len(found) == 1:
        return _follow(found[0])
    if found:
        raise ValueError(f"No {label} is called exactly '{wanted}', and several have it in "
                         "their name — say which by id: "
                         + "; ".join(_listing(f) for f in found[:10]))
    # Nothing by name: files that mention it are suggestions, never a guess.
    mention = [] if folder else _list(
        f"fullText contains '{_quote(wanted)}' and trashed = false" + only, 8)
    hint = (" Files that mention it: " + "; ".join(_listing(f) for f in mention)) \
        if mention else ""
    raise ValueError(f"No {label} called '{wanted}' that this account can see. A file "
                     f"someone shared by link may only be reachable by its link.{hint}")


def _stem(name):
    return os.path.splitext(str(name or "").strip())[0].strip().lower()


def _listing(f):
    return (f"{(f.get('name') or '').strip()} ({_kind(f)}, modified "
            f"{_show(f.get('modifiedTime'))}, id {f['id']})")


def _follow(f):
    """A shortcut's target rather than the shortcut."""
    target = (f.get("shortcutDetails") or {}).get("targetId")
    return _by_id(target) if target else f


# -- reading ------------------------------------------------------------------
def _text(f):
    mime, fid = f.get("mimeType", ""), f["id"]
    if mime == DOC:
        try:
            md = login.download(f"{API}/files/{fid}/export", mimeType="text/markdown") \
                .decode("utf-8", "replace")
            return _IMAGE_REF.sub("[image]", _IMAGE_DATA.sub("", md)).rstrip() + "\n"
        except RuntimeError:
            pass  # older Docs exports lack markdown; plain text always works
        return login.download(f"{API}/files/{fid}/export", mimeType="text/plain") \
            .decode("utf-8", "replace")
    if mime == SHEET:
        csv = login.download(f"{API}/files/{fid}/export", mimeType="text/csv")
        return "(First sheet only, as CSV.)\n" + csv.decode("utf-8", "replace")
    if mime == SLIDES:
        return login.download(f"{API}/files/{fid}/export", mimeType="text/plain") \
            .decode("utf-8", "replace")
    if not (mime == PDF or mime == WORD or mime.startswith("text/") or mime in _TEXTISH):
        return None
    if int(f.get("size") or 0) > config.DRIVE_MAX_DOWNLOAD_MB * 1024 * 1024:
        raise ValueError(f"'{f.get('name')}' is over {config.DRIVE_MAX_DOWNLOAD_MB} MB, "
                         "too big to read here. Open the link instead.")
    data = login.download(f"{API}/files/{fid}", alt="media", supportsAllDrives="true")
    if mime == PDF:
        return _pdf(data)
    if mime == WORD:
        return _word(data)
    return data.decode("utf-8", "replace")


def _pdf(data):
    import logging
    from pypdf import PdfReader
    # It narrates every quirk of a sloppily made PDF; the text still comes out.
    logging.getLogger("pypdf").setLevel(logging.ERROR)
    pages = PdfReader(io.BytesIO(data)).pages
    text = "\n\n".join(f"[page {i}]\n{(p.extract_text() or '').strip()}"
                       for i, p in enumerate(pages, 1))
    if not re.sub(r"\[page \d+\]|\s", "", text):
        return ("(This PDF has no text in it — it's scanned images. Open the link "
                "to read it.)")
    return text


def _word(data):
    """A .docx's paragraphs, from word/document.xml — no Word needed."""
    w = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        root = ElementTree.fromstring(z.read("word/document.xml"))
    paras = ("".join(t.text or "" for t in p.iter(f"{w}t")) for p in root.iter(f"{w}p"))
    return "\n".join(p for p in paras)


def read(wanted, start=0):
    f = find(wanted)
    out = _entry(f)
    if f.get("_also"):
        out["others_with_this_name"] = f["_also"]
    if f.get("mimeType") == FOLDER:
        raise ValueError(f"'{f.get('name')}' is a folder. Use action folder to list it.")
    key = (f["id"], f.get("modifiedTime"))
    text = _texts.get(key)
    if text is None:
        text = _text(f)
        if text is None:
            out["note"] = (f"BananaGent can't read a {_kind(f)} — only Docs, Sheets, "
                           "Slides, PDFs, Word and text files. Open the link instead.")
            return out
        _texts[key] = text
        while len(_texts) > _KEEP:
            _texts.pop(next(iter(_texts)))
    start = max(0, int(start or 0))
    end = start + config.DRIVE_READ_CHARS
    out.update(chars=len(text), start=start, text=text[start:end])
    if end < len(text):
        out["more"] = (f"{len(text) - end} more characters — call again with "
                       f"start={end} to read on.")
    return out


# -- what the endpoint does ----------------------------------------------------------
def search(query):
    query = str(query or "").strip()
    if not query:
        raise ValueError("Say what to search for.")
    found = _list(f"fullText contains '{_quote(query)}' and trashed = false",
                  config.DRIVE_LIST_COUNT)
    return {"query": query, "files": [_entry(f) for f in found]}


def recent():
    found = _list(f"trashed = false and mimeType != '{FOLDER}'", config.DRIVE_LIST_COUNT,
                  order="recency desc")
    return {"files": [_entry(f) for f in found]}


def folder(wanted=None):
    if wanted and str(wanted).strip().lower() not in ("root", "my drive", "/"):
        f = find(wanted, folder=True)
    else:
        f = {"id": "root", "name": "My Drive"}
    items = login.pages(f"{API}/files", "files", limit=60,
                        q=f"'{f['id']}' in parents and trashed = false",
                        orderBy="folder,name", pageSize=60,
                        fields=f"nextPageToken,files({_FIELDS})",
                        supportsAllDrives="true", includeItemsFromAllDrives="true")
    out = {"folder": f.get("name"), "id": f["id"], "items": [_entry(i) for i in items]}
    if f.get("_also"):
        out["others_with_this_name"] = f["_also"]
    return out


# -- the platform ---------------------------------------------------------------
def _me():
    user = login.get(f"{API}/about", fields="user(displayName,emailAddress)")["user"]
    return " ".join(filter(None, [user.get("displayName"),
                                  f"<{user['emailAddress']}>" if user.get("emailAddress")
                                  else None]))


def _connect():
    missing = login.connect()
    _texts.clear()
    note = (" Google left out read access, so it won't work — connect again and tick "
            "the box." if missing else "")
    return f"Connected to {TITLE} as {_me()}.{note}"


def _disconnect():
    _texts.clear()
    if login.disconnect():
        return f"Disconnected from {TITLE} and revoked its login."
    return f"{TITLE} wasn't connected."


def _status():
    if not login.connected():
        return {"connected": False}
    try:
        return {"connected": True, "account": _me()}
    except Exception as e:
        return {"connected": False, "note": str(e)}


register(Platform(
    name="drive",
    title=TITLE,
    description="find, list and read your files — Google Docs, Sheets, Slides, PDFs, "
                "Word and text files, including ones shared with you",
    connect=_connect,
    disconnect=_disconnect,
    status=_status,
))
