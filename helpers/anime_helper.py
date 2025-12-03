import json
import asyncio
import time
import os
import re
from bs4 import BeautifulSoup
import httpx
from playwright.async_api import async_playwright,TimeoutError
from utils.helper import deobfuscate,extract_info

def cookies_expired(cookie_dict):
    now = time.time()
    for c in cookie_dict.values():
        exp = c.get("expires")
        if exp and exp < now:
            return True
    return False


CACHE_FILE = "animepahe_cookies.json"




CACHE_FILE = "animepahe_cookies.json"

async def get_animepahe_cookies():
    # 1️⃣ Check if cached cookies exist and are still valid
    if os.path.exists(CACHE_FILE):
        with open(CACHE_FILE, "r") as f:
            data = json.load(f)
        if not cookies_expired(data):  # Your existing expiry check
            print("Used cookies from Cached")
            return {k: v["value"] for k, v in data.items()}

    # 2️⃣ Else: try to regenerate cookies
    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            context = await browser.new_context()
            page = await context.new_page()

            # Go to Animepahe
            await page.goto("https://animepahe.si")

            # Wait for main content to load, not full network idle
            try:
                await page.wait_for_load_state("domcontentloaded", timeout=10000)
            except TimeoutError:
                print("⚠️ Timeout waiting for DOMContentLoaded, continuing anyway...")

            # Optional small sleep to ensure cookies are set
            await asyncio.sleep(1)

            cookies = await context.cookies()
            await browser.close()

            # Prepare cookie dict
            cookie_dict = {
                c['name']: {
                    "value": c['value'],
                    "expires": c.get("expires")
                }
                for c in cookies
            }

            # Save to cache
            with open(CACHE_FILE, "w") as f:
                json.dump(cookie_dict, f)
            print("Used cookies from animepahe server")
            return {k: v["value"] for k, v in cookie_dict.items()}

    except Exception as e:
        print("⚠️ Failed to get new cookies:", e)
        # Fallback: return cached cookies if they exist, even if expired
        if os.path.exists(CACHE_FILE):
            with open(CACHE_FILE, "r") as f:
                data = json.load(f)
                print("Cookies gotten through the try except method")
            return {k: v["value"] for k, v in data.items()}
        return None  # No cookies available

async def get_actual_episode(external_id):
    try:
        if not external_id:
            return None
        cookies = await get_animepahe_cookies()
        
        async with httpx.AsyncClient() as client:
            res = await client.get(
                f"https://animepahe.si/api?m=release&id={external_id}",
                cookies=cookies,
                timeout=30
            )
        if res.status_code != 200:
            return None
        data = res.json()

        return data.get("total")
    except httpx.ConnectTimeout:
        print("Connection error")
        return None
    except Exception as e:
        print(e)
        return None

async def get_cached_anime_info(id, db):
    try:
        if not id:
            return {"status": 400, "message": "No ID provided"}
        
        if not db:
            return {"status": 500, "message": "Database connection required"}
        
        cursor = await db.execute("SELECT * FROM anime_info WHERE internal_id = ?", (id,))
        row = await cursor.fetchone()
        
        if not row:
            return {"status": 404, "message": "Anime not found in cache"}
        
        # Check if external_id exists
        external_id = row["external_id"]
        if not external_id:
            return {"status": 400, "message": "No external_id found for this anime"}
        
        # Get actual episode count
        episodes = await get_actual_episode(external_id)
        
        if not episodes:
            return {"status": 500, "message": "Failed to fetch episode count"}
        
        # Update if episode count changed
        if int(episodes) != int(row["episodes"]):
            await db.execute(
                "UPDATE anime_info SET episodes = ? WHERE internal_id = ?",
                (episodes, id)
            )
            await db.commit()
            cursor = await db.execute("SELECT * FROM anime_info WHERE internal_id = ?", (id,))
            row = await cursor.fetchone()
        if not row:
            return {"status":404,"message":"Id not registered. Search the anime first"}
        print("Cached anime info function ran")
        return {"status": 200, **row}
    
    except Exception as e:
        print(f"Error in get_cached_anime_info: {e}")
        traceback.print_exc()
        return {"status": 500, "message": f"Internal error: {str(e)}"}

async def get_episode_session(id, db):
    if not id:
        return None
    
    cookies = await get_animepahe_cookies()
    
    cursor = await db.execute(
        "SELECT page_count FROM anime_episode WHERE external_id = ?", (id,))
    row = await cursor.fetchone()
    print("Running get_episode_session function")
    
    if not row or not row["page_count"]:
        async with httpx.AsyncClient(cookies=cookies, timeout=10) as client:
            res = await client.get(f"https://animepahe.si/api?m=release&id={id}")
            data = res.json()
        
        if not data or not data.get("last_page"):
            return None

        await db.execute(
    "INSERT OR IGNORE INTO anime_episode(episode, external_id, page_count) VALUES (?, ?, ?)",
    (data.get("total"), id, data.get("last_page"))
)

        await db.commit()
        page_count = data.get("last_page")
    else:
        page_count = row["page_count"]
    
    print(f"Fetching {page_count} pages concurrently")
    
    async def fetch_page(client, page, delay):
        # Stagger requests with delay
        await asyncio.sleep(delay)
        
        url = (
            f"https://animepahe.si/api"
            f"?m=release&id={id}&sort=episode_asc&page={page}"
        )
        res = await client.get(url, cookies=cookies)
        return res.json().get("data", [])
    
    async with httpx.AsyncClient(timeout=10.0) as client:
        # Create tasks with staggered delays (0.5s between each)
        tasks = [
            fetch_page(client, page, page * 0.5)
            for page in range(1, page_count + 1)
        ]
        
        # Fetch all pages concurrently
        results = await asyncio.gather(*tasks)
    
    # Flatten the results
    episode_result = [episode for page_data in results for episode in page_data]
    
    print("Done with concurrent fetching")
    return episode_result
    
async def get_pahewin_link(external_id, episode_id):
    if not episode_id or not external_id:
        return None
    
    url = f"https://animepahe.si/play/{external_id}/{episode_id}"
    cookies = await get_animepahe_cookies()
    print("Getting anime pahe cookies in get_pahewin_link function")
    
    # Use httpx for async HTTP request
    async with httpx.AsyncClient() as client:
        res = await client.get(url, cookies=cookies, timeout=10)
        html = res.text
    
    # Offload BeautifulSoup parsing to thread pool
    link = await asyncio.to_thread(_parse_pahewin_html, html, url)
    return link


def _parse_pahewin_html(html, url):
    """Synchronous HTML parsing - runs in thread pool"""
    soup = BeautifulSoup(html, "html.parser")
    dropdown = soup.find("div", id="pickDownload")
    if not dropdown:
        return None
    
    links = dropdown.find_all("a", class_="dropdown-item")
    for a in links:
        text = a.get_text(" ", strip=True).lower()
        if "720p" in text and "eng" not in text:
            print(f"Gotten pahe.win link successfully for {url}")
            print(f"Pahe.win link:{a['href']}")
            return a["href"]
    
    print("No link found")
    return None

async def get_kiwi_url(pahe_url):
    if not pahe_url:
        print("No pahe.win link")
        return None
    
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/131 Safari/537.36",
        "Accept-Language": "en-US,en;q=0.9",
        "Accept": "*/*"
    }

    # Async HTTP request with httpx
    async with httpx.AsyncClient() as client:
        res = await client.get(pahe_url, timeout=10, headers=headers)
        html = res.text
    
    # Offload BeautifulSoup parsing to thread pool
    return await asyncio.to_thread(_parse_kiwi_url, html)


def _parse_kiwi_url(html):
    """Synchronous HTML parsing - runs in thread pool"""
    soup = BeautifulSoup(html, "html.parser")
    info = soup.find("script")
    if not info or "kwik" not in info.text:
        return None
    m = re.search(r"https?://(?:www\.)?kwik\.cx[^\s\"');]+", info.text)
    return m.group(0) if m else None

async def get_kiwi_info(kiwi_url):
    try:
        if not kiwi_url:
            return None
        
        headers = {
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8',
            'Accept-Language': 'en-US,en;q=0.9',
            'Accept-Encoding': 'gzip, deflate',
            'DNT': '1',
            'Connection': 'keep-alive',
            'Upgrade-Insecure-Requests': '1',
            'Sec-Fetch-Dest': 'document',
            'Sec-Fetch-Mode': 'navigate',
            'Sec-Fetch-Site': 'none',
            'Sec-Fetch-User': '?1',
            'Cache-Control': 'max-age=0',
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/131 Safari/537.36',
        }

        # Async HTTP request with httpx
        async with httpx.AsyncClient() as client:
            res = await client.get(kiwi_url, timeout=10, headers=headers)
            html = res.text
            cookies = res.cookies
        
        # Offload CPU-bound parsing/deobfuscation to thread pool
        result = await asyncio.to_thread(_parse_and_deobfuscate_kiwi, html, cookies)
        return result
        
    except IndexError:
        print(html)
        print("Script is out of range -2")
        return None
    except Exception as e:
        print("Kiwi error Occured", e)
        traceback.print_exc()
        return None

def _parse_and_deobfuscate_kiwi(html, cookies):
    """Synchronous parsing and deobfuscation - runs in thread pool"""
    html_soup = BeautifulSoup(html, "html.parser")
    scripts = html_soup.find_all("script")
    obf_js = scripts[-3].text
    deobf_js = deobfuscate(obf_js)
    
    return {
        **extract_info(deobf_js),
        "kwik_session": cookies.get("kwik_session")
    }

async def get_redirect_link(url, id, episode, db,snapshot):
    if not url or not id or not episode:
        print("No url,episode or id detected ending now")
        return None
    
    info = await get_kiwi_info(url)
    if not info:
        return {
            "status": 500,
            "message": "Server timed out, retry request"
        }
    
    base_url = "https://kwik-test.vercel.app/kwik"
    # base_url = "http://localhost:5000/kwik"
    payload = {
        "kwik_url": url,
        "token": info.get("token"),
        "kwik_session": info.get("kwik_session")
    }
    
    # Async HTTP POST with httpx
    async with httpx.AsyncClient() as client:
        res = await client.post(
            base_url,
            content=json.dumps(payload),
            timeout=10,
            headers={"Content-Type": "application/json"}
        )
    
    if res.status_code != 200:
        print(res.text)
        return {
            "status": 500,
            "message": "Server timed out"
        }
    
    data = res.json()
    size = info.get("size")
    direct_link = data.get("download_link")
    
    # Async database operations
    await db.execute(
        "INSERT OR REPLACE INTO cached_video_url(internal_id,episode,video_url,size,snapshot) VALUES(?,?,?,?,?)",
        (id, episode, direct_link, size,snapshot)
    )
    await db.commit()
    
    print(f"Direct url {direct_link} detected sending response now")
    return {
        "direct_link": direct_link,
        "episode": episode,
        "snapshot": snapshot,
        "status": 200,
        "size": size
    }