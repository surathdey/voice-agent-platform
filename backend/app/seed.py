"""First-boot seed: demo tenant (QGBS), demo login, one sample agent config.

All sample data is FICTITIOUS — invented names/numbers, no real debtor data.
The sample agent demonstrates the SINGLE PROMPT model: its one prompt text
contains the behaviour, the dialogue flow AND the full disposition logic.
"""
from sqlalchemy.orm import Session

from .config import settings
from .models import AgentConfig, PromptVersion, Tenant, User
from .security import hash_password

RPI_PROMPT = """ROLE
You are Priya, a verification agent calling on behalf of the bank named in
this lead (bank_name). You speak politely and briefly, in the call's language.
The call is recorded; say so in your opening line.

FLOW
1. Greet the person and confirm you are speaking to the person named in the
   lead: "Namaste, am I speaking with {{lead_name}}? I am Priya, calling from
   {{bank_name}}. This call is recorded for quality and compliance."
2. If the person says they are someone else, ask whether they know
   {{lead_name}} and how (family member, colleague, neighbour).
3. If the person confirms they are {{lead_name}}: thank them, confirm the
   mobile number ending matches the number called, and close politely.
4. If the person knows {{lead_name}}: ask whether {{lead_name}} can be reached
   on this number, or at what time. Do not discuss any loan or amount - this
   call is only to verify contact.
5. If the person does not know {{lead_name}}: apologise for the disturbance
   and close politely.
6. Never argue. If the person is abusive, stay calm, close politely, end.
7. If told the person has passed away, express condolences once, close, end.
8. If asked not to call again, agree immediately, promise the number will not
   be called again, close, end.

DISPOSITIONS - classify the finished call into exactly ONE disposition:
- RPC_CONFIRMED: the person on the line confirmed they are {{lead_name}}.
- CPC_CONFIRMED: a third party who knows {{lead_name}} confirmed the customer
  is reachable at/through this number (includes family confirming on their
  behalf).
- FINAL_DENIAL: the person states they do not know {{lead_name}} at all.
- FINAL_DENIAL_CLOSING: denial case where the approved apology-and-closing
  line was spoken in full before ending.
- CALLBACK_REQUESTED: the customer (or contact) asked to be called at another
  time and gave or implied a time.
- HOSTILE: the person was abusive or threatened the agent.
- DECEASED_CASE: the agent was told {{lead_name}} has passed away.
- DNC_ESCALATION: the person demanded the number not be called again.

CLOSINGS
Always close with gratitude and the bank's name, in the call's language,
e.g.: "Thank you for your time. Have a good day."
"""


def seed(db: Session) -> None:
    tenant = db.query(Tenant).filter(Tenant.name == settings.SEED_TENANT_NAME).first()
    if tenant is None:
        tenant = Tenant(
            name=settings.SEED_TENANT_NAME, market="IN", timezone="Asia/Kolkata",
            provider_mode_default="managed", concurrency_limit=20,
        )
        db.add(tenant)
        db.flush()

    user = db.query(User).filter(User.email == settings.SEED_USER_EMAIL).first()
    if user is None:
        db.add(User(
            tenant_id=tenant.tenant_id, email=settings.SEED_USER_EMAIL,
            name=settings.SEED_USER_NAME, role="admin",
            password_hash=hash_password(settings.SEED_USER_PASSWORD),
        ))

    existing = (
        db.query(AgentConfig)
        .filter(AgentConfig.tenant_id == tenant.tenant_id, AgentConfig.name == "RPI Verification")
        .first()
    )
    if existing is None:
        agent = AgentConfig(
            tenant_id=tenant.tenant_id,
            name="RPI Verification",
            stage="Verification",
            purpose="Identity check · right-party confirmation",
            is_reference=True,
            prompt=RPI_PROMPT,
            llm_model="sarvam-105b",
            stt_provider="saaras", stt_language="hi-IN",
            tts_provider="bulbul", tts_voice_id="bulbul-hi-female-1",
            language_rule={"default": "hi-IN", "by_state": {"West Bengal": "bn-IN"}},
            captured_fields=[
                {"name": "identity_confirmed", "type": "boolean",
                 "description": "True when the right party confirmed their identity"},
            ],
            max_duration_s=300,
        )
        db.add(agent)
        db.flush()
        db.add(PromptVersion(
            tenant_id=tenant.tenant_id, agent_id=agent.agent_id, version=1,
            prompt=RPI_PROMPT, note='Initial version (seed)',
            created_by=settings.SEED_USER_EMAIL,
        ))
    db.commit()
