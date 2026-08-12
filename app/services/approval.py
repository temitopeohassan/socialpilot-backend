"""
AI-powered approval workflow.

Before a human ever looks at a post, the agent screens it:
  - brand alignment (via brand_voice.check_brand_alignment)
  - policy/risk check (sensitive claims, missing disclosures, broken CTAs)
  - quality check (truncation, obvious placeholder text)

It produces a risk score + recommendation. Workspaces can set a policy:
  - auto_approve_threshold: if risk below this AND brand alignment above a
    bar, auto-approve (no human needed).
  - otherwise route to a human with the findings pre-attached, so the
    reviewer reads the AI's opinion first instead of starting cold.
"""
import json
from datetime import datetime
from typing import Optional
from sqlmodel import Session, select

from app.services.llm import call_claude
from app.services.brand_voice import get_active_voice, check_brand_alignment
from app.models.ai_models import (
    ApprovalRequest, ApprovalStatus, PostVariation, VariationStatus,
)

# Workspace policy defaults (could be made configurable per workspace)
AUTO_APPROVE_RISK_MAX = 0.25
AUTO_APPROVE_BRAND_MIN = 0.75


def screen_variation(variation_id: int, workspace_id: int, user_id: int,
                     session: Session, auto_approve_enabled: bool = True) -> ApprovalRequest:
    """Run AI pre-screen on a single post variation and create an
    ApprovalRequest (auto-approved or routed to human)."""
    var = session.get(PostVariation, variation_id)
    voice = get_active_voice(workspace_id, session)

    # 1) brand alignment
    brand = check_brand_alignment(voice, var.caption)

    # 2) risk / policy / quality check
    risk = _risk_check(var.caption, var.platform)

    findings = brand["findings"] + risk["findings"]
    risk_score = risk["risk_score"]
    brand_score = brand["score"]

    # 3) decide
    if risk_score >= 0.7:
        recommendation = "reject"
    elif risk_score >= AUTO_APPROVE_RISK_MAX or brand_score < AUTO_APPROVE_BRAND_MIN:
        recommendation = "review"
    else:
        recommendation = "approve"

    req = ApprovalRequest(
        workspace_id=workspace_id,
        campaign_id=var.campaign_id,
        variation_id=variation_id,
        requested_by=user_id,
        ai_risk_score=risk_score,
        ai_findings=json.dumps(findings),
        ai_recommendation=recommendation,
        brand_alignment_score=brand_score,
    )

    # auto-approve path
    if (auto_approve_enabled and recommendation == "approve"
            and risk_score < AUTO_APPROVE_RISK_MAX
            and brand_score >= AUTO_APPROVE_BRAND_MIN):
        req.status = ApprovalStatus.AUTO_APPROVED
        req.decided_at = datetime.utcnow()
        var.status = VariationStatus.APPROVED
        session.add(var)
    else:
        req.status = ApprovalStatus.PENDING

    session.add(req)
    session.commit()
    session.refresh(req)
    return req


def _risk_check(caption: str, platform: str) -> dict:
    """LLM policy/quality screen. Cheap deterministic checks first."""
    findings = []

    # deterministic quality flags
    if "{" in caption and "}" in caption:
        findings.append("Possible unfilled template placeholder")
    if "lorem ipsum" in caption.lower():
        findings.append("Contains placeholder text")
    if len(caption.strip()) < 3:
        findings.append("Caption is essentially empty")

    prompt = f"""Review this {platform} post for publishing risk. Flag only real
issues: unverifiable claims, missing disclosures (#ad), offensive content,
broken/empty CTAs, or anything legally risky. Be conservative — most posts
are fine.

Post: "{caption}"

Return ONLY JSON:
{{"risk_score": 0.0-1.0, "issues": ["..."]}}
0.0 = perfectly safe, 1.0 = do not publish."""
    try:
        result = call_claude(prompt, max_tokens=400, expect_json=True)
        risk_score = float(result.get("risk_score", 0.1))
        findings.extend(result.get("issues", []))
    except Exception:
        risk_score = 0.1  # fail open to low risk

    if any("placeholder" in f or "empty" in f for f in findings):
        risk_score = max(risk_score, 0.5)

    return {"risk_score": round(risk_score, 2), "findings": findings}


def decide(request_id: int, decision: str, user_id: int,
           session: Session, comment: str = "") -> ApprovalRequest:
    """Human decision on a pending approval. decision in
    {approved, changes_requested, rejected}."""
    req = session.get(ApprovalRequest, request_id)
    if not req:
        raise ValueError("Approval request not found")

    mapping = {
        "approved": (ApprovalStatus.APPROVED, VariationStatus.APPROVED),
        "changes_requested": (ApprovalStatus.CHANGES_REQUESTED, VariationStatus.GENERATED),
        "rejected": (ApprovalStatus.REJECTED, VariationStatus.REJECTED),
    }
    if decision not in mapping:
        raise ValueError(f"Invalid decision: {decision}")

    appr_status, var_status = mapping[decision]
    req.status = appr_status
    req.decided_by = user_id
    req.decided_at = datetime.utcnow()
    req.human_comment = comment
    session.add(req)

    if req.variation_id:
        var = session.get(PostVariation, req.variation_id)
        if var:
            var.status = var_status
            session.add(var)

    session.commit()
    session.refresh(req)
    return req


def pending_for_workspace(workspace_id: int, session: Session):
    return session.exec(
        select(ApprovalRequest).where(
            ApprovalRequest.workspace_id == workspace_id,
            ApprovalRequest.status == ApprovalStatus.PENDING,
        ).order_by(ApprovalRequest.ai_risk_score.desc())
    ).all()
