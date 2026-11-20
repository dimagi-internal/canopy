"""`canopy brain report DIR` — one self-contained HTML page from a `brain preview` directory.

Reads what ``canopy brain preview`` wrote (``index.json`` + ``<session>.json``) and what the
``brain-preview`` skill's learner step wrote, and renders ``DIR/report.html``: summary counts, the
links the brain would store grouped by project and tier with their evidence, each session's
would-read now vs if everyone agreed with the reasons, and the recommendations ranked, each with
evidence linking to the session page on canopy-web. Local only: nothing is sent anywhere.

Learner input (both optional; the report says when there is none):

* ``DIR/learner/<session id>.json`` — one per session read::

      {"session_id": "…", "read": true, "not_read": "",
       "findings": [{"title": "…", "signal": "F3", "route": "skill PR", "target": "canopy:act-as-you",
                     "severity": "high|medium|low", "proof": "proven|inferred",
                     "what": "…", "why": "…", "evidence": [{"index": 12, "quote": "…"}]}]}

* ``DIR/recommendations.json`` — the consolidated, ranked list (the skill's last step): the same
  finding fields, with ``evidence`` items carrying ``session_id``. When it is absent the report
  builds it from ``learner/``: findings with the same title are merged, then ranked by severity,
  proven before inferred, then by how many sessions show it.
"""
from __future__ import annotations

import html
import json
from collections import defaultdict
from pathlib import Path

import click

SEVERITY = {"high": 0, "medium": 1, "low": 2}
PROOF = {"proven": 0, "inferred": 1}


def _load(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def learner_notes(out: Path) -> dict:
    notes = {}
    d = out / "learner"
    if d.is_dir():
        for f in sorted(d.glob("*.json")):
            doc = _load(f, None)
            if isinstance(doc, dict) and doc.get("session_id"):
                notes[str(doc["session_id"])] = doc
    return notes


def _rank_key(rec: dict):
    sessions = {e.get("session_id") for e in rec.get("evidence") or [] if isinstance(e, dict)}
    return (SEVERITY.get(str(rec.get("severity")).lower(), 3), PROOF.get(str(rec.get("proof")).lower(), 2),
            -len(sessions), str(rec.get("title") or ""))


def consolidate(notes: dict) -> list:
    """Findings across sessions, merged by title, ranked."""
    merged: dict = {}
    for sid, doc in notes.items():
        for f in doc.get("findings") or []:
            if not isinstance(f, dict) or not f.get("title"):
                continue
            key = " ".join(str(f["title"]).lower().split())
            ev = [{**e, "session_id": e.get("session_id") or sid}
                  for e in f.get("evidence") or [] if isinstance(e, dict)] or [{"session_id": sid}]
            if key not in merged:
                merged[key] = {**f, "evidence": ev}
                continue
            m = merged[key]
            m["evidence"] = m["evidence"] + ev
            if SEVERITY.get(str(f.get("severity")).lower(), 3) < SEVERITY.get(str(m.get("severity")).lower(), 3):
                m["severity"] = f.get("severity")
            if str(f.get("proof")).lower() == "proven":
                m["proof"] = "proven"
    return sorted(merged.values(), key=_rank_key)


def recommendations(out: Path, notes: dict) -> list:
    recs = _load(out / "recommendations.json", None)
    if isinstance(recs, dict):
        recs = recs.get("recommendations")
    if isinstance(recs, list):
        return [r for r in recs if isinstance(r, dict)]
    return consolidate(notes)


# --- rendering --------------------------------------------------------------------

def e(v) -> str:
    return html.escape("" if v is None else str(v), quote=True)


CSS = """
:root{--bg:#fbfaf7;--fg:#1d1d1b;--muted:#6b6a65;--line:#e4e1d8;--card:#fff;--ok:#1f7a4d;--no:#a33b2b;
--warn:#9a6700;--accent:#2f5d8a;--chip:#f1eee6}
@media (prefers-color-scheme:dark){:root:not([data-theme="light"]){--bg:#161615;--fg:#ecebe6;--muted:#a3a29c;
--line:#33322f;--card:#1f1f1d;--ok:#5cc28f;--no:#ef7f6c;--warn:#e0b252;--accent:#8fb6e0;--chip:#2a2a27}}
:root[data-theme="dark"]{--bg:#161615;--fg:#ecebe6;--muted:#a3a29c;--line:#33322f;--card:#1f1f1d;--ok:#5cc28f;
--no:#ef7f6c;--warn:#e0b252;--accent:#8fb6e0;--chip:#2a2a27}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);
font:15px/1.5 system-ui,-apple-system,"Segoe UI",sans-serif}
main{max-width:1100px;margin:0 auto;padding:24px 16px 64px}h1{font-size:24px;margin:0 0 4px}
h2{font-size:18px;margin:36px 0 10px;border-bottom:1px solid var(--line);padding-bottom:6px}
h3{font-size:15px;margin:18px 0 6px}a{color:var(--accent)}.muted{color:var(--muted)}
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:10px;margin-top:16px}
.card{background:var(--card);border:1px solid var(--line);border-radius:8px;padding:12px}
.card b{display:block;font-size:22px}.card small{color:var(--muted)}
table{width:100%;border-collapse:collapse;background:var(--card);border:1px solid var(--line);border-radius:8px}
th,td{text-align:left;vertical-align:top;padding:7px 9px;border-bottom:1px solid var(--line);font-size:13.5px}
th{font-weight:600;color:var(--muted)}.scroll{overflow-x:auto}
.ok{color:var(--ok)}.no{color:var(--no)}.chip{display:inline-block;background:var(--chip);border-radius:10px;
padding:0 8px;font-size:12px;margin:0 4px 2px 0}.sev-high{color:var(--no)}.sev-medium{color:var(--warn)}
.rec{background:var(--card);border:1px solid var(--line);border-radius:8px;padding:12px 14px;margin:10px 0}
.rec h3{margin:0 0 4px}blockquote{margin:4px 0 4px 0;padding:2px 10px;border-left:3px solid var(--line);
color:var(--muted);white-space:pre-wrap;word-break:break-word}code{font-size:12.5px}
"""


def _verdict(v) -> str:
    if not isinstance(v, dict):
        return '<span class="muted">—</span>'
    if v.get("ok"):
        return '<span class="ok">yes</span>'
    return f'<span class="no">no</span> <span class="muted">{e(v.get("reason"))}</span>'


def _evidence(ev) -> str:
    if isinstance(ev, dict):
        return "; ".join(f"{e(k)}: {e(json.dumps(v) if isinstance(v, (dict, list)) else v)}"
                         for k, v in ev.items())
    return e(ev)


def render(index: dict, notes: dict, recs: list) -> str:
    c = index.get("counts") or {}
    w = index.get("window") or {}
    by_id = {r.get("id"): r for r in index.get("sessions") or []}
    parts = [f"<!doctype html><html lang=en><head><meta charset=utf-8>"
             f"<meta name=viewport content='width=device-width,initial-scale=1'>"
             f"<title>Brain preview</title><style>{CSS}</style></head><body><main>",
             "<h1>Brain preview</h1>",
             f"<p class=muted>{e(w.get('since'))} → {e(w.get('until'))} · view {e(index.get('view'))} · "
             f"read as {e(index.get('as'))} · generated {e(index.get('generated_at'))}. "
             "What canopy's brain <em>would</em> do — nothing was written to canopy-web.</p>"]
    # summary
    read_n = sum(1 for n in notes.values() if n.get("read", True))
    cards = [("sessions", c.get("sessions", 0), f"{c.get('transcripts_truncated', 0)} truncated"),
             ("read by the learner", read_n, f"of {c.get('sessions', 0)}"),
             ("recommendations", len(recs), "ranked below")]
    for cls, v in (c.get("would_read") or {}).items():
        cards.append((f"would read · {cls}", f"{v.get('now', 0)} → {v.get('if_everyone_agreed', 0)}",
                      "now → if everyone agreed"))
    for tier, n in (c.get("links") or {}).items():
        cards.append((f"{tier} links", n, f"across {len(c.get('links_by_project') or {})} projects"))
    parts.append("<div class=cards>" + "".join(
        f"<div class=card><small>{e(k)}</small><b>{e(v)}</b><small>{e(s)}</small></div>" for k, v, s in cards)
        + "</div>")

    # recommendations
    parts.append("<h2>Recommendations, ranked</h2>")
    if not recs:
        parts.append("<p class=muted>No recommendations — the learner step has not run, or found "
                     "nothing that passed the evidence gate.</p>")
    for i, r in enumerate(recs, 1):
        sev = str(r.get("severity") or "").lower()
        meta = " ".join(f"<span class=chip>{e(x)}</span>" for x in
                        (r.get("signal"), r.get("route"), r.get("target"), r.get("proof")) if x)
        ev_html = []
        for ev in r.get("evidence") or []:
            sid = ev.get("session_id")
            row = by_id.get(sid) or {}
            label = row.get("title") or sid
            where = f" · message {e(ev.get('index'))}" if ev.get("index") is not None else ""
            link = f"<a href='{e(row.get('url'))}'>{e(label)}</a>" if row.get("url") else e(label)
            quote = f"<blockquote>{e(ev.get('quote'))}</blockquote>" if ev.get("quote") else ""
            ev_html.append(f"<li>{link}{where}{quote}</li>")
        parts.append(
            f"<div class=rec><h3>{i}. {e(r.get('title'))} <span class='sev-{e(sev)}'>{e(sev)}</span></h3>"
            f"<div>{meta}</div><p>{e(r.get('what'))}</p>"
            + (f"<p class=muted>Why: {e(r.get('why'))}</p>" if r.get("why") else "")
            + f"<ul>{''.join(ev_html)}</ul></div>")

    # links by project and tier
    parts.append("<h2>Links it would store</h2>")
    grouped: dict = defaultdict(lambda: defaultdict(list))
    for row in index.get("sessions") or []:
        for ln in row.get("links") or []:
            grouped[ln.get("project") or "?"][ln.get("tier") or "unknown"].append((row, ln))
    if not grouped:
        parts.append("<p class=muted>No project links in this window.</p>")
    tier_order = {"core": 0, "adjacent": 1, "candidate": 2}
    for project in sorted(grouped):
        parts.append(f"<h3>{e(project)}</h3><div class=scroll><table><tr><th>tier</th><th>session</th>"
                     "<th>how</th><th>evidence</th></tr>")
        for tier in sorted(grouped[project], key=lambda t: tier_order.get(t, 9)):
            for row, ln in grouped[project][tier]:
                parts.append(f"<tr><td>{e(tier)}</td><td><a href='{e(row.get('url'))}'>{e(row.get('title') or row.get('id'))}</a>"
                             f"</td><td>{e(ln.get('how'))}</td><td>{_evidence(ln.get('evidence'))}</td></tr>")
        parts.append("</table></div>")

    # sessions
    parts.append("<h2>Sessions: would read now vs if everyone agreed</h2><div class=scroll><table>"
                 "<tr><th>session</th><th>people</th><th>class</th><th>now</th><th>if everyone agreed</th>"
                 "<th>learner</th></tr>")
    for row in index.get("sessions") or []:
        classes = row.get("would_read") or {}
        if "now" in classes or "if_everyone_agreed" in classes:
            classes = {"all": classes}
        classes = classes or {"—": {}}
        note = notes.get(row.get("id"))
        if note is None:
            learner = '<span class=muted>not read</span>'
        elif not note.get("read", True):
            learner = f'<span class=no>not read</span> <span class=muted>{e(note.get("not_read"))}</span>'
        else:
            learner = f"{len(note.get('findings') or [])} findings"
        people = ", ".join(e(p.get("name")) for p in row.get("participants") or [] if isinstance(p, dict))
        ws = (row.get("workspace") or {}).get("slug") if isinstance(row.get("workspace"), dict) else ""
        trunc = " <span class=chip>truncated</span>" if row.get("truncated") else ""
        n = len(classes)
        for j, (cls, v) in enumerate(classes.items()):
            v = v if isinstance(v, dict) else {}
            head = (f"<td rowspan={n}><a href='{e(row.get('url'))}'>{e(row.get('title') or row.get('id'))}</a>"
                    f"<br><span class=muted>{e(ws)} · {e(row.get('started_at'))} · {e(row.get('messages'))} msgs"
                    f"</span>{trunc}</td><td rowspan={n}>{people}</td>") if j == 0 else ""
            tail = f"<td rowspan={n}>{learner}</td>" if j == 0 else ""
            parts.append(f"<tr>{head}<td>{e(cls)}</td><td>{_verdict(v.get('now'))}</td>"
                         f"<td>{_verdict(v.get('if_everyone_agreed'))}</td>{tail}</tr>")
    parts.append("</table></div></main></body></html>")
    return "".join(parts)


def build(out: Path) -> Path:
    index = _load(out / "index.json", None)
    if not isinstance(index, dict):
        raise click.ClickException(f"no index.json in {out} — run `canopy brain preview --out {out}` first.")
    notes = learner_notes(out)
    page = render(index, notes, recommendations(out, notes))
    dest = out / "report.html"
    dest.write_text(page, encoding="utf-8")
    return dest
