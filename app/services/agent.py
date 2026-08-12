"""
Natural-language interface — the agent's "front door".

A user types something like:
  "Schedule three LinkedIn posts for next week about our new product launch"

interpret() classifies intent and extracts structured parameters. dispatch()
then routes to the right service (campaign planner, scheduler, editor, or a
plain answer) and records an AgentRun for transparency + undo.

Supported intents:
  plan_campaign   -> build a multi-post campaign from the prompt
  schedule_posts  -> like plan_campaign but lighter (a few standalone posts)
  ask             -> a question about the account/strategy (answered directly)
  edit            -> modify an existing campaign/post (returns guidance)
"""
import json
from datetime import datetime, timedelta
from typing import Optional
from sqlmodel import Session

from app.services.llm import call_claude
from app.services import campaign_planner
from app.models.ai_models import AgentRun, Campaign


def interpret(prompt: str, now: Optional[datetime] = None) -> dict:
    """Classify intent + extract parameters from a natural-language prompt.
    Resolves relative dates ('next week', 'tomorrow') against `now`."""
    now = now or datetime.utcnow()

    system = (
        "You convert social-media requests into structured commands. "
        "Resolve all relative dates to absolute ISO dates. "
        f"The current datetime is {now.isoformat()} (UTC). "
        "Monday is the start of the week."
    )
    parse_prompt = f"""User request: "{prompt}"

Classify and extract. Return ONLY JSON:
{{
  "intent": "plan_campaign | schedule_posts | ask | edit",
  "confidence": 0.0-1.0,
  "params": {{
    "goal": "what the content is about",
    "platforms": ["linkedin","instagram",...],
    "post_count": <int>,
    "starts_at": "ISO datetime or null",
    "ends_at": "ISO datetime or null",
    "themes": ["..."]
  }},
  "needs_clarification": false,
  "clarifying_question": "ask only if truly ambiguous, else empty"
}}

Rules:
- "a few" = 3, "a couple" = 2 unless stated.
- If no platform stated, leave platforms empty (caller will default).
- "next week" = the upcoming Mon-Sun. "this week" = current Mon-Sun.
- plan_campaign when it's a themed set with an arc; schedule_posts for a
  simple "post X times about Y" with no campaign framing."""

    result = call_claude(parse_prompt, system=system, max_tokens=800, expect_json=True)
    return result


def dispatch(prompt: str, workspace_id: int, user_id: int, session: Session,
             auto_execute: bool = True) -> dict:
    """Interpret + act. Returns a response describing what happened.
    Always records an AgentRun."""
    interp = interpret(prompt)
    intent = interp.get("intent", "ask")
    params = interp.get("params", {})

    run = AgentRun(
        workspace_id=workspace_id, user_id=user_id, prompt=prompt,
        intent=intent, plan=json.dumps(interp),
    )

    # Ask a clarifying question instead of guessing
    if interp.get("needs_clarification") and interp.get("clarifying_question"):
        run.result_summary = "Asked for clarification."
        run.actions_taken = json.dumps([])
        session.add(run)
        session.commit()
        return {
            "type": "clarification",
            "question": interp["clarifying_question"],
            "understood_so_far": params,
            "agent_run_id": run.id,
        }

    if intent in ("plan_campaign", "schedule_posts"):
        # default platform if none given
        if not params.get("platforms"):
            params["platforms"] = ["linkedin"]
        if not params.get("post_count"):
            params["post_count"] = 3

        campaign = campaign_planner.plan_campaign(
            workspace_id, user_id, prompt, params, session
        )
        variations = campaign_planner.generate_variations(campaign.id, session)
        campaign_planner.schedule_variations(campaign.id, session)

        run.campaign_id = campaign.id
        run.result_summary = (
            f"Planned '{campaign.name}': {len(variations)} variations across "
            f"{', '.join(params['platforms'])}."
        )
        run.actions_taken = json.dumps([
            "created_campaign", "generated_variations", "scheduled_variations"
        ])
        session.add(run)
        session.commit()
        session.refresh(run)

        return {
            "type": "campaign_created",
            "campaign_id": campaign.id,
            "name": campaign.name,
            "strategy": campaign.strategy_summary,
            "variation_count": len(variations),
            "platforms": params["platforms"],
            "message": run.result_summary,
            "agent_run_id": run.id,
            "next_step": "Review the draft variations, then send for approval.",
        }

    if intent == "edit":
        run.result_summary = "Edit intent — needs target campaign/post id."
        session.add(run)
        session.commit()
        return {
            "type": "needs_target",
            "message": "Which campaign or post would you like to edit?",
            "agent_run_id": run.id,
        }

    # intent == ask
    answer = _answer_question(prompt, workspace_id, session)
    run.result_summary = "Answered a question."
    run.actions_taken = json.dumps(["answered"])
    session.add(run)
    session.commit()
    return {"type": "answer", "message": answer, "agent_run_id": run.id}


def _answer_question(prompt: str, workspace_id: int, session: Session) -> str:
    """Answer a strategy/account question conversationally."""
    return call_claude(
        f"Answer this social-media question concisely and practically:\n{prompt}",
        max_tokens=600,
    )


def undo(agent_run_id: int, session: Session) -> dict:
    """Undo what a run did (currently: archive a created campaign)."""
    run = session.get(AgentRun, agent_run_id)
    if not run or not run.campaign_id:
        return {"undone": False, "reason": "Nothing reversible for this run."}
    from app.models.ai_models import CampaignStatus
    campaign = session.get(Campaign, run.campaign_id)
    if campaign:
        campaign.status = CampaignStatus.ARCHIVED
        session.add(campaign)
        session.commit()
    return {"undone": True, "campaign_id": run.campaign_id}
