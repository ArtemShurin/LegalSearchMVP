from contextlib import asynccontextmanager

import torch
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from src.routers import auth, search, history, collections, documents
from ml.models import load_models


@asynccontextmanager
async def lifespan(app: FastAPI):
    device = "cuda" if torch.cuda.is_available() else "cpu"
    load_models(device=device)
    yield


app = FastAPI(title="LegalSearch API", version="0.1.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(auth.router)
app.include_router(search.router)
app.include_router(history.router)
app.include_router(collections.router)
app.include_router(documents.router)


@app.get("/")
async def root():
    return {"status": "ok", "service": "LegalSearch API"}

