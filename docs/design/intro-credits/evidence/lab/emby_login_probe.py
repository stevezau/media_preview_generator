import asyncio
from playwright.async_api import async_playwright
async def main():
    async with async_playwright() as p:
        b = await p.chromium.launch(); pg = await b.new_page(viewport={"width":1280,"height":720})
        await pg.goto("http://127.0.0.1:18096/web/index.html"); await pg.wait_for_timeout(6000)
        await pg.screenshot(path="/home/data/workspace/plex_generate_vid_previews/docs/design/intro-credits/evidence/lab/emby_login.png")
        print(pg.url); print(await pg.evaluate("() => Array.from(document.querySelectorAll('input,button')).filter(e=>e.offsetParent!==null).map(e=>e.tagName+'#'+e.id+'.'+e.className+'['+(e.type||'')+']'+(e.innerText||'')).join('\\n')"))
        await b.close()
asyncio.run(main())
