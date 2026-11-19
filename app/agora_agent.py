"""Agora Conversational AI agent for PropertyOps.

Adapted from the official Python quickstart (`_lab/agora-lab/server/src/agent.py`),
which is the same stack: FastAPI + the `agora-agents` SDK.

Two deliberate departures from the quickstart:
  1. TTS is Murf Falcon, not the quickstart's default MiniMax.
  2. The LLM carries our 14 hotel tools, pointed at this app's /tools/* endpoints.
"""
from __future__ import annotations

import json
import logging
import os
import random
from pathlib import Path
from typing import Any

from agora_agent import Area, AsyncAgora
from agora_agent.agentkit import Agent as AgoraAgent
from agora_agent.agentkit.token import generate_convo_ai_token
from agora_agent.agentkit.vendors import DeepgramSTT, MurfTTS, OpenAI

from .tools import at_risk_tasks

logger = logging.getLogger("uvicorn.error")

ROOT = Path(__file__).resolve().parent.parent
PROMPT_PATH = ROOT / "prompts" / "system_prompt.txt"
TOOLS_PATH = ROOT / "agent_tools.json"

GREETING = "Front desk copilot here. What is happening on the floor?"
FAILURE = "One moment, I am checking."

# The agent should talk like a night-shift colleague: calm, brief, operational.
DEFAULT_INSTRUCTIONS = (
    "You are the FrontDesk Copilot for a 40-room hotel, working the night shift. "
    "You speak with the duty manager, never with a guest. Be brief and operational. "
    "Always look up the guest before proposing anything, and never invent policy."
)


class AgentService:
    """Owns the Agora client and the live agent sessions."""

    def __init__(self) -> None:
        self.app_id = (os.getenv("AGORA_APP_ID") or "").strip()
        self.app_certificate = (os.getenv("AGORA_APP_CERTIFICATE") or "").strip()
        self.murf_key = (os.getenv("MURF_API_KEY") or "").strip()
        self.murf_voice = (os.getenv("MURF_VOICE_ID") or "Matthew").strip()
        self.murf_locale = (os.getenv("MURF_LOCALE") or "en-US").strip()
        self.llm_key = (os.getenv("LLM_API_KEY") or "").strip()
        self.llm_model = (os.getenv("LLM_MODEL") or "gpt-4o-mini").strip()
        self.tools_base = (
            os.getenv("TOOLS_BASE_URL")
            or os.getenv("RENDER_EXTERNAL_URL")
            or ""
        ).strip().rstrip("/")

        self.client: AsyncAgora | None = None
        if self.app_id and self.app_certificate:
            self.client = AsyncAgora(
                area=Area.US,
                app_id=self.app_id,
                app_certificate=self.app_certificate,
            )
        self._sessions: dict[str, Any] = {}
        self.current_agent_id: str | None = None

    # --- readiness ---------------------------------------------------

    def missing(self) -> list[str]:
        """Exactly what still blocks a working voice loop."""
        missing = []
        if not self.app_id:
            missing.append("AGORA_APP_ID")
        if not self.app_certificate:
            missing.append("AGORA_APP_CERTIFICATE")
        if not self.murf_key:
            missing.append("MURF_API_KEY")
        if not self.tools_base or "localhost" in self.tools_base or "127.0.0.1" in self.tools_base:
            missing.append("TOOLS_BASE_URL (a public https tunnel, not localhost)")
        return missing

    def ready(self) -> bool:
        return not self.missing()

    # --- tokens ------------------------------------------------------

    def rtc_config(self, channel: str, uid: int) -> dict:
        """Everything the browser needs to join the same channel as the agent."""
        if not self.app_id or not self.app_certificate:
            raise RuntimeError("Agora credentials are not configured")
        token = generate_convo_ai_token(
            app_id=self.app_id,
            app_certificate=self.app_certificate,
            channel_name=channel,
            uid=uid,
            token_expire=3600,
        )
        return {
            "app_id": self.app_id,
            "token": token,
            "uid": str(uid),
            "channel": channel,
            "agent_uid": str(random.randint(10000000, 99999999)),
        }

    # --- tools -------------------------------------------------------

    def tool_payloads(self) -> list[dict]:
        """Turn our JSON schemas into Agora custom-tool entries with server blocks."""
        if not self.tools_base:
            raise RuntimeError("TOOLS_BASE_URL is not configured")

        spec = json.loads(TOOLS_PATH.read_text(encoding="utf-8-sig"))
        payloads = []
        for entry in spec["tools"]:
            fn = entry["function"]
            props = (fn.get("parameters") or {}).get("properties") or {}
            # Every declared argument is forwarded as a complete placeholder.
            body = {name: "{{args.%s}}" % name for name in props}
            payloads.append({
                "type": "function",
                "function": fn,
                "execution": {"mode": "sync"},
                "server": {
                    "method": "POST",
                    "url": f"{self.tools_base}/tools/{fn['name']}",
                    "headers": {"Content-Type": "application/json"},
                    "body": body,
                    "timeout_ms": 8000,
                },
            })
        return payloads

    # --- lifecycle ---------------------------------------------------

    def _opening(self) -> tuple[str, str]:
        """Speak first, about what is already at risk. The agent should not wait to be asked."""
        try:
            alerts = at_risk_tasks(15)
        except Exception:  # never block a session because the alert query failed
            alerts = []
        if not alerts:
            return GREETING, ""

        first = alerts[0]
        minutes = first["minutes_left"]
        when = f"{minutes} minutes left" if minutes >= 0 else f"{abs(minutes)} minutes over"
        greeting = (
            f"Front desk copilot. Heads up: {first['department']} on {first['location']} "
            f"has {when} on its SLA. What else is happening?"
        )
        note = (
            "At the start of this shift these tasks were already at risk: "
            + "; ".join(f"{a['department']} on {a['location']}, {a['minutes_left']} minutes"
                        for a in alerts[:3])
            + ". Lead with the most urgent one before anything else."
        )
        return greeting, note

    def _build_agent(self) -> AgoraAgent:
        instructions = (
            PROMPT_PATH.read_text(encoding="utf-8-sig")
            if PROMPT_PATH.exists() else DEFAULT_INSTRUCTIONS
        )
        greeting, alert_note = self._opening()
        if alert_note:
            instructions = f"{instructions}\n\n{alert_note}"

        llm_kwargs: dict[str, Any] = {}
        if self.llm_key:
            llm_kwargs["api_key"] = self.llm_key

        llm = OpenAI(
            model=self.llm_model,
            greeting_message=greeting,
            failure_message=FAILURE,
            max_history=20,
            max_tokens=1024,
            temperature=0.4,
            tools=self.tool_payloads(),
            **llm_kwargs,
        )
        stt = DeepgramSTT(model="nova-3", language="en")
        tts = MurfTTS(
            key=self.murf_key,
            voice_id=self.murf_voice,
            base_url="wss://global.api.murf.ai/v1/speech/stream-input",
            locale=self.murf_locale,
            model="FALCON",
            sample_rate=24000,
        )

        agent = AgoraAgent(
            client=self.client,
            instructions=instructions,
            greeting=greeting,
            failure_message=FAILURE,
            max_history=50,
            turn_detection={
                "config": {
                    "speech_threshold": 0.5,
                    "start_of_speech": {
                        "mode": "vad",
                        "vad_config": {"interrupt_duration_ms": 160, "prefix_padding_ms": 300},
                    },
                    "end_of_speech": {
                        "mode": "vad",
                        "vad_config": {"silence_duration_ms": 480},
                    },
                },
            },
            advanced_features={"enable_tools": True},
            parameters={
                "audio_scenario": "chorus",
                "enable_error_message": True,
                "enable_metrics": True,
            },
        )
        return agent.with_stt(stt).with_llm(llm).with_tts(tts)

    async def start(self, channel: str, agent_uid: int, user_uid: int) -> dict:
        if self.client is None:
            raise RuntimeError("Agora credentials are not configured")
        blockers = self.missing()
        if blockers:
            raise RuntimeError("Cannot start the agent yet. Missing: " + ", ".join(blockers))

        session = self._build_agent().create_async_session(
            channel=channel,
            agent_uid=str(agent_uid),
            remote_uids=[str(user_uid)],
            enable_string_uid=False,
            idle_timeout=60,
            expires_in=3600,
        )
        agent_id = await session.start()
        self._sessions[agent_id] = session
        self.current_agent_id = agent_id
        logger.info("Agent started id=%s channel=%s", agent_id, channel)
        return {"agent_id": agent_id, "channel": channel, "status": "started"}

    async def stop(self, agent_id: str) -> None:
        session = self._sessions.pop(agent_id, None)
        if self.current_agent_id == agent_id:
            self.current_agent_id = None
        if session is not None:
            try:
                await session.stop()
                return
            except Exception:
                logger.warning("Session stop failed, falling back to client.stop_agent", exc_info=True)
        if self.client is None:
            raise RuntimeError("Agora credentials are not configured")
        await self.client.stop_agent(agent_id)

    # --- server-initiated interaction --------------------------------

    def _require_session(self) -> str:
        if self.client is None:
            raise RuntimeError("Agora credentials are not configured")
        if not self.current_agent_id:
            raise RuntimeError("No live agent session — connect the microphone first.")
        return self.current_agent_id

    def _auth_options(self) -> dict | None:
        """In app-credentials mode the SDK sends an empty Authorization header.

        `stop_agent` mints a ConvoAI token itself, but speak/think do not, so we
        build one here. With customer_id/customer_secret or auth_token configured,
        the client already authenticates every request and we pass nothing.
        """
        if self.client is None or getattr(self.client, "auth_mode", "") != "app-credentials":
            return None
        token = generate_convo_ai_token(
            app_id=self.app_id,
            app_certificate=self.app_certificate,
            channel_name="agent-control",
            uid=0,
        )
        return {"additional_headers": {"Authorization": f"agora token={token}"}}

    async def speak(self, text: str, priority: str = "APPEND") -> dict:
        """Make the running agent say something it was not asked to say."""
        agent_id = self._require_session()
        await self.client.agents.speak(
            self.app_id, agent_id, text=text, priority=priority, interruptable=True,
            request_options=self._auth_options())
        logger.info("agent speak id=%s priority=%s", agent_id, priority)
        return {"agent_id": agent_id, "text": text, "priority": priority}

    async def think(self, text: str) -> dict:
        """Inject typed text as if the manager had spoken it."""
        agent_id = self._require_session()
        await self.client.agent_management.agent_think(
            self.app_id, agent_id, text=text,
            on_listening_action="interrupt",
            on_thinking_action="append",
            on_speaking_action="append",
            interruptable=True,
            request_options=self._auth_options(),
        )
        logger.info("agent think id=%s chars=%d", agent_id, len(text))
        return {"agent_id": agent_id, "text": text}


service = AgentService()
