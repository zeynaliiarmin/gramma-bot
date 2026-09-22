"""Gramma — automation scenarios (multi-step auto-reply workflows).

A scenario models a ManyChat/vardast-style conversation flow:

  * `AutomationScenario` — a named flow owned by one Instagram account,
    with a channel (dm/comment/story_reply), a match mode
    (`keyword` | `ai` | `always`), keyword/trigger list, an optional AI
    instruction (used when `ai` mode or as the smart fallback) and
    enabled + priority flags.
  * `AutomationStep`     — an ordered step inside a scenario. Each step can
    send a message, then either END the flow, WAIT for the next inbound
    message, or GOTO another step. WAIT-steps turn the flow into a real
    multi-step conversation (e.g. "قیمت" → price list → "کدوم؟" → order).

The scenario data model lives here, next to `AutoReply` (keyword → reply
rules), so the whole automation layer is importable from one module.
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
)
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base, BIGINT_PK


# ── Enums-as-strings (portable across SQLite & Postgres) ─────
class ScenarioChannel:
    DM = "dm"
    COMMENT = "comment"
    STORY_REPLY = "story_reply"


class MatchMode:
    KEYWORD = "keyword"   # trigger substrings (comma-separated trigger text)
    AI = "ai"             # any non-keyword inbound text → AI decides
    ALWAYS = "always"     # fire on every inbound message


class StepAction:
    SEND = "send"         # reply with `text` (possibly AI-injected)
    END = "end"           # stop the flow
    GOTO = "goto"         # jump to `next_step_order`
    WAIT = "wait"         # expect the next inbound message
    COLLECT = "collect"   # capture the reply into the session (for orders)


class AutomationScenario(Base):
    """A conversation flow attached to one Instagram account."""

    __tablename__ = "automation_scenarios"

    id: Mapped[int] = mapped_column(BIGINT_PK, primary_key=True, autoincrement=True)
    account_id: Mapped[int] = mapped_column(BigInteger, index=True)
    name: Mapped[str] = mapped_column(String(120), default="")
    # dm | comment | story_reply
    channel: Mapped[str] = mapped_column(String(16), default=ScenarioChannel.DM)
    # keyword | ai | always
    match_mode: Mapped[str] = mapped_column(String(12), default=MatchMode.KEYWORD)
    trigger_text: Mapped[str] = mapped_column(Text, default="")   # comma separated (keyword mode)
    ai_instruction: Mapped[str] = mapped_column(Text, default="")  # system prompt (ai mode / fallback)
    fallback_reply: Mapped[str] = mapped_column(Text, default="")  # used when no AI / no rule
    use_ai: Mapped[bool] = mapped_column(Boolean, default=True)     # allow AI to draft SEND text
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    priority: Mapped[int] = mapped_column(Integer, default=0)       # higher runs first
    hits: Mapped[int] = mapped_column(BigInteger, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))

    @property
    def triggers(self) -> list[str]:
        return [t.strip() for t in (self.trigger_text or "").split(",") if t.strip()]


class AutomationStep(Base):
    """An ordered step in a scenario."""

    __tablename__ = "automation_steps"

    id: Mapped[int] = mapped_column(BIGINT_PK, primary_key=True, autoincrement=True)
    scenario_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("automation_scenarios.id", ondelete="CASCADE"), index=True
    )
    order_index: Mapped[int] = mapped_column(Integer, default=0)
    action: Mapped[str] = mapped_column(String(12), default=StepAction.SEND)  # send|wait|collect|end|goto
    text: Mapped[str] = mapped_column(Text, default="")          # message body for SEND/WAIT prompt
    use_ai: Mapped[bool] = mapped_column(Boolean, default=False)  # draft `text` with AI for this step
    next_step_order: Mapped[int | None] = mapped_column(Integer, nullable=True)  # for goto
    variable: Mapped[str] = mapped_column(String(64), default="")                 # for collect
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))


# ── Session: in-flight scenario conversation ─────────────────
class AutomationSession(Base):
    """Tracks an end-user's position inside a running scenario (stateful flow).

    Keyed by (account_id, channel, peer_id) so a follower can be mid-flow in
    exactly one DM / story thread at a time for a given account.
    """

    __tablename__ = "automation_sessions"

    id: Mapped[int] = mapped_column(BIGINT_PK, primary_key=True, autoincrement=True)
    account_id: Mapped[int] = mapped_column(BigInteger, index=True)
    channel: Mapped[str] = mapped_column(String(16), default=ScenarioChannel.DM)
    peer_id: Mapped[str] = mapped_column(String(128), default="")   # thread_id / sender id
    scenario_id: Mapped[int] = mapped_column(BigInteger, index=True)
    step_order: Mapped[int] = mapped_column(Integer, default=0)      # current step
    collected: Mapped[str] = mapped_column(Text, default="{}")       # JSON of collected vars
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )


# ────────────────────────────────────────────────────────────────
#  Engine — scenario matching + stateful step execution
# ────────────────────────────────────────────────────────────────
from dataclasses import dataclass, field  # noqa: E402
import json as _json  # noqa: E402
from sqlalchemy import select  # noqa: E402

from app.core.config import get_settings  # noqa: E402

settings = get_settings()


@dataclass
class Outcome:
    """What the engine decided to do for one inbound message."""

    send_text: str | None       # message to send back (None = no reply)
    final: bool                 # flow ended
    waiting: bool               # flow is now waiting for the next message
    scenario_id: int | None     # active scenario (None when nothing matched)
    next_index: int | None      # step index to resume from on next inbound


async def get_scenarios(
    session: AsyncSession, account_id: int, *, channel: str | None = None, only_enabled: bool = True
) -> list[AutomationScenario]:
    q = select(AutomationScenario).where(AutomationScenario.account_id == account_id)
    if channel:
        q = q.where(AutomationScenario.channel == channel)
    if only_enabled:
        q = q.where(AutomationScenario.enabled.is_(True))
    q = q.order_by(AutomationScenario.priority.desc(), AutomationScenario.id.asc())
    return list((await session.execute(q)).scalars())


async def get_steps(session: AsyncSession, scenario_id: int) -> list[AutomationStep]:
    q = (
        select(AutomationStep)
        .where(AutomationStep.scenario_id == scenario_id)
        .order_by(AutomationStep.order_index.asc())
    )
    return list((await session.execute(q)).scalars())


def _keyword_hits(scenario: AutomationScenario, text: str) -> bool:
    low = (text or "").lower()
    return any(t.lower() in low for t in scenario.triggers)


async def find_matching_scenario(
    session: AsyncSession, account_id: int, channel: str, text: str
) -> AutomationScenario | None:
    """Pick the scenario that should handle this inbound message.

    Priority: (1) active session resumes first (handled by caller), here we
    only match *new* inbound text. Keyword matches always win over AI/ALWAYS;
    among keyword matches, higher `priority` wins.
    """
    scenarios = await get_scenarios(session, account_id, channel=channel)
    keyword = [s for s in scenarios if s.match_mode == MatchMode.KEYWORD and _keyword_hits(s, text)]
    if keyword:
        return keyword[0]
    others = [s for s in scenarios if s.match_mode in (MatchMode.ALWAYS, MatchMode.AI)]
    # Only let an AI scenario claim the message when it has an instruction.
    for s in others:
        if s.match_mode == MatchMode.ALWAYS:
            return s
    for s in others:
        if s.match_mode == MatchMode.AI and s.ai_instruction.strip():
            return s
    return None


# ── AI drafting (AvalAI → OpenClaw → fallback text) ───────────
async def draft_automation_reply(text: str, instruction: str, fallback: str) -> str:
    """Draft a reply with AI; degrade gracefully to `fallback`."""
    if not instruction.strip():
        return fallback or text
    prompt = (
        f"{instruction.strip()}\n\n"
        f"پیام کاربر: «{text[:500]}»\n"
        "فقط متن پاسخ را بنویس (بدون توضیح)."
    )
    try:
        from openai import AsyncOpenAI  # AvalAI (OpenAI-compatible)

        if settings.ai_api_key:
            client = AsyncOpenAI(api_key=settings.ai_api_key, base_url=settings.ai_base_url or None, timeout=20.0)
            resp = await client.chat.completions.create(
                model=settings.ai_model,
                messages=[
                    {"role": "system", "content": "You are a Persian Instagram DM assistant."},
                    {"role": "user", "content": prompt},
                ],
                temperature=0.7,
            )
            out = (resp.choices[0].message.content or "").strip()
            if out:
                return out
    except Exception:  # noqa: BLE001
        pass
    return fallback or text


def _apply_template(template: str, inbound_text: str, variables: dict) -> str:
    out = template.replace("{message}", inbound_text or "")
    for k, v in variables.items():
        out = out.replace("{" + str(k) + "}", str(v))
    return out


async def _step_reply(
    st: AutomationStep, inbound_text: str, variables: dict, scenario: AutomationScenario
) -> str:
    """Render the text a SEND/WAIT step should actually send."""
    body = _apply_template(st.text or "", inbound_text, variables)
    if st.use_ai and scenario.use_ai:
        return await draft_automation_reply(inbound_text, scenario.ai_instruction, body or scenario.fallback_reply)
    return body or (scenario.fallback_reply if scenario.fallback_reply else inbound_text)


def _index_of_order(steps: list[AutomationStep], order: int | None) -> int:
    for i, st in enumerate(steps):
        if st.order_index == order:
            return i
    return len(steps)  # out-of-range → treat as end


async def _run_from(
    session: AsyncSession,
    scenario: AutomationScenario,
    steps: list[AutomationStep],
    start_index: int,
    inbound_text: str,
    variables: dict,
) -> Outcome:
    """Advance a scenario from `start_index` for one inbound message.

    Semantics:
      * SEND    → reply with its text; the flow CONTINUES within this same
                  turn (so a chain of SEND steps collapses into one message).
      * WAIT    → optionally reply, then pause: the next inbound message
                  resumes at start_index+1.
      * COLLECT → store the user's message into `variable` and continue.
      * GOTO    → jump to `next_step_order` and continue.
      * END     → stop, nothing more to send.
    """
    idx = start_index
    last_send: list[str] = []
    while 0 <= idx < len(steps):
        st = steps[idx]
        if st.action == StepAction.COLLECT:
            if st.variable:
                variables[st.variable] = inbound_text or ""
            idx += 1
            continue
        if st.action == StepAction.GOTO:
            target = _index_of_order(steps, st.next_step_order)
            if target == idx:  # guard against self-loop
                idx += 1
            else:
                idx = target
            continue
        if st.action == StepAction.END:
            return Outcome(
                send_text="\n\n".join(last_send) or None,
                final=True, waiting=False,
                scenario_id=scenario.id, next_index=None,
            )
        if st.action == StepAction.SEND:
            text = await _step_reply(st, inbound_text, variables, scenario)
            if text:
                last_send.append(text)
            idx += 1
            continue
        if st.action == StepAction.WAIT:
            # Optional prompt before pausing, then wait for next message.
            prompt = None
            if (st.text or "").strip():
                prompt = await _step_reply(st, inbound_text, variables, scenario)
            body = "\n\n".join(last_send + ([prompt] if prompt else []))
            return Outcome(
                send_text=body or None,
                final=False, waiting=True,
                scenario_id=scenario.id, next_index=idx + 1,
            )
        idx += 1
    return Outcome(
        send_text="\n\n".join(last_send) or None,
        final=True, waiting=False,
        scenario_id=scenario.id, next_index=None,
    )


async def _get_active_session(
    session: AsyncSession, account_id: int, channel: str, peer_id: str
) -> AutomationSession | None:
    q = select(AutomationSession).where(
        AutomationSession.account_id == account_id,
        AutomationSession.channel == channel,
        AutomationSession.peer_id == peer_id,
    )
    return (await session.execute(q)).scalars().first()


async def process_inbound(
    session: AsyncSession, account_id: int, channel: str, peer_id: str, inbound_text: str
) -> Outcome | None:
    """Entry point: decide + advance a scenario for one inbound message.

    Returns None when no scenario is active AND no scenario matches (the
    caller falls back to keyword AutoReply / AI / nothing).
    """
    outcome: Outcome | None = None
    active = await _get_active_session(session, account_id, channel, peer_id)

    if active is not None:
        scenario = await session.get(AutomationScenario, active.scenario_id)
        if scenario is None or not scenario.enabled:
            await session.delete(active)
            active = None
        else:
            steps = await get_steps(session, scenario.id)
            variables: dict = {}
            try:
                variables = _json.loads(active.collected or "{}")
            except ValueError:
                variables = {}
            outcome = await _run_from(session, scenario, steps, active.step_order, inbound_text, variables)
            # persist/advance
            if outcome.final:
                await session.delete(active)
            else:
                active.step_order = outcome.next_index or 0
                active.collected = _json.dumps(variables, ensure_ascii=False)
            scenario.hits = (scenario.hits or 0) + 1
            await session.flush()
            return outcome

    # No active session → try to match a new scenario.
    scenario = await find_matching_scenario(session, account_id, channel, inbound_text)
    if scenario is None:
        return None

    steps = await get_steps(session, scenario.id)
    outcome = await _run_from(session, scenario, steps, 0, inbound_text, {})

    scenario.hits = (scenario.hits or 0) + 1
    if outcome.final:
        # nothing to persist
        pass
    else:
        session.add(
            AutomationSession(
                account_id=account_id,
                channel=channel,
                peer_id=peer_id,
                scenario_id=scenario.id,
                step_order=outcome.next_index or 0,
                collected=_json.dumps({}, ensure_ascii=False),
            )
        )
    await session.flush()
    return outcome
