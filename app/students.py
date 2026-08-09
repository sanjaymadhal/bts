"""Students CRUD + assign-to-bus."""

from __future__ import annotations

from typing import Annotated, Optional

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from ..deps import CurrentUser, get_current_user, get_supabase_user

router = APIRouter()


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
# Helpers
# ---------------------------------------------------------------------------


def _load(student_id: str, supabase) -> StudentOut:
    row = (
        supabase.table("students")
        .select("id, bus_id, name, class_grade, stop")
        .eq("id", student_id)
        .single()
        .execute()
        .data
    )
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Student not found.")
    return StudentOut(**row)


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.get("", response_model=list[StudentOut])
def list_students(
    _user: Annotated[CurrentUser, Depends(get_current_user)],
    supabase=Depends(get_supabase_user),
) -> list[StudentOut]:
    rows = supabase.table("students").select("id").execute().data or []
    return [_load(r["id"], supabase) for r in rows]


@router.get("/{student_id}", response_model=StudentOut)
def get_student(
    student_id: str,
    _user: Annotated[CurrentUser, Depends(get_current_user)],
    supabase=Depends(get_supabase_user),
) -> StudentOut:
    return _load(student_id, supabase)


@router.post("", response_model=StudentOut, status_code=status.HTTP_201_CREATED)
def create_student(
    body: StudentCreate,
    _user: Annotated[CurrentUser, Depends(get_current_user)],
    supabase=Depends(get_supabase_user),
) -> StudentOut:
    inserted = (
        supabase.table("students")
        .insert({
            "name": body.name,
            "class_grade": body.class_grade,
            "stop": body.stop,
            "bus_id": body.bus_id,
        })
        .execute()
        .data
    )
    return _load(inserted[0]["id"], supabase)


@router.patch("/{student_id}", response_model=StudentOut)
def update_student(
    student_id: str,
    body: StudentUpdate,
    _user: Annotated[CurrentUser, Depends(get_current_user)],
    supabase=Depends(get_supabase_user),
) -> StudentOut:
    patch = body.model_dump(exclude_none=True)
    if patch:
        supabase.table("students").update(patch).eq("id", student_id).execute()
    return _load(student_id, supabase)


@router.delete("/{student_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_student(
    student_id: str,
    _user: Annotated[CurrentUser, Depends(get_current_user)],
    supabase=Depends(get_supabase_user),
) -> None:
    supabase.table("students").delete().eq("id", student_id).execute()


@router.post("/{student_id}/assign", response_model=StudentOut)
def assign_student(
    student_id: str,
    body: AssignRequest,
    _user: Annotated[CurrentUser, Depends(get_current_user)],
    supabase=Depends(get_supabase_user),
) -> StudentOut:
    supabase.table("students").update({"bus_id": body.bus_id}).eq("id", student_id).execute()
    return _load(student_id, supabase)
