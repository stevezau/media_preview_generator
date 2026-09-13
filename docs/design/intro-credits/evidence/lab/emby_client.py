import asyncio, sys
from playwright.async_api import async_playwright
E="http://127.0.0.1:18096"; ITEM=sys.argv[1]; OUT=sys.argv[2]
async def main():
    async with async_playwright() as p:
        b = await p.chromium.launch(args=["--autoplay-policy=no-user-gesture-required"])
        pg = await b.new_page(viewport={"width":1280,"height":720})
        await pg.goto(f"{E}/web/index.html"); await pg.wait_for_timeout(6000)
        await pg.click("button.cardMediaInfoItem:has-text('lab')"); await pg.wait_for_timeout(2500)
        await pg.fill("input[type=password]", "lab"); await pg.keyboard.press("Enter"); await pg.wait_for_timeout(6000)
        await pg.goto(f"{E}/web/index.html#!/item?id={ITEM}&serverId=f8a1448eef5e458ab6c67bca12e86738"); await pg.wait_for_timeout(5000)
        await pg.screenshot(path=OUT.replace(".png","_item.png"))
        await pg.get_by_role("button", name="Play", exact=True).first.click(timeout=15000)
        await pg.wait_for_timeout(10000)
        await pg.evaluate("() => { const v=document.querySelector('video'); if (v) v.currentTime = 22; }")
        found=False; txt=""
        for i in range(20):
            await pg.wait_for_timeout(1000)
            txt = await pg.evaluate("() => Array.from(document.querySelectorAll('button')).filter(b=>b.offsetParent!==null).map(b=>(b.innerText||b.title||'').trim()).filter(Boolean).join(' | ')")
            if "Skip" in txt or "Intro" in txt: found=True; break
        await pg.screenshot(path=OUT)
        t = await pg.evaluate("() => { const v=document.querySelector('video'); return v ? v.currentTime : -1 }")
        print("skip found:", found, "| t:", round(t,1), "| visible buttons:", txt[:400])
        await b.close()
asyncio.run(main())
