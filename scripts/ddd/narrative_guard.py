"""Autonomous narrative review — every narrative edit is checked, nothing waits on a human.

Why this module exists
----------------------
The narrative-agreement gate was built for high-polish demos: a human reads the
story and approves it before anything renders. DDD now also drives ordinary
builds, and the human rarely reviews narratives (Jonathan, 2026-10-06: "I'm not
going to review and approve but go for it"). What happened instead, from Hal's
audit of the last two days of runs (canopy#789):

* ACE Spark: 20 narrative versions, every one still ``pending``; 9 posted on
  10-06 alone, 5 with no run attached. connect-labs supply-sophie-sheets: v1..v8,
  all agent-authored, each a new pending review.
* What slipped through un-reviewed: "comparable" became a narrated feature
  (``all-comparable``, "three of three comparable") that Jonathan later rejected
  ("not a key feature ... a ton of hardcoded 'comparable' rules"); the email
  focus came back after he had said "the video I saw was way too focused on the
  e-mail aspect", and he had to re-steer.
* Narrative edits were used to make judge findings go away rather than to tell
  a better story.

So the narrative is no longer frozen OR left unwatched. Every revision is
reviewed here, autonomously, against three references:

1. **the prior version** — what this edit actually changed;
2. **the original brief** — the narrative's baseline (``baseline`` in the intent
   ledger, captured the first time the narrative is guarded);
3. **the intent ledger** — every human steer, verbatim, with the terms it
   forbids, limits or requires (``<slug>.intent.yaml`` beside the spec, so it
   travels with the narrative in the target repo).

An edit is REJECTED when it

* ``unrequested_feature`` — adds a feature whose name is a concept neither the
  brief nor any steer mentions;
* ``contradicts_steer`` — brings back a forbidden term, mentions a limited term
  more than the steer left it, or drops a required one;
* ``judge_driven`` — is motivated by a judge finding and ADDS to the story
  (a feature, or net narration) instead of making the words follow the product;
* ``explanation`` — grows the narration with explanatory sentences ("which
  means", "so that", "in other words" ...): telling the viewer what the product
  should be showing.

Accepted edits are recorded as versions in the run dir. None of them mints a
pending review. A MATERIAL change (features, personas, scene set or order, or a
large rewrite) is listed for the digest; in ``polish`` mode it is also the one
thing that still goes to the human gate.

Modes (``loop.narrative_mode`` — ``auto`` | ``build`` | ``polish``):

``build``
    The narrative is a living spec the loop may evolve. The first version is
    posted for visibility but does not block; agent revisions pass this guard
    and are recorded locally; material drift is reported in the digest.
``polish``
    Human-gated as before: the first version blocks on the narrative-agreement
    gate. Agent revisions still pass this guard; a MATERIAL one is the only kind
    that goes back to the human.

``auto`` = ``build`` unless the run's objective is ``demo``.

CLI::

    python -m scripts.ddd.narrative_guard check <spec> --run <run_id> --reason "..." [--findings ID ...]
    python -m scripts.ddd.narrative_guard steer <spec> --quote "..." [--by NAME] [--source S]
                                                [--forbid T ...] [--limit T ...] [--require T ...]
    python -m scripts.ddd.narrative_guard ledger <spec>
    python -m scripts.ddd.narrative_guard mode [--objective O]

``check`` exits 0 on accept (prints the record), 2 on reject (the edit must be
reverted or re-made), 1 on a usage/IO error.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

import yaml

BUILD = "build"
POLISH = "polish"
AUTO = "auto"
MODES = (AUTO, BUILD, POLISH)

ACCEPT = "accept"
REJECT = "reject"

UNREQUESTED_FEATURE = "unrequested_feature"
CONTRADICTS_STEER = "contradicts_steer"
JUDGE_DRIVEN = "judge_driven"
EXPLANATION = "explanation"

#: Net narration growth (fraction of the prior word count) a judge-motivated
#: edit may not exceed. Accuracy fixes make the words follow the product; they
#: shorten or swap words, they do not add paragraphs.
JUDGE_GROWTH_LIMIT = 0.10
#: A change to this fraction of the narration's words is a material rewrite.
MATERIAL_REWRITE = 0.25

_STOP = frozenset(
    """a an and are as at be been being but by can could did do does doing for from had has
    have her hers him his how i if in into is it its just may me might more most my no nor not
    of on once only or other our out over own same she should so some such than that the their
    them then there these they this those through to too under until up very was we were what
    when where which while who whom why will with would you your each every all any both few
    one two three four five six seven eight nine ten new now also yet still here""".split()
)

#: Phrases that mark a sentence as explaining rather than showing.
_EXPLAIN = re.compile(
    r"\b(which means|that means|this means|so that|in other words|this is why|that is why|"
    r"this shows|which shows|note that|to make (?:it|this) clear|to be clear|the point is|"
    r"because|meaning that|that way|this way)\b",
    re.IGNORECASE,
)

#: A revision reason that names a judge / finding / score.
_JUDGE_REASON = re.compile(
    r"\b(finding|findings|judge|judged|verdict|score|scored|dimension|rubric|cell|"
    r"concept[_ ]eval|visual[_ -]judge|arc[_ ]eval|product[_ ]lens|lint)\b|"
    r"\b(clarity|trust|task_completion|claim_reality_coherence|why_groundedness|"
    r"visual_variety|visual_polish|concept_clarity|design_soundness|feature_use)\b",
    re.IGNORECASE,
)

_SENTENCE = re.compile(r"(?<=[.!?])\s+")
_WORD = re.compile(r"[a-z][a-z0-9'-]*")


# ---------------------------------------------------------------------------
# Mode
# ---------------------------------------------------------------------------


def resolve_mode(configured: str | None, objective: str | None) -> str:
    """``build`` | ``polish`` — explicit config wins, else from the objective."""
    c = str(configured or AUTO).strip().lower()
    if c in (BUILD, POLISH):
        return c
    return POLISH if str(objective or "").strip().lower() == "demo" else BUILD


# ---------------------------------------------------------------------------
# Reading a narrative (raw spec dicts — a mid-edit spec need not validate)
# ---------------------------------------------------------------------------


def _text(x: Any) -> str:
    if isinstance(x, list):
        return " ".join(str(p).strip() for p in x if str(p).strip())
    return str(x or "").strip()


def _scene_key(s: dict, i: int) -> str:
    sid = str(s.get("id") or "").strip()
    if sid:
        return sid
    title = str(s.get("title") or "").strip().lower()
    return re.sub(r"[^a-z0-9]+", "-", title).strip("-") or f"scene-{i + 1}"


def view(raw: dict) -> dict:
    """The story-bearing part of a spec — what a narrative revision can change."""
    raw = raw if isinstance(raw, dict) else {}
    scenes = []
    for i, s in enumerate(raw.get("scenes") or []):
        if not isinstance(s, dict):
            continue
        scenes.append(
            {
                "key": _scene_key(s, i),
                "title": str(s.get("title") or ""),
                "persona": str(s.get("persona") or ""),
                "narration": _text(s.get("narrative")) or _text(s.get("concept_claim")),
                "features": [
                    {"id": str(f.get("id") or ""), "description": str(f.get("description") or "")}
                    for f in s.get("features") or []
                    if isinstance(f, dict)
                ],
            }
        )
    personas = {
        str(k): f"{(v or {}).get('name', '')} {(v or {}).get('role', '')}".strip()
        for k, v in (raw.get("personas") or {}).items()
        if isinstance(v, dict)
    }
    return {"narrative": _text(raw.get("narrative")), "personas": personas, "scenes": scenes}


def view_hash(v: dict) -> str:
    return hashlib.sha256(json.dumps(v, sort_keys=True).encode()).hexdigest()[:16]


def _story_text(v: dict) -> str:
    parts = [v.get("narrative") or ""]
    for s in v.get("scenes") or []:
        parts.append(s["title"])
        parts.append(s["narration"])
        for f in s["features"]:
            parts.append(f"{f['id']} {f['description']}")
    return "\n".join(parts)


def _narration_text(v: dict) -> str:
    return "\n".join([v.get("narrative") or ""] + [s["narration"] for s in v.get("scenes") or []])


def _words(text: str) -> list[str]:
    return _WORD.findall(str(text or "").lower())


def _content(text: str) -> set[str]:
    return {w for w in _words(text.replace("-", " ").replace("_", " ")) if len(w) >= 4 and w not in _STOP}


def _stem(w: str) -> str:
    for suf in ("ities", "ity", "ings", "ing", "ies", "ed", "es", "s"):
        if len(w) > len(suf) + 3 and w.endswith(suf):
            return w[: -len(suf)]
    return w


def _stems(words: Iterable[str]) -> set[str]:
    return {_stem(w) for w in words}


def _strings(x: Any) -> Iterable[str]:
    """Every string leaf of a parsed YAML document (the brief's whole text)."""
    if isinstance(x, str):
        yield x
    elif isinstance(x, dict):
        for v in x.values():
            yield from _strings(v)
    elif isinstance(x, list):
        for v in x:
            yield from _strings(v)


def term_count(text: str, term: str) -> int:
    """Whole-word, case-insensitive occurrences of ``term`` (a word or phrase)."""
    t = str(term or "").strip()
    if not t:
        return 0
    pat = r"\b" + r"\s+".join(re.escape(p) for p in t.split()) + r"\w*"
    return len(re.findall(pat, str(text or ""), flags=re.IGNORECASE))


# ---------------------------------------------------------------------------
# Intent ledger
# ---------------------------------------------------------------------------


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def ledger_path(spec_path: str | Path) -> Path:
    p = Path(spec_path)
    stem = p.name[: -len(".yaml")] if p.name.endswith(".yaml") else p.stem
    return p.with_name(f"{stem}.intent.yaml")


def brief_path(spec_path: str | Path, raw: dict | None = None) -> Path | None:
    p = Path(spec_path)
    if raw and raw.get("why_brief"):
        cand = (p.parent / str(raw["why_brief"])).resolve()
        if cand.exists():
            return cand
    stem = p.name[: -len(".yaml")] if p.name.endswith(".yaml") else p.stem
    cand = p.with_name(f"{stem}.why_brief.yaml")
    return cand if cand.exists() else None


def load_ledger(spec_path: str | Path) -> dict:
    lp = ledger_path(spec_path)
    try:
        data = yaml.safe_load(lp.read_text()) if lp.exists() else None
    except Exception:
        data = None
    data = data if isinstance(data, dict) else {}
    data.setdefault("baseline", None)
    data["steers"] = [s for s in data.get("steers") or [] if isinstance(s, dict)]
    return data


def save_ledger(spec_path: str | Path, ledger: dict) -> Path:
    lp = ledger_path(spec_path)
    header = (
        "# Intent ledger (scripts/ddd/narrative_guard.py, canopy#789): the narrative's\n"
        "# baseline and every human steer, verbatim. Every narrative revision is checked\n"
        "# against it. Add a steer with `python -m scripts.ddd.narrative_guard steer`.\n"
    )
    lp.write_text(header + yaml.safe_dump(ledger, sort_keys=False, allow_unicode=True, width=100))
    return lp


def _read_raw(spec_path: str | Path) -> dict:
    return yaml.safe_load(Path(spec_path).read_text()) or {}


def ensure_baseline(spec_path: str | Path, ledger: dict | None = None) -> dict:
    """Capture the ORIGINAL brief + narrative once; later revisions are read against it."""
    ledger = ledger if ledger is not None else load_ledger(spec_path)
    if ledger.get("baseline"):
        return ledger
    raw = _read_raw(spec_path)
    v = view(raw)
    bp = brief_path(spec_path, raw)
    brief_text = ""
    if bp is not None:
        try:
            brief_text = "\n".join(_strings(yaml.safe_load(bp.read_text()) or {}))
        except Exception:
            brief_text = ""
    ledger["baseline"] = {
        "captured_at": _now(),
        "hash": view_hash(v),
        "brief": brief_text,
        "story": _story_text(v),
    }
    return ledger


def add_steer(
    spec_path: str | Path,
    quote: str,
    *,
    by: str = "",
    source: str = "",
    forbid: Iterable[str] = (),
    limit: Iterable[str] = (),
    require: Iterable[str] = (),
    at: str | None = None,
) -> dict:
    """Record a human steer. ``limit`` terms freeze their CURRENT mention count."""
    ledger = ensure_baseline(spec_path)
    narration = _narration_text(view(_read_raw(spec_path)))
    steer = {
        "id": f"steer-{len(ledger['steers']) + 1}",
        "at": at or _now(),
        "by": by,
        "source": source,
        "quote": str(quote).strip(),
        "forbid": [t for t in (str(x).strip() for x in forbid) if t],
        "limit": {t: term_count(narration, t) for t in (str(x).strip() for x in limit) if t},
        "require": [t for t in (str(x).strip() for x in require) if t],
    }
    ledger["steers"].append(steer)
    save_ledger(spec_path, ledger)
    return steer


def _grounding_corpus(ledger: dict) -> set[str]:
    base = ledger.get("baseline") or {}
    text = "\n".join([base.get("brief") or "", base.get("story") or ""])
    for s in ledger.get("steers") or []:
        text += "\n" + str(s.get("quote") or "")
        text += "\n" + " ".join(s.get("require") or [])
    return _stems(_content(text))


# ---------------------------------------------------------------------------
# The review
# ---------------------------------------------------------------------------


def _features(v: dict) -> dict[str, dict]:
    out = {}
    for s in v.get("scenes") or []:
        for f in s["features"]:
            if f["id"]:
                out[f["id"]] = {**f, "scene": s["key"]}
    return out


def _sentences(text: str) -> set[str]:
    return {s.strip() for s in _SENTENCE.split(str(text or "")) if s.strip()}


def _rewrite_ratio(prior: str, new: str) -> float:
    import difflib

    a, b = _words(prior), _words(new)
    if not a and not b:
        return 0.0
    sm = difflib.SequenceMatcher(a=a, b=b, autojunk=False)
    same = sum(blk.size for blk in sm.get_matching_blocks())
    return 1.0 - (2.0 * same) / (len(a) + len(b))


def review(
    prior: dict,
    new: dict,
    ledger: dict,
    *,
    reason: str = "",
    findings: Iterable[str] = (),
) -> dict:
    """Review one narrative revision ``prior`` -> ``new`` (both :func:`view` dicts).

    Returns ``{decision, violations, standing, material, material_why, summary}``.
    ``standing`` lists ledger violations the PRIOR version already had (reported,
    not a reason to reject an edit that did not cause them).
    """
    findings = [str(f) for f in findings or []]
    violations: list[dict] = []
    standing: list[dict] = []
    corpus = _grounding_corpus(ledger)
    pf, nf = _features(prior), _features(new)
    added = [nf[k] for k in nf if k not in pf]
    removed = [pf[k] for k in pf if k not in nf]

    # (a) a feature nobody asked for.
    for f in added:
        name_terms = _content(f["id"])
        novel_name = sorted(t for t in name_terms if _stem(t) not in corpus)
        # The id NAMES the feature; a description is free to use new words for
        # a grounded concept. Only an id with no content words (``f1``) falls
        # back to its description.
        desc_terms = _content(f["description"]) if not name_terms else set()
        novel_desc = [t for t in desc_terms if _stem(t) not in corpus]
        if novel_name or (desc_terms and len(novel_desc) / len(desc_terms) >= 0.5):
            violations.append(
                {
                    "rule": UNREQUESTED_FEATURE,
                    "scene": f["scene"],
                    "feature": f["id"],
                    "detail": (
                        f"new feature {f['id']!r} names "
                        f"{', '.join(repr(t) for t in (novel_name or sorted(novel_desc)[:4]))}, which neither "
                        "the original brief nor any human steer mentions"
                    ),
                }
            )

    # (b) contradicts a human steer.
    p_story, n_story = _story_text(prior), _story_text(new)
    p_narr, n_narr = _narration_text(prior), _narration_text(new)
    for s in ledger.get("steers") or []:
        cite = f"{s.get('id')}: \"{str(s.get('quote') or '')[:160]}\""
        for t in s.get("forbid") or []:
            before, after = term_count(p_story, t), term_count(n_story, t)
            if after > before:
                violations.append(
                    {"rule": CONTRADICTS_STEER, "steer": s.get("id"), "term": t,
                     "detail": f"brings back {t!r} ({before} -> {after} mention(s)) against {cite}"}
                )
            elif after:
                standing.append(
                    {"rule": CONTRADICTS_STEER, "steer": s.get("id"), "term": t,
                     "detail": f"{t!r} is still in the story ({after} mention(s)) against {cite}"}
                )
        for t, cap in (s.get("limit") or {}).items():
            before, after = term_count(p_narr, t), term_count(n_narr, t)
            if after > max(int(cap or 0), before):
                violations.append(
                    {"rule": CONTRADICTS_STEER, "steer": s.get("id"), "term": t,
                     "detail": f"mentions {t!r} {after}x, more than the {int(cap or 0)} left by {cite}"}
                )
        for t in s.get("require") or []:
            if term_count(p_story, t) and not term_count(n_story, t):
                violations.append(
                    {"rule": CONTRADICTS_STEER, "steer": s.get("id"), "term": t,
                     "detail": f"drops {t!r}, which {cite} asked for"}
                )

    # (c) exists to satisfy a judge.
    p_words, n_words = len(_words(p_narr)), len(_words(n_narr))
    growth = (n_words - p_words) / max(p_words, 1)
    judge_motivated = bool(findings) or bool(_JUDGE_REASON.search(reason or ""))
    if judge_motivated and (added or growth > JUDGE_GROWTH_LIMIT):
        what = []
        if added:
            what.append(f"adds feature(s) {[f['id'] for f in added]}")
        if growth > JUDGE_GROWTH_LIMIT:
            what.append(f"grows the narration {growth:+.0%}")
        violations.append(
            {
                "rule": JUDGE_DRIVEN,
                "detail": (
                    "a judge-motivated edit " + " and ".join(what) + ". A finding is answered by "
                    "making the words follow the product (or by fixing the product), never by "
                    "adding to the story to satisfy the judge"
                ),
            }
        )

    # (d) explanation in place of product.
    new_sentences = [x for x in _sentences(n_narr) - _sentences(p_narr) if _EXPLAIN.search(x)]
    if new_sentences and n_words > p_words:
        violations.append(
            {
                "rule": EXPLANATION,
                "detail": "adds explanatory narration instead of showing it in the product: "
                + " | ".join(repr(x[:140]) for x in sorted(new_sentences)[:3]),
            }
        )

    # Materiality — what a human would want to hear about.
    why: list[str] = []
    if added:
        why.append(f"features added {[f['id'] for f in added]}")
    if removed:
        why.append(f"features removed {[f['id'] for f in removed]}")
    if prior.get("personas") != new.get("personas"):
        why.append("personas changed")
    pk, nk = [s["key"] for s in prior.get("scenes") or []], [s["key"] for s in new.get("scenes") or []]
    if set(pk) != set(nk):
        why.append(f"scenes {sorted(set(nk) - set(pk))} added / {sorted(set(pk) - set(nk))} removed")
    elif pk != nk:
        why.append("scenes reordered")
    rewrite = _rewrite_ratio(p_narr, n_narr)
    if rewrite >= MATERIAL_REWRITE:
        why.append(f"{rewrite:.0%} of the narration rewritten")

    decision = REJECT if violations else ACCEPT
    return {
        "decision": decision,
        "violations": violations,
        "standing": standing,
        "material": bool(why),
        "material_why": why,
        "summary": {
            "features_added": [f["id"] for f in added],
            "features_removed": [f["id"] for f in removed],
            "narration_words": [p_words, n_words],
            "rewrite_ratio": round(rewrite, 3),
            "reason": reason,
            "findings": findings,
        },
    }


# ---------------------------------------------------------------------------
# Versions (run dir) — recorded, never posted as a pending review
# ---------------------------------------------------------------------------


def versions_dir(run_dir: str | Path) -> Path:
    return Path(run_dir) / "narrative-versions"


def latest_version(run_dir: str | Path) -> tuple[int, dict] | None:
    d = versions_dir(run_dir)
    if not d.exists():
        return None
    best: tuple[int, dict] | None = None
    for p in d.glob("v*.json"):
        m = re.fullmatch(r"v(\d+)\.json", p.name)
        if not m:
            continue
        n = int(m.group(1))
        if best is None or n > best[0]:
            try:
                best = (n, json.loads(p.read_text()))
            except Exception:
                continue
    return best


def _append_log(run_dir: Path, record: dict) -> None:
    d = versions_dir(run_dir)
    d.mkdir(parents=True, exist_ok=True)
    with (d / "log.jsonl").open("a") as fh:
        fh.write(json.dumps(record, sort_keys=True) + "\n")


def check(
    spec_path: str | Path,
    run_dir: str | Path,
    *,
    reason: str = "",
    findings: Iterable[str] = (),
    mode: str = BUILD,
) -> dict:
    """Review the spec's current narrative against the last accepted version in ``run_dir``.

    * No version yet: the current narrative becomes ``v0`` (the run's starting
      story) and the ledger's baseline is captured if missing. Nothing is judged.
    * Unchanged since the last version: ``unchanged``.
    * Changed: :func:`review`. An accepted revision becomes the next version; a
      rejected one is logged and NOT recorded (the last accepted version stays
      the reference, so the edit must be reverted or re-made).

    Returns the record (also appended to ``narrative-versions/log.jsonl``), with
    ``post_review`` true only for a material revision in ``polish`` mode — the
    one case that still goes to the human gate.
    """
    run_dir = Path(run_dir)
    ledger = ensure_baseline(spec_path)
    save_ledger(spec_path, ledger)
    current = view(_read_raw(spec_path))
    h = view_hash(current)
    last = latest_version(run_dir)
    base = {"at": _now(), "hash": h, "mode": mode, "reason": reason, "findings": list(findings)}
    if last is None:
        rec = {**base, "version": 0, "decision": "baseline", "material": False, "post_review": False}
        versions_dir(run_dir).mkdir(parents=True, exist_ok=True)
        (versions_dir(run_dir) / "v0.json").write_text(json.dumps({"view": current, **rec}, indent=1))
        _append_log(run_dir, rec)
        return rec
    n, prev = last
    if prev.get("hash") == h:
        return {**base, "version": n, "decision": "unchanged", "material": False, "post_review": False}
    result = review(prev.get("view") or {}, current, ledger, reason=reason, findings=findings)
    rec = {**base, **result, "from_version": n}
    if result["decision"] == ACCEPT:
        rec["version"] = n + 1
        rec["post_review"] = bool(result["material"] and mode == POLISH)
        (versions_dir(run_dir) / f"v{n + 1}.json").write_text(json.dumps({"view": current, **rec}, indent=1))
    else:
        rec["version"] = None
        rec["post_review"] = False
    _append_log(run_dir, rec)
    return rec


def unchecked_change(spec_path: str | Path, run_dir: str | Path) -> bool:
    """True when the narrative changed since the last recorded version (an edit nobody checked)."""
    last = latest_version(run_dir)
    if last is None:
        return False
    try:
        return last[1].get("hash") != view_hash(view(_read_raw(spec_path)))
    except Exception:
        return False


def digest(run_dir: str | Path) -> dict:
    """What the run's digest reports: material drift + rejections. Minor wording is omitted."""
    p = versions_dir(run_dir) / "log.jsonl"
    rows = []
    if p.exists():
        for line in p.read_text().splitlines():
            try:
                rows.append(json.loads(line))
            except Exception:
                continue
    return {
        "versions": sum(1 for r in rows if r.get("decision") == ACCEPT),
        "material": [
            {"version": r.get("version"), "why": r.get("material_why"), "reason": r.get("reason")}
            for r in rows
            if r.get("decision") == ACCEPT and r.get("material")
        ],
        "rejected": [
            {"at": r.get("at"), "reason": r.get("reason"),
             "violations": [v.get("detail") for v in r.get("violations") or []]}
            for r in rows
            if r.get("decision") == REJECT
        ],
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _run_dir(run_id: str) -> Path:
    from scripts.ddd.runstate import _resolve_ddd_dir, _run_dir_for

    return _run_dir_for(_resolve_ddd_dir(), run_id)


def _mode_for_run(run_id: str) -> str:
    from scripts.ddd import loop_config
    from scripts.ddd.runstate import load

    cfg = loop_config.load()
    try:
        objective = load(run_id).objective
    except Exception:
        objective = None
    objective = objective or cfg.loop.objective
    return resolve_mode(cfg.loop.narrative_mode, objective)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m scripts.ddd.narrative_guard")
    sub = ap.add_subparsers(dest="cmd", required=True)

    c = sub.add_parser("check", help="review the spec's narrative revision (exit 2 = rejected)")
    c.add_argument("spec")
    c.add_argument("--run", required=True, help="run id")
    c.add_argument("--reason", default="", help="why the narrative changed")
    c.add_argument("--findings", nargs="*", default=[], help="finding ids the edit answers")
    c.add_argument("--json", action="store_true")

    s = sub.add_parser("steer", help="record a human steer in the intent ledger")
    s.add_argument("spec")
    s.add_argument("--quote", required=True, help="the human's own words, verbatim")
    s.add_argument("--by", default="")
    s.add_argument("--source", default="", help="where it was said (thread, review id, chat)")
    s.add_argument("--forbid", nargs="*", default=[])
    s.add_argument("--limit", nargs="*", default=[])
    s.add_argument("--require", nargs="*", default=[])

    l = sub.add_parser("ledger", help="print the intent ledger")
    l.add_argument("spec")

    d = sub.add_parser("digest", help="material drift + rejections for a run")
    d.add_argument("run")

    m = sub.add_parser("mode", help="print build|polish for a run or objective")
    m.add_argument("--run")
    m.add_argument("--objective")

    a = ap.parse_args(argv)
    if a.cmd == "check":
        if not Path(a.spec).exists():
            print(f"ERROR: spec not found: {a.spec}", file=sys.stderr)
            return 1
        rec = check(a.spec, _run_dir(a.run), reason=a.reason, findings=a.findings, mode=_mode_for_run(a.run))
        if a.json:
            print(json.dumps(rec, indent=1))
        elif rec["decision"] == REJECT:
            print("NARRATIVE EDIT REJECTED — revert it (or re-make it without the violation):")
            for v in rec["violations"]:
                print(f"  - [{v['rule']}] {v['detail']}")
            print(
                "If a human actually asked for this, record their words first:\n"
                "  python -m scripts.ddd.narrative_guard steer <spec> --quote \"...\" --require <term>"
            )
        else:
            line = f"narrative {rec['decision']}"
            if rec.get("version") is not None:
                line += f" -> v{rec['version']}"
            if rec.get("material"):
                line += f" (MATERIAL: {'; '.join(rec['material_why'])})"
            if rec.get("post_review"):
                line += " — polish mode: post it to the narrative gate"
            print(line)
            for v in rec.get("standing") or []:
                print(f"  standing: {v['detail']}")
        return 2 if rec["decision"] == REJECT else 0
    if a.cmd == "steer":
        steer = add_steer(
            a.spec, a.quote, by=a.by, source=a.source, forbid=a.forbid, limit=a.limit, require=a.require
        )
        print(json.dumps(steer))
        return 0
    if a.cmd == "ledger":
        print(json.dumps(load_ledger(a.spec), indent=1))
        return 0
    if a.cmd == "digest":
        print(json.dumps(digest(_run_dir(a.run)), indent=1))
        return 0
    if a.cmd == "mode":
        print(_mode_for_run(a.run) if a.run else resolve_mode(None, a.objective))
        return 0
    return 1


__all__ = [
    "ACCEPT",
    "BUILD",
    "POLISH",
    "REJECT",
    "add_steer",
    "check",
    "digest",
    "ensure_baseline",
    "ledger_path",
    "load_ledger",
    "resolve_mode",
    "review",
    "term_count",
    "unchecked_change",
    "view",
]

if __name__ == "__main__":
    sys.exit(main())
