import logging
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app.errors import AppError
from app.routes import chat, deals

logging.basicConfig(level=logging.INFO)
BASE = Path(__file__).resolve().parent

app = FastAPI(title="DealPilot", description="Your deals remember what you forget.")
app.mount("/static", StaticFiles(directory=BASE / "static"), name="static")
templates = Jinja2Templates(directory=BASE / "templates")

app.include_router(deals.router)
app.include_router(chat.router)


@app.exception_handler(AppError)
async def app_error(_: Request, exc: AppError):
    return JSONResponse(status_code=exc.status, content={"error": exc.message, "code": exc.code})


@app.exception_handler(RequestValidationError)
async def bad_request(_: Request, exc: RequestValidationError):
    return JSONResponse(status_code=422, content={"error": "Invalid request body.", "code": "invalid_request"})


@app.exception_handler(Exception)
async def unexpected(_: Request, exc: Exception):
    logging.getLogger("dealpilot").exception("Unhandled error")
    return JSONResponse(status_code=500, content={"error": "Something went wrong on the server.", "code": "server_error"})


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.get("/")
async def index(request: Request):
    return templates.TemplateResponse(request, "index.html")
