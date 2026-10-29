from enum import StrEnum

from pydantic import BaseModel, Field


class PageFamily(StrEnum):
    SEARCH_RESULTS = "search_results"
    VACANCY = "vacancy"
    NEGOTIATIONS = "negotiations"
    CHAT_LIST = "chat_list"
    CHAT_THREAD = "chat_thread"
    APPLICATION_FLOW = "application_flow"
    PROFILE = "profile"
    CAPTCHA = "captcha"
    ERROR = "error"
    UNKNOWN = "unknown"


class Capabilities(BaseModel):
    can_open_vacancy: bool = False
    can_start_application: bool = False
    has_resume_selector: bool = False
    has_cover_letter_field: bool = False
    has_employer_questions: bool = False
    has_submit_control: bool = False
    has_post_application_cover_action: bool = False
    has_chat_input: bool = False
    has_bot_options: bool = False
    has_external_form_link: bool = False
    already_applied: bool = False


class PageObservation(BaseModel):
    url: str
    family: PageFamily
    capabilities: Capabilities
    confidence: str = Field(pattern="^(confirmed|partial|unknown)$")
    evidence: list[str] = Field(default_factory=list)


class VacancyDTO(BaseModel):
    vacancy_id: str | None = None
    url: str
    title: str | None = None
    company: str | None = None
    compensation: str | None = None
    description_summary: str | None = None
    full_description: str | None = None
    structured_facts: dict[str, object] = Field(default_factory=dict)
    observation: PageObservation


class SearchDTO(BaseModel):
    url: str | None = None
    query: str | None = None
    card_count: int | None = None
    filter_drawer_open: bool = False
    control_count: int = 0
    observation: PageObservation


class NegotiationsDTO(BaseModel):
    url: str
    tab_counts: dict[str, int] = Field(default_factory=dict)
    visible_item_count: int = 0
    visible_statuses: list[str] = Field(default_factory=list)
    observation: PageObservation


class ApplicationFlowDTO(BaseModel):
    vacancy_id: str | None = None
    cover_letter_required: bool | None = None
    cover_letter_max_length: int | None = None
    submit_enabled: bool | None = None
    employer_question_count: int = 0
    observation: PageObservation


class ChatListItemDTO(BaseModel):
    chat_id: str
    title: str
    category_hints: list[str] = Field(default_factory=list)
    status: str = "unknown"


class ChatListDTO(BaseModel):
    items: list[ChatListItemDTO] = Field(default_factory=list)
    observation: PageObservation


class ChatMessageDTO(BaseModel):
    direction: str = Field(pattern="^(incoming|outgoing|system|unknown)$")
    text: str
    # Do not substitute observation time: an SLA may use only an HH-rendered
    # machine-readable timestamp.
    received_at: str | None = None


class ChatThreadDTO(BaseModel):
    chat_id: str | None = None
    url: str
    title: str | None = None
    messages: list[ChatMessageDTO] = Field(default_factory=list)
    observation: PageObservation
