"""Pydantic models for content distribution system.

Defines schemas for content items, variants, schedules, and manifests.
All models are intake-agnostic (work with Telegram, web UI, or any future source).
"""

from datetime import datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


class ContentType(str, Enum):
    PHOTO = "photo"
    VIDEO = "video"
    CAROUSEL = "carousel"
    REEL = "reel"
    STORY_PHOTO = "story_photo"
    STORY_VIDEO = "story_video"


class PostingFlow(str, Enum):
    """Determines which Instagram UI flow the agent uses."""
    FEED_PHOTO = "feed_photo"
    FEED_CAROUSEL = "feed_carousel"
    FEED_VIDEO = "feed_video"
    REEL = "reel"
    STORY_PHOTO = "story_photo"
    STORY_VIDEO = "story_video"


CONTENT_TYPE_TO_FLOW: dict[ContentType, PostingFlow] = {
    ContentType.PHOTO: PostingFlow.FEED_PHOTO,
    ContentType.VIDEO: PostingFlow.FEED_VIDEO,
    ContentType.CAROUSEL: PostingFlow.FEED_CAROUSEL,
    ContentType.REEL: PostingFlow.REEL,
    ContentType.STORY_PHOTO: PostingFlow.STORY_PHOTO,
    ContentType.STORY_VIDEO: PostingFlow.STORY_VIDEO,
}


class UniqualizationStatus(str, Enum):
    PENDING = "pending"
    PROCESSING = "processing"
    DONE = "done"
    FAILED = "failed"
    CANCELLED = "cancelled"


class VariantStatus(str, Enum):
    PENDING = "pending"
    ENCODING = "encoding"
    READY = "ready"
    SCHEDULED = "scheduled"
    UPLOADING = "uploading"
    ON_DEVICE = "on_device"
    POSTING = "posting"
    POSTED = "posted"
    FAILED = "failed"


class PostingState(str, Enum):
    """Granular state machine for crash recovery during posting."""
    SCHEDULED = "scheduled"
    UPLOADING = "uploading"
    ON_DEVICE = "on_device"
    GALLERY_OPENED = "gallery_opened"
    MEDIA_SELECTED = "media_selected"
    CAPTION_ENTERED = "caption_entered"
    SHARING = "sharing"
    POSTED = "posted"
    VERIFIED = "verified"
    FAILED = "failed"


class MediaFile(BaseModel):
    path: str
    order: int = 1
    mime: str = "image/jpeg"
    filename: str = ""


class UploadedBy(BaseModel):
    telegram_id: int = 0
    username: str = ""
    source: str = "telegram"


class ContentItem(BaseModel):
    """Original content uploaded by SMM manager."""
    content_id: str
    type: ContentType
    posting_flow: PostingFlow
    original_media: list[MediaFile]
    original_caption: str = ""
    uploaded_by: UploadedBy = Field(default_factory=UploadedBy)
    uploaded_at: datetime = Field(default_factory=datetime.utcnow)
    confirmed_at: datetime | None = None
    uniqualization_status: UniqualizationStatus = UniqualizationStatus.PENDING
    uniqualization_error: str | None = None
    total_variants_needed: int = 0
    variants_done: int = 0
    distribution_status: str = "pending"

    def to_mongo(self) -> dict:
        data = self.model_dump()
        data["type"] = self.type.value
        data["posting_flow"] = self.posting_flow.value
        data["uniqualization_status"] = self.uniqualization_status.value
        return data

    @classmethod
    def from_mongo(cls, data: dict) -> "ContentItem":
        return cls(**data)


class ContentVariant(BaseModel):
    """One uniqualized copy of content for a specific account."""
    content_id: str
    account_id: str
    variant_index: int
    media: list[MediaFile]
    caption: str = ""
    uniqualization_params: dict[str, Any] = Field(default_factory=dict)
    status: VariantStatus = VariantStatus.PENDING
    scheduled_date: str | None = None
    posted_at: datetime | None = None
    error: str | None = None
    retry_count: int = 0

    def to_mongo(self) -> dict:
        data = self.model_dump()
        data["status"] = self.status.value
        return data

    @classmethod
    def from_mongo(cls, data: dict) -> "ContentVariant":
        data.pop("_id", None)
        return cls(**data)


class ContentSchedule(BaseModel):
    """Posting schedule entry — who posts what and when."""
    date: str
    account_id: str
    device_id: str
    content_id: str
    variant_id: str | None = None
    posting_flow: PostingFlow
    posting_state: PostingState = PostingState.SCHEDULED
    session_id: str | None = None
    started_at: datetime | None = None
    completed_at: datetime | None = None
    error: str | None = None
    retry_count: int = 0
    max_retries: int = 2

    def to_mongo(self) -> dict:
        data = self.model_dump()
        data["posting_flow"] = self.posting_flow.value
        data["posting_state"] = self.posting_state.value
        return data

    @classmethod
    def from_mongo(cls, data: dict) -> "ContentSchedule":
        data.pop("_id", None)
        return cls(**data)


class DeviceManifest(BaseModel):
    """Manifest uploaded to device alongside media files.
    
    Agent reads this to know the posting flow, media order, and caption.
    Uploaded as /sdcard/DCIM/ToPost/manifest.json
    """
    content_id: str
    type: ContentType
    posting_flow: PostingFlow
    media: list[MediaFile]
    caption: str
    account_id: str
