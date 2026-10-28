
import json
import os
from contextlib import asynccontextmanager
from datetime import datetime

import redis
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import Boolean, DateTime, Integer, String, create_engine
from sqlalchemy import func, select
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker

DATABASE_URL = os.environ["DATABASE_URL"]
REDIS_URL = os.environ["REDIS_URL"]

engine = create_engine(DATABASE_URL, pool_pre_ping=True)
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)
cache = redis.Redis.from_url(REDIS_URL, decode_responses=True)


class Base(DeclarativeBase):
    pass


class Task(Base):
    __tablename__ = "tasks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    completed: Mapped[bool] = mapped_column(
        Boolean, default=False, nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, nullable=False
    )


class TaskCreate(BaseModel):
    title: str = Field(min_length=1, max_length=200)


class TaskUpdate(BaseModel):
    completed: bool


class TaskResponse(BaseModel):
    id: int
    title: str
    completed: bool
    created_at: datetime

    model_config = {"from_attributes": True}


@asynccontextmanager
async def lifespan(app: FastAPI):
    Base.metadata.create_all(bind=engine)
    yield


app = FastAPI(title="TaskFlow API", lifespan=lifespan)


def invalidate_cache():
    try:
        cache.delete("task_stats")
    except redis.RedisError:
        pass


@app.get("/api/health")
def health():
    return {"status": "ok"}


@app.get("/api/ready")
def ready():
    try:
        with engine.connect() as connection:
            connection.exec_driver_sql("SELECT 1")
        cache.ping()
        return {"status": "ready"}
    except Exception:
        raise HTTPException(status_code=503, detail="Dependency unavailable")


@app.get("/api/tasks", response_model=list[TaskResponse])
def get_tasks():
    with SessionLocal() as session:
        return session.scalars(
            select(Task).order_by(Task.id.desc())
        ).all()


@app.post("/api/tasks", response_model=TaskResponse, status_code=201)
def create_task(payload: TaskCreate):
    title = payload.title.strip()

    if not title:
        raise HTTPException(status_code=422, detail="Title cannot be empty")

    with SessionLocal() as session:
        task = Task(title=title)
        session.add(task)
        session.commit()
        session.refresh(task)
        result = TaskResponse.model_validate(task)

    invalidate_cache()
    return result


@app.patch("/api/tasks/{task_id}", response_model=TaskResponse)
def update_task(task_id: int, payload: TaskUpdate):
    with SessionLocal() as session:
        task = session.get(Task, task_id)

        if not task:
            raise HTTPException(status_code=404, detail="Task not found")

        task.completed = payload.completed
        session.commit()
        session.refresh(task)
        result = TaskResponse.model_validate(task)

    invalidate_cache()
    return result


@app.delete("/api/tasks/{task_id}")
def delete_task(task_id: int):
    with SessionLocal() as session:
        task = session.get(Task, task_id)

        if not task:
            raise HTTPException(status_code=404, detail="Task not found")

        session.delete(task)
        session.commit()

    invalidate_cache()
    return {"message": "Task deleted"}


@app.get("/api/stats")
def get_stats():
    try:
        cached = cache.get("task_stats")
        if cached:
            return {**json.loads(cached), "source": "redis"}
    except redis.RedisError:
        pass

    with SessionLocal() as session:
        total = session.scalar(select(func.count(Task.id))) or 0
        completed = session.scalar(
            select(func.count(Task.id)).where(Task.completed.is_(True))
        ) or 0

    stats = {
        "total": total,
        "completed": completed,
        "pending": total - completed,
    }

    try:
        cache.setex("task_stats", 30, json.dumps(stats))
    except redis.RedisError:
        pass

    return {**stats, "source": "database"}
