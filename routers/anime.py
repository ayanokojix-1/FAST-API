import uuid
import json
import traceback
import io
import zipfile
import asyncio
from fastapi import APIRouter, Query, Depends,Request
from fastapi.responses import JSONResponse,StreamingResponse,Response
import httpx
from db import get_db
from helpers.anime_helper import get_pahewin_link,get_episode_session,get_kiwi_url,get_redirect_link
from helpers.anime_helper import get_animepahe_cookies,get_actual_episode,get_cached_anime_info
from utils.helper import generate_internal_id,encodeURIComponent
router = APIRouter(prefix="/anime", tags=["Anime"])
@router.get("/search", description="Searches for a specific anime", summary="Search anime")
async def anime_search(query: str = Query(..., description="Anime name for the search",example="one piece"),db = Depends(get_db)):
    if not query:
        return JSONResponse(status_code=400,content={
            "status":400,
            "message":"Query is a required parameter"
        })
    search_result = []
    try:
        cookies = await get_animepahe_cookies()
        async with httpx.AsyncClient(cookies=cookies,timeout=30) as client:
            encode_query = await encodeURIComponent(query)
            res = await client.get(f"https://animepahe.si/api?m=search&q={encode_query}")
        try:
            results = res.json()
        except ValueError:
            print("❌ Not a JSON response:", res.text[:200])  # show first part of the response for debugging
            return JSONResponse(status_code=500,content={
                "status":500,
                "message":"An error occured"
            })

        info = results.get('data')
        for i in info:
            cursor = await db.execute(
                "SELECT internal_id FROM anime_info WHERE external_id = ?", (i.get("session"),))
            row = await cursor.fetchone()
            episodes = await get_actual_episode(i.get("session")) if i.get(
                "episodes") == 0 or i.get("status") == "Currently Airing" else i.get("episodes")
            if not row:
                internal_id = await generate_internal_id(i.get("title"))
                await db.execute('''
                INSERT INTO anime_info(internal_id, external_id, title, episodes)
VALUES (?, ?, ?, ?)
ON CONFLICT(external_id) DO UPDATE SET
    title = excluded.title,
    episodes = excluded.episodes;

                ''',
                        (internal_id, i.get("session"), i.get("title"), episodes))
                await db.commit()
            else:
                internal_id = row["internal_id"]
            filtered_search_result = {
                "id": internal_id,
                "title": i.get("title"),
                "episodes": episodes,
                "status": i.get("status"),
                "year": i.get("year"),
                "poster": i.get("poster"),
                "rating": i.get("score")
            }
            search_result.append(filtered_search_result)
        return search_result
    except httpx.ConnectError:
        print("Connection error occured")
        traceback.print_exc()
        return JSONResponse(status_code=500,content={
            "status":500,
            "message":"Connection error occured Try again later"
        })
    except httpx.ConnectTimeout:
        print("Connection error occured")
        return JSONResponse(status_code=500,content={
            "status":500,
            "message":"Connection error occured Try again later"
        })

    except Exception as e:
        print("Anime search error: ",e)
        traceback.print_exc()
        return JSONResponse(status_code=500,content={
            "status":500,
            "message":"Internal Server error"
        })

@router.get("/download", description="Download anime using id gotten from search",summary="Download anime")
async def anime_download(id:str = Query(...,description="id for the anime from search",example="OP3526"),episode:int = Query(...,description="Anime episode number",example=6),db= Depends(get_db)):
    if not id or not episode:
        return JSONResponse(status_code=400,content={
            "status":400,
            "message":"Id and episode are required"
        })
    info = await get_cached_anime_info(id,db)
    if not info.get("status") == 200:
        return JSONResponse(
            status_code=info.get("status"),content={
                **info
            }
        )
    ep_count = info["episodes"]
    if int(episode) > int(ep_count):
        return JSONResponse(status_code=422,content={
            "status": 422,
            "message": "Episode number exceed available count"
        })
    if not info["external_id"]:
        return JSONResponse(status_code=404,content={
            "status": 404,
            "message": "No external id found"
        })
    if int(episode)<=0:
        return JSONResponse(status_code=400,content={
            "status":400,
            "message": "Episode count cannot be zero or below"
        })
    cursor = await db.execute(
        "SELECT * FROM cached_video_url WHERE internal_id = ? and episode = ?", (id, episode))
    row = await cursor.fetchone()
    if row and row["video_url"]:
        link = row["video_url"]
        async with httpx.AsyncClient(timeout=10) as client:
            res = await client.head(link)
        if res.status_code == 200:
            print("Link is a valid link with a status code of 200")
            return {
                "status": 200,
                "direct_link": row["video_url"],
                "size": row["size"],
                "episode": row["episode"]
            }
    search_result = await get_episode_session(info["external_id"],db)
    episode_info = search_result[int(episode)-1]
    episode_session = episode_info.get("session")
    episode_snapshot = episode_info.get("snapshot")
    pahe_link = await get_pahewin_link(info["external_id"], episode_session)
    if pahe_link is None:
        return JSONResponse(status_code=404,content={
            "status": 404,
            "message": "Internal Link not found"
        })
    kiwi_url = await get_kiwi_url(pahe_link)
    results = await get_redirect_link(kiwi_url, id, episode,db,episode_snapshot)
    if not results:
        return JSONResponse(status_code=500,content={
        "status": 500,
        "message": "Internal error: no results returned"
    })


    return JSONResponse(status_code=500 if results.get("status") == 500 else 200,content=results)

@router.get("/bulk-download", description="Bulk download multiple anime episodes", summary="Bulk download anime episodes")
async def anime_bulk_download(
    id: str = Query(..., description="ID for the anime from search", example="OP3526"),
    ep_from: int = Query(..., alias="from", description="Starting episode number", example=1, ge=1),
    ep_to: int = Query(..., alias="to", description="Ending episode number", example=24, ge=1),
    db = Depends(get_db)
):
    # Validation
    if ep_from > ep_to:
        return JSONResponse(status_code=400, content={
            "status": 400,
            "message": "Starting episode cannot be greater than ending episode"
        })
    
    # Get anime info
    info = await get_cached_anime_info(id, db)
    if not info.get("status") == 200:
        return JSONResponse(
            status_code=info.get("status"),
            content={**info}
        )
    
    ep_count = info["episodes"]
    
    # Check if episodes are within range
    if ep_to > int(ep_count) or ep_from > int(ep_count):
        return JSONResponse(status_code=422, content={
            "status": 422,
            "episodes": ep_count,
            "message": "Episode number exceeds available count"
        })
    
    if not info["external_id"]:
        return JSONResponse(status_code=404, content={
            "status": 404,
            "message": "No external id found"
        })
    
    # Create list of episode numbers to fetch
    episodes = list(range(ep_from, ep_to + 1))
    semaphore = asyncio.Semaphore(5)
    # Fetch all episodes concurrently with asyncio.gather
    download_links = await asyncio.gather(*[
        _fetch_single_episode(id, episode, info["external_id"], db,semaphore)
        for episode in episodes
    ])
    
    # Filter out any None results (failed episodes)
    successful_links = [link for link in download_links if link is not None]
    
    if not successful_links:
        return JSONResponse(status_code=500, content={
            "status": 500,
            "message": "Failed to fetch any episode links"
        })
    
    # CREATE SESSION - Store links in DB
    session_id = str(uuid.uuid4())
    
    await db.execute(
        "INSERT INTO download_sessions (session_id, anime_id, anime_title, links, created_at) VALUES (?, ?, ?, ?, datetime('now'))",
        (session_id, id, info.get("title", "Unknown"), json.dumps(successful_links))
    )
    await db.commit()
    
    print(f"✅ Created session {session_id} for {info.get('title')}")
    
    return JSONResponse(status_code=200, content={
        "status": 200,
        "session_id": session_id,  # NEW: Return session ID
        "anime_title": info.get("title", "Unknown"),
        "total_requested": len(episodes),
        "total_fetched": len(successful_links),
        "links": successful_links
    })


async def _fetch_single_episode(id: str, episode: int, external_id: str, db, semaphore):
    """Helper function to fetch a single episode link"""
      # Only N requests at once
    try:
        # Check cache first
        cursor = await db.execute(
            "SELECT * FROM cached_video_url WHERE internal_id = ? and episode = ?", 
            (id, episode)
        )
        row = await cursor.fetchone()
        
        if row and row["video_url"]:
            link = row["video_url"]
            
            try:
                async with httpx.AsyncClient(timeout=10) as client:
                    res = await client.head(link)
                
                if res.status_code == 200:
                    print(f"✅ Episode {episode}: Using cached link")
                    return {
                        "episode": row["episode"],
                        "direct_link": row["video_url"],
                        "size": row["size"],
                        "snapshot": row["snapshot"],
                        "status": 200
                    }
            except Exception as e:
                print(f"⚠️ Episode {episode}: Cached link check failed ({e}), fetching fresh...")
        
        # Fetch fresh link
        print(f"🔄 Episode {episode}: Fetching fresh link")
        
        # Add delay between requests
        await asyncio.sleep(0.5)
        
        search_result = await get_episode_session(external_id, db)
        episode_info = search_result[episode - 1]
        episode_session = episode_info.get("session")
        episode_snapshot = episode_info.get("snapshot")
        
        pahe_link = await get_pahewin_link(external_id, episode_session)
        if not pahe_link:
            print(f"❌ Episode {episode}: No pahe link found")
            return None
        
        kiwi_url = await get_kiwi_url(pahe_link)
        if not kiwi_url:
            print(f"❌ Episode {episode}: No kiwi URL found")
            return None
        
        results = await get_redirect_link(kiwi_url, id, episode, db, episode_snapshot)
        
        if results and results.get("status") == 200:
            print(f"✅ Episode {episode}: Successfully fetched")
            return results
        else:
            print(f"❌ Episode {episode}: Failed to get redirect link")
            return None
            
    except Exception as e:
        print(f"❌ Episode {episode}: Error - {e}")
        import traceback
        traceback.print_exc()
        return None
import os
import tempfile
import shutil

@router.get("/bulk-download-zip")
async def bulk_download_zip_get(
    session_id: str = Query(..., description="Download session ID"),
    db = Depends(get_db)
):
    """
    Download episodes to disk, ZIP them, stream, then delete
    Uses 2x bandwidth but WORKS every time!
    """
    
    print(f"🔍 Fetching session {session_id}")
    
    # Get session
    cursor = await db.execute(
        "SELECT * FROM download_sessions WHERE session_id = ?",
        (session_id,)
    )
    row = await cursor.fetchone()
    
    if not row:
        return JSONResponse(status_code=404, content={"status": 404, "message": "Session not found"})
    
    links = json.loads(row["links"])
    anime_title = row["anime_title"].replace(" ", "_").lower()
    
    # Get episode range
    episodes = [link_info.get("episode") for link_info in links if link_info.get("episode")]
    from_ep = min(episodes) if episodes else 1
    to_ep = max(episodes) if episodes else 1
    
    # Create filename: gachiakuta_19-21_episodes.zip
    zip_filename = f"{anime_title}_{from_ep}-{to_ep}_episodes.zip"
    
    print(f"🔍 Creating ZIP for {anime_title} with {len(links)} episodes ({from_ep}-{to_ep})")
    
    # Create temporary directory
    temp_dir = tempfile.mkdtemp()
    zip_path = os.path.join(temp_dir, zip_filename)
    
    try:
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
            'Referer': 'https://kwik.cx/',
        }
        
        # Step 1: Download all episodes to disk
        downloaded_files = []
        
        async with httpx.AsyncClient(timeout=300, follow_redirects=True, headers=headers) as client:
            for link_info in links:
                episode = link_info.get("episode")
                url = link_info.get("direct_link")
                
                if not url:
                    continue
                
                filename = f"{anime_title}_Episode_{str(episode).zfill(3)}.mp4"
                filepath = os.path.join(temp_dir, filename)
                
                print(f"📥 Downloading episode {episode} to disk...")
                
                try:
                    response = await client.get(url, timeout=300)
                    
                    if response.status_code == 200 and len(response.content) > 100000:
                        # Save to disk
                        with open(filepath, 'wb') as f:
                            f.write(response.content)
                        
                        downloaded_files.append(filepath)
                        print(f"✅ Episode {episode} saved to disk")
                    else:
                        print(f"❌ Episode {episode} failed: {response.status_code}")
                
                except Exception as e:
                    print(f"❌ Episode {episode} error: {e}")
                    continue
        
        if not downloaded_files:
            return JSONResponse(status_code=500, content={"status": 500, "message": "No episodes downloaded"})
        
        # Step 2: Create ZIP from downloaded files
        print(f"📦 Creating ZIP file...")
        
        with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_STORED) as zip_file:
            for filepath in downloaded_files:
                zip_file.write(filepath, os.path.basename(filepath))
                print(f"✅ Added {os.path.basename(filepath)} to ZIP")
        
        zip_size = os.path.getsize(zip_path)
        print(f"✅ ZIP created! Size: {zip_size / (1024*1024):.2f} MB")
        
        # Step 3: Stream the ZIP file
        def iterate_file():
            with open(zip_path, 'rb') as f:
                chunk_size = 64 * 1024
                while True:
                    chunk = f.read(chunk_size)
                    if not chunk:
                        break
                    yield chunk
        
        # Delete session
        await db.execute("DELETE FROM download_sessions WHERE session_id = ?", (session_id,))
        await db.commit()
        
        # Return streaming response with proper filename
        response = StreamingResponse(
            iterate_file(),
            media_type="application/zip",
            headers={
                "Content-Disposition": f'attachment; filename="{zip_filename}"',
                "Content-Length": str(zip_size)  # Exact size!
            }
        )
        
        # Schedule cleanup after response is sent
        async def cleanup():
            await asyncio.sleep(5)  # Wait for download to start
            shutil.rmtree(temp_dir, ignore_errors=True)
            print(f"🗑️ Cleaned up temp directory")
        
        asyncio.create_task(cleanup())
        
        return response
    
    except Exception as e:
        # Cleanup on error
        shutil.rmtree(temp_dir, ignore_errors=True)
        print(f"❌ Error: {e}")
        return JSONResponse(status_code=500, content={"status": 500, "message": str(e)})

@router.get("/proxy-image", description="Proxy images from animepahe")
async def proxy_image(
    url: str = Query(..., description="Image URL to proxy")
):
    """
    Proxy images from animepahe with cookies to bypass 403
    """
    
    # Validate it's from animepahe (security)
    if "animepahe.si" not in url:
        return Response(status_code=400, content="Invalid image URL")
    
    try:
        # Get animepahe cookies
        cookies = await get_animepahe_cookies()
        
        # Fetch image with cookies
        async with httpx.AsyncClient(timeout=10) as client:
            response = await client.get(url, cookies=cookies)
        
        if response.status_code == 200:
            # Return image with proper content type
            return Response(
                content=response.content,
                media_type=response.headers.get("content-type", "image/jpeg"),
                headers={
                    "Cache-Control": "public, max-age=86400",  # Cache for 1 day
                }
            )
        else:
            # Return placeholder or 404
            return Response(status_code=response.status_code)
            
    except Exception as e:
        print(f"Error proxying image: {e}")
        return Response(status_code=500)