# ruff: noqa
import datetime
import json
import logging
import os
import re
from typing import Generator, Any

from pydantic import BaseModel, Field

from google.adk.agents import LlmAgent
from google.adk.agents.context import Context
from google.adk.apps import App, ResumabilityConfig
from google.adk.events.event import Event
from google.adk.events.request_input import RequestInput
from google.adk.models import Gemini
from google.adk.tools import AgentTool
from google.adk.tools.mcp_tool import McpToolset
from google.adk.tools.mcp_tool.mcp_session_manager import StdioConnectionParams
from google.adk.workflow import Workflow, START, FunctionNode
from google.genai import types
from mcp import StdioServerParameters

from .config import config

# Ensure we log details
logger = logging.getLogger("social_media_monitor")
logging.basicConfig(level=logging.INFO)

# Define schemas for structured outputs
class SentimentAnalysis(BaseModel):
    sentiment: str = Field(description="The sentiment of the post: 'positive', 'neutral', or 'negative'.")
    summary: str = Field(description="Brief summary of the post's core issue or praise.")

class ResponseDraft(BaseModel):
    draft_response: str = Field(description="A professional, polite, and context-appropriate reply to the social media post.")

# Configure local MCP toolset connection params
mcp_toolset = McpToolset(
    connection_params=StdioConnectionParams(
        server_params=StdioServerParameters(
            command="uv",
            args=["run", "python", "app/mcp_server.py"],
        ),
    ),
)

# Define specialized LlmAgent sub-agents with automatic retry options
sentiment_analyzer = LlmAgent(
    name="sentiment_analyzer",
    model=Gemini(
        model=config.model,
        retry_options=types.HttpRetryOptions(attempts=5),
    ),
    instruction="""You are a Social Media Sentiment Analyzer.
Analyze the sentiment of the social media post.
Determine if the sentiment is 'positive', 'neutral', or 'negative'.
Provide a concise summary of the post's core issue or compliment.
""",
    output_schema=SentimentAnalysis,
    output_key="sentiment_analysis",
    description="Analyzes the sentiment (positive/neutral/negative) and summarizes a social media post.",
)

response_drafter = LlmAgent(
    name="response_drafter",
    model=Gemini(
        model=config.model,
        retry_options=types.HttpRetryOptions(attempts=5),
    ),
    instruction="""You are a Social Media Response Drafter.
Draft a professional and context-appropriate reply to the social media post based on its sentiment and summary.
If sentiment is positive, express gratitude. If neutral, be helpful and informative. If negative, be empathetic, apologetic, and offer assistance.
If there is user feedback on the previous response draft, incorporate it to refine the draft response.

You have access to the following tools from the company's database:
- fetch_brand_guidelines: Use this to read brand voice rules and make sure your response conforms to them.
- search_response_templates: Use this to find approved response templates that you can reuse or adapt.
""",
    output_schema=ResponseDraft,
    output_key="response_draft",
    description="Drafts a social media reply based on the post's sentiment and summary.",
    tools=[mcp_toolset],
)

# Define the Orchestrator LlmAgent with automatic retry options
orchestrator = LlmAgent(
    name="orchestrator",
    model=Gemini(
        model=config.model,
        retry_options=types.HttpRetryOptions(attempts=5),
    ),
    instruction="""You are the Social Media Monitor Orchestrator.
Your goal is to coordinate the analysis of a social media post and draft a reply.

You have access to two sub-agents:
1. sentiment_analyzer: Call this tool first to analyze the sentiment (positive/neutral/negative) and get a summary of the post.
2. response_drafter: Call this tool second, passing the analyzed sentiment and summary, to draft a response.

In addition, you have access to a tool:
- log_incident: Call this tool if the sentiment is negative, to log the incident in the internal database for tracking.

If the user has provided feedback (found in user_feedback: '{user_feedback}'), you must rewrite/adjust the draft response using response_drafter, directing it to address the feedback.

When both tools have run, summarize the final analysis and the drafted response.
""",
    tools=[AgentTool(sentiment_analyzer), AgentTool(response_drafter), mcp_toolset],
)

# Function nodes for the workflow
def security_checkpoint(ctx: Context, node_input: Any) -> Event:
    # Initialize state keys
    if "user_feedback" not in ctx.state:
        ctx.state["user_feedback"] = ""
    if "raw_input" not in ctx.state:
        ctx.state["raw_input"] = ""

    # Extract text content from user input in a highly robust manner
    text = ""
    if isinstance(node_input, str):
        text = node_input
    elif hasattr(node_input, "parts") and node_input.parts:
        text = "".join(part.text for part in node_input.parts if part.text)
    elif isinstance(node_input, dict):
        parts = node_input.get("parts", [])
        for p in parts:
            if isinstance(p, dict) and "text" in p:
                text += p["text"]
            elif hasattr(p, "text"):
                text += p.text
    else:
        text = str(node_input)
    
    ctx.state["raw_input"] = text

    # PII Scrubbing (Email and Phone numbers)
    scrubbed_text = text
    if config.pii_redaction_enabled:
        email_regex = r"[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+"
        phone_regex = r"\b\d{3}[-.]?\d{3}[-.]?\d{4}\b"
        scrubbed_text = re.sub(email_regex, "[REDACTED_EMAIL]", scrubbed_text)
        scrubbed_text = re.sub(phone_regex, "[REDACTED_PHONE]", scrubbed_text)

    # Save clean text to state
    ctx.state["clean_input"] = scrubbed_text

    # Prompt Injection Detection
    injection_keywords = ["ignore previous instructions", "system prompt", "dan mode", "bypass filter", "override instructions"]
    is_injection = False
    if config.injection_detection_enabled:
        for keyword in injection_keywords:
            if keyword in text.lower():
                is_injection = True
                break

    # Domain-specific rule (Profanity / Offensive words detection)
    profanity_keywords = ["scam", "fraud", "scammers", "stole"]
    has_profanity = any(word in text.lower() for word in profanity_keywords)

    # Audit Log (Structured JSON)
    audit_data = {
        "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat() + "Z",
        "session_id": ctx.session.id,
        "pii_scrubbed": scrubbed_text != text,
        "injection_detected": is_injection,
        "profanity_detected": has_profanity,
        "severity": "CRITICAL" if is_injection else ("WARNING" if has_profanity else "INFO"),
    }
    logger.info(f"AUDIT_LOG: {json.dumps(audit_data)}")

    if is_injection:
        return Event(
            output="Security violation: Prompt injection attempt detected.",
            route="SECURITY_EVENT",
            content=types.Content(role="model", parts=[types.Part.from_text(text="🚨 Security Violation: Potential prompt injection detected. Access Denied.")])
        )

    # If it has profanity, we force it to require review
    if has_profanity:
        ctx.state["force_review"] = True
    else:
        ctx.state["force_review"] = False

    # Standardize output to types.Content so downstream LLM agent validation succeeds
    content_output = types.Content(role="user", parts=[types.Part.from_text(text=scrubbed_text)])

    return Event(
        output=content_output,
        route="__DEFAULT__"
    )

def security_violation_handler(node_input: Any) -> Event:
    return Event(
        output=str(node_input),
        content=types.Content(role="model", parts=[types.Part.from_text(text=f"Process stopped. Reason: {node_input}")])
    )

async def review_gate(ctx: Context, node_input: Any) -> Generator[Any, None, None]:
    # Retrieve sentiment and draft from state
    sentiment_data = ctx.state.get("sentiment_analysis", {})
    draft_data = ctx.state.get("response_draft", {})
    
    sentiment = sentiment_data.get("sentiment", "neutral")
    draft_response = draft_data.get("draft_response", "")
    
    # Check if review is required
    force_review = ctx.state.get("force_review", False)
    requires_review = (sentiment == "negative") or force_review
    
    if requires_review:
        if not ctx.resume_inputs or "approval" not in ctx.resume_inputs:
            # Yield RequestInput to pause and ask for human review
            yield RequestInput(
                interrupt_id="approval",
                message=f"⚠️ NEGATIVE SENTIMENT OR POTENTIAL INCIDENT DETECTED ({sentiment}).\nDraft Response: {draft_response}\n\nType 'approve' to send as-is, or provide feedback to revise: "
            )
            return
            
        # If resumed, read the response
        user_response = ctx.resume_inputs.get("approval", "").strip()
        if user_response.lower() == "approve":
            yield Event(
                output=draft_response,
                route="approved",
                state={"final_response": draft_response}
            )
        else:
            # User provided feedback - standardize output to types.Content for the orchestrator
            feedback_content = types.Content(role="user", parts=[types.Part.from_text(text=user_response)])
            yield Event(
                output=feedback_content,
                route="feedback",
                state={"user_feedback": user_response}
            )
    else:
        # Auto-approve positive/neutral posts
        yield Event(
            output=draft_response,
            route="approved",
            state={"final_response": draft_response}
        )

def final_output(node_input: Any) -> Event:
    msg = f"✅ Response Approved and Published!\n\nResponse:\n{node_input}"
    return Event(
        output=str(node_input),
        content=types.Content(role="model", parts=[types.Part.from_text(text=msg)])
    )

# Define workflow graph using dictionary routing for conditional edges
root_agent = Workflow(
    name="social_media_monitor_workflow",
    edges=[
        (START, security_checkpoint),
        (security_checkpoint, {
            "SECURITY_EVENT": security_violation_handler,
            "__DEFAULT__": orchestrator
        }),
        (orchestrator, review_gate),
        (review_gate, {
            "approved": final_output,
            "feedback": orchestrator
        }),
    ],
)

# Instantiate the App
app = App(
    root_agent=root_agent,
    name="app",
    resumability_config=ResumabilityConfig(is_resumable=True),
)
