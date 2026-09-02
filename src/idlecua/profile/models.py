from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field


class UserCharacteristics(BaseModel):
    occupation: str = ""
    projects: list[str] = Field(default_factory=list)
    goals: list[str] = Field(default_factory=list)
    interests: list[str] = Field(default_factory=list)
    technologies: list[str] = Field(default_factory=list)
    material_types: list[str] = Field(default_factory=list)
    material_depth: str = ""  # e.g. overview, deep dive, mixed
    content_languages: list[str] = Field(default_factory=list)
    unwanted_topics: list[str] = Field(default_factory=list)


class ComputerUsage(BaseModel):
    schedule: str = ""  # e.g. Mon-Fri 9-17
    idle_periods: str = ""
    overnight_habits: str = ""
    screen_lock_habits: str = ""
    monitors: str = ""  # e.g. "1" or "2 external"
    common_apps: list[str] = Field(default_factory=list)
    common_sites: list[str] = Field(default_factory=list)
    return_signals: list[str] = Field(default_factory=list)
    idle_threshold_minutes: int = 10


class BrowserConsent(BaseModel):
    """Explicit consent for using the owner's main Chrome profile.

    The agent attaches to the existing Chrome profile only when this consent is
    recorded. This satisfies the 'main profile via driver's existing-profile
    attachment, with explicit consent recorded in the profile/config' requirement
    (issue #12). The driver-level grant (cua-driver --grant existing-profile) is
    separate and checked at runtime; this flag is the product-level consent.
    """

    main_profile_granted: bool = False
    granted_at: str | None = None
    browser: str = "chrome"  # chrome | chromium | edge | brave
    grant_method: str | None = None  # e.g. "profile interview", "cli grant-browser"


class AutonomyBoundaries(BaseModel):
    allowed_sites: list[str] = Field(default_factory=list)
    allowed_apps: list[str] = Field(default_factory=list)
    auto_allowed_actions: list[str] = Field(default_factory=list)
    confirmation_required_actions: list[str] = Field(default_factory=list)
    forbidden_actions: list[str] = Field(default_factory=list)
    deny_zones: list[str] = Field(default_factory=list)
    results_location: str = ""
    session_duration_minutes: int = 45
    daily_action_limit: int = 200
    daily_llm_call_limit: int = 150
    allowed_hours: str = "00:00-23:59"
    browser_consent: BrowserConsent = Field(default_factory=BrowserConsent)


class ProfileMeta(BaseModel):
    created_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    updated_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


class Profile(BaseModel):
    version: int = 1
    confirmed: bool = False
    user_characteristics: UserCharacteristics = Field(default_factory=UserCharacteristics)
    computer_usage: ComputerUsage = Field(default_factory=ComputerUsage)
    autonomy_boundaries: AutonomyBoundaries = Field(default_factory=AutonomyBoundaries)
    meta: ProfileMeta = Field(default_factory=ProfileMeta)
    # Back-compat: browser consent also mirrored at top-level for older code that checks profile.browser_consent
    # Prefer autonomy_boundaries.browser_consent; this alias keeps serialized files readable if edited manually.
    browser_consent: BrowserConsent | None = None

    def model_post_init(self, __context):  # type: ignore[override]
        # Normalize: if top-level browser_consent is set, migrate to autonomy_boundaries; keep both in sync
        if self.browser_consent is not None and not self.autonomy_boundaries.browser_consent.main_profile_granted:
            # If top-level was granted but nested not, copy over
            if self.browser_consent.main_profile_granted:
                self.autonomy_boundaries.browser_consent = self.browser_consent
        # Always keep alias in sync for serialization convenience
        self.browser_consent = self.autonomy_boundaries.browser_consent

    def touch(self) -> None:
        self.meta.updated_at = datetime.now(timezone.utc).isoformat()

    def to_dict(self) -> dict[str, Any]:
        return self.model_dump()

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Profile":
        return cls.model_validate(data)
