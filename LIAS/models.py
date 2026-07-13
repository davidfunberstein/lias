"""Pydantic data models — guide step 1 / מודלי נתונים — שלב 1 במדריך.

EN: These models are the contract of the whole system. Every table in db.py
    mirrors one model here 1:1. Strict types = cross-referencing works later.
HE: המודלים האלה הם החוזה של כל המערכת. כל טבלה ב-db.py משקפת מודל
    אחד כאן 1:1. טיפוסים קשיחים = ההצלבות עובדות בהמשך.
"""
from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field


# --- Enums / קבועים ---------------------------------------------------------

class Portal(str, Enum):
    NET = "NET"          # נט המשפט
    BDR = "BDR"          # בתי הדין הרבניים


class SideLabel(str, Enum):
    """Who produced the document / מי הפיק את המסמך (step 13 / שלב 13)."""
    SIDE_A = "SIDE_A"    # e.g. husband/plaintiff / הבעל/תובע
    SIDE_B = "SIDE_B"    # e.g. wife/defendant / האישה/נתבעת
    JUDGE = "JUDGE"      # court itself / בית המשפט עצמו
    OTHER = "OTHER"
    UNKNOWN = "UNKNOWN"


class DownloadStatus(str, Enum):
    """Retry state machine — guide step 10 / מכונת מצבים — שלב 10."""
    PENDING = "PENDING"
    IN_PROGRESS = "IN_PROGRESS"
    COMPLETED = "COMPLETED"
    ERROR = "ERROR"
    SKIPPED_STICKY = "SKIPPED_STICKY"   # pinned decision skip / דילוג מדבקה צהובה
    MISSING = "MISSING"                 # was on disk, deleted / היה בדיסק ונמחק


class JobState(str, Enum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    ERROR = "ERROR"
    CANCELLED = "CANCELLED"


# --- Core entities / ישויות ליבה --------------------------------------------

class Client(BaseModel):
    client_id: Optional[int] = None
    display_name: str
    aliases_json: str = "[]"            # other spellings / איותים נוספים


class Case(BaseModel):
    case_id: Optional[int] = None
    client_id: int
    portal: Portal
    court: str = ""
    case_number: str                    # e.g. "1355021" / מספר תיק
    title: str = ""


class SubCase(BaseModel):
    sub_case_id: Optional[int] = None
    case_id: int
    sub_number: str                     # e.g. "1355021/2"
    sub_type: str = ""                  # custody/property… / משמורת/רכוש…
    status: str = ""


class DocumentRec(BaseModel):
    """Central table — one row per court document / הטבלה המרכזית — שורה לכל מסמך."""
    document_id: Optional[int] = None
    sub_case_id: int
    physical_name: str                  # portal's original name / השם המקורי בפורטל
    logical_name: str = ""              # date+type+submitter / תאריך+סוג+מגיש
    doc_type: str = ""                  # החלטה/בקשה/תגובה…
    submitter_est: str = ""             # as shown in table / כפי שמופיע בטבלה
    side_label: SideLabel = SideLabel.UNKNOWN
    submission_date: str = ""           # DD/MM/YYYY as displayed / כמוצג
    submission_time: str = ""           # from hidden HTML / מה-HTML הנסתר (step 5)
    filing_ts_resolved: Optional[datetime] = None  # after smoothing / אחרי החלקה
    pages: int = 0
    file_size_kb: int = 0
    sha256: str = ""
    is_sticky: bool = False             # yellow sticker / מדבקה צהובה (step 7)
    download_status: DownloadStatus = DownloadStatus.PENDING
    retry_count: int = 0
    local_path: str = ""                # relative to COURT_DOCS_DIR / יחסי
    downloaded_at: Optional[datetime] = None
    drive_file_id: str = ""


class SnapshotRec(BaseModel):
    """Guide step 6 / שלב 6 — grid state at a point in time / מצב הגריד בנקודת זמן."""
    snapshot_id: Optional[int] = None
    sub_case_id: int
    taken_at: datetime = Field(default_factory=datetime.now)
    grid_json: str                      # serialized rows / השורות בסריאליזציה
    diff_from_prev_json: str = "{}"     # added/removed/changed / נוסף/נעלם/השתנה


class Job(BaseModel):
    """Async engine — guide step 18 / מנוע אסינכרוני — שלב 18."""
    job_id: Optional[int] = None
    kind: str                           # sync_case / migrate / embed…
    payload_json: str = "{}"
    state: JobState = JobState.PENDING
    progress: float = 0.0               # 0..1
    message: str = ""                   # live status line for UI / שורת סטטוס ל-UI
    error: str = ""
    retry_count: int = 0
    created_at: datetime = Field(default_factory=datetime.now)
    finished_at: Optional[datetime] = None


class SyncRun(BaseModel):
    """Replaces sync_history.csv / מחליף את sync_history.csv."""
    run_id: Optional[int] = None
    portal: Portal
    sub_case_id: Optional[int] = None
    started_at: datetime = Field(default_factory=datetime.now)
    finished_at: Optional[datetime] = None
    total_in_portal: int = 0
    downloaded_new: int = 0
    re_downloaded: int = 0
    failed: int = 0
    portal_hash: str = ""
    prev_hash: str = ""
    hash_changed: str = ""              # ראשון / כן / לא
    note: str = ""
