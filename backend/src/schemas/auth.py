from pydantic import BaseModel, EmailStr, Field, field_validator


class RegisterRequest(BaseModel):
    email: EmailStr
    password: str = Field(max_length=200)
    company_name: str = Field(max_length=255)


class LoginRequest(BaseModel):
    email: EmailStr
    password: str = Field(max_length=200)


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"


class UserResponse(BaseModel):
    id: str
    email: str
    role: str
    company_id: str | None

    @field_validator("id", "company_id", mode="before")
    @classmethod
    def _stringify_uuid(cls, v):
        return str(v) if v is not None else v

    class Config:
        from_attributes = True

