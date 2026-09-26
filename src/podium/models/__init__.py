"""All ORM models. Importing this package registers every table on Base.metadata."""

from podium.models.audit import AuditLog
from podium.models.base import Base, UTCDateTime, new_public_id, utcnow
from podium.models.certificates import Certificate, CertificateKind, InstanceSetting
from podium.models.comments import Comment
from podium.models.events import (
    Event,
    EventRole,
    JudgeInvite,
    JudgeTrack,
    NormalizationMethod,
    Prize,
    RankingBasis,
    Role,
    RubricCriterion,
    Track,
    VotingMode,
)
from podium.models.integrations import DeliveryStatus, Webhook, WebhookDelivery
from podium.models.judging import (
    Assignment,
    AssignmentMethod,
    AssignmentStatus,
    ComparisonSource,
    PairwiseComparison,
    Review,
    ReviewStatus,
    ScoreItem,
)
from podium.models.projects import Project, ProjectStatus
from podium.models.teams import MemberRole, Team, TeamMember
from podium.models.users import ApiToken, Session, User
from podium.models.voting import Vote, VoterCode, VoterLedger

__all__ = [
    "ApiToken",
    "Assignment",
    "AssignmentMethod",
    "AssignmentStatus",
    "AuditLog",
    "Base",
    "Certificate",
    "CertificateKind",
    "Comment",
    "ComparisonSource",
    "DeliveryStatus",
    "Event",
    "EventRole",
    "InstanceSetting",
    "JudgeInvite",
    "JudgeTrack",
    "MemberRole",
    "NormalizationMethod",
    "PairwiseComparison",
    "Prize",
    "Project",
    "ProjectStatus",
    "RankingBasis",
    "Review",
    "ReviewStatus",
    "Role",
    "RubricCriterion",
    "ScoreItem",
    "Session",
    "Team",
    "TeamMember",
    "Track",
    "UTCDateTime",
    "User",
    "Vote",
    "VoterCode",
    "VoterLedger",
    "VotingMode",
    "Webhook",
    "WebhookDelivery",
    "new_public_id",
    "utcnow",
]
