import asyncio
from playwright.async_api import async_playwright

async def run():
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True, args=["--disable-blink-features=AutomationControlled"])
        context = await browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        )
        page = await context.new_page()
        
        url = "https://dl.acm.org/action/doSearch?AllField=machine+learning"
        print(f"Navigating to {url}")
        
        response = await page.goto(url, wait_until="domcontentloaded")
        print("Status code:", response.status)
        
        await asyncio.sleep(5)
        
        html = await page.content()
        
        if "cf-browser-verification" in html or "Cloudflare" in html:
            print("WARNING: Cloudflare detected!")
            
        print("\nHTML Preview (First 500 chars):")
        print(html[:500])
        
        # Look for total count, e.g., "Hits 1 - 20 of 123,456" or "1000 Results"
        import bs4
        soup = bs4.BeautifulSoup(html, "html.parser")
        
        hits_elem = soup.select_one(".hitsLength, span.limit, .result__count")
        if hits_elem:
            print(f"\nFound Total Count string: {hits_elem.text.strip()}")
        else:
            print("\nTotal Count string not explicitly found with common selectors.")
            
        titles = soup.select("h5.issue-item__title a, span.hlFld-Title a, h2.issue-item__title a")
        print(f"\nFound {len(titles)} titles:")
        for t in titles[:5]:
            print(" -", t.text.strip())
            
        await browser.close()

if __name__ == "__main__":
    asyncio.run(run())
