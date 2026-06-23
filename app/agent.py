# ruff: noqa
import datetime
import json
import logging
import re
from typing import AsyncGenerator, Any
from pydantic import BaseModel, Field

from google.adk.agents import LlmAgent
from google.adk.apps import App, ResumabilityConfig
from google.adk.workflow import Workflow, START, node
from google.adk.tools import AgentTool
from google.adk.tools.mcp_tool import McpToolset
from google.adk.tools.mcp_tool.mcp_session_manager import StdioConnectionParams
from mcp import StdioServerParameters
from google.adk.events.event import Event
from google.adk.events.request_input import RequestInput
from google.adk.agents.context import Context
from google.genai import types

from app.config import config

# Setup logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

# Define Data Schemas
class OrchestratorResponse(BaseModel):
    response: str = Field(description="The response message to the user.")
    action_type: str = Field(description="Type of action: 'medication', 'appointment', or 'none'.")
    needs_confirmation: bool = Field(description="True if an appointment proposal was created and needs human confirmation.")

# Local MCP Server config
mcp_tools = McpToolset(
    connection_params=StdioConnectionParams(
        server_params=StdioServerParameters(
            command="uv",
            args=["run", "python", "-m", "app.mcp_server"],
        )
    )
)

# Sub-Agents
medication_agent = LlmAgent(
    name="medication_agent",
    model=config.model,
    instruction="""You are a specialized Medication Assistant. You help track medication schedules, list active prescriptions, and log intake records.
Always be extremely clear and use a warm, reassuring tone suitable for elderly users.
You can use the local MCP tools to fetch medications or log intake.""",
    description="Handles medication tracking, logging intake, and medication schedules.",
    tools=[mcp_tools],
)

doctor_visit_agent = LlmAgent(
    name="doctor_visit_agent",
    model=config.model,
    instruction="""You are a specialized Doctor Visit Coordinator. You help list upcoming appointments and propose new ones.
Always use a reassuring and clear tone suitable for elderly users.
You can use the local MCP tools to get appointments or schedule them.""",
    description="Coordinates and schedules doctor visits, appointments, and consultations.",
    tools=[mcp_tools],
)

# Orchestrator
orchestrator_agent = LlmAgent(
    name="orchestrator_agent",
    model=config.model,
    instruction="""You are the Elderly Care Concierge Orchestrator. Your job is to understand the user's request and delegate to either the Medication Assistant or the Doctor Visit Coordinator by calling the appropriate sub-agent.
Do NOT try to answer medicine or appointment queries yourself; you MUST call the respective sub-agent first, wait for their response, and then use that response to construct your final answer.
Only when you have the final answer, output it strictly as a JSON object matching this schema:
{"response": "final message to user containing the requested information", "action_type": "medication|appointment|none", "needs_confirmation": boolean}
Do not include any markdown formatting or code blocks, just the raw JSON object.""",
    sub_agents=[medication_agent, doctor_visit_agent],
)

# Workflow Function Nodes

def security_checkpoint(ctx: Context, node_input: types.Content) -> Event:
    user_query = ""
    if node_input and node_input.parts:
        user_query = "".join([p.text for p in node_input.parts if p.text])
    
    # Structured Audit Log
    audit_data = {
        "timestamp": datetime.datetime.utcnow().isoformat(),
        "event": "security_check",
        "query_length": len(user_query),
        "severity": "INFO"
    }

    # Prompt Injection Check
    injection_keywords = ["ignore previous instructions", "system prompt", "override", "bypass"]
    if any(kw in user_query.lower() for kw in injection_keywords):
        audit_data["status"] = "REJECTED"
        audit_data["reason"] = "Prompt injection detected"
        audit_data["severity"] = "CRITICAL"
        logging.warning(json.dumps(audit_data))
        return Event(output="Security Warning: Unsafe prompt input detected.", route="security_alert")

    # Domain-Specific Rule: Prevent modification of high-risk drug dosages without authorization
    high_risk_drugs = ["insulin", "warfarin", "oxycodone", "fentanyl", "morphine"]
    if any(drug in user_query.lower() for drug in high_risk_drugs) and any(act in user_query.lower() for act in ["change", "dosage", "increase", "decrease", "modify"]):
        audit_data["status"] = "REJECTED"
        audit_data["reason"] = "Unauthorized dosage modification attempt of high-risk drug"
        audit_data["severity"] = "CRITICAL"
        logging.warning(json.dumps(audit_data))
        return Event(output="Security Warning: Dose modification of restricted medications must be guided by a doctor.", route="security_alert")

    # PII Scrubbing
    scrubbed_query = user_query
    # Scrub SSN (e.g., xxx-xx-xxxx)
    scrubbed_query = re.sub(r'\b\d{3}-\d{2}-\d{4}\b', '[SSN REDACTED]', scrubbed_query)
    # Scrub Card numbers
    scrubbed_query = re.sub(r'\b\d{4}[- ]?\d{4}[- ]?\d{4}[- ]?\d{4}\b', '[CARD REDACTED]', scrubbed_query)
    # Scrub Phone numbers
    scrubbed_query = re.sub(r'\b\d{3}[-.]?\d{3}[-.]?\d{4}\b', '[PHONE REDACTED]', scrubbed_query)
    # Scrub Medical Record Numbers (MRN)
    scrubbed_query = re.sub(r'\bMRN-\d{6}\b', '[MRN REDACTED]', scrubbed_query)
    
    audit_data["status"] = "PASSED"
    logging.info(json.dumps(audit_data))
    
    ctx.state["clean_query"] = scrubbed_query
    return Event(output=scrubbed_query, route="safe")


def security_event(node_input: str) -> Event:
    msg = "I cannot fulfill this request due to safety and security policies."
    return Event(
        content=types.Content(
            role="model",
            parts=[types.Part.from_text(text=msg)]
        ),
        output=msg
    )


@node(rerun_on_resume=True)
async def run_orchestrator(ctx: Context, node_input: str) -> Event:
    resp = await ctx.run_node(orchestrator_agent, node_input=node_input)
    
    parsed_resp = {}
    if isinstance(resp, str):
        try:
            clean_str = resp.strip('` \n')
            if clean_str.startswith('json'):
                clean_str = clean_str[4:].strip()
            parsed_resp = json.loads(clean_str)
        except json.JSONDecodeError:
            parsed_resp = {"response": resp, "needs_confirmation": False, "action_type": "none"}
    elif isinstance(resp, dict):
        parsed_resp = resp
    else:
        parsed_resp = {
            "response": getattr(resp, "response", str(resp)), 
            "needs_confirmation": getattr(resp, "needs_confirmation", False),
            "action_type": getattr(resp, "action_type", "none")
        }

    response_text = parsed_resp.get("response", "")
    needs_confirm = parsed_resp.get("needs_confirmation", False)
        
    ctx.state["last_response"] = response_text
    
    if needs_confirm:
        # Save details of appointment proposal to state
        ctx.state["pending_appointment"] = {
            "doctor": "Dr. Smith",
            "specialty": "Cardiologist",
            "datetime_str": "Friday at 2:00 PM",
            "reason": "Routine Checkup"
        }
        return Event(output=parsed_resp, route="needs_confirmation")
        
    return Event(output=parsed_resp, route="direct_response")


@node(rerun_on_resume=True)
async def confirm_visit(ctx: Context, node_input: Any) -> AsyncGenerator[Event, None]:
    if not ctx.resume_inputs:
        appt = ctx.state.get("pending_appointment", {})
        msg = f"Would you like to confirm scheduling a visit with {appt.get('doctor')} ({appt.get('specialty')}) on {appt.get('datetime_str')}? Please reply 'yes' to confirm or 'no' to cancel."
        yield RequestInput(interrupt_id="confirm_appointment", message=msg)
        return

    user_reply = ctx.resume_inputs.get("confirm_appointment", "").lower().strip()
    
    audit_data = {
        "timestamp": datetime.datetime.utcnow().isoformat(),
        "event": "hitl_appointment_confirmation",
        "user_reply": user_reply,
        "severity": "INFO"
    }

    if "yes" in user_reply:
        appt = ctx.state.get("pending_appointment", {})
        appointments = ctx.state.get("appointments", [])
        appointments.append(appt)
        ctx.state["appointments"] = appointments
        ctx.state["pending_appointment"] = None
        
        audit_data["status"] = "APPROVED"
        logging.info(json.dumps(audit_data))
        
        yield Event(
            content=types.Content(
                role="model",
                parts=[types.Part.from_text(text=f"Great! I have confirmed and scheduled your appointment with {appt.get('doctor')} on {appt.get('datetime_str')}.")]
            ),
            output=f"Appointment scheduled with {appt.get('doctor')} on {appt.get('datetime_str')}."
        )
    else:
        ctx.state["pending_appointment"] = None
        
        audit_data["status"] = "CANCELLED"
        logging.info(json.dumps(audit_data))
        
        yield Event(
            content=types.Content(
                role="model",
                parts=[types.Part.from_text(text="Understood. I have cancelled the appointment proposal.")]
            ),
            output="Appointment proposal cancelled."
        )


def final_response(node_input: Any) -> Event:
    if isinstance(node_input, dict):
        text = node_input.get("response", str(node_input))
    elif hasattr(node_input, "response"):
        text = node_input.response
    else:
        text = str(node_input)
        
    return Event(
        content=types.Content(
            role="model",
            parts=[types.Part.from_text(text=text)]
        ),
        output=text
    )


# Workflow Definition
root_agent = Workflow(
    name="elderly_care_workflow",
    edges=[
        (START, security_checkpoint),
        (security_checkpoint, {"security_alert": security_event, "safe": run_orchestrator}),
        (run_orchestrator, {"needs_confirmation": confirm_visit, "direct_response": final_response}),
        (confirm_visit, final_response),
        (security_event, final_response)
    ],
    rerun_on_resume=True
)

app = App(
    root_agent=root_agent,
    name="app",
    resumability_config=ResumabilityConfig(is_resumable=True)
)

