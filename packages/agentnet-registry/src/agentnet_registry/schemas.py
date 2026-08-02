"""Registry API 请求/响应模型(协议之外的控制面入参)。"""

from typing import Literal

from pydantic import BaseModel, Field


class KeyCreate(BaseModel):
    role: Literal["provider", "consumer"]
    name: str = Field(min_length=1, max_length=64)


class FeedbackRequest(BaseModel):
    task_id: str
    rating: float = Field(ge=0, le=5)


class TopupRequest(BaseModel):
    name: str = Field(min_length=1, max_length=64)  # consumer 身份(key 属主名)
    amount: float = Field(gt=0)
