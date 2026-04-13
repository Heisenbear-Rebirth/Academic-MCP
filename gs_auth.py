import os
import asyncio
from playwright.async_api import async_playwright

async def run_auth():
    print("启动 Google Scholar 认证会话...")
    print("如有验证码请手动完成（红绿灯图形等），完成后不要关闭浏览器，回到这里按 Enter 继续以保存 Cookie！")
    
    async with async_playwright() as p:
        profile_dir = os.path.join(os.getcwd(), ".gs_profile")
        context = await p.chromium.launch_persistent_context(
            user_data_dir=profile_dir,
            headless=False,
            args=["--disable-blink-features=AutomationControlled"],
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            viewport={"width": 1280, "height": 800}
        )
        
        page = await context.new_page()
        # force position visible
        await context.browser.new_window()
        await page.goto("https://scholar.google.com/scholar?hl=en&q=test")
        
        input("当您在弹出窗口中完成任何可能的验证码后，按 Enter 键以保存会话...")
        
        await context.close()
        print("会话已保存到 .gs_profile 中！MCP 即可免疫验证码执行检索！")

if __name__ == "__main__":
    asyncio.run(run_auth())
