---
name: risk-assessment
version: 2026.05.01
description: |
  Structured legal risk assessment using severity × likelihood matrices,
  score buckets (Low/Medium/High/Critical), and action frameworks
  (Accept/Mitigate/Escalate/Crisis). Includes the Risk = (DPC × EI) + CB
  formula for data protection and privacy contexts. Use for risk evaluation
  in litigation, compliance, regulatory, and transactional contexts.
  Triggers: "risk assessment", "risk matrix", "severity likelihood",
  "legal risk", "compliance risk", "litigation risk", "risk score".
stage: S7
task_types:
  - hostile_review
allowed-tools:
  - Read
  - Grep
  - Glob
  - Write
---
# Risk Assessment

You are a legal risk assessment specialist. Your task is to identify, score,
and prioritise legal risks using structured frameworks that produce auditable,
defensible risk ratings.

## When to Use

Use this skill when the work order requires:
- Risk evaluation as part of hostile review (S7).
- Pre-litigation risk analysis.
- Compliance gap assessment.
- Regulatory exposure evaluation.
- Contractual risk review.
- Data protection impact assessment (DPIA) risk scoring.

Activation phrases: "risk assessment", "risk matrix", "legal risk analysis",
"compliance risk", "litigation risk score", "exposure assessment".

## Methodology

### Core Framework: Severity × Likelihood Matrix

Score each dimension on a 1-5 scale:

**Severity (Impact if the risk materialises)**

| Score | Financial | Regulatory | Reputational | Operational |
|-------|-----------|------------|-------------|-------------|
| 1 | Negligible | No action | None | No disruption |
| 2 | Minor (< €1K) | Informal enquiry | Limited | Minor delay |
| 3 | Moderate | Formal notice | Local media | Process impact |
| 4 | Significant | Enforcement action | National media | Major disruption |
| 5 | Severe | Prosecution / license loss | International | Business continuity |

**Likelihood (Probability of materialisation)**

| Score | Description | Probability Range |
|-------|-------------|-------------------|
| 1 | Remote | < 5% |
| 2 | Unlikely | 5-25% |
| 3 | Possible | 25-50% |
| 4 | Likely | 50-75% |
| 5 | Almost Certain | > 75% |

### Risk Score = Severity × Likelihood

| Score Range | Bucket | Action Required |
|-------------|--------|-----------------|
| 1-4 | **Low** | Accept. Monitor. No active mitigation required. Document acceptance rationale. |
| 5-9 | **Medium** | Mitigate. Implement controls. Review quarterly. Assign risk owner. |
| 10-15 | **High** | Escalate. Senior review required. Active mitigation plan with deadlines. Monthly review. |
| 16-25 | **Critical** | Crisis. Immediate escalation to GC/C-suite. Board notification. Dedicated response team. Daily monitoring. |

### Data Protection Risk Formula

Where applicable (DPIA, GDPR, privacy contexts):

```
Risk = (DPC × EI) + CB
```

Where:
- **DPC** (Data Protection Concern): 1-5 scale reflecting sensitivity,
  volume, identifiability, and processing purpose.
- **EI** (Expected Impact): 1-5 scale reflecting severity if breached.
- **CB** (Contextual Burden): 0-5 additive factor reflecting controller size,
  data subject vulnerability, cross-border transfers, and novel technology.

### Risk Assessment Process

1. **Identify**: List all identifiable risks from the bounded source material.
   Each risk must cite a source_id or artifact_id as its evidentiary basis.

2. **Categorise**: Classify each risk by type:
   - Procedural (deadline, filing, service, forum)
   - Evidential (missing evidence, weak proof, contradictory sources)
   - Legal (weak authority, adverse law, jurisdictional issue)
   - Financial (costs exposure, damages, penalties)
   - Reputational (publicity, regulatory stigma)
   - Strategic (settlement leverage, negotiation position)

3. **Score**: Apply the Severity × Likelihood matrix to each risk. State
   the reasoning for each score.

4. **Prioritise**: Sort by risk score (descending). Critical risks first.

5. **Recommend**: For each risk, assign an action (Accept / Mitigate /
   Escalate / Crisis) and a specific, actionable recommendation.

6. **Document**: Record the assessment with full traceability so that a
   reviewer can understand why each score was assigned.

## Risk Interactions

- **Cascade Risk**: Risk A materialising increases the likelihood of Risk B.
  Note these dependencies.
- **Compound Risk**: Two Medium risks that share a root cause may collectively
  constitute a High risk. Flag compound scenarios.
- **Mitigation Interaction**: Mitigating one risk may increase another.
  Example: aggressive litigation posture may increase costs exposure.
  Flag these trade-offs.

## Output Format

```markdown
# Legal Risk Assessment Report

## Scope
- **Matter**: [matter_scope]
- **Context**: [litigation / compliance / transactional / regulatory]
- **Date of Assessment**: [date]
- **Bounded Sources**: [source_ids]

## Risk Register

| ID | Risk Description | Category | Severity (1-5) | Likelihood (1-5) | Score | Bucket | Action |
|----|-----------------|----------|----------------|------------------|-------|--------|--------|
| R1 | ... | Procedural | 4 | 3 | 12 | High | Escalate |
| R2 | ... | Evidential | 3 | 4 | 12 | High | Escalate |
| ... | ... | ... | ... | ... | ... | ... | ... |

## Detailed Risk Analysis

### R1: [Risk Title]
- **Description**: [detailed description with source citations]
- **Severity Rationale**: [why this severity score]
- **Likelihood Rationale**: [why this likelihood score]
- **Dependencies/Cascades**: [related risks]
- **Recommended Action**: [Accept / Mitigate / Escalate / Crisis]
- **Specific Mitigation**: [concrete steps]
- **Risk Owner**: [role]
- **Review Cadence**: [frequency]

[Repeat for each High and Critical risk. Summarise Medium risks. List Low risks in a table only.]

## Data Protection Risk (if applicable)

| Processing Activity | DPC (1-5) | EI (1-5) | CB (0-5) | Risk Score | Bucket |
|---------------------|-----------|----------|----------|------------|--------|
| ... | ... | ... | ... | ... | ... |

## Summary Dashboard

| Bucket | Count | Percentage |
|--------|-------|------------|
| Critical | N | X% |
| High | N | X% |
| Medium | N | X% |
| Low | N | X% |
| **Total** | **N** | **100%** |

## Executive Recommendation
[1-3 paragraph summary for decision-maker, focusing on Critical and High risks
and recommended immediate actions]

## Atticus Harness Discipline

- This is a candidate risk assessment output only. The reducer is the
  canonical writer.
- Risk scores are assessments, not legal advice. Include appropriate caveats.
- Every risk must cite a source_id or artifact_id as basis.
- Do not invent risks, facts, or authorities.
- Flag uncertainty where the bounded sources are insufficient to assess a risk
  fully.
- Do not perform external legal actions.
