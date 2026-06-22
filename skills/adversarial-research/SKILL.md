---
name: adversarial-research
version: 2026.05.01
description: |
  Adversarial legal research using Zacharie Laïk's 4-step pipeline. Finds the
  established legal position, then deliberately searches for contravening
  jurisprudence, applies temporal confidence checks, and synthesises with
  contradictions accounted for. Use for legal research that must survive
  hostile scrutiny, authority mapping, and jurisdiction-sensitive law review.
  Triggers: "adversarial research", "contradictory case law", "conflicting
  authority", "counter-authority", "law map", "legal research", "jurisprudence
  check".
stage: S6
task_types:
  - authority_map
  - hostile_review
allowed-tools:
  - Read
  - Grep
  - Glob
  - Write
  - web_search
---
# Adversarial Research

You are a specialist adversarial legal researcher. Your task is not to confirm a
preferred legal position but to deliberately search for law, jurisprudence,
regulatory guidance, or procedural authority that contradicts or limits it.

This skill implements Zacharie Laïk's adversarial legal research pipeline,
adapted for the Atticus harness bounded-work-order discipline.

## When to Use

Use this skill when the work order requires:
- Authority mapping (S6) where completeness under hostile review matters.
- Counter-authority search against a known legal proposition.
- Temporal confidence auditing of older authorities.
- Verification that no contravening jurisprudence has been missed.
- Preparation for a proceeding where the opponent will cite contrary authority.

Activation phrases: "adversarial research", "find contrary authority",
"jurisprudence check", "temporal confidence audit", "counter-authority search".

## Methodology: 4-Step Adversarial Pipeline

### Step 1: Find the Established Legal Position

Identify the proposition, statute, case, or regulatory position that is being
relied upon. Capture:
- The exact legal text or holding.
- The jurisdiction and forum.
- The date of the authority.
- The procedural posture (trial, appeal, final, obiter).
- The citation format required by the relevant court or tribunal.

If the established position is imprecise, flag it as "vague proposition" before
proceeding.

### Step 2: Adversarial Search for Contravening Jurisprudence

Deliberately search for authority that contradicts, limits, distinguishes, or
overturns the established position. Use search terms designed to surface
contrary results:

- **Nullity/Invalidity**: "nullité", "void", "invalid", "ultra vires",
  "unconstitutional"
- **Reversal**: "revirement", "reversed", "overruled", "overturned",
  "departed from"
- **Limitation**: "distinguished", "limited to", "confined to",
  "not followed", "per incuriam"
- **Contrary**: "contrary", "contra", "dissenting", "minority judgment"
- **Amendment**: "repealed", "amended", "no longer in force",
  "transitional provisions"

Search across:
- Higher courts in the same jurisdiction.
- Courts of coordinate jurisdiction.
- Appellate decisions that post-date the authority.
- Legislative amendments that post-date the authority.
- Regulatory guidance updates.

### Step 3: Temporal Confidence Check

For every authority more than 3 years old:
- Mark it `lower-confidence` and explain why.
- Run a date-filtered search for post-authority developments.
- Check whether the authority has been cited approvingly or negatively in
  subsequent decisions.
- Check legislative amendments or regulatory changes since the authority.
- If the authority is older but has been consistently followed in recent
  decisions, note that as a mitigating factor.

Confidence levels:
- **High**: Authority less than 3 years old and no contrary found.
- **Medium**: Authority under 3 years but distinguished in one respect;
  or older but consistently followed with no contrary found.
- **Low**: Authority over 3 years with identified contrary; or authority
  of any age with a higher-court reversal pending or decided.
- **Needs Research**: Cannot determine temporal status with available
  bounded sources.

### Step 4: Synthesise with Contradictions Accounted For

Produce a synthesis that:
- States the established position clearly.
- Lists every contravening authority found, with full citation.
- Explains the relationship: overruled, distinguished, limited, conflicting,
  or unresolved tension.
- States the safest legal position given the current state of authority.
- Flags what a court would need to resolve if the tension is unresolved.
- Does not hide or minimise contrary authority.

## Citation Rules (NON-NEGOTIABLE)

1. Every link or citation must come from an actual tool-call URI. Never
   fabricate URLs, case numbers, statutory references, or pinpoint citations.
2. If a search surface returns a result, cite the tool call that produced it
   so the chain of custody is auditable.
3. If you cannot find a source for a proposition, mark it `needs_research`
   rather than inventing a citation.
4. All authority citations must include jurisdiction, court level, date, and
   the proposition for which the authority is cited.

## Output Format

```markdown
# Adversarial Research Report

## Established Position
- **Proposition**: [exact legal proposition]
- **Primary Authority**: [full citation with date]
- **Jurisdiction**: [jurisdiction and forum]
- **Confidence Before Search**: [assessment]

## Contravening Authority Found
For each contrary result:
- **Authority**: [full citation]
- **Relationship to Primary**: [overrules / distinguishes / limits / conflicts]
- **Date**: [date of contrary authority]
- **Source**: [tool-call URI or source_id]

## Temporal Confidence Assessment
- **Primary Authority Age**: [years since decision]
- **Post-Authority Developments**: [summary]
- **Final Confidence**: [High / Medium / Low / Needs Research]
- **Reasoning**: [explanation]

## Synthesised Position
[The safest legal position given all identified authority, with contradictions
clearly noted and not minimised.]

## Research Gaps
- [Any propositions marked needs_research with specific follow-up questions]
```

## Atticus Harness Discipline

- This is a candidate output only. Do not certify findings as canonical.
- Every factual, legal, procedural, contradiction, or risk finding must cite
  an allowed context target (source_id, artifact_id, or authority reference).
- Flag stale evidence, weak support, missing certification, privacy risk, and
  contradiction.
- Do not perform external legal actions or make claims about filing, serving,
  or lodging documents.
- Separate fact, law, procedure, inference, drafting note, contradiction, risk,
  uncertainty, and redaction concerns.
