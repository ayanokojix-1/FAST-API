import zipfile
import io
import asyncio
from fastapi import APIRouter, Query, Depends,Request
from fastapi.responses import JSONResponse,StreamingResponse
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
        async with httpx.AsyncClient(cookies=cookies,timeout=10) as client:
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
            if not row or not row["internal_id"]:
                internal_id = await generate_internal_id(i.get("title"))
                await db.execute("INSERT INTO anime_info(internal_id,external_id,title,episodes) VALUES(?,?,?,?)",
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
    ep_from: int = Query(...,alias="from",description="Starting episode number", example=1, ge=1),
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
    
    # Fetch all episodes concurrently with asyncio.gather
    download_links = await asyncio.gather(*[
        _fetch_single_episode(id, episode, info["external_id"], db)
        for episode in episodes
    ])
    
    # Filter out any None results (failed episodes)
    successful_links = [link for link in download_links if link is not None]
    
    if not successful_links:
        return JSONResponse(status_code=500, content={
            "status": 500,
            "message": "Failed to fetch any episode links"
        })
    
    return JSONResponse(status_code=200, content={
        "status": 200,
        "anime_title": info.get("title", "Unknown"),
        "total_requested": len(episodes),
        "total_fetched": len(successful_links),
        "links": successful_links
    })


async def _fetch_single_episode(id: str, episode: int, external_id: str, db):
    """Helper function to fetch a single episode link"""
    try:
        # Check cache first
        cursor = await db.execute(
            "SELECT * FROM cached_video_url WHERE internal_id = ? and episode = ?", 
            (id, episode)
        )
        row = await cursor.fetchone()
        
        if row and row["video_url"]:
            link = row["video_url"]
            async with httpx.AsyncClient(timeout=10) as client:
                res = await client.head(link)
            
            if res.status_code == 200:
                print(f"✅ Episode {episode}: Using cached link")
                return {
                    "episode": row["episode"],
                    "direct_link": row["video_url"],
                    "size": row["size"],
                    "status": 200
                }
        
        # Fetch fresh link
        print(f"🔄 Episode {episode}: Fetching fresh link")
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
        
        results = await get_redirect_link(kiwi_url, id, episode, db,episode_snapshot)
        
        if results and results.get("status") == 200:
            print(f"✅ Episode {episode}: Successfully fetched")
            return results
        else:
            print(f"❌ Episode {episode}: Failed to get redirect link")
            return None
            
    except Exception as e:
        print(f"❌ Episode {episode}: Error - {e}")
        return None




@router.post("/bulk-download-zip")
async def bulk_download_zip(request: Request):
    body = await request.json()
    links: List[Dict] = body.get("links", [])
    anime_title: str = body.get("anime_title", "Anime").replace(" ", "_")
    
    if not links:
        return {"status": 400, "message": "No links provided"}
    
    print(f"🔍 Starting ZIP creation for {len(links)} episodes")
    
    # Browser-like headers to bypass blocking
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36',
        'Accept': '*/*',
        'Accept-Language': 'en-US,en;q=0.9',
        'Accept-Encoding': 'gzip, deflate',
        'Referer': 'https://kwik.cx/',
        'Origin': 'https://kwik.cx',
        'Connection': 'keep-alive',
        'Sec-Fetch-Dest': 'empty',
        'Sec-Fetch-Mode': 'cors',
        'Sec-Fetch-Site': 'same-origin',
    }
    
    # Create ZIP in memory FIRST (not in generator)
    zip_buffer = io.BytesIO()
    
    async with httpx.AsyncClient(timeout=300, follow_redirects=True, headers=headers) as client:
        with zipfile.ZipFile(zip_buffer, 'w', zipfile.ZIP_DEFLATED, compresslevel=1) as zip_file:
            for link_info in links:
                episode = link_info.get("episode")
                url = link_info.get("direct_link")
                
                if not url:
                    print(f"⚠️ Skipping episode {episode}: No URL")
                    continue
                
                filename = f"{anime_title}_Episode_{str(episode).zfill(3)}.mp4"
                
                print(f"📥 Downloading episode {episode}...")
                
                try:
                    # Download episode with proper headers
                    response = await client.get(url, timeout=300)
                    
                    print(f"📊 Episode {episode}: Status {response.status_code}, Size: {len(response.content)} bytes")
                    
                    # Check if actually got video (not 403 error page)
                    if response.status_code == 200 and len(response.content) > 100000:  # At least 100KB
                        # Write to ZIP
                        zip_file.writestr(filename, response.content)
                        print(f"✅ Episode {episode} added to ZIP ({len(response.content)} bytes)")
                    else:
                        print(f"❌ Episode {episode} failed: Status {response.status_code}, Size: {len(response.content)} bytes")
                        
                except httpx.TimeoutException:
                    print(f"❌ Episode {episode} timed out")
                except Exception as e:
                    print(f"❌ Episode {episode} error: {str(e)}")
                    continue
    
    # Get final ZIP size
    zip_size = zip_buffer.tell()
    print(f"✅ ZIP creation complete! Total size: {zip_size} bytes ({zip_size / (1024*1024):.2f} MB)")
    
    # Check if ZIP is suspiciously small (all downloads failed)
    if zip_size < 1000:
        print("⚠️ WARNING: ZIP file is too small - all downloads likely failed!")
        return JSONResponse(
            status_code=500,
            content={
                "status": 500,
                "message": "Failed to download episodes - source may be blocking requests"
            }
        )
    
    # Stream the completed ZIP
    zip_buffer.seek(0)
    
    def iterate_file():
        """Generator to stream the completed ZIP"""
        chunk_size = 64 * 1024  # 64KB chunks
        while True:
            chunk = zip_buffer.read(chunk_size)
            if not chunk:
                break
            yield chunk
    
    filename = f"{anime_title}_Episodes.zip"
    
    return StreamingResponse(
        iterate_file(),
        media_type="application/zip",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Content-Type": "application/zip",
            "Content-Length": str(zip_size)  # Use actual size
        }
    )