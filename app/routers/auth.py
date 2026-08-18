from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, status
from fastapi.security import OAuth2PasswordRequestForm
from sqlmodel import Session, select
from pydantic import BaseModel, EmailStr
from app.core.database import get_session
from app.core.security import verify_password, hash_password, create_access_token, get_current_user
from app.models.user import User, Workspace, WorkspaceMember, UserRole
from app.services.email import send_welcome_email
import re

router = APIRouter()


class RegisterRequest(BaseModel):
    name: str
    email: EmailStr
    password: str


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    user: dict


@router.post("/register", response_model=TokenResponse)
async def register(
    data: RegisterRequest,
    background_tasks: BackgroundTasks,
    session: Session = Depends(get_session),
):
    existing = session.exec(select(User).where(User.email == data.email)).first()
    if existing:
        raise HTTPException(status_code=400, detail="Email already registered")

    if len(data.password) < 8:
        raise HTTPException(status_code=400, detail="Password must be at least 8 characters")

    user = User(
        name=data.name,
        email=data.email,
        hashed_password=hash_password(data.password),
    )
    session.add(user)
    session.commit()
    session.refresh(user)

    # Auto-create personal workspace
    slug = re.sub(r"[^a-z0-9]", "-", data.name.lower())[:20] + f"-{user.id}"
    workspace = Workspace(name=f"{data.name}'s Workspace", slug=slug, owner_id=user.id)
    session.add(workspace)
    session.commit()
    session.refresh(workspace)

    member = WorkspaceMember(workspace_id=workspace.id, user_id=user.id, role=UserRole.OWNER)
    session.add(member)
    session.commit()

    background_tasks.add_task(send_welcome_email, user.name, user.email)

    token = create_access_token({"sub": str(user.id)})
    return TokenResponse(
        access_token=token,
        user={"id": user.id, "name": user.name, "email": user.email, "plan": user.plan},
    )


@router.post("/login", response_model=TokenResponse)
async def login(form: OAuth2PasswordRequestForm = Depends(), session: Session = Depends(get_session)):
    user = session.exec(select(User).where(User.email == form.username)).first()
    if not user or not verify_password(form.password, user.hashed_password):
        raise HTTPException(status_code=401, detail="Invalid credentials")

    token = create_access_token({"sub": str(user.id)})
    return TokenResponse(
        access_token=token,
        user={"id": user.id, "name": user.name, "email": user.email, "plan": user.plan},
    )


@router.get("/me")
async def me(current_user: User = Depends(get_current_user)):
    return {
        "id": current_user.id,
        "name": current_user.name,
        "email": current_user.email,
        "plan": current_user.plan,
        "avatar_url": current_user.avatar_url,
        "created_at": current_user.created_at,
    }
