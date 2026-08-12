"""Students CRUD + assign-to-bus."""

from __future__ import annotations

from typing import Annotated, Optional

from fastapi import APIRouter, Depends, HTTPException, Response, status
from pydantic import BaseModel, Field

from .deps import (
    CurrentUser,
    get_current_user,
    get_supabase_admin,
    load_app_role,
    load_parent_scope,
)

router = APIRouter()


def _get_data(res):
    if res is None:
        return None
    return getattr(res, "data", None)


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------


class StudentOut(BaseModel):
    id: str
    bus_id: Optional[str] = None
    name: str
    class_grade: str
    stop: str


class StudentCreate(BaseModel):
    bus_id: Optional[str] = None
    name: str = Field(min_length=1, max_length=120)
    class_grade: str = Field(min_length=1, max_length=40)
    stop: str = Field(min_length=1, max_length=200)


class StudentUpdate(BaseModel):
    name: Optional[str] = Field(default=None, min_length=1, max_length=120)
    class_grade: Optional[str] = Field(default=None, min_length=1, max_length=40)
    stop: Optional[str] = Field(default=None, min_length=1, max_length=200)
    bus_id: Optional[str] = None


class AssignRequest(BaseModel):
    bus_id: Optional[str] = None  # null = unassign


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.get("", response_model=list[StudentOut])
def list_students(
    user: Annotated[CurrentUser, Depends(get_current_user)],
    supabase=Depends(get_supabase_admin),
) -> list[StudentOut]:
    # Parents see only their linked student; everyone else sees all.
    if user.app_role is None:
        user.app_role = load_app_role(user, supabase)
    if user.app_role == "parent":
        scope = load_parent_scope(user, supabase)
        sid = scope.get("linked_student_id")
        if not sid:
            return []
        res = (
            supabase.table("students")
            .select("id, bus_id, name, class_grade, stop")
            .eq("id", sid)
            .execute()
        )
    else:
        # Single select — no N+1.
        res = (
            supabase.table("students")
            .select("id, bus_id, name, class_grade, stop")
            .execute()
        )
    rows = _get_data(res) or []
    return [StudentOut(**r) for r in rows]


@router.get("/{student_id}", response_model=StudentOut)
def get_student(
    student_id: str,
    user: Annotated[CurrentUser, Depends(get_current_user)],
    supabase=Depends(get_supabase_admin),
) -> StudentOut:
    # Parents can only fetch their own linked student.
    if user.app_role is None:
        user.app_role = load_app_role(user, supabase)
    if user.app_role == "parent":
        scope = load_parent_scope(user, supabase)
        if scope.get("linked_student_id") != student_id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="This student is not linked to your account.",
            )
    res = (
        supabase.table("students")
        .select("id, bus_id, name, class_grade, stop")
        .eq("id", student_id)
        .maybe_single()
        .execute()
    )
    row = _get_data(res)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Student not found.")
    return StudentOut(**row)


@router.post("", response_model=StudentOut, status_code=status.HTTP_201_CREATED)
def create_student(
    body: StudentCreate,
    user: Annotated[CurrentUser, Depends(get_current_user)],
    supabase=Depends(get_supabase_admin),
) -> StudentOut:
    if user.app_role is None:
        user.app_role = load_app_role(user, supabase)
    if user.app_role != "admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only admins can create students.",
        )
    payload = {
        "name": body.name,
        "class_grade": body.class_grade,
        "stop": body.stop,
        "bus_id": body.bus_id,
    }
    res = supabase.table("students").insert(payload).execute()
    inserted = _get_data(res)
    if not inserted or not inserted[0].get("id"):
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Insert succeeded but the database returned no row id.",
        )
    student_id = inserted[0]["id"]
    res = (
        supabase.table("students")
        .select("id, bus_id, name, class_grade, stop")
        .eq("id", student_id)
        .maybe_single()
        .execute()
    )
    row = _get_data(res)
    return StudentOut(**row)


@router.patch("/{student_id}", response_model=StudentOut)
def update_student(
    student_id: str,
    body: StudentUpdate,
    user: Annotated[CurrentUser, Depends(get_current_user)],
    supabase=Depends(get_supabase_admin),
) -> StudentOut:
    if user.app_role is None:
        user.app_role = load_app_role(user, supabase)
    if user.app_role != "admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only admins can edit students.",
        )
    patch = body.model_dump(exclude_none=True)
    if patch:
        # Verify the student exists so PATCH returns 404 on bad id.
        res = (
            supabase.table("students")
            .select("id")
            .eq("id", student_id)
            .maybe_single()
            .execute()
        )
        existing = _get_data(res)
        if not existing:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Student not found.")
        supabase.table("students").update(patch).eq("id", student_id).execute()
    row = (
        supabase.table("students")
        .select("id, bus_id, name, class_grade, stop")
        .eq("id", student_id)
        .maybe_single()
        .execute()
        .data
    )
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Student not found.")
    return StudentOut(**row)


@router.delete("/{student_id}", status_code=status.HTTP_204_NO_CONTENT, response_class=Response)
def delete_student(
    student_id: str,
    user: Annotated[CurrentUser, Depends(get_current_user)],
    supabase=Depends(get_supabase_admin),
):
    if user.app_role is None:
        user.app_role = load_app_role(user, supabase)
    if user.app_role != "admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only admins can delete students.",
        )
    supabase.table("students").delete().eq("id", student_id).execute()


@router.post("/{student_id}/assign", response_model=StudentOut)
def assign_student(
    student_id: str,
    body: AssignRequest,
    user: Annotated[CurrentUser, Depends(get_current_user)],
    supabase=Depends(get_supabase_admin),
) -> StudentOut:
    if user.app_role is None:
        user.app_role = load_app_role(user, supabase)
    if user.app_role != "admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only admins can assign students.",
        )
    # Verify the student exists (so we don't silently no-op on a bad id).
    res = (
        supabase.table("students")
        .select("id")
        .eq("id", student_id)
        .maybe_single()
        .execute()
    )
    existing = _get_data(res)
    if not existing:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Student not found.")
    # If a bus_id was provided, verify it exists — otherwise the FK
    # constraint would 500 with a confusing PostgREST error.
    if body.bus_id is not None:
        res = (
            supabase.table("buses")
            .select("id")
            .eq("id", body.bus_id)
            .maybe_single()
            .execute()
        )
        bus_exists = _get_data(res)
        if not bus_exists:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Bus '{body.bus_id}' not found.",
            )
    supabase.table("students").update({"bus_id": body.bus_id}).eq("id", student_id).execute()
    row = (
        supabase.table("students")
        .select("id, bus_id, name, class_grade, stop")
        .eq("id", student_id)
        .maybe_single()
        .execute()
        .data
    )
    return StudentOut(**row)