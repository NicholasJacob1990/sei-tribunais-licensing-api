"""
Iudex Licensing API - Minimal Version
"""
from fastapi import FastAPI

app = FastAPI(title="Iudex Licensing API", version="1.0.0")


@app.get("/health")
async def health_check():
    return {"status": "healthy", "version": "1.0.0"}


@app.get("/")
async def root():
    return {"name": "Iudex Licensing API", "version": "1.0.0", "status": "minimal"}
