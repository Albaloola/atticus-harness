# Scots Legal Humanizer

A specialist AI writing skill for Scottish legal and formal procedural prose.

## What it does

It rewrites AI-sounding drafts so they read like careful, natural Scottish legal writing. It is designed for:

- Scottish sheriff court material
- simple procedure claims and responses
- ordinary cause style documents
- Court of Session and judicial review notes
- tribunal and regulator correspondence
- SPSO-style complaints
- internal complaints and escalation requests
- procedural letters
- party litigant drafts
- witness-style factual narratives

## What makes it different from a generic humanizer

Generic humanizers often add personality. This skill does not do that. It adds legal credibility by making the text precise, proportionate, evidence-led, and locally correct.

It focuses on:

- Scottish terminology
- correct procedural register
- plain legal English
- chronology
- document references
- safe allegation wording
- clear remedies
- avoiding imported England and Wales or US terms
- removing generic AI legal phrasing

## How to use

Ask your AI assistant something like:

"Use the Scots Legal Humanizer skill. Rewrite this as a natural Scottish sheriff court simple procedure response. Keep the facts unchanged and do not add legal authorities."

Or:

"Humanise this complaint escalation for a Scottish public authority. Keep it firm but measured."

## Important limitation

This skill edits wording. It does not replace legal advice. Check the relevant court rules, tribunal rules, complaint process, deadlines, and any professional advice before lodging or sending a document.

## Pipeline configuration

When converting humanised markdown to PDF via pandoc and weasyprint, disable smart typography to prevent automatic reintroduction of em dashes, curly quotes, and other decorative characters:

```bash
pandoc -f markdown-smart input.md -o output.html
```

Without this flag, pandoc's default `+smart` extension converts `--` and `---` to en and em dashes inside prose, and may also affect URLs containing `--`. This would reintroduce patterns the humaniser removed.

## Humanisation policy

The scots-legal-humanizer should be applied to all human-facing harness outputs, not only the final case-pack PDF. This includes:

- Run reports (`reports/harness-runs/<RUN_ID>.md`)
- Blocker reports
- Matter-health summaries delivered as prose
- Any text shown in Telegram or chat that is longer than a brief status line
- Atticus worker final output reports
- Summaries of repairs, reducer reviews, or provider health

**Exemptions:** Brief inline status messages and machine-parsable JSON.

The harness resolves the skill automatically via `skills_for_task()` for relevant task types. For intermediate artifacts, the skill can be attached via the work order by including "humanize", "humanise", or "de-ai" in the task title or task type.
