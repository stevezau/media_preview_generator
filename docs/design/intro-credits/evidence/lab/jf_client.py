import asyncio, sys
from playwright.async_api import async_playwright
J="http://127.0.0.1:18097"; ITEM=sys.argv[1]; OUT=sys.argv[2]
async def main():
    async with async_playwright() as p:
        b = await p.chromium.launch(args=["--autoplay-policy=no-user-gesture-required"])
        pg = await b.new_page(viewport={"width":1280,"height":720})
        await pg.goto(f"{J}/web/#/login.html"); await pg.wait_for_timeout(3000)
        await pg.fill("#txtManualName", "lab"); await pg.fill("#txtManualPassword", "lab")
        await pg.click("button[type=submit]"); await pg.wait_for_timeout(4000)
        await pg.goto(f"{J}/web/#/details?id={ITEM}"); await pg.wait_for_timeout(4000)
        await pg.click("button.btnPlay, .detailButton.btnPlay, button[data-action=resume], button[title=Play]", timeout=15000)
        await pg.wait_for_timeout(12000)
        # seek into the intro segment (126.8-157.1s)
        await pg.evaluate("() => { const v=document.querySelector('video'); if (v) v.currentTime = 22; }")
        found = False
        for i in range(20):
            await pg.wait_for_timeout(1000)
            txt = await pg.evaluate("() => Array.from(document.querySelectorAll('button')).map(b=>b.innerText.trim()).filter(Boolean).join(' | ')")
            if "Skip" in txt: found = True; break
        await pg.screenshot(path=OUT)
        t = await pg.evaluate("() => { const v=document.querySelector('video'); return v ? v.currentTime : -1 }")
        print("skip button found:", found, "| video time:", round(t,1), "| buttons:", txt[:300])
        await b.close()
asyncio.run(main())
