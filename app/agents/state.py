from typing import TypedDict
from uuid import UUID

from app.schemas.finance import Advice, Extraction
from app.schemas.visualization import VisualizationSpec


class WorkflowState(TypedDict, total=False):
    user_id: UUID
    update_id: int
    message_id: int
    text: str
    image: bytes
    input_source: str
    extraction: Extraction
    period: str | None
    pending_id: UUID
    callback_token: str
    edit_text: str
    facts: dict
    summary: dict
    advice: Advice
    response: dict
    error: str
    visualization_data: dict
    visualization_spec: VisualizationSpec
    visualization_analysis_status: str
