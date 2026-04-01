import asyncio
from playwright.async_api import async_playwright

async def main():
    print("Starting Web of Science UI structural research...")
    async with async_playwright() as p:
        # headless=False lets the user monitor if WOS asks for IP validation or cookies
        browser = await p.chromium.launch(headless=False)
        context = await browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        )
        
        page = await context.new_page()
        print("Loading WoS Search page...")
        await page.goto("https://www.webofscience.com/wos/woscc/basic-search", wait_until="domcontentloaded")
        
        # Give it time for angular/react to fully render the UI
        await asyncio.sleep(10)
        
        html = await page.content()
        with open("wos_home.html", "w", encoding="utf-8") as f:
            f.write(html)
            
        print("Saved wos_home.html. Research step 1 complete.")
        await browser.close()

if __name__ == "__main__":
    asyncio.run(main())
