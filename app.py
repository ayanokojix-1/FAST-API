from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
import aiosqlite
from dotenv import load_dotenv
from slowapi import Limiter
from slowapi.util import get_remote_address
from fastapi import Request
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware
from routers.tiktok import router as tiktok_router, file_router
from routers.anime import router as anime_router
from helpers.anime_helper import get_animepahe_cookies

load_dotenv()
limiter = Limiter(
    key_func=get_remote_address,
    default_limits=["40/minute"],
    headers_enabled=True,
)

app = FastAPI(
    title="FAST-API Service",
    description="API for TikTok downloader and Anime scraper",
    version="1.0.0",
)
app.state.limiter = limiter
# ---------------- CORS ----------------
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:5173",
        "http://localhost:3000"
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.add_middleware(SlowAPIMiddleware)
@app.exception_handler(RateLimitExceeded)
async def rate_limit_handler(request: Request, exc: RateLimitExceeded):
    return JSONResponse(
        status_code=429,
        content={
            "error": "Rate limit exceeded",
            "message": "You have sent too many requests. Try again in 15 minutes.",
            "retry_after_minutes": 15
        }
    )

# ---------------- ROUTERS ----------------
app.include_router(tiktok_router)
app.include_router(anime_router)
app.include_router(file_router)

# ---------------- DATABASE INIT ----------------
@app.on_event("startup")
async def startup():
    await get_animepahe_cookies()
    async with aiosqlite.connect("cache.db") as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS videos (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL,
                filepath TEXT NOT NULL,
                short_code TEXT UNIQUE NOT NULL
            )
        """)

        await db.execute("""
            CREATE TABLE IF NOT EXISTS anime_info (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL,
                episodes TEXT NOT NULL,
                internal_id TEXT NOT NULL UNIQUE,
                external_id TEXT NOT NULL UNIQUE
            )
        """)

        await db.execute("""
            CREATE TABLE IF NOT EXISTS anime_episode (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                page_count INTEGER,
                episode TEXT,
                external_id TEXT NOT NULL UNIQUE
            )
        """)

        await db.execute("""
            CREATE TABLE IF NOT EXISTS cached_video_url (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                internal_id TEXT NOT NULL,
                episode TEXT,
                video_url TEXT,
                size TEXT,
                snapshot TEXT,
                UNIQUE(internal_id, episode)
            )
        """)
        await db.execute("""
        CREATE TABLE IF NOT EXISTS download_sessions (
    session_id TEXT PRIMARY KEY,
    anime_id TEXT NOT NULL,
    anime_title TEXT NOT NULL,
    links TEXT NOT NULL,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
);      
        """)

        await db.commit()

# ---------------- ROOT ROUTE ----------------
@app.get(
    "/",
    tags=["Root"],
    summary="API Root",
    description="Welcome message and available information about the API.",
)
async def root():
    return {
        "message": "Welcome to the FAST-API Service 🚀",
        "version": "1.0.0",
        "docs": {
            "swagger": "/docs",
            "redoc": "/redoc",
        },
        "routes": [
            "/tiktok",
            "/anime",
            "/files",
        ]
    }

# ---------------- RUNNING DIRECTLY ----------------
if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app:app", host="0.0.0.0", port=7860, reload=True)
