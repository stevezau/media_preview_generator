import asyncio
from playwright.async_api import async_playwright
async def main():
    async with async_playwright() as p:
        b=await p.chromium.launch(); pg=await b.new_page(viewport={"width":1280,"height":900})
        await pg.goto("file:///home/data/workspace/plex_generate_vid_previews/docs/design/intro-credits/evidence/design/index.html"); await pg.wait_for_timeout(2500)
        await pg.screenshot(path="/home/data/workspace/plex_generate_vid_previews/docs/design/intro-credits/evidence/design/design_preview.png", full_page=True)
        await b.close()
asyncio.run(main())
