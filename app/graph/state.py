import operator
from typing import Annotated, Literal, TypedDict

from pydantic import BaseModel, Field


#  ── Models ─────────────────────────────────────────────────────────────
class Email(BaseModel):
    """
    Represents an email message.
    """
    message_id: str = Field(..., 
                        description="Unique identifier for the email message.")
    sender: str = Field(...,
                        description="Email address of the sender.")
    subject: str = Field(...,
                        description="Subject line of the email.")
    body: str = Field(...,
                        description="Content of the email message.")
    to: str = Field(...,
                    description="Email address of the recipient.")  
    date: str = Field(...,
                    description="Date and time when the email was sent.")
class Draft(BaseModel): 
    """
    Represents a draft email message.
    """
    subject: str = Field(...,
                    description="Subject line of the Draft.")
    recipient: str = Field(...,
                    description="Email address of the recipient.")
    body: str = Field(...,
                    description="Content of the draft email message.")

class FlaggedEmail(BaseModel): 
    """ 
    Represents an email message that has been flagged.
    """
    email: Email
    reason: str = Field(...,
                    description="Reason for flagging the email.")
    category: str = Field(...,
                    description="Category of the flagged email, if applicable.")

class ClassifierResult(BaseModel):
    """
    Represents the result of a classification operation on an email.
    """
    # Originally `is_injection: bool` (Phase 1 threat-model lock: classifier's
    # only output is a yes/no flag). Replaced with this 3-way Literal once
    # INJECTION_DETECTION_PROMPT was written to return "ambiguous" as a real,
    # distinct verdict (encoded/gibberish content, not provably malicious) --
    # collapsing that into the boolean would throw away exactly the label
    # Phase 3's false-positive-rate measurement needs to tell "confident
    # detection" apart from "erred cautious." Whether "ambiguous" is treated
    # as unsafe is a policy decision for the node that reads this result, not
    # something baked into the classifier itself.
    injection_type: Literal["injection", "clean", "ambiguous"] = Field(...,
                        description="The type of injection, if applicable.")
    
class RejectionRecord(BaseModel):
    """
    Represents a record of a rejected email, including the email itself and the reason for rejection.
    """
    message_id: str = Field(...,
                        description="Unique identifier for the rejected email message.")
    reason: str = Field(...,
                        description="Reason for rejecting the email message.")

class State(TypedDict):
    """
    Represents the state of the email triage agent.
    """
    authenticated: bool 
    inbox: list[Email]
    current_email: Email | None 
    draft: Draft | None
    thread_history: list[str]
    flagged_emails: Annotated[list[FlaggedEmail], operator.add] 
    rejection_reasons: Annotated[list[RejectionRecord], operator.add] 

