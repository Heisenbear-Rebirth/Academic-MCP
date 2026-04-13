import asyncio
import os
from playwright.async_api import async_playwright

async def debug_sd():
    profile_dir = os.path.abspath(".sd_profile")
    
    async with async_playwright() as p:
        context = await p.chromium.launch_persistent_context(
            user_data_dir=profile_dir,
            headless=False,
            args=["--disable-blink-features=AutomationControlled"],
            accept_downloads=True
        )
        page = await context.new_page()
        
        target_article = "https://www.sciencedirect.com/science/article/pii/S2452414X26000191"
        await page.goto(target_article, wait_until="domcontentloaded")
        await asyncio.sleep(8)
        
        title = await page.title()
        print("Page Title:", title)
        
        if "Just a moment" in title or "稍候" in title or "Robot" in title:
            print("CAPCHA DETECTED. SD Profile cookies are burned or invalid without stealth!")
        else:
            print("Page loaded normally! Dumping all A tags to find PDF...")
            links = await page.evaluate("Array.from(document.querySelectorAll('a')).map(a => a.className + ' | ' + a.href).filter(s => s.includes('pdf'))")
            for link in links:
                print(link)
                
        await context.close()

if __name__ == "__main__":
    asyncio.run(debug_sd())
